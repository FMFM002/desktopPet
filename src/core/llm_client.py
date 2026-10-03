"""
LLM 客户端模块
- LLM1: 视觉模型总结 + 入库
- LLM2: 对话模型 + 工具调用
- 支持多提供商热切换 (DeepSeek / OpenAI / Ollama / 自定义)
"""
import base64
import logging
import os
import threading
from io import BytesIO
from typing import Optional, Generator
from openai import OpenAI
from PIL import Image

_log = logging.getLogger("LLM")


def _get_llm_config(provider_override: str = None):
    """获取 LLM 配置，支持运行时覆盖"""
    from src.utils.config import get_config, PROVIDER_PRESETS
    cfg = get_config().llm
    provider = provider_override or cfg.provider
    preset = PROVIDER_PRESETS.get(provider, PROVIDER_PRESETS["deepseek"])
    return {
        "api_key": cfg.api_key or os.getenv("DEEPSEEK_API_KEY", ""),
        "host": cfg.host or preset["host"],
        "model": cfg.model or preset["model"],
        "temperature": cfg.temperature,
        "max_tokens": cfg.max_tokens,
    }


class LLMClient:
    """统一的 LLM 客户端"""

    def __init__(self, config=None, provider: str = None):
        self._provider = provider
        if config:
            self.client = OpenAI(api_key=config.api_key, base_url=config.host,
                                 timeout=30.0, max_retries=2)
            self.model = config.model
            self.temperature = config.temperature
            self.max_tokens = config.max_tokens
        else:
            kc = _get_llm_config(provider)
            self.client = OpenAI(api_key=kc["api_key"], base_url=kc["host"],
                                 timeout=30.0, max_retries=2)
            self.model = kc["model"]
            self.temperature = kc["temperature"]
            self.max_tokens = kc["max_tokens"]
        _log.info(f"LLM客户端就绪: {self.client.base_url} / {self.model}")

    def chat(self, messages: list, stream: bool = False, tools: list = None):
        kwargs = dict(
            model=self.model,
            messages=messages,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
        )
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        if stream:
            return self.client.chat.completions.create(stream=True, **kwargs)
        else:
            return self.client.chat.completions.create(**kwargs)


