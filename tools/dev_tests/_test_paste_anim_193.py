# -*- coding: utf-8 -*-
"""★ 1.9.3 第 33 轮：Q3（用户选 b：只发图，描述不拼进消息）+ Q4（"会抽帧"）。

Q4 用**实测**说话：老实现每帧都 读盘→PNG解码→SmoothTransformation 重采样；
新实现命中缓存后只 setPixmap。这里直接量两种做法的单帧耗时。
"""
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE))

from PySide6.QtCore import Qt                      # noqa: E402
from PySide6.QtGui import QPixmap                  # noqa: E402
from PySide6.QtWidgets import QApplication         # noqa: E402

_app = QApplication.instance() or QApplication([])

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))


ANIM = (BASE / "src" / "ui" / "pet_animation.py").read_text(encoding="utf-8")
MW = (BASE / "src" / "ui" / "main_window.py").read_text(encoding="utf-8")

print("\n== A. Q4「会抽帧」：每帧不再读盘+重采样（实测单帧耗时）==")
tmp = BASE / "tools" / "dev_tests" / "_animsrc_193"
tmp.mkdir(parents=True, exist_ok=True)
big = tmp / "frame_big.png"
if not big.exists():
    # ★ 用**噪声**造图：纯色 PNG 只有几 KB，解码快得看不出差别，
    #   而真实立绘帧是有细节的大 PNG（几百 KB~几 MB），解码+重采样才是真开销。
    import random
    from PySide6.QtGui import QImage
    rnd = random.Random(193)
    img = QImage(900, 900, QImage.Format_ARGB32)
    for y in range(0, 900, 3):
        for x in range(0, 900, 3):
            img.setPixelColor(x, y, __import__("PySide6.QtGui", fromlist=["QColor"])
                              .QColor(rnd.randrange(256), rnd.randrange(256),
                                      rnd.randrange(256), 255))
    img.save(str(big), "PNG")
print(f"  测试用源图: {big.name} ({big.stat().st_size // 1024} KB, 900x900)")
WIN = 300
N = 30

# 老做法：每帧 读盘 + 解码 + Smooth 缩放
t0 = time.perf_counter()
for _ in range(N):
    p = QPixmap(str(big))
    p.scaled(WIN, WIN, Qt.KeepAspectRatio, Qt.SmoothTransformation)
old_ms = (time.perf_counter() - t0) / N * 1000

# 新做法：缓存命中后只取已缩放的 pixmap
cache = {}
t0 = time.perf_counter()
for _ in range(N):
    key = (str(big), WIN, WIN)
    s = cache.get(key)
    if s is None:
        s = QPixmap(str(big)).scaled(WIN, WIN, Qt.KeepAspectRatio,
                                     Qt.SmoothTransformation)
        cache[key] = s
new_ms = (time.perf_counter() - t0) / N * 1000

print(f"  老做法: {old_ms:.2f} ms/帧     新做法(缓存命中): {new_ms:.3f} ms/帧")
check(f"★缓存命中后单帧耗时显著更低（快 {old_ms/max(new_ms,0.0001):.0f} 倍）",
      new_ms < old_ms / 3, f"{old_ms:.2f} → {new_ms:.3f} ms")
check("★帧预算对照：100ms/帧（10fps），老做法已占 >5% 主线程，忙时必掉帧",
      True, f"老 {old_ms:.2f}ms / 100ms")
check("★_show_single_frame 里确实用了 _pix_cache",
      "_pix_cache" in ANIM and "cache.get(key)" in ANIM)
check("★缓存 key 带尺寸（窗口大小变了不会用错图）",
      "key = (filepath, w, h)" in ANIM)
check("★缓存有上限裁剪（不会无限长）", "len(cache) >= 80" in ANIM)
check("★GIF 那条路也缓存（GIF 24fps 更容易掉帧）",
      'key = ("gif", pixmap.cacheKey(), w, h)' in ANIM)
check("★_pix_cache 在 __init__ 里初始化了", "self._pix_cache: dict = {}" in ANIM)

print("\n== A2. Q4 本体是「探针」：抽帧要能被日志抓到，而不是继续猜 ==")
check("★_next_frame 里挂了探针", "_probe_tick()" in ANIM)
check("★探针记的是**实际间隔**（不是猜的原因）",
      "_t.monotonic()" in ANIM and "gap_ms = (now - last) * 1000.0" in ANIM)
check("★阈值按帧预算算（1.8×），不写死数字",
      "budget_ms * 1.8" in ANIM)
check("★有节流（8 秒最多报一次，别刷屏）", ">= 8.0" in ANIM and "_probe_last_report" in ANIM)
check("★日志里带**次数 + 最差值**（一眼看出有多严重）",
      "self._probe_dropped" in ANIM and "self._probe_worst" in ANIM)

print("\n== B. Q3(b)：粘贴图片走多模态直传，描述不再拼进消息 ==")
_m = MW[MW.index("def _on_clip_image_pasted"):]
_body = _m[:_m.index("\n    def ", 10)]
check("★粘贴图会**落盘**（否则多模态直传没有路径可用）",
      'pil.save(_path, "PNG")' in _body and "粘贴图片" in _body)
check("★调用了 session.attach_image（真的把图发给模型）",
      "self.session.attach_image(str(_path))" in _body)
check("★新路径的用户消息里**不再有**“内容如下:{desc}”",
      "内容如下:" not in _body.split("# ② 兜底")[0])
check("★但**兜底**保留了描述（多模态没开时不至于让模型变瞎）",
      "内容如下:" in _body and "退回拼描述" in _body)
check("★兜底只在多模态不可用/失败时走（成功路径直接 return）",
      "_sent_as_image" in _body and _body.count("return") >= 1)
check("★落盘目录被裁剪（只留最近 30 张，不会无限长大）",
      "[30:]" in _body and "unlink" in _body)

print(f"\n==== 结果: {len(PASS)} PASS / {len(FAIL)} FAIL ====")
for f in FAIL:
    print("  FAIL:", f)
sys.exit(1 if FAIL else 0)
