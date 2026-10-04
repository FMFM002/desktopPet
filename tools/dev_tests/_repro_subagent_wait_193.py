# -*- coding: utf-8 -*-
"""★ 1.9.3 第 61 轮：一次性真机复现脚本（**不是回归套件**，跑完即弃）。

目的：回答"子代理交付了，主 LLM 为什么还说『已经派了/稍等一下』"。
做法：把真机日志里那一轮**真实的 messages 原样拼出来**，只打 **2 次**真 API，
      每次 max_tokens=300、思考关闭（够省），**不注册任何工具**
      （所以模型绝不可能真的再派一次子代理出去跑任务）：

  · 变体 A（交付物在上下文里，且是最后一条)=真机第 61 轮实际发生的那次续轮；
  · 变体 B（同一条时间线，但**去掉**交付物那一对 tool 结果）=真机第 3 轮的状态。

若 A 直接汇报、B 说"等它回话"，就证明：**模型没问题、续轮没问题**，
"还在等"那句话来自交付之前就已经在飞的那一轮。

★ 它**故意不叫 `_test_*.py`**（`_sweep190.ps1` 不会自动跑到它），而且必须显式
  打开开关才会真的打 API：
      $env:PRTS_ALLOW_LIVE_API=1; python tools\\dev_tests\\_repro_subagent_wait_193.py
  共计 **2 次**请求，每次 max_tokens=300、思考关闭、**不注册任何工具**
  （模型不可能真的派出子代理去跑长任务）。
"""
import os
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

if os.environ.get("PRTS_ALLOW_LIVE_API") != "1":
    print("★ 这是**真机 API** 复现脚本（2 次请求），默认不跑。\n"
          "  要跑就设环境变量：PRTS_ALLOW_LIVE_API=1")
    sys.exit(0)

from src.core.llm_client import ChatClient  # noqa: E402

_STALE = ("好哒！已经把查天气的活派给铃兰了～ (๑>ᴗ<๑)\n\n"
          "它正在后台查重庆明天的天气，查完会自动把结果送回来，不用干等着。\n\n"
          "稍等一下下就好～")
_DELIV = ("铃兰查好啦，明天（10-05）重庆的天气是：\n**小雨转多云**，"
          "气温 **18~21°C**，西南风 1-3 级，湿度约 91%。明早出门记得带把伞～")

_BASE_MSGS = [
    {"role": "user", "content": "叫铃兰帮我查一下明天天气"},
    {"role": "assistant", "content": None, "tool_calls": [{
        "id": "call_1", "type": "function",
        "function": {"name": "subagent",
                     "arguments": '{"action":"start","agent":"铃兰"}'}}]},
    {"role": "tool", "tool_call_id": "call_1",
     "content": ('{"success": true, "action": "subagent", "job": "sa-1", '
                 '"agent": "铃兰", "state": "running", "granted": ["weather_query"], '
                 '"text": "已经把活交给「铃兰」（sa-1），它自己在后台做，'
                 '不会占着你。做完它的交付物会自动回来。"}')},
    {"role": "assistant", "content": _STALE},
]
_DELIV_PAIR = [
    {"role": "assistant", "content": None, "tool_calls": [{
        "id": "subagent_sa-1", "type": "function",
        "function": {"name": "subagent",
                     "arguments": '{"action":"result","job":"sa-1","agent":"铃兰"}'}}]},
    {"role": "tool", "tool_call_id": "subagent_sa-1",
     "content": (f"【子代理「铃兰」的交付物（sa-1）】\n{_DELIV}\n"
                 "（这就是它的交付物，已经送到你手上了：请据此直接回复/汇报，"
                 "不要再等它。）")},
]

_c = ChatClient()
_sys_prompt = _c._build_system_prompt()
print(f"model={_c.model}  system_prompt={len(_sys_prompt)} 字\n")

for _label, _msgs in (("A 交付物在上下文（真机续轮那次）", _BASE_MSGS + _DELIV_PAIR),
                      ("B 交付物还没回来（真机第 3 轮那次）", list(_BASE_MSGS))):
    _resp = _c.client.chat.completions.create(
        model=_c.model,
        messages=[{"role": "system", "content": _sys_prompt}] + _msgs,
        temperature=0.3, max_tokens=300,
        extra_body={"thinking": {"type": "disabled"}})
    _txt = (_resp.choices[0].message.content or "").strip()
    print(f"── {_label} ──  messages={len(_msgs)}")
    print(_txt)
    print(f"  [含『等』={'等' in _txt}]  [含天气结论={'小雨转多云' in _txt}]  "
          f"[含 job handle={'sa-1' in _txt}]\n")
