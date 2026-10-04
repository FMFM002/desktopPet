"""
桌宠形象管理器 v1.0 — "形象线程"
- 管理 data/桌宠形象/<角色>/ 下的形象资源
- 独立工作线程接收指令（切角色/切行为/切GIF），不阻塞 UI
- 接收 LLM 思考内容（reasoning_content），在思考结束时立即解析并切换
  到最合适的 GIF（支持思考末尾 JSON {"gif": "名字"} 的进阶协议）
- settings.json 结构（每个角色目录一个）:
  {
    "name": "铃兰",
    "avatar": "铃兰头像.jpg",
    "icon": "图标.ico",
    "persona_prompt": "...",
    "reply_style": "concise",
    "emoji_level": "light",
    "scale": 1.0,
    "model": {"temperature": 0.7, "max_tokens": 2048},
    "gif_dir": "",                     // GIF 所在子目录（空 = 角色根目录递归扫描）
    "startup_gif": "",                 // 启动/切角色时只播一遍的 GIF（空 = 自动）
    "idle_gifs": ["..."],              // 待机轮播列表（设置页多选）
    "idle_switch_rule": {"mode": "random", "value": 5}
  }
  ★ 1.9.0: 已删除 behavior_gifs「待机/思考/执行/说话」四槽 —— 动作由 LLM
    在思考末尾按文件名直接挑（_parse_gif_from_thinking）。
"""
import json
import logging
import queue
import random
import threading
import time
from pathlib import Path

from src.core import character_settings as _cs

_log = logging.getLogger("PetManager")

BASE_DIR = Path(__file__).resolve().parent.parent.parent
CHARACTERS_DIR = BASE_DIR / "data" / "桌宠形象"

BEHAVIORS = ("待机", "思考", "执行", "说话")

# ★ 1.9.1: HOLD_SECONDS 删掉了 —— 动作保持多久由"下一次切换命令"决定
#   （待机计时器到点 / 下一次思考给新 JSON），不再自己定一个 25 秒。
_IMAGE_EXTS = {".gif", ".png", ".webp"}


class CharacterInfo:
    """单个角色的形象信息"""

    def __init__(self, char_dir: Path):
        self.dir = char_dir
        self.name = char_dir.name
        self.settings: dict = {}
        self.gifs: dict[str, Path] = {}   # gif 名（去扩展名）→ 文件路径
        self.avatar: Path | None = None
        self.icon: Path | None = None
        self.reload()

    def reload(self):
        # ★ 1.9.0: settings.json 走新版结构（性格 / 输出控制 / 行为控制）。
        #   旧文件（没有 schema）由 character_settings.load() 在内存里自动转换，
        #   所以**不迁移也能用**；迁移只是把它落盘成新格式。
        s_file = self.dir / "settings.json"
        self.settings = {}
        self.settings_new: dict = {}
        self.settings_was_old = False
        if s_file.exists():
            self.settings_new, self.settings_was_old = _cs.load(s_file)
            self.settings = dict(self.settings_new)   # 兼容旧字段访问
            # 旧字段名继续可用（其它模块还在读它们）
            self.settings.setdefault("startup_gif", self.settings_new["gif"].get("startup", ""))
            self.settings.setdefault("idle_gifs", self.settings_new["gif"].get("idle", []))
            self.settings.setdefault("idle_switch_rule",
                                     self.settings_new["gif"].get("idle_switch_rule"))
            self.settings.setdefault("gif_dir", self.settings_new["gif"].get("gif_dir", ""))
            self.settings["persona_prompt"] = _cs.persona_text(self.settings_new)
            self.settings["reply_style"] = self.settings_new["输出控制"]["reply_style"]
            self.settings["emoji_level"] = self.settings_new["输出控制"]["emoji_level"]
        if self.settings.get("name"):
            self.name = self.settings["name"]

        # 扫描 GIF（gif_dir 优先，否则全目录递归）
        gif_dir_name = self.settings.get("gif_dir", "")
        search_root = (self.dir / gif_dir_name) if gif_dir_name else self.dir
        self.gifs.clear()
        if search_root.exists():
            for f in sorted(search_root.rglob("*")):
                if f.is_file() and f.suffix.lower() in _IMAGE_EXTS:
                    self.gifs[f.stem] = f

        # 头像 / 图标
        avatar_name = self.settings.get("avatar", "")
        if avatar_name and (self.dir / avatar_name).exists():
            self.avatar = self.dir / avatar_name
        else:
            for cand in list(self.dir.glob("*.jpg")) + list(self.dir.glob("*.png")):
                self.avatar = cand
                break
        icon_name = self.settings.get("icon", "")
        if icon_name and (self.dir / icon_name).exists():
            self.icon = self.dir / icon_name
        else:
            ico = self.dir / "图标.ico"
            self.icon = ico if ico.exists() else None

    # ---- GIF 解析 ----

    def resolve_gif(self, name: str) -> Path | None:
        """按名字解析 GIF：精确 → 包含 → 模糊"""
        if not name or not self.gifs:
            return None
        name = name.strip().removesuffix(".gif")
        if name in self.gifs:
            return self.gifs[name]
        low = name.lower()
        for k, v in self.gifs.items():
            if low == k.lower():
                return v
        for k, v in self.gifs.items():
            if low in k.lower() or k.lower() in low:
                return v
        return None

    def behavior_gif(self, behavior: str) -> Path | None:
        """行为 → GIF 的**兜底**。

        ★ 1.9.0: 去掉 behavior_gifs「待机/思考/执行/说话」四槽映射 ——
        动作由 LLM 在思考末尾按**文件名**直接挑（文件名本身就说清了内容，
        见 PetManager._parse_gif_from_thinking），四槽只会把可选范围压窄、
        还要人去维护一份映射。这里只负责"LLM 没挑时"的兜底，顺序：
          与行为同名的 GIF → 与行为同名的子目录 → 待机轮播第一个
          → 启动默认 GIF → 任一 GIF
        """
        if behavior in self.gifs:
            return self.gifs[behavior]
        # 回退1：与行为同名的子目录里第一个文件
        sub = self.dir / behavior
        if sub.is_dir():
            for f in sorted(sub.iterdir()):
                if f.is_file() and f.suffix.lower() in _IMAGE_EXTS:
                    return f
        # 回退2：待机轮播列表里的第一个
        for _name in (self.settings.get("idle_gifs") or []):
            p = self.resolve_gif(_name)
            if p:
                return p
        # 回退3：启动默认 GIF
        _sg = self.settings.get("startup_gif") or ""
        if _sg:
            p = self.resolve_gif(_sg)
            if p:
                return p
        # 回退4：任一 GIF
        return next(iter(self.gifs.values()), None)

    def persona_prompt(self) -> str:
        return (self.settings.get("persona_prompt") or "").strip()

    def model_params(self) -> dict:
        return self.settings.get("model", {}) or {}


