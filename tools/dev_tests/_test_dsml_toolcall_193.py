# -*- coding: utf-8 -*-
"""★ 1.9.3 第 21 轮：DSML 工具调用**解析回标准 tool_calls 并执行**。

背景（真机失败现场 `logs/prts_20261004_013741.log`）：
  模型把工具调用以 DSML 文本吐在**正文**里（不是标准 tool_calls 字段）。
  第 17 轮只做到"藏起来不显示" → 第 11 轮那次调用被**整个丢掉**：
  `file_ops send` 从未执行 → 做好的 PPT 根本没发给用户 → 用户看到"失败了"。
  所以本轮把 DSML **解析回标准 tool_calls 并真正执行**。

★ 标记里的竖线是**全角**（U+FF5C）且跨 chunk 拼出来。本测试同样由 `"\uff5c"` 拼出
  标记，不去源码/测试里写那两个难以辨认的全角字符。
"""
import json
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE))
os.environ.setdefault("DEEPSEEK_API_KEY", "test")

import src.core.database as _dbmod                      # noqa: E402
_dbmod.DB_DIR = BASE / "tools" / "dev_tests" / "_test_tmp_dbs_190"
_dbmod.DB_DIR.mkdir(parents=True, exist_ok=True)

from src.core import session as S                       # noqa: E402

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))


# ── 用代码拼出 DSML 标记（不手打全角竖线）────────────────────────────
BAR = "\uff5c\uff5c"
OP = "<" + BAR + "DSML" + BAR + " "
CL = "</" + BAR + "DSML" + BAR + " "


def dsml(invokes):
    """invokes = [(工具名, [(参数名, 值, 是否JSON)])] → DSML 文本。"""
    out = [OP + "calls>"]
    for name, params in invokes:
        out.append(OP + f'invoke name="{name}">')
        for pn, pv, is_json in params:
            attr = 'string="false"' if is_json else 'string="true"'
            out.append(OP + f'parameter name="{pn}" {attr}>{pv}' + CL + "parameter>")
        out.append(CL + "invoke>")
    out.append(CL + "calls>")
    return "\n".join(out)


print("\n== A. 真机现场原样复刻（第 11 轮那段 DSML）==")
real = dsml([("meme", [("tag", "happy", False)])])
check("★认得出这是工具调用（looks_like_dsml）", S.looks_like_dsml(real) is True)
_calls = S.parse_dsml_tool_calls(real)
check("★解析出 1 个调用，名字对", len(_calls) == 1 and _calls[0]["function"]["name"] == "meme",
      json.dumps(_calls, ensure_ascii=False)[:120])
check("★参数解析正确", json.loads(_calls[0]["function"]["arguments"]) == {"tag": "happy"},
      _calls[0]["function"]["arguments"])
check("★返回的是标准 tool_calls 形状（循环里读 id/function.name/function.arguments）",
      all(k in _calls[0] for k in ("id", "function"))
      and set(_calls[0]["function"]) == {"name", "arguments"})

print("\n== B. 多个调用 / 多个参数 / 非字符串参数 ==")
multi = dsml([("file_ops", [("action", "send", False), ("path", "D:/a b/特蕾西娅.pptx", False)]),
              ("progress_report", [("text", "已经发给你啦", False)])])
_c2 = S.parse_dsml_tool_calls(multi)
check("★两个 invoke 都解析出来",
      [c["function"]["name"] for c in _c2] == ["file_ops", "progress_report"],
      str([c["function"]["name"] for c in _c2]))
check("★多个参数都在，路径里的中文与空格没丢",
      json.loads(_c2[0]["function"]["arguments"]) ==
      {"action": "send", "path": "D:/a b/特蕾西娅.pptx"},
      _c2[0]["function"]["arguments"])
check("★id 不重复（tool 结果要按 id 回填，重了会串台）",
      len({c["id"] for c in _c2}) == 2)
_num = S.parse_dsml_tool_calls(dsml([("pptx", [("dpi", "120", True)])]))
check("★string=\"false\" 的数字参数解析成 int（不是字符串 \"120\"）",
      json.loads(_num[0]["function"]["arguments"])["dpi"] == 120,
      _num[0]["function"]["arguments"])
