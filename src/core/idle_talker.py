"""
主动待机说话模块
- 定期触发 LLM 生成温暖关怀话语
- 基于时间、活跃度、记忆等上下文
"""
import logging
import random
import threading
import time
from datetime import datetime
from typing import Optional

# ★ 1.9.3 第 37 轮：复用 monitor.py 的"可打断睡眠"（见 stop() 的注释）
from src.core.monitor import StoppableSleep

_log = logging.getLogger("IdleTalker")

# 不同时段的话题引导
TIME_TOPICS = {
    (5, 8): "现在是清晨，主人可能刚起床，说点温暖的早安问候",
    (8, 12): "现在是上午，主人可能在工作，说点加油打气的话",
    (12, 14): "现在是中午，提醒主人按时吃午饭",
    (14, 18): "现在是下午，主人可能有些疲惫，说点轻松的",
    (18, 21): "现在是傍晚，提醒主人休息一下眼睛",
    (21, 23): "现在是晚上，主人可能还在工作，说点关心的话",
    (23, 24): "夜深了，提醒主人早点休息",
    (0, 5): "深夜了，关心主人为什么还不睡",
}


class IdleTalker(StoppableSleep):
    """主动待机说话器"""

    def __init__(self):
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._interval = 10 * 60  # 默认10分钟
        self._min_interval = 3 * 60  # 最小3分钟
        self._max_interval = 20 * 60  # 最大20分钟
        self._last_talk_time = 0
        self._cooldown_after_user = 3 * 60  # 用户消息后冷却3分钟
        self._last_user_time = 0
        self._last_idle_msg = ""
        self._init_stop_event()      # ★ 第 37 轮：可打断睡眠（见 stop 的注释）

        # 回调
        self.on_idle_message: Optional[callable] = None  # (text) -> 发送消息到聊天
        self.on_tts: Optional[callable] = None  # (text) -> TTS 播报
        # ★ 1.9.0: 主动说话改成"注入会话"这条路（同一上下文、能调工具）。
        #   回调签名: (prompt: str) -> None；没接就退回老的单次 LLM 调用。
        self.on_idle_trigger: Optional[callable] = None

    def configure(self, enabled: bool = True, interval_minutes: int = 10):
        self._interval = max(2, interval_minutes) * 60
        self._min_interval = max(1, max(2, interval_minutes) // 3) * 60
        self._max_interval = max(5, max(2, interval_minutes) * 2) * 60
        if enabled and not self._running:
            self.start()
        elif not enabled and self._running:
            self.stop()

    def start(self):
        if self._running:
            return
        self._stop_event.clear()      # ★ 第 37 轮：重启前复位
        self._running = True
        self._last_talk_time = time.time()
        self._thread = threading.Thread(target=self._run, daemon=True, name="idle-talker")
        self._thread.start()
        _log.info(f"主动待机说话已启动 (间隔: {self._interval//60}分钟)")

    def stop(self):
        """★★ 1.9.3 第 37 轮：**先叫醒它再 join**。

        真机证据（`logs/prts_20261004_071339.log`）：第 36 轮把监控改成可打断后，
        退出时间线里剩下的那 2 秒全在这里 ——
            07:14:09 监控已停止（已经瞬间）
            ~~~ 2 秒空档 ~~~
            07:14:11 待机说话已停止
        原因一模一样：`_run` 里 `time.sleep(next_sleep)` 最长可睡到 **20 分钟**
        （`_max_interval`），而这里是 `join(timeout=2)` —— **只能干等满 2 秒**。
        """
        self._running = False
        try:
            self._stop_event.set()
        except Exception:
            pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)
        _log.info("主动待机说话已停止")

    def on_user_message(self):
        """用户发送消息时调用，重置冷却"""
        self._last_user_time = time.time()

    def _run(self):
        # ★ 1.9.3 第 37 轮：可打断睡眠（见 _nap / stop 的注释）。
        #   这一句原来在 while 外面 `time.sleep(10)`，stop() 时同样要干等。
        if self._nap(10):
            return
        while self._running:
            try:
                now = time.time()
                # 冷却检查
                if now - self._last_user_time < self._cooldown_after_user:
                    if self._nap(30):
                        break
                    continue
                if now - self._last_talk_time < self._min_interval:
                    if self._nap(10):
                        break
                    continue

                # 到达最长间隔或随机触发
                elapsed = now - self._last_talk_time
                if elapsed >= self._max_interval or random.random() < 0.15:
                    self._generate_and_send()
                    self._last_talk_time = time.time()
                    # 下次间隔随机化
                    next_sleep = random.randint(self._min_interval, self._max_interval)
                else:
                    next_sleep = 15  # 每15秒检查一次
                # ★ 这一睡最长可到 20 分钟（_max_interval）—— 原来 join 干等满 2 秒的头号来源
                if self._nap(next_sleep):
                    break
            except Exception as e:
                _log.error(f"IdleTalker 错误: {e}")
                if self._nap(60):
                    break

    def _process_context(self) -> str:
        """★ 1.9.0（你当初的要求）：看用户在干什么 —— 就前 3 个最新进程 + 前 3 个内存最大的。

        特意写得独立、简单（只依赖 psutil），放在主动说话模块里，不影响别处。
        """
        try:
            import psutil
        except Exception:
            return ""
        try:
            procs = []
            for p in psutil.process_iter(["pid", "name", "create_time", "memory_info"]):
                try:
                    info = p.info
                    if not info.get("name"):
                        continue
                    mem = 0
                    if info.get("memory_info") is not None:
                        mem = int(getattr(info["memory_info"], "rss", 0) or 0)
                    procs.append({
                        "name": info["name"],
                        "created": float(info.get("create_time") or 0),
                        "mem": mem,
                    })
                except Exception:
                    continue
            newest = sorted(procs, key=lambda x: x["created"], reverse=True)[:3]
            biggest = sorted(procs, key=lambda x: x["mem"], reverse=True)[:3]
            fmt = lambda lst: "、".join(          # noqa: E731
                f"{x['name']}({x['mem'] / 1024 / 1024:.0f}MB)" for x in lst) or "未知"
            return (f"【用户当前在用什么】\n"
                    f"- 最近启动的程序: {fmt(newest)}\n"
                    f"- 占内存最多的程序: {fmt(biggest)}")
        except Exception as e:
            _log.debug(f"取进程上下文失败: {e}")
            return ""

    def _process_brief(self) -> str:
        """★ 1.9.0: 进程上下文压成**一行**（你反馈"注入了一大段"）。

        之前把整段性格设定 + 一大块进程清单都塞进消息里，聊天记录里看起来就是
        "用户自己发了一大段"。现在性格交给会话的系统提示词（那里本来就有），
        这里只留一行"他在用什么"，而且明确标注这是背景、不要复述。
        """
        try:
            import psutil
            newest, biggest = [], []
            procs = []
            for p in psutil.process_iter(["name", "create_time", "memory_info"]):
                try:
                    i = p.info
                    if not i.get("name"):
                        continue
                    mem = int(getattr(i.get("memory_info"), "rss", 0) or 0)
                    procs.append((i["name"], float(i.get("create_time") or 0), mem))
                except Exception:
                    continue
            newest = [x[0] for x in sorted(procs, key=lambda x: x[1], reverse=True)[:3]]
            biggest = [x[0] for x in sorted(procs, key=lambda x: x[2], reverse=True)[:3]]
            _n = "、".join(newest) or "未知"
            _b = "、".join(biggest) or "未知"
            return f"他最近打开 {_n}；占用最多 {_b}。别复述这行。"
        except Exception as e:
            _log.debug(f"取进程上下文失败: {e}")
            return ""

    def _persona_line(self) -> str:
        """★ 1.9.0: 主动说话也要带**当前角色**的性格（以前硬编码"铃兰"）。"""
        try:
            from src.core.pet_manager import CHARACTERS_DIR, CharacterInfo
            from src.utils.config import get_config
            name = get_config().pet.character
            info = CharacterInfo(CHARACTERS_DIR / name)
            persona = (info.persona_prompt() or "").strip()
            if persona:
                return f"你的名字是「{name}」，下面是你的性格设定（照着说话）：\n{persona}"
            return f"你的名字是「{name}」。"
        except Exception as e:
            _log.debug(f"取角色性格失败: {e}")
            return ""

    def _generate_and_send(self):
        """生成并发送主动话语

        ★ 1.9.0: 改成"和正常聊天同一性质"——把触发语**注入会话**（on_idle_trigger），
        LLM 因此带着完整上下文、还能自己调工具来和用户互动；不再另起一个
        client.chat() 的孤立调用（你当初批注的"上下文断裂"就是这个）。
        没接 on_idle_trigger 时退回老的单次调用，保证不炸。
        """
        now = datetime.now()
        topic_hint = ""
        for (start_h, end_h), hint in TIME_TOPICS.items():
            if start_h <= now.hour < end_h or (start_h > end_h and (now.hour >= start_h or now.hour < end_h)):
                topic_hint = hint
                break

        # 真实"多久没理我"：以最后一次用户消息为准（没有再退回本轮计时）
        _base = self._last_user_time or self._last_talk_time or time.time()
        idle_minutes = max(1, int((time.time() - _base) / 60))

        # ★ 1.9.0（你的要求）：提示词可在设置 → 工具设置 → idle_talk 里改。
        #   默认就是你说的那句；这里额外补上"现在几点/多久没理你/他在用什么"做背景。
        _custom = ""
        try:
            from src.utils.config import get_config
            _custom = (getattr(get_config().idle, "prompt", "") or "").strip()
        except Exception:
            _custom = ""
        _trigger = _custom or "用户长时间没有理你了，你可以主动一次，可以调用工具看看用户再干嘛"
        prompt = (
            f"[自动工具触发] {_trigger}\n"
            f"（背景：现在 {now.strftime('%H:%M')}，用户 {idle_minutes} 分钟没说话。"
            f"{self._process_brief()}"
            f"{('　' + topic_hint) if topic_hint else ''}）"
        )

        if self.on_idle_trigger:
            try:
                _log.info("主动说话: 注入会话（系统触发，短触发语）")
                self.on_idle_trigger(prompt)
                return
            except Exception as e:
                _log.warning(f"注入会话失败，退回单次调用: {e}")

        try:
            from src.core.llm_client import get_active_chat_client
            client = get_active_chat_client()
            messages = [{"role": "user", "content": prompt}]
            response = client.chat(messages)
            text = response.choices[0].message.content
            if text is None:
                _log.warning("待机说话 LLM 返回空内容")
                return
            text = text.strip()
            # 清理可能的引号
            text = text.strip('"''「」『』')
            if text and len(text) <= 50 and text != self._last_idle_msg:
                self._last_idle_msg = text
                _log.info(f"主动说话: {text}")
                if self.on_idle_message:
                    self.on_idle_message(text)
                if self.on_tts:
                    self.on_tts(text)
        except Exception as e:
            _log.error(f"生成主动话语失败: {e}")


_idle_talker: Optional[IdleTalker] = None


def get_idle_talker() -> IdleTalker:
    global _idle_talker
    if _idle_talker is None:
        _idle_talker = IdleTalker()
    return _idle_talker
