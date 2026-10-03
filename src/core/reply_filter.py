"""
★ 1.9.0: 把 LLM 回复里**尾部那段协议 JSON**挡在聊天窗口之外。

【为什么需要这个模块（真机实测的教训）】
  桌宠和 LLM 之间有个约定：动作/表情包用一段 JSON 表达，例如
        {"gif": "赞啊!", "meme": "009.webp"}
  原本以为它会出现在 `reasoning_content`（思考通道）里，所以只在思考里解析。
  但实测（2026-09-16 的日志）deepseek-flash 会把它吐在**正文**里：

      [Session] DEBUG: LLM回复: 哼，测试就测试嘛……表情包功能在线。
      {"gif": "赞啊!", "meme": "009.webp"}

  于是用户直接在聊天里看到了这段 JSON，而表情包也没发出来（因为没解析正文）。

【为什么不能"等流结束再删"】
  正文是**逐块**推给聊天窗口的（`on_text` → 气泡实时追加）。
  等流结束再删，那段 JSON 早就显示出来了。所以必须**边流边挡**：
  遇到 `{` 就 hold 住不转发，等它闭合、看清里面有没有协议键，再决定
  是丢掉（协议）还是补发（普通文本）。

【保守原则】
  · 只挡**含协议键**的 JSON 块（gif / meme / memo_file / sticker / 表情包），
    合法的普通文本（哪怕带花括号）最终一定会补发，不会丢字。
  · hold 超过 `MAX_HOLD` 字符就放弃等待、原样放行（防止正文里一个孤立的
    `{` 把后面的话全卡住）。
  · 没闭合的尾巴在流结束时统一 `flush()` 处理。
"""
from __future__ import annotations

import json
from typing import Optional

# 协议 JSON 的识别键（出现任意一个就当成协议，不是普通正文）
PROTOCOL_KEYS = ("gif", "meme", "meme_file", "sticker", "表情包")

# hold 住这么多字符还没闭合就放弃（远大于正常协议 JSON 的长度）
MAX_HOLD = 800


def try_json_block(text: str) -> Optional[dict]:
    """尽量把一段文本解析成 dict（宽松版：先整体，再退化成最后一个花括号块）"""
    if not text:
        return None
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except Exception:
        pass
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        try:
            data = json.loads(text[start:end + 1])
            return data if isinstance(data, dict) else None
        except Exception:
            return None
    return None


def is_protocol(data) -> bool:
    """这段 JSON 是不是"桌宠协议"（含 gif/meme 之类键）"""
    return isinstance(data, dict) and any(k in data for k in PROTOCOL_KEYS)


def split_protocol(text: str) -> tuple[str, Optional[dict]]:
    """把正文尾部的协议 JSON 剥掉。

    Returns:
        (干净正文, 协议 dict 或 None)
    用于**落库/写进历史**的那份文本：绝不能把协议 JSON 存进对话历史
    （既难看又白烧 token，还会让模型下一轮跟着学）。
    """
    if not text:
        return text, None
    payload = None
    out = []
    rest = text
    while True:
        i = rest.find("{")
        if i < 0:
            out.append(rest)
            break
        out.append(rest[:i])
        tail = rest[i:]
        j = tail.find("}")
        if j < 0:
            out.append(tail)          # 没闭合 → 当普通文本
            break
        block = tail[:j + 1]
        data = try_json_block(block)
        if is_protocol(data):
            payload = data             # 协议块 → 丢掉，只留 payload
        else:
            out.append(block)
        rest = tail[j + 1:]
    return "".join(out).strip(), payload


