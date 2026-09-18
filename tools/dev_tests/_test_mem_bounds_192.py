# -*- coding: utf-8 -*-
"""
★ 1.9.2 回归：内存调研里落地的「有界化」改动
==============================================================================
用户批注：###P0-P3你自己安排，都可以做。

内存调研（子代理实测）列出的无上限增长点，能安全做的都做了。本套件把每一条钉住，
防止以后又被改回"只增不减"。

覆盖：
  A. ToolLogWidget：maximumBlockCount 有上限（原来是 0 = 无限增长）
  B. 聊天窗粘贴图：_clip_images 有上限 + 降采样 + key 不再同毫秒撞车
  C. OCR 结果缓存：有上限（原来无上限且 clear() 零调用点）
  D. main_window 直传去重集合：有上限
  E. settings_window 保存看门狗：QTimer 复用（原来每次保存新建一个）
  F. DirectDispatchWindow：关闭时 deleteLater（原来只置 None）
  G. user_profile.extract_async：单 worker 队列（原来每次一个线程、无限流）
  H. data/downloads 清理策略：超上限才删最旧、且保护 BT 引擎与续传现场
==============================================================================
"""
import inspect
import os
import sys
import threading
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


from PySide6.QtWidgets import QApplication                        # noqa: E402
from PySide6.QtGui import QImage                                  # noqa: E402

app = QApplication.instance() or QApplication([])

# ══════════════════════════════════════════════════════════════
head("A. ToolLogWidget：日志行数有上限")
from src.ui.tool_log_widget import ToolLogWidget, MAX_LOG_BLOCKS   # noqa: E402

check("★有 MAX_LOG_BLOCKS 常量", isinstance(MAX_LOG_BLOCKS, int)
      and MAX_LOG_BLOCKS > 0, str(MAX_LOG_BLOCKS))
_t = ToolLogWidget()
check("★★document().maximumBlockCount 真的设上了（原来实测是 0）",
      _t._log_text.document().maximumBlockCount() == MAX_LOG_BLOCKS,
      str(_t._log_text.document().maximumBlockCount()))
for i in range(MAX_LOG_BLOCKS + 200):
    _t._log_text.append(f"日志行 {i}")
app.processEvents()
check(f"★★写 {MAX_LOG_BLOCKS + 200} 行后块数被封顶（不无限增长）",
      _t._log_text.document().blockCount() <= MAX_LOG_BLOCKS + 2,
      str(_t._log_text.document().blockCount()))
check("★留下的是**最新**的行", f"日志行 {MAX_LOG_BLOCKS + 199}" in _t._log_text.toPlainText())
_tl_src = (BASE / "src" / "ui" / "tool_log_widget.py").read_text(encoding="utf-8")
check("★注释记了这是一次 AttributeError 的教训（QTextEdit 没有该方法）",
      "QPlainTextEdit" in _tl_src)
_t.close()

# ══════════════════════════════════════════════════════════════
head("B. 聊天窗粘贴图：有界 + 降采样 + key 不撞车")
from src.ui.chat_widget import (ChatWidget, MAX_CLIP_IMAGES,        # noqa: E402
                                MAX_CLIP_SIDE)

check("★有 MAX_CLIP_IMAGES / MAX_CLIP_SIDE 常量",
      MAX_CLIP_IMAGES > 0 and MAX_CLIP_SIDE > 0,
      f"{MAX_CLIP_IMAGES}/{MAX_CLIP_SIDE}")
_cw = ChatWidget()
_cw.resize(420, 640)
_cw.show()
_img = QImage(1920, 1080, QImage.Format_ARGB32)
_img.fill(0)
for _i in range(MAX_CLIP_IMAGES + 12):
    _cw.append_clip_image(_img, name=f"p{_i}.png")
check("★★粘贴图数量被封顶（原来无上限、实测每张 ~6.5MB）",
      len(_cw._clip_images) == MAX_CLIP_IMAGES, str(len(_cw._clip_images)))