class VisionClient:
    """视觉模型客户端 - 处理截图，支持 API / Ollama / 本地 OCR 三种模式"""

    def __init__(self):
        from src.utils.config import get_config
        cfg = get_config().vision
        self._cfg = cfg
        self.mode = cfg.mode  # "api" | "ollama" | "ocr"
        self.model = cfg.model

        # API 模式客户端
        self._api_client = None
        if cfg.host and cfg.api_key:
            try:
                self._api_client = OpenAI(api_key=cfg.api_key, base_url=cfg.host,
                                          timeout=30.0, max_retries=2)
            except Exception as e:
                _log.warning(f"视觉 API 客户端初始化失败: {e}")

        # Ollama 模式客户端
        self._ollama_client = None
        self._ollama_model = cfg.ollama_model
        if cfg.ollama_host:
            try:
                self._ollama_client = OpenAI(api_key="ollama", base_url=cfg.ollama_host,
                                             timeout=60.0, max_retries=1)
            except Exception as e:
                _log.warning(f"Ollama 视觉客户端初始化失败: {e}")

        # OCR 引擎（延迟加载）
        self._ocr_engine = None
        self._ocr_engine_name = cfg.ocr_engine
        self._ocr_lang = cfg.ocr_lang

        _log.info(f"视觉客户端就绪: mode={self.mode}, model={self.model}")

    def reload_config(self):
        """热更新配置"""
        from src.utils.config import get_config
        self._cfg = get_config().vision
        self.mode = self._cfg.mode
        self.model = self._cfg.model
        self._ollama_model = self._cfg.ollama_model
        self._ocr_engine_name = self._cfg.ocr_engine
        self._ocr_lang = self._cfg.ocr_lang
        # 重建客户端
        if self._cfg.host:
            try:
                self._api_client = OpenAI(api_key=self._cfg.api_key, base_url=self._cfg.host,
                                          timeout=30.0, max_retries=2)
            except Exception:
                pass
        if self._cfg.ollama_host:
            try:
                self._ollama_client = OpenAI(api_key="ollama", base_url=self._cfg.ollama_host,
                                             timeout=60.0, max_retries=1)
            except Exception:
                pass
        self._ocr_engine = None  # 强制重新加载
        _log.info(f"视觉客户端配置已更新: mode={self.mode}")

    @staticmethod
    def _image_to_base64(image: Image.Image) -> str:
        buffer = BytesIO()
        image.save(buffer, format="PNG")
        return base64.b64encode(buffer.getvalue()).decode("utf-8")

    def analyze(self, image: Image.Image, prompt: str = None) -> str:
        if prompt is None:
            prompt = "请简要描述这张图片中的内容,包括关键文字、图标、窗口标题等。不要超过200字。"

        if self.mode == "ocr":
            return self._analyze_ocr(image)
        elif self.mode == "ollama":
            return self._analyze_ollama(image, prompt)
        else:
            return self._analyze_api(image, prompt)

    def _analyze_api(self, image: Image.Image, prompt: str, max_tokens: int = 300,
                     extra: dict = None) -> str:
        """云端 API 视觉分析（max_tokens/extra 供"如实描述"复用）"""
        if self._api_client is None:
            return "[视觉API未配置]"
        img_b64 = self._image_to_base64(image)
        messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img_b64}"}},
            ]
        }]
        try:
            kwargs = {"model": self.model, "messages": messages, "max_tokens": max_tokens}
            if extra:
                kwargs.update(extra)
            response = self._api_client.chat.completions.create(**kwargs)
            result = response.choices[0].message.content
            if result is None:
                result = ""
            _log.debug(f"视觉分析(API)完成: {len(result)} 字符")
            return result
        except Exception as e:
            _log.error(f"视觉分析(API)失败: {e}")
            return f"[视觉分析失败: {e}]"

    def _analyze_ollama(self, image: Image.Image, prompt: str, max_tokens: int = 300,
                        extra: dict = None) -> str:
        """Ollama 本地视觉模型分析"""
        if self._ollama_client is None:
            return "[Ollama未连接]"
        img_b64 = self._image_to_base64(image)
        messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img_b64}"}},
            ]
        }]
        try:
            kwargs = {"model": self._ollama_model, "messages": messages,
                      "max_tokens": max_tokens}
            if extra:
                kwargs.update(extra)
            response = self._ollama_client.chat.completions.create(**kwargs)
            result = response.choices[0].message.content
            if result is None:
                result = ""
            _log.debug(f"视觉分析(Ollama)完成: {len(result)} 字符")
            return result
        except Exception as e:
            _log.error(f"视觉分析(Ollama)失败: {e}")
            return f"[Ollama视觉分析失败: {e}]"

    def _thinking_disabled_extra(self) -> dict:
        """★ 1.9.0: deepseek 推理模型要**显式关思考**。

        否则 reasoning 会把 max_tokens 吃光、content 为空 ——
        1.7.1 在长期记忆上踩过一模一样的坑。
        """
        try:
            host = (getattr(self._cfg, "host", "") or "").lower()
            model = (self.model or "").lower()
            if "deepseek" in host and any(k in model for k in ("flash", "reasoner", "r1")):
                return {"extra_body": {"thinking": {"type": "disabled"}}}
        except Exception:
            pass
        return {}

    def describe_strict(self, image: Image.Image, prompt: str = None,
                        max_tokens: int = 900, mode: str = None) -> str:
        """★ 1.9.0: 给「文件查看工具」用的**如实描述**。

        与 analyze() 的区别：
          · 提示词默认强调"只说你真看到的"，禁推测/禁客套（调用方一般传
            file_view.STRICT_PROMPT）
          · 显式关掉推理模型的 thinking（否则 content 可能为空）
          · max_tokens 更大（要逐条抄录文字，300 不够）
          · mode 可覆盖本次使用的后端（api/ollama/ocr）—— 设置页「多模态」那块
            由此真正接上（多模态开 → 用视觉大模型；关 → 听「视觉识别」那块）
        """
        prompt = prompt or ("只描述你真实看到的内容，不要推测、不要客套。"
                            "逐条抄录画面中的文字，并客观描述画面主体。")
        use = (mode or self.mode or "api").lower()
        if use == "ocr":
            return self._analyze_ocr(image)
        extra = self._thinking_disabled_extra()
        if use == "ollama":
            return self._analyze_ollama(image, prompt, max_tokens=max_tokens, extra=None)
        return self._analyze_api(image, prompt, max_tokens=max_tokens, extra=extra)

    def _analyze_ocr(self, image: Image.Image) -> str:
        """本地 OCR 文字识别"""
        try:
            from src.core.ocr_engine import OCREngine, check_ocr_availability
        except ImportError:
            return "[OCR引擎模块加载失败]"

        if self._ocr_engine is None:
            self._ocr_engine = OCREngine(
                engine=self._ocr_engine_name,
                lang=self._ocr_lang,
            )
            if not self._ocr_engine.available:
                # ★ 1.8.10: 不可用就别缓存它 —— 否则下一次调用会跳过这段提示，
                #   直接落到 extract_text() 返回 ""，于是报"[图片中未检测到文字]"，
                #   既丢了"请安装 xx"的可操作提示，还把"没装引擎"说成"图里没字"。
                self._ocr_engine = None
                avail = check_ocr_availability()
                hint = f"[OCR引擎 '{self._ocr_engine_name}' 不可用。"
                if self._ocr_engine_name == "tesseract":
                    if not avail["tesseract_system"]:
                        hint += "\n请安装 Tesseract 系统程序。"
                    elif not avail["pytesseract"]:
                        hint += "\n请运行: pip install pytesseract"
                elif self._ocr_engine_name == "easyocr":
                    hint += "\n请运行: pip install easyocr"
                return hint + "]"

        try:
            text = self._ocr_engine.extract_text(image)
            if not text:
                return "[图片中未检测到文字]"
            _log.debug(f"OCR 识别完成: {len(text)} 字符")
            return f"[OCR识别结果]\n{text}"
        except Exception as e:
            _log.error(f"OCR 识别失败: {e}")
            return f"[OCR识别失败: {e}]"

    def recognize_image(self, image: Image.Image, custom_prompt: str = None) -> str:
        """
        通用图片识别接口（供聊天上传图片使用）
        - 使用当前配置的模式识别图片内容
        - custom_prompt: 自定义识别提示
        """
        default_prompt = (
            "请详细识别这张图片的内容。如果是截图，描述界面、文字、图标；"
            "如果是照片，描述场景、物体、人物；如果有文字，提取所有文字内容。"
        )
        prompt = custom_prompt or default_prompt
        return self.analyze(image, prompt)


