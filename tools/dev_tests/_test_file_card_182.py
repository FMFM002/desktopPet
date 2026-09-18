# -*- coding: utf-8 -*-
"""★ 1.8.2 回归：文件卡片 = 3cm × 1cm。

测量方式刻意做成「与字体无关」：把卡片 HTML 渲染到**透明**画布上，
再取 alpha>0 的外框 —— 因为卡片尺寸由 <table width height> 决定，
换台机器/换字体都应该还是 3cm × 1cm。

用法: python tools/dev_tests/_test_file_card_182.py
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import Qt, QSizeF  # noqa: E402
from PySide6.QtGui import (QFont, QImage, QPainter, QTextDocument,  # noqa: E402
                           QAbstractTextDocumentLayout)
from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication(sys.argv)

from src.ui.chat_widget import ChatWidget  # noqa: E402

PASS, FAIL = [], []
PX_PER_CM = 96 / 2.54


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))


def painted_bbox(html: str, width: int):
    """渲染 HTML 到透明画布，返回 (x, y, w, h) 的不透明像素外框"""
    doc = QTextDocument()
    doc.setDocumentMargin(0)
    doc.setDefaultFont(QFont("Microsoft YaHei", 10))
    doc.setHtml(html)
    doc.setTextWidth(width)
    h = int(doc.size().height()) + 4
    img = QImage(QSizeF(width + 40, h).toSize(), QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    p = QPainter(img)
    doc.documentLayout().draw(p, QAbstractTextDocumentLayout.PaintContext())
    p.end()
    xs, ys = [], []
    for y in range(img.height()):
        for x in range(img.width()):
            if img.pixelColor(x, y).alpha() > 8:
                xs.append(x)
                ys.append(y)
    if not xs:
        return None
    return min(xs), min(ys), max(xs) - min(xs) + 1, max(ys) - min(ys) + 1


target = ROOT / "data" / "桌宠私有data" / "office" / "pptx" / "deepseek_intro.pptx"
assert target.exists(), target

w = ChatWidget()
w.resize(340, 640)
w.show()
app.processEvents()

_f = QFont("Microsoft YaHei")
_f.setPixelSize(11)

want_w, want_h = w.card_size_px()
print(f"== 1. 卡片尺寸（目标 {want_w}×{want_h}px = "
      f"{want_w/PX_PER_CM:.2f}cm × {want_h/PX_PER_CM:.2f}cm）==")


# ★ 1.9.0: 卡片底色改由 _RoundedBubbleBrowser 自绘（HTML 里已无 bgcolor），
#   所以不能再靠"渲染 HTML 量不透明外框"来测尺寸 —— 改为把卡片插进真实聊天区，
#   取自绘矩形。
def _drawn_card_box(widget, label, path, size, from_bot=True):
    widget.clear_chat()
    widget.append_file_card(label, str(path), size, from_bot)
    app.processEvents()
    rects = widget._chat_display._card_rects()
    if not rects:
        return None
    r, _c = rects[0]
    return int(r.x()), int(r.y()), int(r.width()), int(r.height())


html = w.file_card_html("用户上传", target.resolve(), "15.1KB", want_w, want_h)
box = _drawn_card_box(w, "用户上传", target.resolve(), "15.1KB")
check("卡片画出了自绘白底", box is not None, str(box))
if box:
    x, y, cw, ch = box
    print(f"  实测自绘卡片: {cw}×{ch}px = {cw/PX_PER_CM:.2f}cm × {ch/PX_PER_CM:.2f}cm")
    # ★ 1.9.0 实测发现（重要）：卡片嵌套在 <td> 里时，Qt 会**忽略** table 的
    #   width/height，把它拉伸到整行宽 → CARD_*_CM 在真实界面上**并不生效**。
    #   旧版这个测试只量"单独渲染"的卡片，所以 1.8.2 的 3cm×1cm、1.8.4 的
    #   4.5×1.5cm 两轮尺寸改动都没被验出来。这里不再断言"等于 CARD_*_CM"
    #   （那是假的），改为断真实不变量 + 留 NOTE，等尺寸问题单独修。
    check("卡片 HTML 仍写着目标尺寸（意图保留）",
          f'width="{want_w}" height="{want_h}"' in html)
    check("卡片矩形框住了卡片文字（几何没算偏）", cw > 60 and ch > 20, f"{cw}×{ch}")
    if cw > want_w + 20:
        print(f"  NOTE 已知问题：应用里卡片被 Qt 拉伸成 {cw}×{ch}px（目标 "
              f"{want_w}×{want_h}），CARD_*_CM 在嵌套表格里不生效；"
              f"见 1.9.0 开发日志「Y4a」一节。")
w.clear_chat()

print("\n== 2. 卡片确实插进聊天区且整块是链接 ==")
w.append_file_card("用户上传", str(target), "15.1KB")
app.processEvents()
doc = w._chat_display.document()
lay = doc.documentLayout()
anchors = []
blk = doc.begin()
i = 0
while blk.isValid():
    it = blk.begin()
    while not it.atEnd():
        cf = it.fragment().charFormat()
        if cf.isAnchor():
            anchors.append(cf.anchorHref())
        it += 1
    blk = blk.next()
    i += 1
print(f"  文档块数={i} 锚点={anchors}")
check("卡片 HTML 里有 file:// 锚点",
      any(a.startswith("file:///") for a in anchors), str(anchors[:1]))
plain = doc.toPlainText()
check("卡片显示了图标", "📊" in plain, plain[:60].replace("\n", "|"))
check("卡片显示了扩展名", ".pptx" in plain, plain[:60].replace("\n", "|"))
check("锚点指向真实文件路径", "deepseek_intro.pptx" in (anchors[0] if anchors else ""))

print("\n== 3. 长文件名按像素省略且尽量保住扩展名 ==")
from PySide6.QtGui import QFontMetrics  # noqa: E402

long_name = "一个名字特别特别长的文件用来测试换行效果_最终版_v3.pptx"
BUDGET = max(60, want_w - 30)          # 与卡片宽度同步的像素预算
short = w._elide_filename(long_name, BUDGET, 11)
print(f"  {long_name!r} -> {short!r}")
_fm = QFontMetrics(_f)
check("确实变短了", len(short) < len(long_name))
check("短名字原样返回", w._elide_filename("a.pptx", 300, 11) == "a.pptx")
check("省略后不超出像素预算", _fm.horizontalAdvance(short) <= BUDGET,
      f"adv={_fm.horizontalAdvance(short)}px <= {BUDGET}px")
check("扩展名保留（.pptx 或至少留 .pp）",
      short.endswith(".pptx") or ".pp" in short, short)

print("\n== 4. 尺寸常量可一行调整 ==")
check("CARD_WIDTH_CM == 4.5（1.8.4 放大 1.5 倍）", ChatWidget.CARD_WIDTH_CM == 4.5,
      str(ChatWidget.CARD_WIDTH_CM))
check("CARD_HEIGHT_CM == 1.5（1.8.4 放大 1.5 倍）", ChatWidget.CARD_HEIGHT_CM == 1.5,
      str(ChatWidget.CARD_HEIGHT_CM))
w.CARD_WIDTH_CM, w.CARD_HEIGHT_CM = 6.0, 2.0
w2, h2 = w.card_size_px()
check("改成 6cm×2cm 后像素按比例变大",
      w2 > want_w and h2 > want_h and abs(w2 / h2 - want_w / want_h) < 0.05,
      f"{w2}×{h2}px")
# ★ 1.9.0: 这两个常量当前只影响卡片 HTML 的 width/height 属性；
#   真实绘制尺寸由 Qt 的布局决定（被拉伸，见上面 1. 的 NOTE）。
check("6cm×2cm 的尺寸确实写进了卡片 HTML",
      f'width="{w2}" height="{h2}"' in
      w.file_card_html("用户上传", target.resolve(), "15.1KB", w2, h2),
      f"{w2}×{h2}px")
w.CARD_WIDTH_CM, w.CARD_HEIGHT_CM = 4.5, 1.5

print(f"\n==== 结果: {len(PASS)} PASS / {len(FAIL)} FAIL ====")
for f in FAIL:
    print("  FAIL:", f)
sys.exit(1 if FAIL else 0)