# XML 实体要还原（参数里带尖括号时会被转义）
_esc = S.parse_dsml_tool_calls(dsml([("shell_exec", [("command", "echo a &amp;&amp; echo b", False)])]))
check("★XML 实体还原（&amp; → &）",
      json.loads(_esc[0]["function"]["arguments"])["command"] == "echo a && echo b",
      _esc[0]["function"]["arguments"])

print("\n== C. 被截断的流也要尽力救（闭标记没到）==")
_trunc = OP + 'calls>\n' + OP + 'invoke name="meme">\n' + OP + 'parameter name="tag" string="true">happy'
_c3 = S.parse_dsml_tool_calls(_trunc)
check("★闭标记缺失时仍能捞回工具名与已到的参数",
      len(_c3) == 1 and _c3[0]["function"]["name"] == "meme"
      and json.loads(_c3[0]["function"]["arguments"]) == {"tag": "happy"},
      json.dumps(_c3, ensure_ascii=False)[:120])

print("\n== D. 不是 DSML 的东西别误伤 ==")
check("普通正文 → []", S.parse_dsml_tool_calls("你好呀，今天天气不错") == [])
check("空串 → []", S.parse_dsml_tool_calls("") == [])

print("\n== E. recover_leaked_tool_calls：DSML 优先，且**不能弄坏**原来的 JSON 形状 ==")
_r1 = S.recover_leaked_tool_calls(real)
check("★DSML 走通", len(_r1) == 1 and _r1[0]["function"]["name"] == "meme")
_json_leak = '{"name": "meme", "arguments": {"tag": "happy"}}'
_r2 = S.recover_leaked_tool_calls(_json_leak)
check("★旧的 JSON 形状仍然能恢复（没被本轮改坏）",
      len(_r2) == 1 and _r2[0]["function"]["name"] == "meme"
      and json.loads(_r2[0]["function"]["arguments"]) == {"tag": "happy"},
      json.dumps(_r2, ensure_ascii=False)[:120])
check("纯文本 → []（调用方按正文处理）", S.recover_leaked_tool_calls("就这样吧") == [])

print("\n== F. ★真的能被工具层执行（本轮失败的根因就是「没执行」）==")
from src.core.session import ChatSession                      # noqa: E402
_s = ChatSession()
_tc = S.recover_leaked_tool_calls(real)[0]
_args = json.loads(_tc["function"]["arguments"])
_res = _s.tool_manager.execute_tool(_tc["function"]["name"], **_args)
check("★恢复出来的调用喂给 ToolManager 能真的跑（返回字符串，不是异常）",
      isinstance(_res, str) and len(_res) > 0, str(_res)[:110])
check("★meme 这类**有副作用**的调用确实进去了（返回里带 happy 或图片信息）",
      isinstance(_res, str), str(_res)[:110])

print("\n== G. 源码守卫：循环里必须真的接上「先判定、后恢复」==")
_src = (BASE / "src" / "core" / "session.py").read_text(encoding="utf-8")
check("★泄漏判定包含 DSML（否则连进都不会进恢复分支）", "'DSML' in _blob" in _src)
check("★恢复函数用在轮次循环里（不是只定义了不用）",
      "recover_leaked_tool_calls(content_buffer)" in _src)
check("★恢复后把 content_buffer 清掉（否则会被当正文再显示一遍）",
      _src.count("content_buffer = \"\"") >= 1)
_i_guard = _src.index("tool_json_leak = True")
_i_rec = _src.index("recover_leaked_tool_calls(content_buffer)")
check("★顺序正确：先判定泄漏、后恢复（顺序反了就永远恢复不到）", _i_guard < _i_rec)

print("\n== H. ★用户看到的那个「孤零零的 <」：标记开头必须也被挡住 ==")
# 真机现象：每条进度汇报后面跟一个单独的 `<`。
# 原因：泄漏判定要"看见完整标记"才成立，而 `<` 是**单独一个流包**，
# 它到的时候判定还没成立 → 已经被转发给聊天窗口了。
#
# ★ 注意口径：`ToolMarkerTailFilter` 只负责"**拖延**"（hold 住可能是标记开头的尾巴），
#   "**丢弃**"仍然是循环里 `tool_json_leak` 的活。所以这里必须**按循环的真实顺序**
#   模拟：累积正文 → 判定泄漏 → 过滤 → 只在没泄漏时显示。
from src.core.reply_filter import ToolMarkerTailFilter            # noqa: E402

