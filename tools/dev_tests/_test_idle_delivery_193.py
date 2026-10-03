# -*- coding: utf-8 -*-
"""★ 1.9.3 第 25 轮：真机"启动了 ComfyUI 又被自己关掉 / 一直搜集素材不交付"的修复。

真机现场 `logs/prts_20261004_025634.log`（纳西妲那次，02:56~03:10）：
  03:03:39  模型调 `comfyui start`
  03:04:09  返回"已就绪（27 秒）"
  03:04:12  `工具 [comfyui] 卸载（空闲超时）` → **卸载钩子把刚拉起来的 ComfyUI 关了**
  之后模型以为它还开着，转去下载素材；**一张图都没画成**，最后在第 20 轮撞上预算，
  PPT 只写了两段脚本、什么都没交付。

本套件钉住四件事：
  A. `ToolInfo.execute()` **调用结束也要重新盖 `_last_used`**（长调用不能一返回就算"空闲"）；
  B. `comfyui_jobs.on_tool_unloaded()` **刚被显式 start、还没画过图时不关**；
  C. `file_ops` 漏给 `path` 时要报"缺少 path"，而不是含糊的"只读模式"；
  D. 提示词里要有"先出东西、别一直搜集素材"，且轮次预算够（MAX_ROUNDS=30）。
"""
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE))
os.environ.setdefault("DEEPSEEK_API_KEY", "test")
# ★ 回归里绝不允许真的把 ComfyUI 拉起来（第 3 轮踩过：跑测试把用户的 ComfyUI 启动了）
os.environ["PRTS_NO_COMFY_START"] = "1"

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))


print("\n== A. ★长调用结束后要重新盖空闲时间戳（旧代码只盖在调用前）==")
import src.core.database as _dbmod                              # noqa: E402
_dbmod.DB_DIR = BASE / "tools" / "dev_tests" / "_test_tmp_dbs_190"
_dbmod.DB_DIR.mkdir(parents=True, exist_ok=True)

from src.core.tool_manager import ToolInfo, IDLE_UNLOAD_TIMEOUT   # noqa: E402

_probe = ToolInfo("probe", "", {}, "probe.py", manager=None)


def _slow(**kw):
    time.sleep(2.0)
    return "done"


_probe._func = _slow
_t0 = time.time()
_out = _probe.execute()
_elapsed = time.time() - _t0
_idle = _probe.idle_seconds()
check("探针调用确实跑了约 2 秒", _elapsed >= 1.9 and _out == "done", f"{_elapsed:.2f}s")
# 旧代码：idle_seconds() ≈ 2.0（>= 1.0 就红）；新代码：≈ 0.0
check("★返回后 idle_seconds()≈0（旧的实现这里是 2.0 —— 长调用一返回就被判空闲）",
      _idle <= 1.0, f"idle={_idle:.2f}s")
check("★空闲阈值仍是 30 秒（没把规则改掉）", IDLE_UNLOAD_TIMEOUT == 30.0,
      str(IDLE_UNLOAD_TIMEOUT))
_probe._func = None


print("\n== B. ★刚 start 起来、还没画过图的 ComfyUI 不能被空闲卸载关掉 ==")
from src.core import comfyui_jobs as J                            # noqa: E402
from src.core import comfyui_client as CC                         # noqa: E402

_calls = {"alive": 0, "stop": 0, "free": 0}
_orig = (CC.alive, CC.stop, CC.free_models)
CC.alive = lambda *a, **k: (_calls.__setitem__("alive", _calls["alive"] + 1), True)[1]
CC.stop = lambda *a, **k: (_calls.__setitem__("stop", _calls["stop"] + 1),
                           {"ok": True, "killed": 1})[1]
CC.free_models = lambda *a, **k: (_calls.__setitem__("free", _calls["free"] + 1), {})[1]

