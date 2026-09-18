# -*- coding: utf-8 -*-
"""
★ 1.9.2 任务 3 回归：表情包返回值增加「表情包描述」
==============================================================================
用户原话：
  ###3，增加：表情包优化。传回数据时，增加参数"表情包描述"

为什么需要：模型挑图时只看得见文件名（009.webp），不知道这张图画的是什么。
把描述回传给它，它才能①核对这张图和本轮情绪对不对得上、②需要时把图意
写进文字回复。同时信箱里也带上描述，日志能查出"这次发的图是什么"。

测什么：
  A. meme_library.describe() 取值优先级 caption → keywords → pack_name → 空串
  B. meme 工具返回值里同时有 "表情包描述" 和 "description"
  C. 描述进信箱（meme_bus），主窗口/日志看得到
  D. 描述不影响原有字段与发送流程（成功/失败路径都不炸）
  E. 工具文档（docstring + TOOL_DESCRIPTION）说明了这个字段
==============================================================================
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

PASS = FAIL = 0
FAILS = []


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        FAILS.append(name)
        print(f"  FAIL  {name}  {extra}")


def head(t):
    print(f"\n== {t} ==")


from src.core import meme_library as ml          # noqa: E402
from src.core import meme_bus as bus             # noqa: E402
import tools.meme_tool as MT                     # noqa: E402

# ══════════════════════════════════════════════════════════════
head("A. describe() 的取值优先级与容错")
check("★describe 已导出", hasattr(ml, "describe"))

check("★caption 优先（有 caption 就不看 keywords）",
      ml.describe({"caption": "比心", "keywords": "kw", "pack_name": "p"}) == "比心")
check("★没 caption 时退到 keywords",
      ml.describe({"caption": "", "keywords": "生气 瞪眼", "pack_name": "p"})
      == "生气 瞪眼")
check("★都没有时退到包名",
      ml.describe({"caption": "", "keywords": "", "pack_name": "dafeiyu-001"})
      == "dafeiyu-001")
check("★啥都没有时是空串（不报错）",
      ml.describe({"caption": "", "keywords": "", "pack_name": ""}) == "")
check("★空白字符串也算没有（strip 后判空）",
      ml.describe({"caption": "   ", "keywords": "  ", "pack_name": "  "}) == "")
check("★传 None 不炸", ml.describe(None) == "")
check("★传空串不炸", ml.describe("") == "")
check("★传不存在的文件不炸", ml.describe("绝不存在的表情包xyz.webp") == "")

# 用真实素材验证（库里有东西才有意义）
_real = ml.all_memes()
if _real:
    _with_cap = [m for m in _real if (m.get("caption") or "").strip()]
    check("★真实库里存在带 caption 的素材（不然这功能是空的）",
          len(_with_cap) > 0, f"{len(_with_cap)}/{len(_real)}")
    if _with_cap:
        _m = _with_cap[0]
        d = ml.describe(_m["file"])
        check(f"★按文件名查得到描述（{_m['file']} → {d!r}）",
              d == _m["caption"].strip(), f"期望 {_m['caption']!r} 实得 {d!r}")
        check("★describe 与 all_memes 的 caption 一致",
              ml.describe(_m["path"]) == _m["caption"].strip())
    print(f"        库内素材 {len(_real)} 张，带 caption {len(_with_cap)} 张")
else:
    check("★库非空（否则无法验证真实描述）", False, "all_memes() 为空")

# ══════════════════════════════════════════════════════════════
head("B. meme 工具返回值里有「表情包描述」")
bus.reset_turn()
r = json.loads(MT.execute(tag="happy", reason="单测"))
check("★调用成功", r.get("success") is True, str(r)[:120])
check("★返回值里有中文键「表情包描述」", "表情包描述" in r, str(list(r.keys())))
check("★返回值里有英文别名 description", "description" in r)
check("★两个键的值相同", r.get("表情包描述") == r.get("description"))
check("★原有字段没被破坏（meme/path/tag/text 都在）",
      all(k in r for k in ("meme", "path", "tag", "text", "action")))
check("★描述是字符串", isinstance(r.get("表情包描述"), str))

_picked = bus.peek()
check("★描述也进了信箱（主窗口/日志看得到）", "desc" in _picked,
      str(list(_picked.keys())))
check("★信箱里的描述与返回值一致",
      _picked.get("desc") == r.get("表情包描述"),
      f"{_picked.get('desc')!r} vs {r.get('表情包描述')!r}")
check("★text 里也带上了描述（模型直接读 text 也能看到）",
      (not r.get("表情包描述")) or (r["表情包描述"] in r["text"]), r.get("text", "")[:160])

# ══════════════════════════════════════════════════════════════
head("C. 信箱 pick() 的 desc 参数")
bus.reset_turn()
bus.pick("dummy.webp", name="dummy.webp", tag="t", reason="r", desc="一只猫在笑")
check("★pick 收下了 desc", bus.peek().get("desc") == "一只猫在笑")
bus.reset_turn()
bus.pick("dummy.webp", name="dummy.webp")
check("★不传 desc 时是空串（向后兼容老调用）", bus.peek().get("desc") == "")

# ══════════════════════════════════════════════════════════════
head("D. 失败路径不受影响（描述逻辑不干扰原有校验）")
bus.reset_turn()
r2 = json.loads(MT.execute())
check("★空 tag 仍被拒绝", r2.get("success") is False and "tag" in r2.get("error", ""),
      str(r2)[:120])
check("★被拒的返回里没有假描述", "表情包描述" not in r2)
bus.reset_turn()
r3 = json.loads(MT.execute(tag="绝不存在的标签xyz"))
check("★不存在的标签仍被拒绝", r3.get("success") is False, str(r3)[:120])
bus.reset_turn()
r4 = json.loads(MT.execute(name="绝不存在的表情包xyz.webp"))
check("★不存在的文件名仍被拒绝", r4.get("success") is False, str(r4)[:120])
bus.reset_turn()
r5 = json.loads(MT.execute(tag="happy"))
check("★重置后第一次调用成功（描述已带上）",
      r5.get("success") is True and "表情包描述" in r5, str(r5)[:120])
r6 = json.loads(MT.execute(tag="happy"))
check("★★同一轮第二次调用被拒（一轮只生效一次）",
      r6.get("success") is False and "一轮只生效一次" in r6.get("error", ""),
      str(r6)[:140])
check("★被拒的第二次也没有假描述", "表情包描述" not in r6)
bus.reset_turn()

# ══════════════════════════════════════════════════════════════
head("E. 文档里说明了这个字段（模型与接手的人都要知道）")
_doc = MT.__doc__ or ""
check("★模块 docstring 写了【返回】含表情包描述", "表情包描述" in _doc)
check("★模块 docstring 说明了为什么需要它", "不知道这张图画的是什么" in _doc
      or "根本不知道这张图画的是什么" in _doc)
check("★TOOL_DESCRIPTION 告诉了模型有这个字段",
      "表情包描述" in MT.TOOL_DESCRIPTION)
_mlsrc = (ROOT / "src" / "core" / "meme_library.py").read_text(encoding="utf-8")
check("★meme_library 的 describe 有 1.9.2 注释说明用户原话",
      "1.9.2" in _mlsrc and "表情包描述" in _mlsrc)
_bussrc = (ROOT / "src" / "core" / "meme_bus.py").read_text(encoding="utf-8")
check("★meme_bus 的 pick 有 desc 参数注释", "desc" in _bussrc and "1.9.2" in _bussrc)

# ══════════════════════════════════════════════════════════════
head("F. 参数 schema 没被这次改动碰坏")
check("★TOOL_PARAMETERS 仍是 tag/name/reason 三个属性",
      set(MT.TOOL_PARAMETERS["properties"].keys()) == {"tag", "name", "reason"},
      str(list(MT.TOOL_PARAMETERS["properties"].keys())))
check("★tag 仍是必填", MT.TOOL_PARAMETERS["required"] == ["tag"])
check("★TOOL_NAME 没变", MT.TOOL_NAME == "meme")

bus.reset_turn()
print("\n" + "=" * 60)
print(f"★ 1.9.2 任务3 表情包描述：{PASS} PASS / {FAIL} FAIL")
for f in FAILS:
    print(f"  FAIL: {f}")
print("=" * 60)
sys.exit(1 if FAIL else 0)