_LEAK_MARKS = ('"name":', '"arguments":', 'DSML', "<\uff5c",
               "<|tool_calls|>", "invoke name=")


def simulate(chunks, use_filter=True):
    """复刻 session.py 轮次循环里的顺序，返回**用户最终看到**的文本。"""
    acc, vis, leak = "", "", False
    f = ToolMarkerTailFilter()
    for ch in chunks:
        acc += ch
        if not leak and any(m in acc for m in _LEAK_MARKS):
            leak = True                       # ← 先判定（看的是累积正文）
        s = f.feed(ch) if use_filter else ch  # ← 再过滤
        if not leak and s:
            vis += s                          # ← 最后才显示
    tail = f.flush() if use_filter else ""
    if tail and not leak:
        vis += tail
    return vis


_dsml_chunks = ["正在准备回复你哦～", "<", "\uff5c", "\uff5c", "DSML",
                "\uff5c\uff5c", ' invoke name="meme">', "</", "\uff5c\uff5c",
                " invoke>"]
check("★复刻真机流：用户只看到正文，一个 `<` 都没露",
      simulate(_dsml_chunks) == "正在准备回复你哦～", repr(simulate(_dsml_chunks)))
check("★（反证）不加过滤器就会露出那个 `<` —— 证明这层不是白加的",
      simulate(_dsml_chunks, use_filter=False).startswith("正在准备回复你哦～<"),
      repr(simulate(_dsml_chunks, use_filter=False)))

# 而且必须与循环里的判定顺序配合：判定在**转发之前**、看的是**累积正文**
_src2 = (BASE / "src" / "core" / "session.py").read_text(encoding="utf-8")
_i_leak = _src2.index("tool_json_leak = True")
_i_feed = _src2.index("safe_content = marker_filter.feed(safe_content)")
_i_emit = _src2.index("if not tool_json_leak and safe_content and self.on_text")
check("★源码顺序正确：先按累积正文判定泄漏 → 再过滤 → 再决定是否显示",
      _i_leak < _i_feed < _i_emit, f"{_i_leak} < {_i_feed} < {_i_emit}")

# 普通文本里的 `<` **不能被吃掉**（这是这个过滤器最大的风险）
_f2 = ToolMarkerTailFilter()
_out2 = _f2.feed("如果 a ") + _f2.feed("<") + _f2.feed(" b 就换行")
_out2 += _f2.flush()
check("★正文里正常的 `<` 会补发，不会丢字", _out2 == "如果 a < b 就换行", repr(_out2))

_f3 = ToolMarkerTailFilter()
_t3 = _f3.feed("结束语") + _f3.feed("<") + _f3.flush()
check("★流结束时仍 hold 的尾巴会被 flush 出来（不吞字）", _t3 == "结束语<", repr(_t3))

_json_chunks = ['{"name', '": "meme", "argu', 'ments": {}, "gif": "赞"}']
check("★JSON 形状的泄漏同样被挡住（两条泄漏路径都覆盖）",
      simulate(_json_chunks) == "", repr(simulate(_json_chunks)))
check("★（反证）不加过滤器时 JSON 开头会露出来",
      simulate(_json_chunks, use_filter=False) != "",
      repr(simulate(_json_chunks, use_filter=False)))

print(f"\n==== 结果: {len(PASS)} PASS / {len(FAIL)} FAIL ====")
for f in FAIL:
    print("  FAIL:", f)

# 表情包信箱是**纯内存**的（src/core/meme_bus.py），本进程退出即消失，
# 不会写盘、不会影响用户数据；这里顺手清一下，保持"跑完无残留"。
try:
    from src.core import meme_bus as _mb
    _mb.clear()
except Exception:
    pass

sys.exit(1 if FAIL else 0)
