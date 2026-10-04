# -*- coding: utf-8 -*-
"""★ 1.9.3 第 56 轮：**粘贴图在重画/重启后能不能真的看见** —— 用跑的不是读的。

## 为什么要单独开这一条
第 56 轮盘点待办时，我看到 `append_clip_image()` 的 docstring 写着
"像素只在内存里，**重启后预览会变成占位提示**"，
就对外报了"粘贴图没持久化、这是个待办"。

**结果是我错了。** 就在那段 docstring **下面 6 行**（`_render_message_html` 里）写着：

    # ★ 1.9.2（R44）：内存里没有（重启后 / 被 LRU 淘汰）→ 从磁盘缓存恢复。

而且 `append_clip_image()` 里也确实会把图**落盘成 WebP**
（`data/cache/chat_clips/`，注释："这就是'重启后可见'的修复"）。

★ 这跟第 53 轮我犯的错是**同一个错**：**只读代码（还读到过期注释）就下结论**。
  所以这一轮**先跑证据**，再用断言把它钉死 —— 而不是再"读一遍然后说应该没问题"。

本套件证明：
  A. `append_clip_image()` 返回的 `[[CLIP:...]]` 标记里能取出 key，且**磁盘上真有缓存文件**；
  B. ★ 模拟"重启/重画"（**清空内存里的图**）后用同一个标记重画 →
     **图从磁盘恢复**，不是占位提示；
  C. 缓存目录在 `data/cache/chat_clips/` 下（用户 1.9.2 要求"缓存统一放 data/cache"）；
  D. 缓存文件被删掉时 → **优雅退化成占位**，不抛异常。
"""
import os
import re
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from PySide6.QtWidgets import QApplication        # noqa: E402
from PySide6.QtGui import QImage, QColor          # noqa: E402

app = QApplication.instance() or QApplication([])

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))


import src.ui.chat_widget as CW                   # noqa: E402
from src.ui.chat_widget import ChatWidget         # noqa: E402

w = ChatWidget()
w.resize(420, 640)
w.show()

img = QImage(320, 200, QImage.Format_ARGB32)
img.fill(QColor(20, 160, 220))

print("== A. 粘贴：标记里能取到 key，磁盘上真有缓存 ==")
marker = w.append_clip_image(img, "测试粘贴.png")
_m = re.match(r"\[\[CLIP:([^|\]]+)\|", marker)
check("★返回了 [[CLIP:key|名字|尺寸]] 标记", bool(_m), marker)
key = _m.group(1) if _m else ""
_cache = CW._clip_cache_path(key)
check("★★磁盘上真的有缓存文件（这才是'重启后可见'的依据）",
      _cache.is_file(), str(_cache))
check("★缓存在 data/cache/chat_clips/ 下（1.9.2 用户要求缓存统一）",
      "cache" in str(_cache).replace("\\", "/")
      and "chat_clips" in str(_cache).replace("\\", "/"), str(_cache))
check("★缓存是 WebP（1.9.2 定的格式）", _cache.suffix.lower() == ".webp", _cache.suffix)

print("\n== B. ★模拟重启/重画：清空内存后从磁盘恢复 ==")
_check_uri = _cache.as_uri()
_has_img_before = _check_uri.replace("&", "&amp;") in w._chat_display.toHtml()
check("★刚粘贴时，聊天里确实画了这张图", _has_img_before)
w._clip_images.clear()                            # ← 模拟重启：内存里的像素没了
check("★前置：内存里已经没有了", key not in w._clip_images)
w._append_message("user", marker)                 # ← 重画这条消息（历史重画走的就是这条路）
check("★★重画后**从磁盘恢复**了（内存里又有它了）", key in w._clip_images,
      "没恢复 = 用户看到的就是占位提示")
_uri2 = CW._clip_cache_path(key).as_uri().replace("&", "&amp;")
check("★★重画出来的 HTML 指向的是**磁盘文件**（不是破图占位）",
      f'src="{_uri2}"' in w._chat_display.toHtml() or _uri2 in w._chat_display.toHtml())
check("★图被重新注册成了文档资源（Qt 才画得出来）",
      not w._chat_display.document().resource(
          CW.QTextDocument.ImageResource, CW.QUrl(f"clip://{key}")).isNull())

print("\n== C. 磁盘缓存没了 → 优雅退化，不抛异常 ==")
w._clip_images.clear()
try:
    _cache.unlink()
except Exception:
    pass
try:
    w._append_message("user", marker)
    _ok = True
    _err = ""
except Exception as e:                            # noqa: BLE001
    _ok, _err = False, f"{type(e).__name__}: {e}"
check("★★缓存文件不在时，重画不抛异常（退化成占位）", _ok, _err)
check("★此时内存里不会凭空多出图", key not in w._clip_images)

w.close()
print(f"\n==== 结果: {len(PASS)} PASS / {len(FAIL)} FAIL ====")
for f in FAIL:
    print("  FAIL:", f)
sys.exit(1 if FAIL else 0)
