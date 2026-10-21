# -*- coding: utf-8 -*-
"""★★ 1.9.4：**首次配置引导的检测核心**（只探测、不写用户的设置）。

## 探路结论（1.9.3 第 59 轮实测，探针在 临时文件/_scratch_dev/first_run_probe_59.py）
新用户**只需要手填 1 项**：LLM API Key。其余都能自动发现、或有默认值、或"想用才问"：

    LLM API Key      必须手填（没有它整个桌宠不能用）
    能自动发现(5)     whisper 模型 / easyocr 模型 / 形象目录 / 私有数据目录 /
                      base_url+model 默认值
    想用才问(7)       ComfyUI(端口+路径) / Ollama / 代理端口 / 天气 /
                      Tavily / 终末地 / Pixiv

★ 1.9.4（用户 2026-10-21）：**"Python 依赖 / Python 版本"两条已删除** ——
  引导页不该出现"py 环境检测"（exe 版根本没有这个概念，用户看了也无可操作项）。

## ★★ 一个必须记住的坑（探路当场踩到）
`.env` **文件里明明有** `DEEPSEEK_API_KEY`，但 `os.getenv()` **读不到** ——
因为 `.env` 要先被 `load_dotenv()` 加载才会进环境，而加载时机不确定
（`net_proxy.py` 里为此专门写了一次"自己兜底读"）。
⇒ 所以 `has_llm_key()` **同时**查三处：环境变量、`.env` 文件、配置对象。
   只查 `os.getenv` 会把**已经配好的用户**又拦一遍要 key。

## 本模块**只做检测与报告**，不改用户的任何设置
  · `detect()` → 一份报告（哪些发现了、怎么发现的、哪些必须问、哪些可选）
  · `save_llm_key()` → 走**设置窗口同一条路**（`cfg.llm.api_key = ...; cfg.save()`），
    不另造一套写法 —— 否则又会出现"两处真理"。
"""
from __future__ import annotations

import os
import socket
from dataclasses import dataclass, field
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]

# ── 状态 ──
AUTO = "auto"                 # 已自动发现
MUST_FILL = "must_fill"       # 必须手填
OPTIONAL = "optional"         # 可选能力（想用才问）
OK = "ok"                     # 已有配置，无需动作


@dataclass
class Finding:
    """一条检测结果。`how` = **我是怎么知道的**（取证口径，便于用户核对）。"""
    key: str
    label: str
    status: str
    how: str
    detail: str = ""
    group: str = ""
    # 可选能力的"要不要"开关（UI 用它渲染勾选框）
    askable: bool = False
    hint: str = ""            # 怎么装/怎么开（可选能力给一句指引）

    @property
    def ok(self) -> bool:
        return self.status in (AUTO, OK)


@dataclass
class FirstRunReport:
    findings: list[Finding] = field(default_factory=list)

    def by(self, status: str) -> list[Finding]:
        return [f for f in self.findings if f.status == status]

    @property
    def auto_found(self) -> list[Finding]:
        return self.by(AUTO)

    @property
    def must_fill(self) -> list[Finding]:
        return self.by(MUST_FILL)

    @property
    def optional(self) -> list[Finding]:
        return [f for f in self.findings if f.status in (OPTIONAL, OK)
                and f.askable]

    @property
    def needs_setup(self) -> bool:
        return bool(self.must_fill)

    def summary(self) -> str:
        return (f"能自动发现 {len(self.auto_found)} 项 ｜ "
                f"必须手填 {len(self.must_fill)} 项 ｜ "
                f"可选 {len(self.optional)} 项")


# ══════════════════════════════════════════════════════════════
# .env 读取（★ 不依赖 load_dotenv 的时机）
# ══════════════════════════════════════════════════════════════
def read_env_file(path: Path | None = None) -> dict:
    """**直接读** `.env` 文件，返回 {键: 值}。

    ★ 为什么不用 `os.getenv`：见模块 docstring 里那个坑 ——
      `.env` 没被 `load_dotenv` 加载时 `os.getenv` 是空的，
      会把已经配好 key 的用户又拦一遍。
    """
    p = path or (BASE / ".env")
    out: dict[str, str] = {}
    if not p.exists():
        return out
    try:
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, _, v = s.partition("=")
            out[k.strip()] = v.strip().strip('"').strip("'")
    except Exception:
        pass
    return out


# 仅测试用：把 `.env` 的读取整体指向另一个文件，用来模拟"一台什么都没配的新机器"。
# 不设它时行为与生产完全一致。
_ENV_OVERRIDE: Path | None = None