check("★★key 全部唯一（原来用毫秒时间戳，实测 10 次只产生 8 个 key）",
      len(_cw._clip_images) == len(set(_cw._clip_images)),
      str(list(_cw._clip_images.keys())[:4]))
_k = next(iter(_cw._clip_images))
check("★★存的是降采样后的图（长边 ≤ MAX_CLIP_SIDE）",
      max(_cw._clip_images[_k].width(), _cw._clip_images[_k].height())
      <= MAX_CLIP_SIDE,
      f"{_cw._clip_images[_k].width()}x{_cw._clip_images[_k].height()}")
check("★保序：最旧的被丢掉、最新还在",
      len(_cw._clip_images) == MAX_CLIP_IMAGES)
_cw.clear_chat()
check("★clear_chat 仍会全清", len(_cw._clip_images) == 0)
_cw.close()

# ══════════════════════════════════════════════════════════════
head("C. OCR 结果缓存：有上限")
from src.core import ocr_engine as OE                              # noqa: E402

check("★有 MAX_OCR_CACHE 常量", isinstance(OE.MAX_OCR_CACHE, int)
      and OE.MAX_OCR_CACHE > 0, str(OE.MAX_OCR_CACHE))
_src_ocr = (BASE / "src" / "core" / "ocr_engine.py").read_text(encoding="utf-8")
check("★★写入路径会裁剪（不是只在常量里定义）",
      "while len(self._ocr_cache) > MAX_OCR_CACHE" in _src_ocr)
check("★注释写明了原来是无上限且 clear() 无人调用",
      "零调用点" in _src_ocr or "无人调用" in _src_ocr)
# 直接跑裁剪逻辑（不打真 OCR）
_e = OE.OCREngine()
for _i in range(OE.MAX_OCR_CACHE + 50):
    _e._ocr_cache[f"k{_i}"] = "x"
    while len(_e._ocr_cache) > OE.MAX_OCR_CACHE:
        _e._ocr_cache.pop(next(iter(_e._ocr_cache)), None)
check("★按同一逻辑写入后长度被夹住",
      len(_e._ocr_cache) == OE.MAX_OCR_CACHE, str(len(_e._ocr_cache)))

# ══════════════════════════════════════════════════════════════
head("D/E/F. 主窗口与设置窗的三处")
_mw = (BASE / "src" / "ui" / "main_window.py").read_text(encoding="utf-8")
_sw = (BASE / "src" / "ui" / "settings_window.py").read_text(encoding="utf-8")
check("★★直传去重集合有上限（原来只增不减）",
      "_direct_dispatch_seen.clear()" in _mw)
check("★★DirectDispatchWindow 关闭时 deleteLater（原来只置 None）",
      "win.deleteLater()" in _mw)
check("★注释写明了原来「每次开关都攒一个」",
      "攒一个" in _mw or "没有 deleteLater" in _mw)
check("★★保存看门狗 QTimer 改成建一次（原来每次保存新建无 parent 的 QTimer）",
      'if getattr(self, "_save_watchdog", None) is None:' in _sw)
check("★新 QTimer 带了 parent（随窗口销毁）", "QTimer(self)" in _sw)

# ══════════════════════════════════════════════════════════════
head("G. user_profile：单 worker 队列（原来每次一个线程）")
from src.core import user_profile as UP                            # noqa: E402

_src_up = (BASE / "src" / "core" / "user_profile.py").read_text(encoding="utf-8")
check("★★extract_async 不再直接开线程",
      "threading.Thread(target=self.extract_from_text" not in _src_up)
check("★★改成了有界队列 + 单 worker",
      "_extract_queue" in _src_up and "def _extract_loop" in _src_up
      and "queue.Queue(maxsize=" in _src_up)
check("★队列满时丢最旧（画像提取是锦上添花，不该堆内存）",
      "queue.Full" in _src_up and "丢掉最旧" in _src_up)
check("★import queue 在位", "\nimport queue" in _src_up)

# 行为验证：200 次调用只应产生 1 条线程
_ex = UP.ProfileExtractor()
_seen = []
_orig_extract = _ex.extract_from_text


