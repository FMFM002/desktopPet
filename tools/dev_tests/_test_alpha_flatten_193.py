# -*- coding: utf-8 -*-
"""★ 1.9.3 第 22 轮：**透明图必须先压底色再转 RGB**（三处，一处都不能漏）。

【真机证据】用户反馈"做出来的 PPT 效果不好"，导出一看：立绘页两侧全是
黑白横条。查明是透明 PNG 被 `convert("RGB")` 压平 ——
`theresa_prts1.png` 是 1496×1496 的 RGBA 立绘，**四角 alpha=0 而透明区的 RGB 是垃圾值**，
直接 convert("RGB") 就等于把"看不见的底"当图画了出来。

同一类写法在仓库里还有两处（都已一并修）：
  · `tools/file_view.py::_open_image`  → 喂给**视觉模型**看的图
  · `src/core/session.py::_image_to_data_uri` → 用户上传的图**转给对话模型**的 data URI
两处都会把透明 PNG 变成"黑底图"，让模型看到/描述错东西。

本套件构造一张"透明区 RGB=白（垃圾值）、中间一块红"的 RGBA 图，
到三处各走一遍，断言**四角是白底**、**中间内容还在**。
"""
import base64
import io
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE))
os.environ.setdefault("DEEPSEEK_API_KEY", "test")

from PIL import Image                                   # noqa: E402

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))


def corners(im):
    im = im.convert("RGB")
    w, h = im.size
    return [im.getpixel(p) for p in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1))]


def all_white(pxs):
    return all(all(c >= 240 for c in px) for px in pxs)


def is_red(px):
    return px[0] > 150 and px[1] < 90 and px[2] < 90


# ── 造样本：透明区 RGB 故意设成垃圾值（白），中间一块不透明的红 ──
TMP = BASE / "临时文件" / "_scratch_dev" / "alpha_flatten_probe"
TMP.mkdir(parents=True, exist_ok=True)
SRC = TMP / "transparent_probe.png"
_probe = Image.new("RGBA", (400, 400), (255, 255, 255, 0))    # 透明 + 垃圾 RGB
_probe.paste((200, 30, 30, 255), (140, 140, 260, 260))
_probe.save(SRC)
check("样本已生成（全透明底 + 中间红块）",
      SRC.exists() and Image.open(SRC).mode == "RGBA")

print("\n== 1. tools/pptx_tool.py：_load_image + _crop_to（做 PPT 用）==")
import tools.pptx_tool as PT                                     # noqa: E402
_img = PT._load_image(SRC)
check("★_load_image 保留 alpha（不再在这里拍平成 RGB）",
      _img is not None and "A" in _img.getbands(), str(getattr(_img, "mode", None)))
_crop = TMP / "crop.jpg"
_cropped = PT._crop_to(SRC, _crop, 1.0, _img)
check("裁剪产物落盘", _cropped is not None and _crop.exists())
if _crop.exists():
    with Image.open(_crop) as _im:
        _c = corners(_im)
        check("★四角是白底（不是黑）", all_white(_c), str(_c))
        check("★中间红块还在", is_red(_im.convert("RGB").getpixel((200, 200))))

print("\n== 2. tools/file_view.py：_open_image（喂给视觉模型看的图）==")
import tools.file_view as FV                                     # noqa: E402
_fv = FV._open_image(SRC)
_c2 = corners(_fv)
check("★四角是白底（视觉模型看到的是白底立绘，不是黑底）", all_white(_c2), str(_c2))
check("★中间内容还在", is_red(_fv.convert("RGB").getpixel((200, 200))))

print("\n== 3. src/core/session.py：_image_to_data_uri（用户上传图给对话模型）==")
from src.core.session import ChatSession                          # noqa: E402
_uri = ChatSession._image_to_data_uri(str(SRC))
check("返回 data:image/jpeg;base64", _uri.startswith("data:image/jpeg;base64,"),
      _uri[:40])
_raw = base64.b64decode(_uri.split(",", 1)[1])
with Image.open(io.BytesIO(_raw)) as _im3:
    _c3 = corners(_im3)
    check("★四角是白底（模型看到的是白底图）", all_white(_c3), str(_c3))
    check("★中间内容还在", is_red(_im3.convert("RGB").getpixel(
        (_im3.width // 2, _im3.height // 2))))

print("\n== 4. 不透明图不受影响（别把正常流程改坏）==")
OP = TMP / "opaque_probe.png"
Image.new("RGB", (300, 300), (10, 120, 200)).save(OP)
_fv2 = FV._open_image(OP)
check("★普通 RGB 图原样返回（四角是原色，没被涂白）",
      all(abs(c[0] - 10) < 6 and abs(c[1] - 120) < 6 for c in corners(_fv2)),
      str(corners(_fv2)))
_p2 = PT._load_image(OP)
check("★普通图也能读", _p2 is not None and _p2.size == (300, 300))

print("\n== 5. 坏图仍然被如实拦下（校验逻辑没被改坏）==")
BAD = TMP / "broken.png"
BAD.write_bytes(b"not an image at all")
check("★坏图 → None（调用方按坏图跳过）", PT._load_image(BAD) is None)
try:
    FV._open_image(BAD)
    _raised = False
except Exception:
    _raised = True
check("★file_view 对坏图仍会抛（上层有 try 兜住并如实报错）", _raised)

print(f"\n==== 结果: {len(PASS)} PASS / {len(FAIL)} FAIL ====")
for f in FAIL:
    print("  FAIL:", f)

# 全绿就把样本目录清掉（失败时留着，方便去看那几张图到底什么颜色）
if not FAIL:
    try:
        import shutil
        shutil.rmtree(TMP, ignore_errors=True)
    except Exception:
        pass

sys.exit(1 if FAIL else 0)