def use_env_file_for_tests(path: Path | None):
    """★ **仅供测试**：把 `.env` 读取指向 `path`（传 None 恢复真实行为）。

    为什么需要：`_OPTIONAL_SPECS` 里那些探测也读 `.env`，
    如果只能改一个函数，测试就模拟不出"新机器"的完整样子。
    """
    global _ENV_OVERRIDE
    _ENV_OVERRIDE = path


def _env_val(name: str, env_path: Path | None = None) -> str:
    """从"环境变量"和"`.env` 文件"两处取，**谁有算谁**。"""
    p = env_path if env_path is not None else _ENV_OVERRIDE
    if p is not None:
        return read_env_file(p).get(name, "").strip()
    return (os.getenv(name) or "").strip() or read_env_file().get(name, "").strip()


def has_llm_key(env_path: Path | None = None) -> bool:
    """是否已经配好 LLM Key（**三处都查**：环境变量 / .env 文件 / 配置对象）。

    ★ 模拟模式（传了 env_path 或设了 override）下**不回落**去读真实环境 ——
      否则测试永远测不出"没有 key"那条路。
    """
    if _env_val("DEEPSEEK_API_KEY", env_path):
        return True
    if env_path is not None or _ENV_OVERRIDE is not None:
        return False
    try:
        from src.utils.config import get_config
        return bool((getattr(get_config().llm, "api_key", "") or "").strip())
    except Exception:
        return False


# ══════════════════════════════════════════════════════════════
# 各项探测（每项都写清"我是怎么知道的"）
# ══════════════════════════════════════════════════════════════
def _port_open(host: str, port: int, t: float = 0.35) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=t):
            return True
    except Exception:
        return False


# ── 可选能力的登记表：(key, 标签, 怎么判断有没有配, 指引) ──
#   顺序按"常见程度"排；UI 按这个顺序渲染勾选框。
_OPTIONAL_SPECS = [
    # ★★ 1.9.4 真机修复（用户 2026-10-06）：
    #   原话："我的建议是**首次配置，不提及 comfyUI 和 Ollama**。
    #         **ollama 在设置里面**检测，下载，扫描，选择。
    #         **comfyUI 就完全从项目剥离**作为第三方功能，用户需要这个功能时，
    #         桌宠再实现，你只需要写一份**施工说明书**，让桌宠少走错路就行了。"
    #   ⇒ 这两个**不再出现在首次配置引导里**（见 detect(include_services=False)）。
    #     · Ollama：功能保留，但入口挪到 **设置窗口**；
    #     · ComfyUI：**从产品里剥离**为第三方功能，首次配置不提；
    #       将来要做时按 `施工说明书_ComfyUI集成_1.9.4.md` 走。
    #     `in_wizard=False` 标记它们不该进引导的那张勾选表。
    ("comfyui", "ComfyUI（本地画图）",
     lambda: bool(_env_val("COMFYUI_PATH")) and Path(_env_val("COMFYUI_PATH")).exists(),
     "装好后在 .env 里填 COMFYUI_PATH，或直接启动 ComfyUI 服务",
     False),
    ("weather", "天气查询（和风天气）",
     lambda: bool(_env_val("WEATHER_API_KEY")), "去和风天气控制台申请 KEY", True),
    ("tavily", "联网搜索（Tavily）",
     lambda: bool(_env_val("TAVILY_API_KEY")), "去 tavily.com 申请 KEY", True),
    ("proxy", "代理（P站 / JM 需要）",
     lambda: any(_port_open("127.0.0.1", p) for p in (7890, 7897, 1080)),
     "开着代理客户端即可；程序会自动探测端口", True),
    ("endfield", "终末地（森空岛）",
     lambda: bool(_env_val("ENDFIELD_API_KEY")), "需要 ENFIELD_API_KEY", True),
    ("pixiv", "Pixiv",
     lambda: bool(_env_val("PIXIV_REFRESH_TOKEN")), "需要 PIXIV_REFRESH_TOKEN（会过期）",
     True),
    ("ollama", "本地视觉（Ollama）",
     lambda: _port_open("127.0.0.1", 11434), "装 Ollama 后它会开在 11434", False),
]