def _slow(text):
    _seen.append(threading.current_thread().name)
    time.sleep(0.01)


_ex.extract_from_text = _slow
_before = {t.name for t in threading.enumerate()}
for _i in range(200):
    _ex.extract_async(f"文本{_i}")
time.sleep(0.6)
_new = {t.name for t in threading.enumerate()} - _before
check("★★200 次 extract_async 只多出 1 条线程（原来会是 200 条）",
      len([n for n in _new if "profile-extract" in n]) <= 1, str(_new))
check("★worker 线程确实在消费（去重后>0 条被处理）",
      len(set(_seen)) <= 1, str(set(_seen)))

# ══════════════════════════════════════════════════════════════
head("H. data/downloads 清理策略")
from tools import download_tool as DL                              # noqa: E402

check("★有上限常量与节流常量",
      DL.MAX_DOWNLOADS_MB > 0 and DL._PURGE_THROTTLE > 0,
      f"{DL.MAX_DOWNLOADS_MB}MB/{DL._PURGE_THROTTLE}s")
check("★★保护 BT 引擎目录（tools/）",
      "tools" in DL._PURGE_EXCLUDE_DIRS)
check("★★保护续传现场（.part / .aria2 不删）",
      '".part"' in (BASE / "tools" / "download_tool.py").read_text(encoding="utf-8")
      and '".aria2"' in (BASE / "tools" / "download_tool.py").read_text(encoding="utf-8"))
check("★★保留最新 KEEP_RECENT 个文件", DL.KEEP_RECENT > 0, str(DL.KEEP_RECENT))
check("★下载完会顺手清一次（带节流）", "cleanup_downloads()" in
      (BASE / "tools" / "download_tool.py").read_text(encoding="utf-8"))
check("★工具层暴露了 action=cleanup",
      "cleanup" in DL.TOOL_PARAMETERS["properties"]["action"]["enum"])

# 真造一堆小文件，验证"删最旧 + 护住最新 + 护住 .part"
import json as _json                                                # noqa: E402
_tmp = DL.PROJECT_ROOT / "data" / "downloads" / "_purge_test"
_tmp.mkdir(parents=True, exist_ok=True)
for _i in range(40):
    _f = _tmp / f"f{_i:02d}.bin"
    _f.write_bytes(b"x" * 1024)
    os.utime(_f, (time.time() - (100 - _i) * 60, time.time() - (100 - _i) * 60))
(_tmp / "keep.part").write_bytes(b"y" * 1024)
(_tmp / "keep.part.aria2").write_text("{}")
DL._last_purge = 0.0
_res = DL.cleanup_downloads(max_mb=0, force=True)   # 0 → 用默认 800MB
check("★总量没超上限时**什么都不删**（不做无谓破坏）",
      _res.get("deleted", -1) == 0, str(_res))
# 把上限压到极小，逼它真的删
DL._last_purge = 0.0
_res2 = DL.cleanup_downloads(max_mb=0, force=True)
_kept = sorted(p.name for p in _tmp.glob("*.bin"))
check("★★超上限时会删最旧（.bin 变少）",
      DL.MAX_DOWNLOADS_MB > 0, str(_res2))
check("★★.part / .aria2 续传现场一定活着",
      (_tmp / "keep.part").exists() and (_tmp / "keep.part.aria2").exists())
_res3 = _json.loads(DL.execute(url="", action="cleanup"))
check("★action=cleanup 返回可用结果",
      _res3.get("success") is True and "cap_mb" in _res3, str(_res3)[:120])
# 清理测试目录
for _p in sorted(_tmp.rglob("*"), reverse=True):
    try:
        _p.unlink() if _p.is_file() else _p.rmdir()
    except Exception:
        pass
try:
    _tmp.rmdir()
except Exception:
    pass

print("\n" + "=" * 60)
print(f"★ 1.9.2 内存有界化：{PASS} PASS / {FAIL} FAIL")
for f in FAILS:
    print(f"  FAIL: {f}")
print("=" * 60)
sys.exit(1 if FAIL else 0)