class PetManager:
    """形象线程 — 所有形象切换指令经队列由单一工作线程串行执行"""

    def __init__(self, pet_proxy):
        self.pet = pet_proxy
        self.characters: dict[str, CharacterInfo] = {}
        self.current: str = ""
        self._q: queue.Queue = queue.Queue()
        self._chosen_gif: str = ""          # LLM 思考中选定的 GIF
        self._chosen_meme: str = ""         # ★ 1.9.0: LLM 选定的表情包文件名
        self._thinking_buf: str = ""
        self._thinking_state = ""           # "" / "thinking"
        # ★ 1.5.0: 待机 GIF 轮播（设置页「待机默认」多选 + 「切换规则」）
        self._startup_gif: str = ""          # 启动/切换时显示的 GIF
        self._idle_gifs: list[str] = []       # 待机轮播 GIF 列表
        self._idle_rule: dict = {"mode": "random", "value": 5}  # 切换规则
        self._idle_idx: int = 0
        self._idle_timer: threading.Timer | None = None
        # ★ 1.9.1: 待机计时器会被**两个线程**碰（形象线程的 _idle_tick、
        #   以及 session 回调线程的思考命令重置倒计时）→ 必须加锁，
        #   否则"停旧表 + 排新表"可能交错，留下两个计时器同时跑。
        self._idle_lock = threading.RLock()
        self._startup_pending: bool = False   # ★ 1.6.0: 启动 GIF 是否在等待「播完一遍」
        self._startup_gif_path: str = ""
        self._startup_fallback_timer: threading.Timer | None = None
        self._thread = threading.Thread(target=self._run, daemon=True, name="形象线程")
        self._thread.start()

    # ══════════ 主线程 API（非阻塞，全部投递到形象线程） ══════════

    def scan(self):
        self._q.put(("scan", None))

    def switch_character(self, name: str, apply_cfg: bool = True):
        self._q.put(("switch_character", (name, apply_cfg)))

    def set_behavior(self, behavior: str):
        self._q.put(("set_behavior", behavior))

    def set_gif(self, name: str):
        self._q.put(("set_gif", name))

    def set_scale(self, scale: float):
        self._q.put(("set_scale", scale))

    def on_startup_gif_finished(self):
        """★ 1.6.0: 启动/切换时的「启动默认 GIF」已完整播放一遍 → 切入待机

        由渲染子进程的 gif_once_finished 事件回调到主线程后转发。
        """
        self._q.put(("startup_gif_finished", None))

    # ---- LLM 事件入口（由 session 回调线程直接调用） ----

    # ══════════ ★ 1.9.1: 形象切换的**唯一入口** ══════════

    def apply_switch(self, name: str, source: str = "") -> bool:
        """★ 1.9.1（你的重新设计）：形象切换命令**只有这一个入口**。

        你原话："形象切换命令由计时器（设置里的定时切换，随机则是随机时间切换）
        和思考形象 json 拦截器来发送，给形象管理，然后形象管理根据命令切换成
        指定/范围内随机的 GIF。"

          · source="idle"  —— 待机计时器（范围内随机挑）
          · source="think" —— 思考形象 JSON 拦截器（指定名字）

        以及："切换保持到下一次切换，就是一次性命令"；
        "x 分钟后没有新的切换命令，就按照设置的内容切换待机 gif 了"。
        → 所以**不再有 HOLD_SECONDS 那种"25 秒后自己弹回待机"**：
          待机计时器就是"下一次切换"，它到点自然会顶掉当前动作。
        ★ 1.9.1 修正：注意那句话是"x 分钟后**没有新的切换命令**"——
          **思考命令也是"新的切换命令"**，所以它必须把待机倒计时**重新计时**。
          不重置的话，思考刚切好的动作可能在十几秒后就被待机计时器顶掉。
          真机复现（logs/prts_20260918_002248.log）：
            00:24:34 思考切「点头悠闲」→ 00:24:51 待机切「送你一朵小花」，
            只保持了 17 秒，正是你说的"弹回"。

        Returns: 是否真的切了（名字在当前角色里不存在 → 不切，保持当前）。
        """
        name = (name or "").strip()
        if not name:
            return False
        info = self.characters.get(self.current)
        if info is None or not getattr(info, "gifs", None):
            return False
        if not info.resolve_gif(name):
            _log.warning(f"[形象] 切换命令「{name}」（来源 {source or '?'}）"
                         f"在当前角色里找不到，保持当前")
            return False
        self._chosen_gif = name
        self.set_gif(name)
        _log.info(f"[形象] 切换命令 → GIF「{name}」（来源 {source or '?'}）")
        if source and source != "idle":
            # ★ 1.9.1 修正：一次性命令（思考 JSON）→ 待机倒计时**重新开始**，
            #   这样才能"保持到下一次切换"，而不是被半路杀出来的待机顶掉。
            self._restart_idle_countdown()
        return True

    def _restart_idle_countdown(self):
        """把待机计时器重新计时（幂等；没有待机列表就什么都不做）。

        ★ 1.9.1：由"非待机来源"的切换命令调用 —— 见 apply_switch 的注释。
        """
        if not self._idle_gifs:
            return
        with self._idle_lock:
            self._stop_idle_timer()
            self._schedule_idle_tick()

    def on_user_message(self):
        """用户发消息 → 思考中

        ★ 1.9.1：不再停待机计时器 —— 计时器和"思考 JSON 拦截器"是**并列的两个
        切换命令来源**，谁都不该把对方关掉。
        """
        self._thinking_buf = ""
        self._chosen_gif = ""
        self._chosen_meme = ""   # ★ 1.9.0: 表情包也按轮清空
        self._thinking_state = "thinking"
        self.set_behavior("思考")

    def on_thinking_delta(self, text: str):
        """LLM 思考内容增量（reasoning_content）"""
        if text:
            self._thinking_buf += text
        if self._thinking_state != "thinking":
            self._thinking_state = "thinking"
            self.set_behavior("思考")

    def on_thinking_done(self):
        """思考结束 → 立即解析思考内容选 GIF（赶在回复输出前切换）"""
        self._thinking_state = ""
        gif = self._parse_gif_from_thinking(self._thinking_buf)
        if gif:
            # ★ 1.9.1：走统一入口 —— 这就是"思考形象 JSON 拦截器"这个命令来源
            self.apply_switch(gif, "think")
        # ★ 1.9.0: 同一个 JSON 里还可以带 "meme"（表情包文件名）。
        #   这里只负责**解析并记住**；真正的发送由主窗口在文字回复结束后
        #   单独发一条（你要的"分两条、表情包最后"）。
        meme = self._parse_meme_from_thinking(self._thinking_buf)
        if meme:
            self._chosen_meme = meme
            _log.info(f"[形象] 思考结束 → 表情包「{meme}」（回复结束后单独发）")

    def on_reply_start(self):
        """回复开始流出 → 说话（若已有切换命令选定的 GIF 则沿用）"""
        if not self._chosen_gif:
            self.set_behavior("说话")

    def on_tool_call(self):
        """★ 1.9.1：工具调用**不再**是切换命令来源。

        你的设计里只有两个来源（待机计时器 / 思考 JSON 拦截器），所以原来那句
        `set_behavior("执行")` 和"停掉待机计时器"都删了 —— 形象保持上一个切换
        命令的样子，直到下一个命令到来。
        """

    def on_done(self):
        """对话结束。

        ★ 1.9.0: 不再立刻切待机 —— LLM 挑好的动作要**留住一会儿**。
        以前的时序（实测日志）：同一秒内 `思考结束 → GIF「启动」` 之后紧接
        `对话结束`，形象线程先切「启动」、紧接着被「待机」顶成 idle_gifs[0]
        （吃爆米花）→ 选的 GIF 只闪几毫秒。用户当场就问"你怎么一直在吃爆米花"。
        ★ 1.9.1（你的重新设计）：**不再强制切待机、也不再"保持 25 秒"**。
        模型在思考末尾选的动作保持到"下一次切换命令" —— 要么待机计时器到点，
        要么下一次思考给了新的 JSON。就是你说的"就是一次性命令"。
        """
        self._thinking_buf = ""
        # ★ 1.9.0: 表情包在 on_done 之前已被主窗口取走 → 这里清掉，
        #   避免同一条被重复发送
        self._chosen_meme = ""
        self._thinking_state = ""

    def on_abort(self):
        self._thinking_buf = ""
        self._chosen_gif = ""
        self._chosen_meme = ""
        self._thinking_state = ""
        self.set_behavior("待机")
        self._start_idle_cycle()

    # ─── ★ 1.9.1: "保持 HOLD_SECONDS 秒"这套整个删掉了 ───
    #   你的新设计里，动作保持到"下一次切换命令"（待机计时器到点 / 下一次思考
    #   给新 JSON）—— 计时器本来就是"下一次切换"，不需要再自己定一个 25 秒。

    # ══════════ 形象线程 ══════════

    def _run(self):
        while True:
            try:
                cmd, arg = self._q.get(timeout=2)
            except queue.Empty:
                continue
            try:
                getattr(self, f"_do_{cmd}", lambda a: None)(arg)
            except Exception as e:
                _log.error(f"形象线程处理 {cmd} 失败: {e}", exc_info=True)

    def _do_scan(self, _):
        if not CHARACTERS_DIR.exists():
            CHARACTERS_DIR.mkdir(parents=True, exist_ok=True)
        self.characters.clear()
        for d in sorted(CHARACTERS_DIR.iterdir()):
            if d.is_dir() and not d.name.startswith("."):
                info = CharacterInfo(d)
                if info.gifs or info.settings:
                    self.characters[info.name] = info
        _log.info(f"形象扫描完成: {list(self.characters.keys())}")

    def _do_switch_character(self, arg):
        name, apply_cfg = arg
        if not self.characters:
            self._do_scan(None)
        info = self.characters.get(name)
        if info is None:
            # 容错：大小写 / 部分匹配
            for n, i in self.characters.items():
                if n.lower() == name.lower() or name in n:
                    info, name = i, n
                    break
        if info is None:
            _log.warning(f"角色不存在: {name}，可用: {list(self.characters.keys())}")
            return
        # ★ 1.9.0（你的加载优化要求）：**同一个角色**再"切"一次（保存设置必触发）
        #   不再重载形象 —— 渲染子进程重载 GIF 是肉眼可见的卡顿，而且启动 GIF
        #   还会重播一遍。这里只同步 settings 里可能改过的项（scale / 待机轮播），
        #   动画一眼都不动。
        if name == self.current:
            info.reload()
            s = info.settings
            _prev_idle = list(self._idle_gifs)
            _prev_rule = dict(self._idle_rule)
            self._startup_gif = s.get("startup_gif", "") or ""
            if self._startup_gif.strip() in ("(自动)", "自动", "auto"):
                self._startup_gif = ""
            elif self._startup_gif and not info.resolve_gif(self._startup_gif):
                self._startup_gif = ""
            self._idle_gifs = list(s.get("idle_gifs", []) or [])
            self._idle_rule = s.get("idle_switch_rule", {}) or {"mode": "random", "value": 5}
            if info.gifs and not self._idle_gifs:
                self._idle_gifs = [sorted(info.gifs.keys())[0]]
            scale = float(s.get("scale", 1.0) or 1.0)
            self.pet.set_scale(scale)
            if apply_cfg:
                self._apply_to_config(info)
            # 待机轮播配置真改了才重启轮播（不动当前正在播的动画）
            if (self._idle_gifs != _prev_idle or self._idle_rule != _prev_rule) \
                    and not self._chosen_gif and not self._startup_pending:
                self._start_idle_cycle()
            _log.info(f"[形象] 角色 {name} 未变 → 只同步设置，不重载形象（防卡顿）")
            return
        self.current = name
        # ★ 1.5.0: 待机轮播配置（在 set_character 之前写入，避免 _do_set_behavior 误判）
        s = info.settings
        self._startup_gif = s.get("startup_gif", "") or ""
        # ★ 1.6.0: 兼容设置页曾经的占位文本/失效名字
        if self._startup_gif.strip() in ("(自动)", "自动", "auto"):
            self._startup_gif = ""
        elif self._startup_gif and not info.resolve_gif(self._startup_gif):
            self._startup_gif = ""
        self._idle_gifs = list(s.get("idle_gifs", []) or [])
        self._idle_rule = s.get("idle_switch_rule", {}) or {"mode": "random", "value": 5}
        self._idle_idx = 0
        # 兜底：未配置时，启动默认 = 当前角色第一个 GIF；待机轮播至少包含该 GIF
        if info.gifs:
            first_gif = sorted(info.gifs.keys())[0]
            if not self._startup_gif:
                self._startup_gif = first_gif
            if not self._idle_gifs:
                self._idle_gifs = [first_gif]
        self._stop_idle_timer()
        # ★ 1.6.0: 注意顺序 —— set_character 在渲染端会顺带加载「待机」GIF，
        #   所以必须先切角色，再显式覆盖为「启动默认 GIF」，否则启动 GIF 会被待机 GIF 顶掉。
        self.pet.set_character(name)
        info.reload()  # ★ 1.5.0: 重新读取最新 settings（用户可能在设置页改过 scale/persona）
        scale = float(info.settings.get("scale", 1.0) or 1.0)
        self.pet.set_scale(scale)
        # ★ 1.5.0: 启动默认 GIF 优先（设置页「启动默认」），否则回退到 behavior_gif("待机")
        gif = info.resolve_gif(self._startup_gif) if self._startup_gif else None
        if not gif:
            gif = info.behavior_gif("待机")
        if gif:
            # ★ 1.6.0: 启动默认 GIF「只播放一遍」（由渲染进程统计播放轮次）
            self.pet.set_gif_file(str(gif), once=True)
        if apply_cfg:
            self._apply_to_config(info)
        # ★ 1.6.0: 启动 GIF 只播放一遍 → 播放完成后切入待机轮播
        self._schedule_startup_then_idle(gif)
        _log.info(f"[形象] 切换角色: {name} (GIF×{len(info.gifs)}, 待机轮播×{len(self._idle_gifs)})")

    def _do_set_behavior(self, behavior: str):
        if behavior not in BEHAVIORS:
            behavior = "待机"
        info = self.characters.get(self.current)
        if info is None:
            return
        # ★ 1.9.0: LLM 已在思考末尾按**文件名**挑好了动作 → 本轮内的
        #   思考/说话/执行一律沿用它，不要用兜底 GIF 顶掉。
        #   为什么必须在这里兜：主窗口在回复/工具阶段会直接调
        #   set_behavior("说话")/("执行")，而四槽删掉后这些行为的兜底会
        #   回落到「待机轮播第一个」—— 表现就是"动作一直不变、老是同一个
        #   待机 GIF"（你反馈的 X4）。这里在形象线程里统一拦住最稳。
        if behavior != "待机" and self._chosen_gif:
            _pick = info.resolve_gif(self._chosen_gif)
            if _pick:
                # ★ 1.9.1: 这里原来会 _stop_idle_timer()，而重构后没人再启回来 →
                #   第一次思考之后自动轮播就永久停了（你：自动切换好像也没了）。
                #   待机计时器是**并列的命令来源**，切行为时不该把它关掉。
                # ★★ 1.9.3 第 15 轮（用户报"死循环日志"）：
                #   这里原来**每次 set_behavior 都打一行**，而形象线程每帧都会调它
                #   → 真机 1 分钟内刷了 **1501 行**一模一样的话。
                #   现在只在"**选定动作真的变了**"时打一行（去重），其余降为 debug。
                _key = f"{behavior}|{Path(_pick).name}"
                if getattr(self, "_last_chosen_log", None) != _key:
                    self._last_chosen_log = _key
                    _log.info(f"[形象] {behavior} 沿用 LLM 选定动作: {Path(_pick).name}")
                else:
                    _log.debug(f"[形象] {behavior} 沿用 LLM 选定动作: {Path(_pick).name}")
                self.pet.set_gif_file(str(_pick))
                return
        gif = info.behavior_gif(behavior)
        # ★ 1.6.0: 仅在「启动/切换角色」期间，首个待机 GIF 才只播放一遍
        once = bool(behavior == "待机" and self._startup_pending and self._idle_gifs)
        if gif:
            self.pet.set_gif_file(str(gif), once=once)
            if once:
                # 与启动 GIF 同样的「播完 → 衔接待机」流程
                self._schedule_startup_then_idle(gif)
                return
        else:
            self.pet.set_behavior(behavior)  # 兜底：老协议
        # ★ 1.9.1（你的设计）：待机计时器是**并列的切换命令来源**，
        #   所以切行为时**一律不停它**。以前这里是
        #   「进入待机 → 启动轮播；其它行为 → 停止轮播」，重构后没有任何地方
        #   再把轮播启回来 → 你看到的「全程吃爆米花、自动切换没了」。
        #   现在只**确保它在跑**（幂等），待机与否都按设置的时间自己走。
        self._ensure_idle_timer()

    def _ensure_idle_timer(self):
        """确保待机计时器在跑（幂等，不会重置已排好的那一次）。

        ★ 1.9.1：它是两个切换命令来源之一，**不该被切行为关掉**。
        """
        if not self._idle_gifs:
            return
        if self._idle_timer is None:
            self._schedule_idle_tick()

    # ─── 待机 GIF 轮播（★ 1.5.0） ───

    def _stop_idle_timer(self):
        # ★ 1.9.1: 加锁（可重入）—— 调用方可能是 _restart_idle_countdown（已持锁）
        with self._idle_lock:
            if self._idle_timer is not None:
                self._idle_timer.cancel()
                self._idle_timer = None

    # ─── ★ 1.6.0: 启动默认 GIF「只播放一遍」→ 直接衔接待机 ───

    def _gif_duration_seconds(self, gif_path: str) -> float:
        """★ 1.9.2（R58）：读一个 GIF「播完一遍」要多久（帧数 × 每帧延时）。

        为什么需要：启动 GIF 的"兜底衔接"定时器原来是**写死 12 秒** ——
        子进程的"播完一遍"事件一旦丢掉/来晚，画面就会**停在最后一帧干等十几秒**
        （正是你反馈的"长时间停留启动的最后一帧"）。
        现在按它**自己的时长**定兜底，事件到了就立刻衔接。

        只读元数据（PIL 不解码像素），同一文件只算一次，结果缓存。
        """
        if not gif_path:
            return 0.0
        cache = getattr(self, "_gif_dur_cache", None)
        if cache is None:
            cache = self._gif_dur_cache = {}
        if gif_path in cache:
            return cache[gif_path]
        dur = 0.0
        try:
            from PIL import Image
            with Image.open(gif_path) as im:
                n = getattr(im, "n_frames", 1) or 1
                total = 0.0
                for i in range(n):
                    try:
                        im.seek(i)
                    except Exception:
                        break
                    total += float(im.info.get("duration") or 0) / 1000.0
                # 有些 GIF 不写 duration → 按 100ms/帧 估
                dur = total if total > 0.05 else n * 0.1
        except Exception as e:
            _log.debug(f"[形象] 读 GIF 时长失败（按 4s 估算）: {gif_path} ({e})")
            dur = 4.0
        cache[gif_path] = dur
        return dur

    def _schedule_startup_then_idle(self, startup_gif):
        """启动 GIF 已在播放（once=True）。

        - 渲染进程播完一遍会回传 gif_once_finished → 立即衔接待机轮播；
        - 同时挂一个兜底定时器（防止子进程事件丢失/非 GIF 资源），避免卡在启动帧；
        - 未配置待机列表时不做任何切换（启动 GIF 保持循环播放）。
        """
        self._stop_idle_timer()
        self._cancel_startup_fallback()
        self._startup_pending = bool(self._idle_gifs)
        if not self._idle_gifs:
            return
        self._startup_gif_path = str(startup_gif) if startup_gif else ""
        # ★ 1.9.2（R58，用户反馈"长时间停在启动 GIF 的最后一帧"）：
        #   兜底定时器**不再写死 12 秒**，改成"这个 GIF 自己的时长 + 2 秒余量"
        #   （下限 2.5s / 上限 15s）。事件正常到达时兜底根本不会触发。
        _dur = self._gif_duration_seconds(self._startup_gif_path)
        _fb = max(2.5, min(15.0, (_dur or 4.0) + 2.0))
        self._startup_started_at = time.time()
        _log.info(f"[形象] 启动 GIF 兜底 {_fb:.1f}s（该 GIF 时长约 {_dur:.1f}s；"
                  f"子进程事件到了会立刻衔接）")
        t = threading.Timer(_fb, self._startup_fallback_fire)
        t.daemon = True
        self._startup_fallback_timer = t
        t.start()

    def _cancel_startup_fallback(self):
        if self._startup_fallback_timer is not None:
            self._startup_fallback_timer.cancel()
            self._startup_fallback_timer = None

    def _startup_fallback_fire(self):
        if self._startup_pending:
            _el = time.time() - getattr(self, "_startup_started_at", time.time())
            # ★ 1.9.2（R58）：提到 warning —— 这条一旦出现，就说明"子进程的播完事件
            #   没到/来晚了"，是真机排查"启动 GIF 停住"的关键线索（以前是 debug，看不见）。
            _log.warning(f"[形象] 没等到启动 GIF 的'播完一遍'事件，走兜底衔接"
                         f"（等了 {_el:.1f}s）")
            self._q.put(("startup_gif_finished", None))

    def _do_startup_gif_finished(self, _):
        """启动 GIF 播放完毕（或兜底超时）→ 立即进入待机轮播"""
        if not self._startup_pending:
            return
        _el = time.time() - getattr(self, "_startup_started_at", time.time())
        _log.info(f"[形象] 启动 GIF 已衔接待机（实际等了 {_el:.1f}s）")
        self._startup_pending = False
        self._cancel_startup_fallback()
        self._enter_idle_from_startup()

    def _enter_idle_from_startup(self):
        """启动 GIF 播完后进入待机：从首个待机 GIF 开始轮播。
        若待机轮播第一个恰好就是启动 GIF，则跳过它（避免「播两遍」）。"""
        self._stop_idle_timer()
        if not self._idle_gifs:
            return
        info = self.characters.get(self.current)
        idx = 0
        if info is not None:
            first_name = self._idle_gifs[0]
            first_path = info.resolve_gif(first_name)
            startup_path = getattr(self, "_startup_gif_path", "") or ""
            if first_path and str(first_path) == startup_path:
                idx = 1 % len(self._idle_gifs)   # 跳过重复的启动 GIF
        self._idle_idx = idx
        self._do_set_gif(self._idle_gifs[self._idle_idx])
        self._schedule_idle_tick()

    def _start_idle_cycle(self):
        """启动待机 GIF 轮播（依赖 settings.idle_gifs + idle_switch_rule）"""
        self._stop_idle_timer()
        if not self.current or not self._idle_gifs:
            return
        self._idle_idx = -1   # 首个 tick 落到索引 0（首个待机 GIF）
        self._schedule_idle_tick()

    def _schedule_idle_tick(self):
        if not self._idle_gifs:
            return
        val = max(1, int(self._idle_rule.get("value", 5) or 5))
        if self._idle_rule.get("mode") == "fixed":
            delay = float(val) * 60.0          # 固定时间切换（分钟）
        else:
            delay = random.uniform(2.0, 2.0 + val * 3.0)  # 随机时间切换（随机度 1-10）
        with self._idle_lock:
            # ★ 1.9.1: 排新表前先掐掉旧的，保证"任何时刻最多一个待机计时器"
            if self._idle_timer is not None:
                self._idle_timer.cancel()
            self._idle_timer = threading.Timer(delay, self._idle_tick)
            self._idle_timer.daemon = True
            self._idle_timer.start()

    def _idle_tick(self):
        """★ 1.9.1（你的设计）：待机计时器到点 → 从待机列表里**范围内随机**挑一个。

        你原话："形象管理根据命令切换成指定/范围内随机的 GIF" ——
        「指定」是思考 JSON 给的，「范围内随机」就是这个计时器给的。
        它和思考拦截器是**并列的两个命令来源**，所以这里也走同一个入口。
        """
        if not self._idle_gifs:
            return
        pick = self._pick_idle_gif()
        if pick:
            self.apply_switch(pick, "idle")
        self._schedule_idle_tick()

    def _pick_idle_gif(self) -> str:
        """从待机列表里随机挑一个（尽量别连着重复同一张，观感更好）"""
        if not self._idle_gifs:
            return ""
        if len(self._idle_gifs) == 1:
            return self._idle_gifs[0]
        cur = self._chosen_gif
        pool = [g for g in self._idle_gifs if g != cur] or list(self._idle_gifs)
        return random.choice(pool)

    def _do_set_gif(self, name: str):
        info = self.characters.get(self.current)
        if info is None:
            return
        gif = info.resolve_gif(name)
        if gif:
            # ★ 1.9.0: 明确记一条 INFO —— 以后"动作到底切没切"看日志即可判定
            #   （以前只有失败才 debug，成功时没有任何痕迹，没法排查）
            _log.info(f"[形象] 应用动作 GIF: {Path(gif).name}")
            self.pet.set_gif_file(str(gif))
        else:
            _log.warning(f"[形象] GIF 未找到「{name}」，保持当前")

    def _do_set_scale(self, scale: float):
        try:
            self.pet.set_scale(float(scale))
        except Exception:
            pass

    # ══════════ 内部工具 ══════════

    def _apply_to_config(self, info: CharacterInfo):
        """把角色 settings.json 应用到全局配置（人设/模型参数）"""
        try:
            from src.utils.config import get_config
            cfg = get_config()
            cfg.pet.character = info.name
            persona = info.persona_prompt()
            if persona:
                cfg.pet.personality_prompt = persona
            for k in ("reply_style", "emoji_level"):
                if info.settings.get(k):
                    setattr(cfg.pet, k, info.settings[k])
            mp = info.model_params()
            if mp.get("temperature") is not None:
                cfg.llm.temperature = float(mp["temperature"])
            if mp.get("max_tokens"):
                cfg.llm.max_tokens = int(mp["max_tokens"])
            _log.debug(f"[形象] {info.name} 配置已应用 (persona={len(persona)}字)")
        except Exception as e:
            _log.warning(f"应用角色配置失败: {e}")

    def _parse_gif_from_thinking(self, text: str) -> str:
        """从 LLM 思考文本中解析 GIF 选择。
        优先级：末尾 JSON {"gif": ...} → 文本中出现的 GIF 名（取最后一次）"""
        if not text:
            return ""
        info = self.characters.get(self.current)
        if info is None or not info.gifs:
            return ""
        names = list(info.gifs.keys())

        # 1) 末尾 JSON 协议
        tail = text[-400:]
        idx = tail.rfind("{")
        if idx >= 0:
            try:
                data = json.loads(tail[idx:])
                gif = data.get("gif") or data.get("gif_name") or data.get("animation")
                if gif and info.resolve_gif(str(gif)):
                    return str(gif)
            except Exception:
                pass

        # 2) 文本中直接出现的 GIF 名（越靠后优先）
        best, best_pos = "", -1
        for n in names:
            if len(n) < 2:
                continue
            p = text.rfind(n)
            if p > best_pos:
                best, best_pos = n, p
        return best

    def _parse_meme_from_thinking(self, text: str) -> str:
        """★ 1.9.0: 从思考末尾的 JSON 里取 "meme"（表情包文件名）。

        与 GIF 是同一个协议位置：{"gif": "…", "meme": "…"}，meme 可选。
        容错：模型有时在 JSON 后面又补一句话，所以 json.loads 失败时
        退化成"找最后一个像 JSON 的大括号块"。
        """
        if not text:
            return ""
        import re as _re
        tail = text[-600:]
        idx = tail.rfind("{")
        if idx < 0:
            return ""
        candidate = tail[idx:]
        data = None
        try:
            data = json.loads(candidate)
        except Exception:
            for m in _re.finditer(r"\{[^{}]*\}", candidate):
                try:
                    d = json.loads(m.group(0))
                    if isinstance(d, dict) and ("meme" in d or "gif" in d):
                        data = d
                except Exception:
                    continue
        if not isinstance(data, dict):
            return ""
        for k in ("meme", "meme_file", "sticker", "表情包"):
            v = data.get(k)
            if v:
                return str(v).strip()
        return ""

    def chosen_meme(self) -> str:
        """★ 1.9.0: 本轮 LLM 挑的表情包文件名（主窗口在回复结束后取走并发送）"""
        return self._chosen_meme or ""

    # ★ 1.9.0（你的提议）：工具返回里的"切换的GIF名称"
    #   为什么用**标签**兜一层：每个角色的 GIF 名字完全不同（deepseek 20 个、
    #   铃兰自己那套），工具不可能知道当前角色有哪些名字。所以约定：
    #      工具/会话给一个「名字」或「语义标签」，
    #      这里先按名字精确匹配，匹配不到再按标签→关键词模糊匹配角色自己的 GIF，
    #      都不中就当 "0"（保持当前形象）—— 绝不因为一个提示把形象搞坏。
    # ─── ★ 1.9.1: 工具返回的 GIF 提示整条路删掉了 ───
    #   你原话："决定返回之前的方式" —— 回到"每次思考末尾给一个 JSON"。
    #   所以 GIF_TAGS 语义标签表 + apply_gif_hint() 都不再需要：
    #   切换命令只剩两个来源（待机计时器 / 思考 JSON 拦截器），都走 apply_switch()。

    def apply_protocol(self, data: dict) -> None:
        """★ 1.9.0: 处理**从正文里**解析出来的协议 JSON（{"gif":…, "meme":…}）。

        为什么需要这个入口：模型有时把协议吐在正文而不是思考通道里（真机实测
        2026-09-16），走 `_parse_*_from_thinking` 那条路就会全部落空 ——
        表情包不发、正文里还明晃晃留着一段 JSON。这里统一兜住：
          · gif  → 立刻切换（会先在角色 GIF 表里校验名字，编的不认）
          · meme → 只记住，由主窗口在文字回复**之后**单独发一条
        """
        if not isinstance(data, dict):
            return
        info = self.characters.get(self.current)
        for k in ("gif", "gif_name", "animation"):
            v = data.get(k)
            if v and info is not None and info.resolve_gif(str(v)):
                gif = str(v).strip()
                self._chosen_gif = gif
                self.set_gif(gif)
                _log.info(f"[形象] 正文协议 → GIF「{gif}」")
                break
        for k in ("meme", "meme_file", "sticker", "表情包"):
            v = data.get(k)
            if v:
                self._chosen_meme = str(v).strip()
                _log.info(f"[形象] 正文协议 → 表情包「{self._chosen_meme}」"
                          f"（回复结束后单独发）")
                break

    # ══════════ 查询（供设置页） ══════════


