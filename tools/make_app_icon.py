# -*- coding: utf-8 -*-
"""★★ 1.9.4：**生成程序图标** `data/icon.ico`（exe / 任务栏 / 引导窗口 / 托盘 共用）。

## 为什么需要这个脚本（而不是随手塞一张图）
用户 2026-10-19 的原话：
    "启动 exe 的图标换成托盘一致的图标，引导程序任务栏图标一样，
     py 原本的图标太难看了，最好尽量都优化成小托盘一样的。增加辨识度。"
以及后来补的一句：给了 550×474 的原图，"做完这个我再一起测"。

而以前的实际情况是：
    · exe 是 **PyInstaller 默认图标**（白色小方块）→ 最难看；
    · 引导窗口（QDialog）在 MainWindow 之前弹出，那时还没 `setWindowIcon`
      → 任务栏是 Qt 默认图标；
    · 托盘用的是 `data/桌宠形象/<角色>/ico.ico`（只有 64，且按角色变）。

现在统一成：**`data/icon.ico` 一份**，
    · `PRTS.spec` 把它嵌进 exe（`EXE(icon=...)`）→ 资源管理器/开始菜单/任务栏；
    · `main.py` 在 QApplication 一建好就 `setWindowIcon` → 引导窗口与所有对话框；
    · 托盘的第一候选本来就是 `data/icon.ico` → 三处天然一致。

## 图标从哪来（**不是**把 64 放大）
源图是角色立绘 `data/桌宠形象/铃兰/头像.jpg`（550×474，真实分辨率足够做 256）：
  1. **抠掉白底**（从四边 flood fill，只吃与边缘连通的白，不会吃掉眼白/高光）；
  2. 边缘羽化 0.6px（小尺寸下边缘才不会有白边）；
  3. 按 `CROP` 裁构图 —— 这个框是照原来那个 64px 托盘图标选的，
     所以 16/32/48 看起来**和以前的托盘图标一致**，只是更清楚；
  4. 逐尺寸重采样，**逐帧**写进 ico（Pillow 的 `save(sizes=...)` 只用一张图
     缩放所有帧，会把 16px 做糊，所以这里自己拼 ICO 容器）。

## 用法
    python tools/make_app_icon.py            # 生成 data/icon.ico
    python tools/make_app_icon.py --check    # 只检查现状（给守卫用）

## 换形象 / 换素材时
改 `SOURCE` 与 `CROP` 再跑一次；`--check` 会核对尺寸齐不齐。
"""
from __future__ import annotations

import argparse
import sys
from collections import deque
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageFilter

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "data" / "桌宠形象" / "铃兰" / "头像.jpg"
# ★ 小尺寸（16/24/32/48/64）另有更靠谱的来源：角色自带的 `ico.ico` ——
#   那 4 帧是**按像素手工调过**的，比"从 430px 立绘缩到 32"更清楚。
#   大尺寸（128/256）反过来：64 放大必糊，所以用立绘重采样。
NATIVE_ICO = ROOT / "data" / "桌宠形象" / "铃兰" / "ico.ico"
TARGET = ROOT / "data" / "icon.ico"

# 构图：照原来那个 64px 托盘图标选的（头 + 肩，耳朵贴近上沿）
CROP = (60, 0, 490, 430)

# 任务栏 32 / 桌面快捷方式 48 / Alt+Tab 32 / 菜单 16 / 大图标 256
SIZES = (16, 24, 32, 48, 64, 128, 256)
# ★ 所有尺寸都从"抠好白底的立绘母图"重采样 —— **不混用**角色自带 ico 的帧：
#   实测那几帧的背景是**不透明白色**，在深色任务栏上会显示一个白框
#   （16/32/64 都验过像素）。统一来源也保证 16→256 过渡时不会"跳一下"。
NATIVE_SIZES: tuple[int, ...] = ()


