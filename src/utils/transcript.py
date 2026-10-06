# -*- coding: utf-8 -*-
"""★★ 1.9.4：**全量对话记录（transcript）** —— 用户 2026-10-06 要求。

## 用户的原始要求
    用户报"聊天回复有 bug，汇报应该是调用工具后的，不是每次都强制，现在这样显得混乱"，
    但接着说："**又聊了几句，发现恢复正常了，暂时无法复现。强化log，最好全量记录。**"

    ⇒ 复现不了的问题，只能靠**事发现场的证据**。所以要把
      "模型收到了什么、说了什么、调了什么工具、参数什么、结果什么、
       界面上实际显示了什么" **全部落盘**。

## 为什么要**单独一个文件**
    主日志（`prts_*.log`）里混着 800+ 行启动/工具扫描噪音，真出问题时
    **翻不到对话那几行**。所以全量记录写到**独立文件** `logs/transcript_*.log`：
      · 只记"对话与决策"，一行一条，**带时间戳与轮次**；
      · 出问题时**整个文件发过来就行**，不用在噪音里捞。

## 记什么（对着上面那三个缺口）
    · `chat_bubble()` —— **界面实际渲染出的每个气泡**（这一条最关键：
      "汇报每次都出现"这种 bug，看气泡流水就一目了然）
    · `llm_reply()`   —— 模型这一轮的**原始输出**（正文 + `tool_calls`）
    · `tool_call()`   —— 工具名 + **完整参数值**
    · `tool_result()` —— 工具的**完整结果**
    · `progress()`    —— `progress_report` 的投递（排队/送出都记）

## 安全与体积
    · **密钥一定打码**：参数里凡是 `key/token/secret/password/cookie` 这类名字，
      值只留前 4 位；
    · 单条超长按 `_MAX_FIELD` 截断（**并注明截掉多少字**，不是悄悄丢）；
    · 按天一个文件，**超过 `_MAX_FILES` 个就删最老的**（别把用户磁盘写满）。
"""
from __future__ import annotations

import datetime as _dt
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LOG_DIR = ROOT / "logs"
_MAX_FIELD = 4000          # 单个字段最多记这么多字符
_MAX_FILES = 14            # 最多保留 14 个 transcript 文件

_lock = threading.Lock()
_turn = 0

# ★ 这些键的值必须打码（大小写不敏感，包含匹配）
_SECRET_HINTS = ("key", "token", "secret", "password", "passwd", "cookie",
                 "authorization", "refresh", "credential", "session_id")


def next_turn() -> int:
    """开新一轮（用户说了一句话 = 一轮）。返回轮次号，供其它记录引用。"""
    global _turn
    with _lock:
        _turn += 1
        return _turn


def _mask(name: str, val) -> str:
    """密钥打码：只留前 4 位。名字里带 key/token/cookie… 的一律打。"""
    s = str(val)
    low = str(name).lower()
    if any(h in low for h in _SECRET_HINTS):
        return (s[:4] + "…（已打码）") if len(s) > 4 else "…（已打码）"
    return s


def _short(s: str, limit: int = _MAX_FIELD) -> str:
    s = str(s)
    if len(s) <= limit:
        return s
    return s[:limit] + f"…（此处截掉 {len(s) - limit} 字）"


def _path() -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    return LOG_DIR / f"transcript_{_dt.date.today():%Y%m%d}.log"


def _write(tag: str, text: str):
    """落盘一行。**任何异常都吞掉** —— 记录失败绝不能影响桌宠运行。"""
    try:
        with _lock:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            _p = _path()
            # 体积控制：最多留 _MAX_FILES 个
            try:
                _olds = sorted(LOG_DIR.glob("transcript_*.log"))
                for _o in _olds[:-_MAX_FILES] if len(_olds) > _MAX_FILES else []:
                    _o.unlink(missing_ok=True)
            except Exception:
                pass
            _ts = _dt.datetime.now().strftime("%H:%M:%S")
            with open(_p, "a", encoding="utf-8") as f:
                f.write(f"[{_ts}] [T{_turn}] {tag} {text}\n")
    except Exception:
        pass


# ── 对外 API（都极轻，调用方不用 try）─────────────────────────────
# ★★ 1.9.4：这些函数**自己必须保命**。
#   我第一版只在 `_write` 内部吞异常，但这些 API 函数**直接调 `_write`**
#   —— 万一 `_write` 自己出问题（或将来被改动），异常会**传出去带崩桌宠**。
#   守卫 `_test_transcript_194` 的"落盘抛异常时调用方不被带崩"当场抓到。
#   ⇒ 统一套一层 `_safe`：**记录失败永远不能影响主流程**。
def _safe(fn):
    def _w(*a, **k):
        try:
            return fn(*a, **k)
        except Exception:
            return None
    _w.__name__ = getattr(fn, "__name__", "wrapped")
    _w.__doc__ = fn.__doc__
    return _w


@_safe
def chat_bubble(role: str, text: str, kind: str = ""):
    """★ 界面**实际渲染出**的一个气泡 —— 这是排查显示类 bug 的关键证据。"""
    _write(f"[气泡/{role}{('/' + kind) if kind else ''}]", _short(text))


@_safe
def llm_reply(content: str, tool_calls=None):
    """模型这一轮的原始输出。"""
    _tc = ""
    if tool_calls:
        try:
            _parts = [f"{getattr(c, 'function', None).name if getattr(c, 'function', None) else c}"
                      for c in tool_calls]
            _tc = "  调用=" + ",".join(str(p) for p in _parts)
        except Exception:
            _tc = f"  调用={tool_calls!r}"
    _write("[模型]", _short(content) + _tc)


@_safe
def tool_call(name: str, args: dict):
    """★ 工具名 + **完整参数值**（密钥打码）。"""
    try:
        _a = ", ".join(f"{k}={_short(_mask(k, v), 500)}" for k, v in (args or {}).items())
    except Exception:
        _a = repr(args)
    _write(f"[工具调用] {name}", _a)


@_safe
def tool_result(name: str, result, error: bool = False):
    """★ 工具的**完整结果**。"""
    _write(f"[工具结果{'/错误' if error else ''}] {name}", _short(result))


@_safe
def progress(text: str, delivered: bool):
    """`progress_report` 的投递（送出/排队都记）。"""
    _write(f"[临时汇报/{'送出' if delivered else '排队'}]", _short(text))


@_safe
def note(tag: str, text: str):
    """其它值得记的时刻（异常、强制重试等）。"""
    _write(f"[{tag}]", _short(text))


@_safe
def transcript_path() -> Path:
    """当前 transcript 文件路径（给界面/诊断显示）。"""
    return _path()
