# -*- coding: utf-8 -*-
"""★★ 1.9.4：**生成程序图标** `data/icon.ico`（exe / 任务栏 / 托盘 共用同一个）。

## 为什么需要这个脚本（而不是随手塞一张图）
用户 2026-10-19 的原话：
    "启动 exe 的图标换成托盘一致的图标，引导程序任务栏图标一样，
     py 原本的图标太难看了，最好尽量都优化成小托盘一样的。增加辨识度。"

也就是说：**exe 文件图标 / 任务栏 / 引导窗口 / 托盘 要长得一样**。
而以前的实际情况是：
    · exe 用的是 **PyInstaller 默认图标**（那个白色小方块）→ 最难看；
    · 引导窗口（QDialog）在 MainWindow 之前弹出，那时还没 `setWindowIcon`
      → 任务栏是 Qt 默认图标；
    · 托盘用的是 `data/桌宠形象/<角色>/ico.ico`（按角色变）。

现在统一成：**`data/icon.ico` 一份**（取材自出厂默认形象 铃兰 的 ico），
    · `PRTS.spec` 把它嵌进 exe（`EXE(icon=...)`）→ 资源管理器/开始菜单/任务栏；
    · `main.py` 在 QApplication 一建好就 `setWindowIcon` → 引导窗口与所有对话框；
    · 托盘的第一候选本来就是 `data/icon.ico` → 三处天然一致。

## 用法
    python tools/make_app_icon.py            # 生成 data/icon.ico
    python tools/make_app_icon.py --check    # 只检查现状（给守卫用）

## 为什么只有 16/24/32/48/64 五个尺寸
源图（角色 ico）本身最大就是 64。**往上放大只会变糊**，所以不假装有 256：
Windows 在"大图标"视图里会用 64 平滑放大，观感与"塞一张假 256"一致或更好。
★ 哪天有了高分辨率立绘，把 `SOURCE` 指向它并放宽 `SIZES` 即可。
"""
from __future__ import annotations

import argparse
import sys
from io import BytesIO
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "data" / "桌宠形象" / "铃兰" / "ico.ico"
TARGET = ROOT / "data" / "icon.ico"

# 按重要性排：Windows 任务栏 32、桌面快捷方式 48、Alt+Tab 32、菜单 16
SIZES = (16, 24, 32, 48, 64)


def _ico_sizes(im: Image.Image) -> set[int]:
    """PIL 报的 sizes 有的是 int，有的是 (w, h) 元组 —— 统一成边长。"""
    raw = (getattr(im, "info", {}) or {}).get("sizes") or {im.size}
    return {s[0] if isinstance(s, (tuple, list)) else int(s) for s in raw}


def _frames(src: Path) -> dict[int, Image.Image]:
    """从源 ico 里**按原尺寸**取出各帧（不让 PIL 用一张图缩放出全部尺寸）。"""
    im = Image.open(src)
    have = _ico_sizes(im)
    out: dict[int, Image.Image] = {}
    for s in SIZES:
        if s in have:
            im2 = Image.open(src)
            im2.size = (s, s)                  # PIL: 指定要读哪一帧
            out[s] = im2.convert("RGBA")
        else:
            # 缺的尺寸从**最近的已有尺寸**缩（比从最大图缩更清楚）
            near = min(have, key=lambda x: abs(x - s))
            im2 = Image.open(src)
            im2.size = (near, near)
            out[s] = im2.convert("RGBA").resize((s, s), Image.LANCZOS)
    return out


def build(check_only: bool = False) -> int:
    if not SOURCE.is_file():
        print(f"[错误] 源图标不存在: {SOURCE}")
        return 1
    frames = _frames(SOURCE)
    sizes = sorted(frames)

    if check_only:
        if not TARGET.is_file():
            print("[缺] data/icon.ico 不存在 → python tools/make_app_icon.py")
            return 1
        im = Image.open(TARGET)
        got = sorted(_ico_sizes(im))
        ok = set(sizes).issubset(set(got))
        print(f"[{'OK' if ok else '缺'}] data/icon.ico {TARGET.stat().st_size} 字节，"
              f"尺寸 {got}（期望包含 {sizes}）")
        return 0 if ok else 1

    # ★ 关键：先存 256 的底座会让小尺寸全变糊 —— 这里用**逐帧**拼接，
    #   保证 16/32/48 是源图的原生像素。PIL 的 ICO 保存不支持"每帧不同"，
    #   所以直接按 ICO 格式写（头 + 目录 + 每帧的 PNG 数据）。
    blobs: list[bytes] = []
    for s in sizes:
        buf = BytesIO()
        frames[s].save(buf, format="PNG")
        blobs.append(buf.getvalue())

    count = len(sizes)
    header = (0).to_bytes(2, "little") + (1).to_bytes(2, "little") + count.to_bytes(2, "little")
    offset = 6 + 16 * count
    entries, data = b"", b""
    for s, blob in zip(sizes, blobs):
        dim = 0 if s >= 256 else s
        entries += bytes([dim, dim, 0, 0]) + (1).to_bytes(2, "little") \
            + (32).to_bytes(2, "little") + len(blob).to_bytes(4, "little") \
            + offset.to_bytes(4, "little")
        data += blob
        offset += len(blob)
    TARGET.write_bytes(header + entries + data)

    print(f"[OK] 写出 {TARGET.relative_to(ROOT)}：{TARGET.stat().st_size} 字节，"
          f"尺寸 {sizes}（源: {SOURCE.relative_to(ROOT)}）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="生成 data/icon.ico（exe/任务栏/托盘共用）")
    ap.add_argument("--check", action="store_true", help="只检查，不写文件")
    return build(ap.parse_args().check)


if __name__ == "__main__":
    sys.exit(main())
