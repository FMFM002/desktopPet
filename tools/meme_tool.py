#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
表情包工具（meme）—— ★ 1.9.0（你的设计）

【为什么改成工具】
  原来是"让 LLM 在思考末尾顺手吐一段 {"meme": "xxx.webp"}"。实测很不可靠：
  第一次发了，之后你说「换一个」「没看见」「发一个表情包」，模型**压根不再输出
  那段协议** —— 于是什么都发不出来（聊天记录里那几轮确实一条表情包都没有）。
  工具调用是结构化接口，比"散在思考文本里的约定"可靠得多，所以按你的方案：
     · LLM 在**即将给出回复之前**调用一次 `meme`（一轮只允许一次）
     · 工具把选中的表情包放进"**待发送**"状态
     · 主窗口在**文字回复结束之后**再发出去（两条分开、表情包最后）

【参数】★ 1.9.1 修正
  tag    **必填**。你自己根据"这次回复想表达的情绪"挑一个标签。
         真机踩坑：以前是可选的，模型就真的调 `meme({})` 空参进来，
         工具只能按角色偏好/随机兜底 → 发出来的表情包和回复内容毫无关系
         （你原话："说明llm发送的表情包和llm思考的内容其实没有联系"）。
         现在 schema 里 `required: ["tag"]`，并在运行期也拒绝空 tag。
  name   可选。直接指定文件名（如 009.webp）；给了就以它为准（tag 仅记日志）。
  reason 可选。一句话说明为什么发这张（只写进日志，不发给用户）。

【返回】
  {"success": true, "meme": "009.webp", "path": "...", "tag": "happy",
   "表情包描述": "比心，甜甜的笑", "description": "（同上，英文别名）",
   "text": "已把表情包 009.webp 放进待发送队列（回复之后单独发一条）"}
  ★ 1.9.2（用户要求，任务 3）：返回值里多了 **表情包描述** ——
    挑图时模型只看得见文件名（009.webp），根本不知道这张图画的是什么；
    把描述回传给它，它才能①核对这张图跟这轮情绪对不对得上、
    ②需要时把图意写进文字回复里。描述取值优先级：
    caption（人工补的）→ keywords（索引关键词）→ 包名。
    没有 caption/keywords 的老素材会退化成包名；都取不到时是空串（不会报错）。
  失败：{"success": false, "error": "..."}
    · tag 空着调用 → 拒绝并列出可选标签（让它重挑，不随机兜底）
    · tag 库里不存在 → 先模糊匹配，仍不行则拒绝并列出可选标签

【一轮只允许一次】
  由会话层与主窗口共同保证：`set_pending()` 记录本轮是否已选过；
  重复调用直接返回提示，不会真的换（避免连发/来回改）。