class SummarizerClient:
    """LLM1 - 总结客户端：将视觉数据总结后入库"""

    SYSTEM_PROMPT = """你是一个信息提炼助手。将用户的屏幕活动描述提炼为简洁的记忆条目。
规则:
1. 每条记忆不超过50字
2. 只保留有价值的信息(如: 浏览了什么网页、打开了什么程序、输入了什么关键词)
3. 忽略纯界面切换、空白页面等无意义内容
4. 输出格式: 每行一条记忆,以"- "开头
5. 如果内容无实际价值,输出: [SKIP]"""

    def __init__(self):
        kc = _get_llm_config()
        self.client = OpenAI(api_key=kc["api_key"], base_url=kc["host"],
                             timeout=30.0, max_retries=2)
        self.model = kc["model"]
        _log.info("总结客户端就绪")

    def complete_raw(self, prompt: str, max_tokens: int = 800,
                     temperature: float = 0.2, system: str = "") -> str:
        """★ 1.8.10: 返回**原始回复文本**，不做记忆条目解析。

        给需要 JSON 输出的调用方用（如 ProfileExtractor）。
        以前那些调用方误用了 summarize() —— 它返回的是已经解析好的
        list[str]（记忆条目），于是 isinstance(item, dict) 永远为假，
        结构化字段一条都拿不到，功能静默失效。
        """
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        try:
            response = self.client.chat.completions.create(
                model=self.model, messages=messages,
                max_tokens=max_tokens, temperature=temperature)
            return (response.choices[0].message.content or "").strip()
        except Exception as e:
            _log.error(f"LLM 调用(原始模式)失败: {e}")
            return ""

    def summarize(self, raw_text: str) -> list[str]:
        messages = [
            {"role": "system", "content": self.SYSTEM_PROMPT},
            {"role": "user", "content": raw_text},
        ]
        try:
            response = self.client.chat.completions.create(
                model=self.model, messages=messages, max_tokens=500, temperature=0.3)
            content = response.choices[0].message.content
            if content is None:
                _log.warning("LLM 总结返回空内容")
                return []
            memories = self._parse_memories(content)
            if memories:
                _log.debug(f"总结完成: {len(memories)} 条记忆")
            return memories
        except Exception as e:
            _log.error(f"总结失败: {e}")
            return []

    def summarize_conversation(self, transcript: str) -> str:
        """★ 1.6.0: 总结一段连续对话，用于「旧话题 → 新话题」的上下文续接。

        与 summarize()（记忆条目提炼）不同，这里输出的是一段连贯叙事，
        包含：聊了什么、用户偏好/决定、未完成事项、关键结论。
        """
        if not transcript or not transcript.strip():
            return ""
        system = (
            "你是一个对话归档助手。请把下面这段「用户与桌宠」的连续对话压缩成一段总结，"
            "用于新话题无缝延续上下文。\n"
            "要求：\n"
            "1. 输出纯文本，一段话（可用分号分句），不要 Markdown、不要列表符号、不要标题；\n"
            "2. 必须保留：聊过的主要话题、用户的偏好/身份/决定、已完成的结论、未完成或待办的事项；\n"
            "3. 不要评价、不要客套、不要复述原文；\n"
            "4. 长度控制在 500 字以内，信息密度优先。"
        )
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": transcript[-20000:]},
                ],
                max_tokens=800, temperature=0.3)
            content = response.choices[0].message.content or ""
            content = content.strip()
            if len(content) > 800:
                content = content[:800]
            _log.info(f"对话总结完成: {len(content)} 字")
            return content
        except Exception as e:
            _log.error(f"对话总结失败: {e}")
            return ""

    def _parse_memories(self, content: str) -> list[str]:
        if content is None:
            return []
        lines = content.strip().split("\n")
        memories = []
        for line in lines:
            line = line.strip()
            if line == "[SKIP]":
                continue
            if line.startswith("- "):
                memories.append(line[2:].strip())
            elif line.startswith("-"):
                memories.append(line[1:].strip())
            elif line:
                memories.append(line)
        return memories


