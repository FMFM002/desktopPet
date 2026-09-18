# -*- coding: utf-8 -*-
"""
★ 1.9.2 回归：easyocr Reader 的空闲卸载（内存调研第 1 条，用户指定做法）
==============================================================================
用户批注（针对"easyocr 首次 OCR 后永久常驻 ~776MB"）：
    ###这个直接一样的30s后卸载就行了。

"一样的"= 照 LAZY 工具那套 `IDLE_UNLOAD_TIMEOUT = 30.0`
（src/core/tool_manager.py），挂在**同一个空闲清扫线程**上，
不额外起线程（多起线程会抱住旧对象，反而释放不掉）。

测什么（不加载真模型 —— 那要 4.8s / 776MB，太重）：
  A. 接口在位：release_idle_reader + READER_IDLE_TIMEOUT=30 + _reader_last_used
  B. 逻辑正确：未超时不释放 / 超时释放 / 没 Reader 时不报错 / 释放后能重建
  C. 真的接进了空闲清扫线程（不是写完没人调）
  D. 挂在 tool_manager 的同一个清扫循环里、用同一条 30 秒规则
  E. 诚实性：注释里说清了"能省 174MB，但 torch 的 510MB 本进程要不回来"
==============================================================================
"""
import os
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE))
os.environ.setdefault("DEEPSEEK_API_KEY", "test")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PASS = FAIL = 0
FAILS = []


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        FAILS.append(name)
        print(f"  [FAIL] {name}  {extra}")


def head(t):
    print(f"\n== {t} ==")


import src.core.ocr_engine as OE                                   # noqa: E402
from src.core.tool_manager import IDLE_UNLOAD_TIMEOUT              # noqa: E402

# ══════════════════════════════════════════════════════════════
head("A. 接口与常量")
check("★有 READER_IDLE_TIMEOUT 常量", hasattr(OE, "READER_IDLE_TIMEOUT"))
check("★★超时值和 LAZY 工具那条规则一致（用户说的「一样的30s」）",
      OE.READER_IDLE_TIMEOUT == IDLE_UNLOAD_TIMEOUT == 30.0,
      f"{OE.READER_IDLE_TIMEOUT} vs {IDLE_UNLOAD_TIMEOUT}")
check("★OCREngine 有 release_idle_reader()",
      hasattr(OE.OCREngine, "release_idle_reader"))
check("★__init__ 里有 _reader_last_used",
      "_reader_last_used" in (BASE / "src" / "core" / "ocr_engine.py")
      .read_text(encoding="utf-8"))
check("★import time 在位（第一版漏了它，编译能过但运行会 NameError）",
      "import time" in (BASE / "src" / "core" / "ocr_engine.py")
      .read_text(encoding="utf-8"))

# ══════════════════════════════════════════════════════════════
head("B. 逻辑：未超时不放、超时才放、放了能重建")
eng = OE.OCREngine(engine="easyocr")
# 用一个假 Reader 顶替真模型（避免加载 776MB）
_SENTINEL = object()
eng._easyocr_reader = _SENTINEL
eng._reader_last_used = time.time()

check("★刚用过 → 不释放", eng.release_idle_reader() is False)
check("★未超时时 Reader 还在", eng._easyocr_reader is _SENTINEL)

# 把"最后使用时间"往前拨 31 秒
eng._reader_last_used = time.time() - (OE.READER_IDLE_TIMEOUT + 1)
check("★★空闲超过 30 秒 → 释放", eng.release_idle_reader() is True)
check("★★释放后 _easyocr_reader 变回 None", eng._easyocr_reader is None)
check("★已经没 Reader 时再调不报错、返回 False",
      eng.release_idle_reader() is False)

# 边界：刚好等于超时不应释放（是 < timeout 就不放）
eng._easyocr_reader = _SENTINEL
eng._reader_last_used = time.time() - (OE.READER_IDLE_TIMEOUT - 1)
check("★差 1 秒还不放（边界不越界）", eng.release_idle_reader() is False)
eng._easyocr_reader = None