# ══════════ 角色卡片扫描（无需 proxy，供设置页使用） ══════════

def scan_character_cards() -> list[dict]:
    """扫描 data/桌宠形象/，返回角色卡片数据（不依赖 PetProxy 单例）。

    返回列表元素: {
        "name": str,
        "avatar": str(路径或""),
        "gif_count": int,
        "gif_names": list[str],
    }
    """
    out = []
    if not CHARACTERS_DIR.exists():
        CHARACTERS_DIR.mkdir(parents=True, exist_ok=True)
    for d in sorted(CHARACTERS_DIR.iterdir()):
        if not d.is_dir() or d.name.startswith("."):
            continue
        info = CharacterInfo(d)
        if not (info.gifs or info.settings):
            continue
        out.append({
            "name": info.name,
            "avatar": str(info.avatar) if info.avatar else "",
            "gif_count": len(info.gifs),
            "gif_names": list(info.gifs.keys()),
        })
    return out


# ══════════ 单例 ══════════

_manager: PetManager | None = None
_manager_lock = threading.Lock()


def get_pet_manager(pet_proxy=None) -> PetManager:
    global _manager
    with _manager_lock:
        if _manager is None:
            if pet_proxy is None:
                raise RuntimeError("PetManager 尚未初始化（需要先传入 PetProxy）")
            _manager = PetManager(pet_proxy)
        return _manager