class ChatClient:
    """LLM2 - 对话客户端：多轮对话 + 工具调用"""

    # 默认模板（支持变量替换）
    _DEFAULT_PROMPT = """你是一个可爱的桌面宠物助手，名叫"{character}"。
你生活在用户的桌面上，可以观察用户的活动并提供帮助。

你的特点:
- 性格温柔可爱，像明日方舟的{character}一样
- {style_rule}
- 你能看到用户屏幕活动的记录，偶尔可以提一下
- 你拥有以下工具可以调用（使用 function calling 调用）:
{tools_list}

══════════════════════════════════════
工具调用规则（必须严格遵守）:
══════════════════════════════════════
1. 当用户需要实时信息（天气/搜索/系统操作等）时，直接调用工具，不要输出任何解释文字
2. 只有在工具返回结果后，才对结果进行总结回复
3. 绝对禁止编造数据！工具返回什么就说什么，返回结果中没有的数值绝不捏造
4. 需要多步操作时连续调用多个工具
5. 以下情况不调用工具：纯闲聊/打招呼/常识性问题
6. 工具失败时换个思路重试
7. 🔴致命规则：严禁爬虫！系统已启用运行时拦截，任何 curl/wget/写.py脚本运行/urllib/requests/scrapy 都会被自动拒绝。被拦截后不要重试，直接用 web_search 搜索，用 office 工具创建文档。web_search 已经能获取绝大部分网页内容。
8. 🔴文件查看规则：你完全有能力查看文件！看文件内容**优先用 file_ops 的 view 操作**
   （它会按文件类型和大小自动选路：图片/扫描件走多模态如实描述、大文件自动抽样；
   想看某几页就传 pages，如 pages="3" 或 "3-5"；要逐字原文用 method="text"）。
   要读代码/文本原文也可以用 file_ops 的 read。用户上传的文件（消息里会带绝对路径）
   以及用户提到的本机文件，都去看。严禁说「我看不到文件」「我没有眼睛」「无法查看文件」
   这类话。要把文件发给用户时用 file_ops 的 send 操作（会在聊天里生成文件卡片）。
9. 📓经验小本子：用户要求你学会积累经验。遇到"这个坑我好像踩过"的情况（同一个报错、同一个工具失败、同一步骤反复不成功），**先去长期记忆里翻一翻**有没有现成的解决办法，别从零再试一遍。一个问题解决之后，用 long_term_memory 记一条「什么情况 → 为什么不行 → 最后怎么解决」，下次直接照做。

10. 🧠长期记忆系统（★ 1.9.2 补，用户批注"LLM好像并不知道自己有长期记忆系统和对应工具"）：
   **你确实有一套长期记忆系统**，它由两部分组成，你要知道它们的存在：
   · **自动注入**：每轮对话开始时，如果系统里存过关于这个用户的信息，
     你的上下文里会出现一段以 `[长期记忆]` 开头的文字（按 身份/偏好/技术栈/
     项目/经验 等分类列出）。**那就是你的记忆**，不是用户说的话，
     但你可以自然地用它（比如已经知道他用什么编辑器，就别再问一遍）。
     如果没看到 `[长期记忆]` 段落，说明现在还没存下东西，或者这个功能被关了。
   · **写入工具**：`long_term_memory`（传一段文本，它会自己判断值不值得记、
     提取成结构化条目去重入库）。用户说"记住…""我喜欢…""我是…"这类话，
     以及你自己解决完一个坑之后，都该用它写一条。
   ★ 这个功能**可能被用户在设置里禁用**（设置 → 工具设置 → long_term_memory）。
     如果调用它时收到"功能已被用户在设置里禁用"，那就：
     **正常继续对话，一句话说明这个功能被关了、需要的话可以在设置里开回来，然后就别管了**。
     ★ 绝对不要为了"绕过"它去用 shell_exec / file_ops / 自己写脚本 / 翻数据库文件
     来手工实现一套记忆 —— 那是白费力气，用户也看不懂你在干什么。
     已经被这么坑过一次：禁用之后连跑满 20 轮去翻 read_mem.py、写 mem_inspect.py，
     把一整轮对话全烧掉了。

══════════════════════════════════════
通用推理原则（应用你的常识，不要死板）:
══════════════════════════════════════
- 【启动应用】用户说"打开/启动 XX"时，一律用 system_command 的 action=open_app，
  param 填应用名（如 "QQ"）或用户给的完整路径。**不要自己用 shell_exec 猜 `start xx`**，
  也不要因为不确定路径就先回一句"我找不到"。启动器会依次查 长期记忆 → 注册表 →
  开始菜单 → 常见安装目录，**成功启动后会自动把 exe 路径写进长期记忆**，以后直接说名字就行。
  用户直接给了 exe 路径（形如 D:/Apps/QQ/QQ.exe）→ 用 param 传完整路径启动，同样会被记住。
  只有启动器真的返回"没找到"时，才去问用户要完整路径；用户给了就再启动一次（这次带路径）。
- 【IP查询】一律优先用 system_command 的专用 action，别自己拼 curl 命令：
  · 用户只说"查IP/我的IP/本机IP/IP地址"→ action=get_ip_info（一次给全：内网IP、公网IP、国家/省份/城市/运营商/ASN/时区/经纬度）。
  · 用户明确只要局域网那个（ipconfig里的）→ action=get_local_ip。
  普通用户说"查IP"99%是想看自己电脑的IP配没配好，所以**两个都报**最稳，别只报一个让他再问一次。
- 【命令选型】优先用专用工具（如 get_ip_info / get_local_ip / open_app），没有专用工具才用 shell_exec。Windows用cmd语法，Linux/macOS用bash。
- 【数据采集】绝对不要写爬虫！用 web_search 搜数据即可。web_search 只能返回摘要，不可能靠搜索获取"完整列表"，搜 3-4 次就够用了，立即用已有数据创建文档。搜不到完整数据是正常的，系统会在搜5次后强制你停手。搜不到就告知用户信息不足，不要死循环。
- 【文档操作】用户要建表格/文档/PPT时，用 office 工具三步骤：create→add填数据→set设样式。数据来自 web_search 搜索结果即可，不要追求"完美完整数据"。搜 2-3 次后不管数据够不够都必须开始创建文档。空表格也比永远搜索强。
- 【结果解读】工具返回了数据就直接引用，不要二次推测或"修正"结果。
- 【模糊需求】用户说的不清楚时，用最合理的假设执行，不要反复追问。比如"查IP"直接按本机IP处理即可。

回复规则:
- 使用中文，保持轻松愉快的语气
{emoji_rule}"""

    def __init__(self, provider: str = None):
        kc = _get_llm_config(provider)
        self.client = OpenAI(api_key=kc["api_key"], base_url=kc["host"],
                             timeout=30.0, max_retries=2)
        self.model = kc["model"]
        self.temperature = kc["temperature"]
        self.max_tokens = kc["max_tokens"]
        # ★★ 1.9.3 第 14 轮：**思考型模型的 max_tokens 兜底下限**。
        #   真机反复踩：thinking 写到 5000+ 字就把 2048 的预算吃光 → content=0
        #   （"空回复"）或**正文半句被截断**（表现为 SyntaxError: unterminated string
        #   literal / 最后的输出异常）。而 config.json 每次退出会被程序按内存里的值
        #   重写 —— 只改文件不保险（真机 00:33 那次又是 2048）。
        #   所以在这里兜一层。
        # ★★ 1.9.3 第 26 轮：**下限从 4096 提到 8192** —— 4096 已经被真机证伪：
        #   `logs/prts_20261004_032056.log` 第 7/8/9 轮连续三次
        #   `第N轮空回复（thinking=6947/7017/6640 字，max_tokens=4096）`
        #   —— 光是思考就 6600~7000 字（中文≈1 字 1 token），**比 4096 还大**，
        #   于是正文永远没预算，连续空回复把这一轮逼到放弃。
        #   8192 正好是 config 默认值，也够这份模型把"想 + 说"都写下来。
        try:
            _host = str(kc.get("host", "")).lower()
            _mdl = str(kc.get("model", "")).lower()
            if "deepseek" in _host and any(_k in _mdl for _k in ("flash", "reasoner", "r1")):
                _floor = 8192
                if int(self.max_tokens) < _floor:
                    _log.warning(f"[max_tokens] {self.max_tokens} 太小（思考会吃光预算）"
                                 f"→ 自动提到 {_floor}（配置里仍是 {self.max_tokens}）")
                    self.max_tokens = _floor
        except Exception as _e:
            _log.debug(f"[max_tokens] 兜底判断失败: {_e}")
        self.tools = []
        self._cached_prompt = None
        self._cached_prompt_hash = None
        _log.info(f"对话客户端就绪: {kc['host']}/{kc['model']}")

    @staticmethod
    def _gif_names_for_prompt() -> list[str]:
        """★ 1.5.0: 获取当前角色的可用 GIF 名（供思考末尾选择形象）"""
        try:
            from src.utils.config import get_config
            from src.core.pet_manager import CHARACTERS_DIR, CharacterInfo
            name = get_config().pet.character
            char_dir = CHARACTERS_DIR / name
            if not char_dir.exists():
                return []
            return list(CharacterInfo(char_dir).gifs.keys())
        except Exception:
            return []

    def _gif_prompt_section(self) -> str:
        """★ 1.5.0: 形象选择协议提示词

        ★ 1.9.1（你的重新设计）：**每一段思考的末尾都要给一个 JSON** ——
        一轮对话里可能有多段思考（多轮工具调用），每段结束都表达一次情绪，
        而不是只在整轮的最后给一个。形象管理把每一次都当成一条**一次性切换
        命令**：切过去就一直保持，直到下一条命令（下一次思考的 JSON，或者
        设置里那个待机计时器到点）。
        """
        names = self._gif_names_for_prompt()
        if not names:
            return ""
        return (
            "\n\n══════════════════════════════════════\n"
            "桌宠形象选择（在思考过程中遵守）:\n"
            "你有一套桌宠形象动画（GIF），需要在回复时展示贴合情绪的动作。\n"
            "可用GIF列表: " + "、".join(names) + "\n"
            "规则: **你每一段思考（reasoning）的最后一行，都要输出一个 JSON**"
            "来指定此刻的形象动作:\n"
            '{"gif": "GIF名"}\n'
            "★ 一轮对话里你可能会思考好几次（比如查资料、调工具前后）——"
            "**每次思考结束都要输出一次**，让形象能跟着你的进度换表情；"
            "不用管下一次会不会再变，每一段都是一个独立的切换命令。\n"
            "必须从上面的列表中选择，不要编造名字。如果拿不准就选一个情绪最接近的。\n"
            "这个 JSON 只写在思考里，绝对不要出现在给用户的回复正文中。"
        )

    def _meme_prompt_section(self) -> str:
        """★ 1.9.0: 表情包选择协议（与 GIF 放同一个 JSON 里，可选）。

        设计：LLM 只挑**文件名**，主窗口在文字回复**之后**再单独发一条
        （你要的"分两条、表情包最后"）。文件名→路径的解析在
        src/core/meme_library.py 里做，避免模型编造路径。
        """
        try:
            from src.core.meme_library import prompt_catalog, stats
            if not stats().get("available"):
                return ""
            catalog = prompt_catalog(60)
        except Exception as e:
            _log.debug(f"表情包目录不可用，跳过表情包协议: {e}")
            return ""
        if not catalog:
            return ""
        return (
            "\n\n══════════════════════════════════════\n"
            "表情包（用 **meme 工具**发，别再写 JSON 协议）:\n"
            "想在回复里表达情绪时，就在**给出文字回复之前**调用一次 `meme` 工具；\n"
            "它会把挑好的表情包放进待发送状态，你的文字回复结束后会**单独发一条**。\n"
            "· 一轮里**只调用一次**，别反复调、别用它来试探\n"
            "· 可选参数 tag：happy/angry/sad/shy/daily/confused/surprised/like/see…\n"
            "  不传就按角色设定里配的优先情绪来挑\n"
            "· 用户明确说「发个表情包」「换一个」「没看见」时，**一定要调一次**"
            "（这是实测最容易掉链子的场景）\n"
            "· 不调用就是不发表情包，这完全没问题\n"
            + catalog + "\n"
            "（上面这份目录是给你挑 tag 参考的；具体文件由工具挑，不用你报文件名）"
        )

    def _rules_sections(self, cfg) -> str:
        """★ 1.9.0: 把「输出控制 / 行为控制」拼成两段规则。

        为什么必须单独拼：以前角色写了 `personality_prompt` 时，提示词是**原样使用**
        的 —— 设置页里的「回复风格 / 颜文字使用」以及新版「严禁 markdown / 动作描写」
        对角色**根本不生效**（那两行只在默认模板分支里）。现在无论走哪条分支，
        这两段都会追加，设置才真的算数。
        """
        try:
            out_rules = []
            if getattr(cfg, "no_action_description", True):
                out_rules.append("禁止动作、神态、旁白描写，只输出对话本身。")
            if getattr(cfg, "no_markdown", False):
                out_rules.append("严禁使用 markdown 字符（# * ` - > 之类）。")
            out_rules.append("允许代码块。" if getattr(cfg, "allow_code_block", True)
                             else "不要输出代码块。")
            out_rules.append("允许输出 JSON。" if getattr(cfg, "allow_json", True)
                             else "不要输出 JSON。")
            style_map = {
                "concise": "回复简洁，一般 2-3 句以内。",
                "balanced": "回复适中，3-5 句，可以适当展开。",
                "detailed": "回复可以详细一些，不限制长度。",
            }
            emoji_map = {
                "none": "不要使用颜文字/表情符号。",
                "light": "每句话末尾可适当加 1 个颜文字。",
                "medium": "适当使用颜文字和表情，让对话更生动 (｡･ω･｡)。",
                "heavy": "多用颜文字和表情符号，展现活泼可爱的性格 (๑>ᴗ<๑) ✨。",
            }
            out_rules.append(style_map.get(getattr(cfg, "reply_style", "concise"),
                                           style_map["concise"]))
            out_rules.append(emoji_map.get(getattr(cfg, "emoji_level", "light"),
                                           emoji_map["light"]))

            beh_rules = []
            _meme = {
                "off": "不要发表情包。",
                "rare": "偶尔（很少）可以发一张表情包。",
                "normal": "聊到合适的时机可以发一张表情包。",
                "often": "情绪合适就多发几张表情包。",
            }
            beh_rules.append(_meme.get(getattr(cfg, "meme_tendency", "normal"),
                                       _meme["normal"]))
            _tags = getattr(cfg, "meme_tags_prefer", None)
            if _tags:
                beh_rules.append("挑表情包时优先这些情绪：" + "、".join(map(str, _tags)) + "。")
            if not getattr(cfg, "gif_reaction", True):
                beh_rules.append("不要主动换形象动作。")

            return ("\n【输出控制】\n" + "\n".join("- " + r for r in out_rules)
                    + "\n【行为控制】\n" + "\n".join("- " + r for r in beh_rules))
        except Exception as e:
            _log.debug(f"拼装输出/行为控制失败: {e}")
            return ""

    def _progress_prompt_section(self) -> str:
        """★ 1.9.2（R71，用户设计）：长任务中途汇报一次，别让人干等。

        为什么写在**系统提示词本体**，而不是只写在工具描述里：
        真机日志证据 —— 模型跑了 **5 轮工具调用**（做一个 PPT）**一次都没调
        `progress_report`**；16 个工具里某一条描述对它的"注意力"不够，
        而且我在那条描述里还写了很强的"不要滥用"，反而更容易被跳过。
        所以在系统提示词里再明确说一次：**长任务至少汇报一次**，短聊不要用。
        """
        return ("\n\n## 干活时简单说一声（重要）\n"
                "- 开始做**需要好几步的事**（查资料、下载、生成文件、画图、批量处理…）时，"
                "用 `progress_report` **简单汇报一句你当前在做什么**，别让用户干等。\n"
                "- 就说**当前这一步**在做什么，一句话，像同事随口同步进度。"
                "**不要**罗列步骤，**不要**提「第几轮」「第几个工具」这类内部细节。\n"
                "- 汇报完**继续把活干完**，不要因为汇报就收尾或省略后续步骤。\n"
                "\n## 做 PPT（★ 1.9.3 第 24 轮按用户决定改回来：**以自己写脚本为主**）\n"
                "- 想做出**好看、有设计感**的片子（深色底、强调色条、满版大图、自己排的版式），"
                "**就自己写 python-pptx 脚本**（`file_ops write` 落盘 → `shell_exec` 跑），"
                "不要被模板限制住。用户明确说过手搓的那版比模板好看。\n"
                "- `pptx` 工具仍然可用，适合**结构简单、要快**的稿子：给大纲（`# 标题`/`- 要点`/`@图片`）"
                "一次出片，或给已有 pptx 追加页（`action=edit`）。它内置素材校验、中文默认字体与页码。\n"
                "- ★ **无论用哪种方式，做完都要自检**：用 `pptx action=render` 把每页导出 PNG，"
                "再对每个 PNG 调 `file_ops view` **逐张看**（这是唯一能\"看见成品\"的路子），"
                "查出溢出/裁切/变形就回去改，别不看就交。\n"
                "- ★ 插图前先确认素材是**全身立绘**还是**横幅卡/半身图**（看宽高比："
                "宽高比 ≥1.9 的基本都是横幅卡，人物只有半身）。半身卡当立绘用会显得\"被切了\"。\n"
                "\n## ★ 先出东西，再谈完美（重要，别一直搜集素材）\n"
                "- **素材够用就开工**：不要为了凑齐「更完美的图/更全的资料」一轮一轮搜下去。"
                "手里只要有 2~3 张能用的图，就先把**第一版做出来并交付**，再问用户要不要换图。\n"
                "- **一轮预算有限**（跑满会被截断）：真机反复出现过「搜了十几轮素材、"
                "轮次用完、成品一个字都没交出来」的情况 —— 那是**最差的结果**。\n"
                "- 判断标准：如果你已经连续 **3 轮以上**都在搜/下/看素材而没有产出物，"
                "**立刻停下来用现有素材做**，并在交付时说明「哪几张图不理想、要不要换」。\n"
                "- `download` 失败或只下到 `.part` 就**别再重试同一个链接**，换图或直接用现有的。\n")

    def _build_system_prompt(self) -> str:
        """根据当前配置动态构建系统提示词（包含工具列表）"""
        from src.utils.config import get_config
        cfg = get_config().pet

        # 构建工具列表文本
        tools_text = self._build_tools_text()

        # 生成缓存键（包含工具列表 + 新版规则开关）
        cache_key = (f"{cfg.character}|{cfg.reply_style}|{cfg.emoji_level}|"
                     f"{cfg.personality_prompt}|{len(self.tools)}|"
                     f"{getattr(cfg, 'no_markdown', None)}|{getattr(cfg, 'no_action_description', None)}|"
                     f"{getattr(cfg, 'meme_tendency', None)}|{getattr(cfg, 'gif_reaction', None)}")
        if cache_key == self._cached_prompt_hash and self._cached_prompt:
            return self._cached_prompt

        rules = self._rules_sections(cfg)

        # 自定义 prompt 直接使用
        if cfg.personality_prompt and cfg.personality_prompt.strip():
            prompt = cfg.personality_prompt.strip()
            prompt = prompt.replace("{character}", cfg.character)
            prompt = prompt.replace("{style}", cfg.reply_style)
            prompt = prompt.replace("{emoji}", cfg.emoji_level)
            prompt = prompt.replace("{tools}", tools_text)
            prompt += rules
            prompt += self._gif_prompt_section() + self._meme_prompt_section() + self._progress_prompt_section()
            self._cached_prompt = prompt
            self._cached_prompt_hash = cache_key
            return prompt

        # 默认模板
        style_map = {
            "concise": "回复简洁友好，一般在2-3句话内",
            "balanced": "回复适中，3-5句话，可以适当展开",
            "detailed": "回复可以详细一些，不限制长度",
        }
        emoji_map = {
            "none": "- 不要使用颜文字",
            "light": "- 每句话末尾可适当加 1-2 个颜文字",
            "medium": "- 适当使用颜文字和表情让对话更加生动 (｡･ω･｡)",
            "heavy": "- 多用颜文字和表情符号，展现活泼可爱的性格 (๑>ᴗ<๑) ✨",
        }

        prompt = self._DEFAULT_PROMPT.format(
            character=cfg.character,
            style_rule=style_map.get(cfg.reply_style, style_map["concise"]),
            emoji_rule=emoji_map.get(cfg.emoji_level, emoji_map["light"]),
            tools_list=tools_text,
        )
        prompt += rules          # ★ 1.9.0: 默认模板分支也要带上输出/行为控制
        prompt += self._gif_prompt_section() + self._meme_prompt_section() + self._progress_prompt_section()
        self._cached_prompt = prompt
        self._cached_prompt_hash = cache_key
        return prompt

    def _build_tools_text(self) -> str:
        """构建工具列表说明文本"""
        if not self.tools:
            return "- 无可用工具"
        lines = ["- 以下是你可以调用的工具:"]
        for tool in self.tools:
            func = tool.get("function", {})
            name = func.get("name", "?")
            desc = func.get("description", "")
            # 截取前60字符描述
            short_desc = desc[:80] + ("..." if len(desc) > 80 else "")
            lines.append(f"  · {name}: {short_desc}")
        return "\n".join(lines)

    def set_tools(self, tools: list):
        self.tools = tools
        _log.debug(f"工具已注册: {len(tools)} 个")

    def _thinking_low_kwargs(self) -> dict:
        """★ 1.9.3 第 12 轮（用户要求）：把**思考强度设为 low**。

        为什么（真机证据 logs/prts_20261004_001406.log）：
          第7/8/9 轮 `thinking=5527/3793/3322 字, content=0` —— 思考把输出预算吃光，
          正文为空 → 连续"空回复" → 放弃。max_tokens 已 2048→8192，
          这里再从**源头**把思考强度压到 low。

        兼容性：各家服务端字段不同，所以**两种都给**；
        并且一旦服务端拒绝，调用处会**自动降级**（置 `_thinking_low_unsupported`），
        之后不再发这两个字段 —— 绝不让"降思考"这件事把对话打挂。
        """
        if getattr(self, "_thinking_low_unsupported", False):
            return {}
        # ★★ 1.9.3 第 13 轮（**查官方文档后改正**）：
        #   DeepSeek 官方 OpenAI 格式里，思考**开关**是
        #   `extra_body={"thinking": {"type": "enabled"/"disabled"}}`，
        #   而**思考强度**是**顶层** `reasoning_effort: "low"/"high"/"max"`。
        #   我上一版把强度写进 thinking.type（"low"）→ 服务端 422
        #   「Failed to deserialize the JSON」（真机 00:23 实测）。
        #   文档：https://api-docs.deepseek.com/zh-cn/guides/thinking_mode/
        #   另注：思考模式下 temperature / presence_penalty / frequency_penalty 不生效。
        return {"reasoning_effort": "low",
                "extra_body": {"thinking": {"type": "enabled"}}}

    def chat_stream(self, messages: list) -> Generator:
        full_messages = [{"role": "system", "content": self._build_system_prompt()}] + messages
        kwargs = dict(
            model=self.model, messages=full_messages,
            temperature=self.temperature, max_tokens=self.max_tokens, stream=True)
        if self.tools:
            kwargs["tools"] = self.tools
            kwargs["tool_choice"] = "auto"
        _low = self._thinking_low_kwargs()
        kwargs.update(_low)
        try:
            return self.client.chat.completions.create(**kwargs)
        except Exception as _e:
            if not _low:
                raise
            _log.warning(f"[思考强度] 服务端不接受 low（{type(_e).__name__}: {_e}）"
                         f"→ 自动降级重试（之后不再发该参数）")
            self._thinking_low_unsupported = True
            kwargs.pop("reasoning_effort", None)
            kwargs.pop("extra_body", None)
            return self.client.chat.completions.create(**kwargs)

    def chat(self, messages: list):
        full_messages = [{"role": "system", "content": self._build_system_prompt()}] + messages
        kwargs = dict(
            model=self.model, messages=full_messages,
            temperature=self.temperature, max_tokens=self.max_tokens)
        if self.tools:
            kwargs["tools"] = self.tools
            kwargs["tool_choice"] = "auto"
        _low = self._thinking_low_kwargs()
        kwargs.update(_low)
        try:
            return self.client.chat.completions.create(**kwargs)
        except Exception as _e:
            if not _low:
                raise
            _log.warning(f"[思考强度] 服务端不接受 low（{type(_e).__name__}: {_e}）"
                         f"→ 自动降级重试（之后不再发该参数）")
            self._thinking_low_unsupported = True
            kwargs.pop("reasoning_effort", None)
            kwargs.pop("extra_body", None)
            return self.client.chat.completions.create(**kwargs)