def _cutout_white(im: Image.Image) -> Image.Image:
    """把**与边缘连通的白**变成透明（角色内部的白色保持不动）。"""
    im = im.convert("RGBA")
    w, h = im.size
    px = im.load()

    def white(p):
        return p[0] > 235 and p[1] > 235 and p[2] > 235

    seen = bytearray(w * h)
    q: deque = deque()
    for x in range(w):
        for y in (0, h - 1):
            if white(px[x, y]):
                q.append((x, y)); seen[y * w + x] = 1
    for y in range(h):
        for x in (0, w - 1):
            if white(px[x, y]) and not seen[y * w + x]:
                q.append((x, y)); seen[y * w + x] = 1
    while q:
        x, y = q.popleft()
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nx, ny = x + dx, y + dy
            if 0 <= nx < w and 0 <= ny < h and not seen[ny * w + nx] and white(px[nx, ny]):
                seen[ny * w + nx] = 1
                q.append((nx, ny))
    for y in range(h):
        for x in range(w):
            if seen[y * w + x]:
                r, g, b, _ = px[x, y]
                px[x, y] = (r, g, b, 0)
    im.putalpha(im.getchannel("A").filter(ImageFilter.GaussianBlur(0.6)))
    return im


def _master() -> Image.Image:
    """裁好、抠好、补成正方形的高分辨率母图。"""
    im = _cutout_white(Image.open(SOURCE).convert("RGB"))
    c = im.crop(CROP)
    side = max(c.size)
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    canvas.paste(c, ((side - c.width) // 2, (side - c.height) // 2), c)
    return canvas


def _ico_sizes(im: Image.Image) -> set[int]:
    """PIL 报的 sizes 有的是 int，有的是 (w, h) 元组 —— 统一成边长。"""
    raw = (getattr(im, "info", {}) or {}).get("sizes") or {im.size}
    return {s[0] if isinstance(s, (tuple, list)) else int(s) for s in raw}


def _native_frames() -> dict[int, Image.Image]:
    """从角色自带的 `ico.ico` 里取**原生帧**（没有的尺寸就从最近的缩）。"""
    if not NATIVE_ICO.is_file():
        return {}
    have = _ico_sizes(Image.open(NATIVE_ICO))
    out: dict[int, Image.Image] = {}
    for s in NATIVE_SIZES:
        near = s if s in have else min(have, key=lambda x: abs(x - s))
        im = Image.open(NATIVE_ICO)
        im.size = (near, near)                 # PIL: 指定读哪一帧
        im = im.convert("RGBA")
        out[s] = im if near == s else im.resize((s, s), Image.LANCZOS)
    return out


def build(check_only: bool = False) -> int:
    if not SOURCE.is_file():
        print(f"[错误] 源图不存在: {SOURCE}")
        return 1

    if check_only:
        if not TARGET.is_file():
            print("[缺] data/icon.ico 不存在 → python tools/make_app_icon.py")
            return 1
        im = Image.open(TARGET)
        got = sorted(_ico_sizes(im))
        ok = set(SIZES).issubset(set(got))
        print(f"[{'OK' if ok else '缺'}] data/icon.ico {TARGET.stat().st_size} 字节，"
              f"尺寸 {got}（期望包含 {list(SIZES)}）")
        return 0 if ok else 1

    master = _master()
    frames = {s: master.resize((s, s), Image.LANCZOS) for s in SIZES}
    # ★ 小尺寸用原生帧盖掉（更清楚）；缺 Native ico 时自动退回立绘重采样
    native = _native_frames()
    frames.update({s: im for s, im in native.items() if s in frames})

    # ★ 逐帧写 ICO（Pillow 的 save(sizes=...) 会用同一张图缩所有尺寸 → 16px 会糊）
    blobs: list[bytes] = []
    for s in SIZES:
        buf = BytesIO()
        frames[s].save(buf, format="PNG")
        blobs.append(buf.getvalue())

    count = len(SIZES)
    header = (0).to_bytes(2, "little") + (1).to_bytes(2, "little") + count.to_bytes(2, "little")
    offset = 6 + 16 * count
    entries, data = b"", b""
    for s, blob in zip(SIZES, blobs):
        dim = 0 if s >= 256 else s
        entries += bytes([dim, dim, 0, 0]) + (1).to_bytes(2, "little") \
            + (32).to_bytes(2, "little") + len(blob).to_bytes(4, "little") \
            + offset.to_bytes(4, "little")
        data += blob
        offset += len(blob)
    TARGET.write_bytes(header + entries + data)

    print(f"[OK] 写出 {TARGET.relative_to(ROOT)}：{TARGET.stat().st_size} 字节，"
          f"尺寸 {list(SIZES)}（源: {SOURCE.relative_to(ROOT)}，裁剪 {CROP}）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="生成 data/icon.ico（exe/任务栏/托盘共用）")
    ap.add_argument("--check", action="store_true", help="只检查，不写文件")
    return build(ap.parse_args().check)


if __name__ == "__main__":
    sys.exit(main())