class ProtocolTailFilter:
    """流式过滤器：`feed(chunk)` 返回"可以显示给用户"的那部分。

    用法（在流式循环里）：
        f = ProtocolTailFilter()
        for chunk in stream:
            safe = f.feed(chunk)
            if safe:
                show(safe)
        tail = f.flush()          # 流结束：处理没闭合的尾巴
        if tail:
            show(tail)
        if f.payload:             # 解析到的协议
            handle(f.payload)
    """

    def __init__(self) -> None:
        self._held = ""            # 从某个 '{' 开始、还没定性的文本
        self.payload: Optional[dict] = None

    def feed(self, chunk: str) -> str:
        if not chunk:
            return ""
        text = self._held + chunk
        out = []
        while True:
            i = text.find("{")
            if i < 0:
                out.append(text)
                text = ""
                break
            out.append(text[:i])
            rest = text[i:]
            j = rest.find("}")
            if j < 0:
                if len(rest) > MAX_HOLD:     # 等太久了，放弃：原样放行
                    out.append(rest)
                    text = ""
                else:
                    text = rest              # 继续 hold
                break
            block = rest[:j + 1]
            if is_protocol(try_json_block(block)):
                self.payload = try_json_block(block)
                text = rest[j + 1:]
                continue
            out.append(block)
            text = rest[j + 1:]
        self._held = text
        return "".join(out)

    def flush(self) -> str:
        """流结束：held 若本身就是协议 → 吞掉；否则原样还给调用方去显示"""
        held, self._held = self._held, ""
        if not held:
            return ""
        data = try_json_block(held)
        if is_protocol(data):
            self.payload = data
            return ""
        return held


# ══════════════════════════════════════════════════════════════
# ★★ 1.9.3 第 22 轮：把"工具调用泄漏标记"的**开头**也挡在聊天窗口之外
# ══════════════════════════════════════════════════════════════
# 【真机现象（用户截图）】每条进度汇报后面都跟着一个**孤零零的 `<`**：
#     正在准备回复你哦～ / < / 文件刚写完，next 我接着往下跑～ / < / …
#
# 【原因】泄漏判定是"**看见完整标记**才成立"（`'DSML' in 累积正文`、
#   `'invoke name=' in 累积正文`…），而标记最前面那个 `<` 是**单独一个流包**——
#   它到的时候判定还没成立，于是被原样转发给了聊天窗口；
#   等后面的 `｜｜DSML｜｜` 到齐、判定成立，**剩下的**才被藏起来。
#   结果就是每条被丢弃的调用都留下一个 `<` 尾巴。
#
# 【做法】与其事后藏，不如**提前 hold**：只要正文的尾巴还可能是某个标记的**前缀**，
#   就先不显示，等下一个包来定性 ——
#     · 不是标记 → 立刻补发（**一个字都不会丢**）；
#     · 真是标记 → 泄漏判定随后成立，这段永远不显示。
#   代价只是"极短的延迟"（最多几十毫秒，取决于下一个包什么时候来）。
_DSML_BAR = "\uff5c\uff5c"


class ToolMarkerTailFilter:
    """流式过滤器：hold 住"可能是工具调用标记开头"的尾巴。

    用法与 `ProtocolTailFilter` 一致：`feed(chunk)` 返回可显示的部分，
    流结束时 `flush()` 把最后 hold 的那点补出来（如果它不是标记）。
    """

    # 标记的开头（真实形状见 session.py 的 DSML 解析）。只要正文尾部是其中某一条的
    # **前缀**（含整条，见下面 feed 的说明），就 hold 住 ——
    # 所以 `<`、`<｜`、`invoke nam`、`{"name"` 这些半截都会被挡住。
    HEADS = (
        "<|tool_calls|>",
        "<" + _DSML_BAR + "DSML",
        "<" + _DSML_BAR,
        "invoke name=",
        '{"name":',
        '"name":',
        '"arguments":',
    )

    def __init__(self) -> None:
        self._held = ""

    def feed(self, chunk: str) -> str:
        if not chunk:
            return ""
        text = self._held + chunk
        hold = 0
        for h in self.HEADS:
            # ★ 含**整条**匹配（L <= len(h)）：不然 `{"name"` 到齐的那一刻会被放出去，
            #   而泄漏判定要等下一个包看到 `:` 才成立 → 那个 `{` 就漏到聊天窗口了。
            #   多 hold 一两个字符的代价只是"再等一个流包"，flush() 保证不吞字。
            for L in range(1, len(h) + 1):
                if text.endswith(h[:L]) and L > hold:
                    hold = L
        if hold:
            self._held = text[-hold:]
            return text[:-hold]
        self._held = ""
        return text

    def flush(self) -> str:
        held, self._held = self._held, ""
        return held