# 全局活跃 ChatClient，用于 IdleTalker 等只需要简单调用的场景
_active_chat_client: Optional[ChatClient] = None
_active_client_lock = threading.Lock()

# ★ 全局 SummarizerClient 单例，避免 DataPipeline 每次批次重复创建
_summarizer_client: Optional[SummarizerClient] = None
_summarizer_client_lock = threading.Lock()


def get_active_chat_client() -> ChatClient:
    """获取一个活跃的对话客户端（用于轻量级调用，线程安全）"""
    global _active_chat_client
    with _active_client_lock:
        if _active_chat_client is None:
            _active_chat_client = ChatClient()
        return _active_chat_client


def invalidate_chat_cache():
    """★ 1.5.0: 切换桌宠角色后，强制让对话客户端重建 system prompt。

    ChatClient._build_system_prompt 有缓存（cache_key 含 character+personality_prompt），
    但为彻底避免任何角色残留，这里直接清空所有活跃实例的缓存。"""
    global _active_chat_client
    with _active_client_lock:
        _active_chat_client = None
    try:
        from src.core.session import get_session
        s = get_session()
        if s is not None and getattr(s, "client", None) is not None:
            s.client._cached_prompt = None
            s.client._cached_prompt_hash = None
    except Exception:
        pass


def get_summarizer_client() -> SummarizerClient:
    """获取 SummarizerClient 单例（线程安全）"""
    global _summarizer_client
    with _summarizer_client_lock:
        if _summarizer_client is None:
            _summarizer_client = SummarizerClient()
        return _summarizer_client