"""
from __future__ import annotations

import json
import logging

_log = logging.getLogger("Tool.Meme")

TOOL_NAME = "meme"
TOOL_DESCRIPTION = (
    "发表情包。想在回复里表达情绪时调用**一次**：它会挑一张表情包并进入待发送状态，"
    "你的文字回复结束后会自动单独发出去（表情包最后发）。"
    "一轮里只会生效一次，别重复调用。\n"
    "★ tag **必填**：你必须根据**自己这次回复的情绪**亲自选一个标签，"
    "别留空、别随便填 —— 表情包是按 tag 挑的，tag 不对就会发出和回复无关的图。"
    "常用标签：happy（开心/得意/被夸）、daily（日常/闲聊/无所谓）、"
    "angry（生气/被惹到）、confused（疑惑/听不懂）、sad（难过/委屈）、"
    "shy（害羞/被撩到）；另有 like/see/cpu/color/sleep/work/meow/sigh/"
    "surprised/fool/baka/givemoney/morning 等少量专用标签。\n"
    "name 可选：直接指定文件名（如 009.webp），一般不用。\n"
    "reason 可选：一句话说明为什么发这张（只记日志）。\n"
    "★ 返回里带「表情包描述」——挑完之后你能看到这张图到底画的是什么，"
    "可以据此在文字回复里呼应一下，或确认自己没挑错。"
)
TOOL_POLICY = "lazy"

# ★ 1.9.0: 状态**不放在本模块**（真机踩坑）——
#   ToolManager 对 LAZY 工具是"空闲卸载、下次重新 import"，模块级变量活不过一次卸载，
#   主窗口 import 到的是另一个模块对象，take_pending() 永远拿到空 → 表情包发不出来。
#   真正的信箱在 src/core/meme_bus.py（不属于任何工具，永不被卸载）。


def available_tags() -> list:
    """库里真实存在的标签（按张数从多到少）—— 给"tag 填错了"的提示用"""
    try:
        from src.core import meme_library as ml
        return ml.available_tags()
    except Exception:
        return []


def _parameters() -> dict:
    return {
        "type": "object",
        "properties": {
            "tag": {
                "type": "string",
                "description": (
                    "★必填。你自己根据这次回复想表达的情绪挑一个标签，别留空。"
                    "常用：happy/daily/angry/confused/sad/shy；"
                    "另有 like/see/cpu/color/sleep/work/meow/sigh/surprised/"
                    "fool/baka/givemoney/morning。"
                ),
            },
            "name": {"type": "string",
                     "description": "直接指定文件名（如 009.webp），一般不用"},
            "reason": {"type": "string",
                       "description": "为什么发这张（只记日志，不发给你）"},
        },
        # ★ 1.9.1：改成必填 —— 真机上模型会调 meme({}) 空参，
        #   兜底随机挑出来的图和回复内容没有关系（你报的那个问题）。
        "required": ["tag"],
    }


PARAMETERS = _parameters()
# ★ 1.9.1 关键修复：ToolManager 扫描时读的是 **TOOL_PARAMETERS**
#   （src/core/tool_manager.py:371 `getattr(module, "TOOL_PARAMETERS", {...空...})`）。
#   本文件原来只定义了 `PARAMETERS` → 扫描器拿到的是默认的
#   `{"type":"object","properties":{},"required":[]}`，**模型根本看不到 tag/name/reason
#   这三个参数**，所以真机上它只能调 `meme({})`（日志 prts_20260918_002248.log 00:27:54）。
#   全项目 15 个工具里只有本文件用了错名字，现在对齐。
TOOL_PARAMETERS = PARAMETERS


def get_parameters() -> dict:
    return _parameters()


# ── 与主窗口的接口（全部转发到 src/core/meme_bus，见上面的说明）──
def take_pending() -> dict:
    """取走"待发送"的表情包（主窗口在回复结束后调用）；没有则返回 {}"""
    from src.core.meme_bus import take
    return take()



def reset_turn():
    from src.core.meme_bus import reset_turn as _r
    _r()


def _prefer_tags() -> list:
    """角色「行为控制 → 优先情绪」（读不到就给个通用值）"""
    try:
        from src.utils.config import get_config
        tags = getattr(get_config().pet, "meme_tags_prefer", None)
        if tags:
            return [str(t) for t in tags]
    except Exception:
        pass
    return ["happy"]


def _tendency() -> str:
    try:
        from src.utils.config import get_config
        return str(getattr(get_config().pet, "meme_tendency", "normal") or "normal")
    except Exception:
        return "normal"


def execute(tag: str = "", name: str = "", reason: str = "", **kwargs) -> str:
    """工具入口（同步）"""
    from src.core import meme_bus as _bus

    if _tendency() == "off":
        return json.dumps({"success": False,
                           "error": "当前角色的「行为控制 → 表情包倾向」是「不发」，"
                                    "所以这次不发表情包。要发请让用户去设置里改。"},
                          ensure_ascii=False)

    if _bus.was_picked_this_turn():
        _have = (_bus.peek().get("name") or "（无）")
        return json.dumps({"success": False,
                           "error": f"本轮已经选过表情包了（{_have}），一轮只生效一次；"
                                    f"就发这一张，别再调用。"},
                          ensure_ascii=False)

    try:
        from src.core import meme_library as ml
    except Exception as e:
        return json.dumps({"success": False, "error": f"表情包库不可用: {e}"},
                          ensure_ascii=False)

    path = None
    picked_tag = (tag or "").strip()
    if (name or "").strip():
        path = ml.resolve(name.strip())
        if path is None:
            return json.dumps({"success": False,
                               "error": f"找不到表情包「{name}」。请用目录里列出的文件名，"
                                        f"或改用 tag 让我自己挑。"},
                              ensure_ascii=False)
    else:
        # ★ 1.9.1：tag 必填 —— 拒绝空参。
        #   真机日志 logs/prts_20260918_002248.log 00:27:54 就是 `meme({})`，
        #   旧代码兜底随机挑 → 图和回复内容毫无关系（你报的问题）。
        real = available_tags()
        pref = _prefer_tags()
        hint = [t for t in pref if t in real] + [t for t in real if t not in pref]
        hint_s = "/".join(hint[:12]) if hint else "happy/daily/angry/confused/sad/shy"
        if not picked_tag:
            _log.warning("[表情包工具] 被空参调用 meme({}) → 拒绝，要求模型自己选 tag")
            return json.dumps({
                "success": False,
                "error": (f"tag 是必填的，不能空着调用。请根据你这次回复想表达的"
                          f"情绪选一个标签后再调用一次。可选：{hint_s}。"),
            }, ensure_ascii=False)

        # ★ 必须用严格版：老的 random_meme() 在标签没命中时会静默退化成全库随机，
        #   那正是"表情包和回复内容没关系"的根源。
        m = ml.random_meme_strict(picked_tag)
        if m and m.get("path"):
            path, picked_tag = m["path"], (m.get("tag") or picked_tag)
        else:
            # 标签写错（库里没有）→ 先在真实标签里模糊匹配，仍不行就报错让它重挑，
            # 而不是"退化成全库随机"（那样又变成图和回复没关系）。
            low = picked_tag.lower()
            near = next((t for t in real if t in low or low in t), "")
            if near:
                m = ml.random_meme_strict(near)
                if m and m.get("path"):
                    path, picked_tag = m["path"], (m.get("tag") or near)
                    _log.info(f"[表情包工具] 标签「{picked_tag}」库里没有，"
                              f"模糊匹配到「{near}」")
            if path is None:
                return json.dumps({
                    "success": False,
                    "error": (f"没有「{picked_tag}」这个标签的表情包，别自己造标签。"
                              f"请换成这些里的一个再调用：{hint_s}。"),
                }, ensure_ascii=False)

    if path is None:
        return json.dumps({"success": False,
                           "error": "表情包库是空的（data/memes）。"
                                    "请先跑 tools/fetch_memes.py 抓素材。"},
                          ensure_ascii=False)

    # ★ 1.9.2（用户要求，任务 3）：先把「表情包描述」取出来，再进信箱、再回传。
    #   模型挑图时只看得见文件名（009.webp），拿到描述才能核对
    #   "这张图跟我这轮想表达的情绪对不对得上"。
    _meme_desc = ""
    try:
        _meme_desc = ml.describe(path.name)
    except Exception as e:
        _log.debug(f"[表情包工具] 取描述失败（不影响发送）: {e}")

    _bus.pick(path, name=path.name, tag=picked_tag or "",
              reason=(reason or "").strip(), desc=_meme_desc)
    _log.info(f"[表情包工具] 已选: {path.name} (tag={picked_tag or '-'}"
              f"{', 理由=' + reason if reason else ''})"
              f"{' 描述=' + _meme_desc if _meme_desc else ''} → 待回复结束后发送")
    _txt = (f"已把表情包 {path.name} 放进待发送队列（tag={picked_tag or '-'}）。"
            f"你正常输出文字回复即可，回复结束后它会单独发一条。")
    if _meme_desc:
        _txt += f" 这张图的描述：{_meme_desc}。"
    return json.dumps({
        "success": True, "action": "meme", "meme": path.name,
        "path": str(path), "tag": picked_tag or "",
        # ★ 用户点名要的字段：中文名 "表情包描述" 原样给，同时给英文别名 description
        "表情包描述": _meme_desc, "description": _meme_desc,
        "text": _txt,
    }, ensure_ascii=False)


if __name__ == "__main__":       # 手动排查用
    import sys
    logging.basicConfig(level=logging.INFO)
    args = dict(a.split("=", 1) for a in sys.argv[1:] if "=" in a)
    print(execute(**args))