def detect(base: Path | None = None, include_services: bool = True) -> FirstRunReport:
    """跑一遍全部探测，返回报告。**不修改任何东西。**

    `include_services=False` = **不进首次引导**的那种报告：
    跳过 ComfyUI / Ollama（用户 2026-10-06 要求"首次配置不提及它们"），
    但**其余探测照跑**。设置窗口用默认的 `True`（它需要看到这两项）。
    """
    root = base or BASE
    r = FirstRunReport()

    # ── 1. LLM（唯一必填）──
    if has_llm_key():
        r.findings.append(Finding(
            "llm_key", "LLM API Key", OK,
            "环境变量或 .env 文件里已有 DEEPSEEK_API_KEY", "已配置", "llm"))
    else:
        r.findings.append(Finding(
            "llm_key", "LLM API Key", MUST_FILL,
            "环境变量与 .env 文件里都没有 DEEPSEEK_API_KEY", "", "llm",
            hint="去 platform.deepseek.com 申请；填好后会写进 .env（不进版本库）"))

    # ── 2. 可自动发现的 ──
    # ★★ 1.9.4（用户 2026-10-21）："引导页还残留有 py 环境检测环节，删掉。"
    #   原来这里有两条：
    #       python_deps    —— 逐个 import 试 PySide6/openai/requests/numpy
    #       python_version —— 显示 sys.version
    #   对用户来说这两条**毫无可操作性**（缺依赖他也只能看到一串包名），
    #   而 exe 版根本不存在"Python 环境"这回事 —— 摆在那里只会让人以为要装 Python。
    #   ⇒ 两条都删掉；`_probe_python_deps()` 一并删除（否则就是死代码，守卫会红）。

    # 模型类：找到就自动用，没有就"可选下载"（不属于"必须手填"）
    # ★★ 1.9.4 真机修复（用户原话："**2，缺少TTS块**"）：
    #   原来这里**手写了两项**（whisper / easyocr），**漏了 TTS 的 GENIE** ——
    #   于是向导里根本没有"语音合成"那一块，用户勾完也不知道 TTS 需要模型。
    #   ⇒ 改成**走集中注册表** `src/core/models.py`（那是模型位置的唯一真理）：
    #     ① TTS 自动就进来了（注册表里有 genie）；
    #     ② 每项的"在不在"也用注册表的 resolve（会**优先规范位置、回落旧位置**），
    #        不再是写死一条路径 —— 用户把模型搬过位置也认得出来。
    try:
        from src.core import models as _M
        _specs = list(_M.MODELS.values())
    except Exception:
        _specs = []
    for _spec in _specs:
        # ★ 把 base 传下去 —— 否则"模拟一台空机器"的测试会失效（见 models.resolve_dir）
        _found_path, _where = _M.resolve_dir(_spec.key, base=root)
        _found = _found_path is not None
        _loc = {"canonical": "规范位置", "legacy": "旧位置（建议迁移）",
                "missing": "没有"}[_where]
        r.findings.append(Finding(
            _spec.key, _spec.label, AUTO if _found else OPTIONAL,
            f"按集中注册表找 {_spec.rel}"
            + (f"，回落旧位置 {_spec.legacy[0]}" if _spec.legacy else ""),
            "已就位" if _found else f"没有（要用再下载，{_spec.size_hint}）",
            "models", askable=not _found,
            hint=f"要用「{_spec.feature}」就需要它（{_spec.size_hint}）；"
                 f"本地已有一份的话可以直接填路径让我扫描"))
        if _found and _where == "legacy":
            r.findings[-1].detail += f"（现在在{_loc}）"

    # ── 3. 运行时目录（首次要能自建）──
    _dirs = ["data/桌宠形象", "data/桌宠私有data", "data/cache",
             "data/DBs", "data/downloads", "logs"]
    _miss = [d for d in _dirs if not (root / d).is_dir()]
    r.findings.append(Finding(
        "runtime_dirs", "运行时目录", AUTO if not _miss else AUTO,
        "逐个看目录在不在", "齐了" if not _miss else f"会自动建: {', '.join(_miss[:3])}"
        + (" 等" if len(_miss) > 3 else ""), "env"))

    # ── 4. 可选能力（想用才问）──
    for _k, _label, _probe, _hint, _in_wizard in _OPTIONAL_SPECS:
        # ★★ 1.9.4 真机修复（用户 2026-10-06）：
        #   "首次配置，**不提及 comfyUI 和 Ollama**" —— 它们仍然被探测（报告里留着，
        #   设置窗口要用），但**不进引导的那张勾选表**。
        if not include_services and not _in_wizard:
            continue
        try:
            _has = bool(_probe())
        except Exception:
            _has = False
        # ★★ 1.9.4 真机反馈（用户原话）：
        #   "代理好像不是桌宠的能力，应该是写反了，JM 和 P 站是能力，**需要代理是提示**。"
        #   ⇒ 代理不是"要不要开启的功能"，而是 P站/JM 的**前提条件**。
        #     所以它**不出勾选框**（askable=False），只作为说明出现；
        #     有没有它也不会写任何配置。
        _askable = (_k != "proxy")
        r.findings.append(Finding(
            _k, _label, OK if _has else OPTIONAL,
            "已配置 / 端口在监听" if _has else "环境变量或端口都没有",
            "已就绪" if _has else "未配置", "optional",
            askable=_askable, hint=_hint))

    return r