try:
    with J._lock:
        J._jobs.clear()                     # 没有排队/在跑的任务
    J._explicit_start_ts = 0.0
    J._draw_seen_since_start = False

    # ① 普通情况：没被显式启动过 → 照旧关掉（原有行为不能丢）
    J.on_tool_unloaded()
    check("没显式启动过 → 照旧关掉（原行为保留）", _calls["stop"] == 1, str(_calls))

    # ② 刚被显式 start、还没画过图 → **不关**
    _calls["stop"] = 0
    J.note_explicit_start()
    J.on_tool_unloaded()
    check("★刚 start、还没画过图 → 不关（真机 bug 就在这里）", _calls["stop"] == 0,
          str(_calls))

    # ③ 画过图之后 → 恢复"同加载同卸载"
    J._draw_seen_since_start = True          # 等价于 submit_draw 已经入队
    J.on_tool_unloaded()
    check("★已经画过图 → 照旧关掉（不会一直占着显存）", _calls["stop"] == 1, str(_calls))

    # ④ 宽限期过了 → 也照旧关掉（怕模型永远不回来画）
    _calls["stop"] = 0
    J.note_explicit_start()
    J._explicit_start_ts = time.time() - (J.EXPLICIT_START_GRACE + 60)
    J.on_tool_unloaded()
    check("★宽限期（10 分钟）过了 → 照旧关掉", _calls["stop"] == 1,
          f"grace={J.EXPLICIT_START_GRACE}")

    # ⑤ 队列里还有任务时仍然不关（这条是原有安全前提，别被改坏）
    _calls["stop"] = 0
    J._explicit_start_ts = 0.0
    J._jobs["probe_job"] = {"state": "running"}
    J.on_tool_unloaded()
    check("★后台还有画图任务 → 不关（原有安全前提）", _calls["stop"] == 0, str(_calls))
    with J._lock:
        J._jobs.clear()
finally:
    CC.alive, CC.stop, CC.free_models = _orig
    J._explicit_start_ts = 0.0
    J._draw_seen_since_start = False

_src_j = (BASE / "src" / "core" / "comfyui_jobs.py").read_text(encoding="utf-8")
check("★submit_draw 会撤销宽限（画过就算用过）",
      "_draw_seen_since_start = True" in _src_j)
_tool_src = (BASE / "tools" / "comfyui_tool.py").read_text(encoding="utf-8")
check("★start 成功会登记「特意拉的」，并提醒「尽快 draw」",
      "note_explicit_start" in _tool_src and "尽快接着调" in _tool_src)


print("\n== C. ★file_ops 漏给 path 时要说人话（别报「只读模式」）==")
import tools.file_ops as FO                                       # noqa: E402

_msg = FO._check_write_protection(str(BASE / "somewhere.txt"), "")
check("★漏给 path → 明确指出缺 path", _msg is not None and "缺少" in _msg and "path" in _msg,
      str(_msg)[:90])
check("★漏给 path 时不再含糊地说「只读模式」", "只读模式" not in (_msg or ""), str(_msg)[:90])
_msg2 = FO._check_write_protection(str(BASE / "src" / "x.py"), str(BASE / "src" / "x.py"))
check("真的越界仍然报沙箱拒绝", _msg2 is not None and "只读模式" in _msg2, str(_msg2)[:60])
_ok = FO._check_write_protection(str(FO.WRITABLE_ROOT / "ok.txt"),
                                 "data/桌宠私有data/ok.txt")
check("写在自己目录里放行", _ok is None, str(_ok)[:60])


print("\n== D. ★提示词：先出东西、别一直搜集素材；预算也要够 ==")
_lc = (BASE / "src" / "core" / "llm_client.py").read_text(encoding="utf-8")
check("★有「先出东西、再谈完美」这一段", "先出东西" in _lc)
check("★明确「连续 3 轮以上都在搜就该停下用现有素材做」",
      "3 轮以上" in _lc and "素材够用就开工" in _lc)
check("★提醒 download 失败别死磕同一个链接", "别再重试同一个链接" in _lc)
from src.core.session import ChatSession                          # noqa: E402
check("★轮次预算提到 30（20 轮写不完一份手写脚本的 PPT）",
      ChatSession.MAX_ROUNDS == 30, str(ChatSession.MAX_ROUNDS))
check("★75% 收尾提醒还在", "MAX_ROUNDS * 0.75" in
      (BASE / "src" / "core" / "session.py").read_text(encoding="utf-8"))

print("\n== E. ★思考型模型的 max_tokens 下限提到 8192（4096 已被真机证伪）==")
# 证据 logs/prts_20261004_032056.log：第 7/8/9 轮连续
#   `空回复（thinking=6947/7017/6640 字，max_tokens=4096）` → "多轮空回复，放弃"，
# 光是思考就比 4096 大，正文永远拿不到预算。
check("★llm_client 里的下限是 8192", "_floor = 8192" in _lc,
      [ln.strip() for ln in _lc.splitlines() if "_floor =" in ln][:2])
check("★注释里记着真机证据（空回复 + thinking 字数）", "空回复（thinking=" in _lc)
check("★4096 不再作为下限", "_floor = 4096" not in _lc)

print(f"\n==== 结果: {len(PASS)} PASS / {len(FAIL)} FAIL ====")
for f in FAIL:
    print("  FAIL:", f)
sys.exit(1 if FAIL else 0)