# 释放后下次 OCR 应能重新加载（这里只验证"reader 为 None 会走重建分支"）
_src = (BASE / "src" / "core" / "ocr_engine.py").read_text(encoding="utf-8")
check("★重建分支仍在（if self._easyocr_reader is None → easyocr.Reader(...)）",
      "if self._easyocr_reader is None:" in _src
      and "easyocr.Reader(" in _src)
check("★每次真正用到会刷新 _reader_last_used（否则会被误判空闲）",
      "_reader_last_used = time.time()" in _src)

# ══════════════════════════════════════════════════════════════
head("C. 真的接进了空闲清扫（不是写完没人调）")
from src.core.tool_manager import get_tool_manager                 # noqa: E402

tm = get_tool_manager()
check("★ToolManager 有 _sweep_idle_big_objects()",
      hasattr(tm, "_sweep_idle_big_objects"))
check("★★清扫循环里真的调了它",
      "_sweep_idle_big_objects()" in
      (BASE / "src" / "core" / "tool_manager.py").read_text(encoding="utf-8")
      .split("def _idle_check_loop")[1].split("def ")[0])

# 手动跑一次清扫，确认它会去问 OCR 引擎（用一个可观测的假 VisionClient）
import src.core.llm_client as LC                                   # noqa: E402

_calls = {"n": 0}


class _FakeEngine:
    def release_idle_reader(self, timeout=OE.READER_IDLE_TIMEOUT):
        _calls["n"] += 1
        return False


class _FakeVC:
    _ocr_engine = _FakeEngine()


_orig_get = LC.get_vision_client
LC.get_vision_client = lambda: _FakeVC()
try:
    tm._sweep_idle_big_objects()
finally:
    LC.get_vision_client = _orig_get
check("★★清扫会去调 OCR 引擎的 release_idle_reader()", _calls["n"] == 1,
      f"调了 {_calls['n']} 次")

# 视觉客户端没建过 / 没 OCR 引擎时不能炸
class _NoEngine:
    _ocr_engine = None

    def __init__(self):
        pass


LC.get_vision_client = lambda: _NoEngine()
try:
    tm._sweep_idle_big_objects()
    _ok = True
except Exception as e:
    _ok = False
    print("   ", e)
finally:
    LC.get_vision_client = _orig_get
check("★没 OCR 引擎时清扫不报错（不能把清扫线程带崩）", _ok)

LC.get_vision_client = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
try:
    tm._sweep_idle_big_objects()
    _ok2 = True
except Exception:
    _ok2 = False
finally:
    LC.get_vision_client = _orig_get
check("★视觉客户端取不到时清扫也不报错", _ok2)

# ══════════════════════════════════════════════════════════════
head("D. 用同一条规则、同一个线程（不额外起线程）")
check("★没有为 OCR 单独起线程（避免旧线程抱住旧对象）",
      "Thread(" not in _src.split("class OCREngine")[1],
      "OCR 模块里不该出现 Thread(")
check("★挂在 tool-idle-check 那条线程上",
      "tool-idle-check" in (BASE / "src" / "core" / "tool_manager.py")
      .read_text(encoding="utf-8"))
check("★每次清扫都有 try/except 包着（单次失败不带崩线程）",
      "大对象空闲回收出错（忽略）" in
      (BASE / "src" / "core" / "tool_manager.py").read_text(encoding="utf-8"))

# ══════════════════════════════════════════════════════════════
head("E. 注释如实说明能省多少（不夸大）")
check("★★注释写明 Reader 约 174MB 能释放", "174" in _src)
check("★★注释写明 torch 的 510MB 本进程要不回来（不吹成省 776MB）",
      "510" in _src and "sys.modules" in _src)
check("★注释引用了用户原话「一样的30s后卸载」",
      "一样的30s后卸载" in _src or "一样的 30s" in _src)
check("★注释写了释放后下次会重新加载（行为可预期）", "重新加载" in _src)
check("★释放时主动 gc.collect（Reader 里有循环引用）",
      "gc.collect()" in _src)

print("\n" + "=" * 60)
print(f"★ 1.9.2 easyocr 空闲卸载：{PASS} PASS / {FAIL} FAIL")
for f in FAILS:
    print(f"  FAIL: {f}")
print("=" * 60)
sys.exit(1 if FAIL else 0)