# ★ 1.9.0: 视觉客户端单例 —— 给「文件查看工具」这类非 UI 侧复用
_vision_client: Optional[VisionClient] = None
_vision_client_lock = threading.Lock()


def get_vision_client() -> VisionClient:
    """获取 VisionClient 单例（线程安全）。UI 侧仍可自己 new，这里给工具层用。"""
    global _vision_client
    with _vision_client_lock:
        if _vision_client is None:
            _vision_client = VisionClient()
        return _vision_client


def invalidate_vision_cache():
    """设置里改了视觉配置后调用，强制下次重建"""
    global _vision_client
    with _vision_client_lock:
        _vision_client = None


def switch_provider(provider: str):
    """热切换 LLM 提供商（线程安全）"""
    global _active_chat_client
    from src.utils.config import get_config, PROVIDER_PRESETS
    cfg = get_config()
    cfg.llm.provider = provider
    preset = PROVIDER_PRESETS.get(provider, {})
    if preset:
        cfg.llm.host = preset.get("host", cfg.llm.host)
        cfg.llm.model = preset.get("model", cfg.llm.model)
    with _active_client_lock:
        _active_chat_client = ChatClient(provider=provider)
    _log.info(f"LLM 提供商已切换: {provider} -> {cfg.llm.host}/{cfg.llm.model}")
    cfg.save()