def is_first_run() -> bool:
    """是否算"首次运行"（给 main 判断要不要弹引导）。

    ★ 判据刻意保守：**只差 LLM Key 才算首次**。已经配好 key 的老用户
      不该被引导再拦一次（他们已经配好了，弹窗只会烦人）。
    """
    return not has_llm_key()


# ══════════════════════════════════════════════════════════════
# 写入（走设置窗口同一条路）
# ══════════════════════════════════════════════════════════════
def save_llm_key(key: str, host: str = "") -> tuple[bool, str]:
    """把 LLM Key 存起来。**复用设置窗口的写法**：`cfg.llm.api_key` + `cfg.save()`，
    最终写进 `.env`（`DEEPSEEK_API_KEY`），不进版本库。

    返回 (成功?, 说明)。
    """
    key = (key or "").strip()
    if not key:
        return False, "Key 是空的"
    try:
        from src.utils.config import get_config
        cfg = get_config()
        cfg.llm.api_key = key
        if host.strip():
            cfg.llm.host = host.strip()
        cfg.save()
        return True, "已保存到 .env（不会进入版本库）"
    except Exception as e:
        return False, f"保存失败: {type(e).__name__}: {e}"


def verify_llm_key(key: str, host: str = "", timeout: float = 8.0) -> tuple[bool, str]:
    """用这个 key 发一次**最小请求**验证能不能通（只列模型，不烧对话 token）。

    ★ 直接建 `OpenAI` 客户端 —— **不要用 `ChatClient.chat()`**：
      后者会强制拼上本应用的系统提示词（历史踩坑）。
    """
    host = (host or "").strip() or _env_val("DEEPSEEK_BASE_URL") \
        or "https://api.deepseek.com/v1"
    key = (key or "").strip() or _env_val("DEEPSEEK_API_KEY")
    if not key:
        return False, "没有 Key"
    try:
        from openai import OpenAI
        cli = OpenAI(api_key=key, base_url=host, timeout=timeout)
        ms = cli.models.list()
        n = len(getattr(ms, "data", []) or [])
        return True, f"连接成功，可用模型 {n} 个"
    except Exception as e:
        msg = str(e)
        # ★★ 1.9.4 真机补：把"连接失败"**归因清楚**，别只回一句 Connection error。
        #   实测最常见的元凶是 **Windows 系统代理开着、但代理客户端没开** ——
        #   请求全被送去一个没人监听的端口。用户看到"检测异常"根本想不到这点
        #   （他当时的判断是"没给桌宠联网权限"）。
        _diag = ""
        try:
            from src.core.net_proxy import diagnose_conn_error
            _diag = diagnose_conn_error(e)
        except Exception:
            pass
        if "401" in msg or "Unauthorized" in msg or "invalid_api_key" in msg:
            return False, "Key 无效（服务端返回 401）—— 检查是否复制完整"
        if _diag:
            return False, _diag
        if "timed out" in msg.lower() or "timeout" in msg.lower():
            return False, ("连不上（超时）—— 检查网络，或 Host 是否写错。\n"
                           f"   原始错误: {type(e).__name__}: {msg[:60]}")
        return False, f"{type(e).__name__}: {msg[:80]}"


def ensure_runtime_dirs(base: Path | None = None) -> list[str]:
    """把运行时目录建起来（首次运行用）。返回实际新建了哪些。"""
    root = base or BASE
    made = []
    for d in ("data/桌宠形象", "data/桌宠私有data", "data/cache",
              "data/DBs", "data/downloads", "logs",
              # ★★ 1.9.4（子代理查出的缺口，2026-10-06 补）：
              #   **B站的 cookie 存在文件里**（`tools/bilibili_tool.py`），
              #   不是 `.env` 键 —— 可这个目录**原来不会被创建**，
              #   用户按说明书去放 cookie 时会发现"没这个目录"。
              #   ⇒ 首次运行就把它建好，用户只管往里放文件。
              "data/cookies"):
        p = root / d
        if not p.exists():
            try:
                p.mkdir(parents=True, exist_ok=True)
                made.append(d)
            except Exception:
                pass
    return made
