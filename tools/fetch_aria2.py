#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把 BT 下载引擎（aria2c）装进项目 —— ★ 1.9.2

## 为什么要单独一个脚本
`data/tools/aria2c.exe` 是 11MB 第三方二进制，**没有进 git**（见 .gitignore 里的
说明）。这个脚本负责把它复原：下载 → **双重 sha256 校验** → 装到 data/tools/。

## ★ 为什么不用官方 aria2 发布包
实测（2026-09-18，本机）：
    官方 aria2-1.37.0-win-64bit-build1 是 mingw + **WinTLS** 构建，
    所有 HTTPS 请求都失败：
        [WinTLSContext.cc:188] errorCode=1
        Failed to initialize WinTLS context handle
    四种写法都试过（默认 / --check-certificate=false / --ca-certificate=… /
    --min-tls-version=TLSv1.2）全部失败。
    后果：https:// tracker 全部连不上 → BT 一个 peer 都找不到。

所以要装的是 **Motrix 自己维护的 OpenSSL 构建**
（repo=motrixapp/aria2, tag=v1.37.0-motrix.14, Libraries 里有 OpenSSL/3.5.8）。
Motrix 正是为此才自带 aria2 —— 它的 `scripts/engine.lock.json` 里连校验和都有，
本脚本的下载地址与两个 sha256 **直接读那份锁文件**，不自己猜。

## 用法
    python tools/fetch_aria2.py            # 缺了就装
    python tools/fetch_aria2.py --force    # 强制重装
    python tools/fetch_aria2.py --check    # 只校验现有文件

顺带还会准备 `data/tools/cacert.pem`（OpenSSL 版 aria2 没有 CA 包时
https tracker 会报 "unable to get local issuer certificate"）。
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = PROJECT_ROOT / "data" / "tools"
TARGET = TOOLS_DIR / "aria2c.exe" if sys.platform == "win32" else TOOLS_DIR / "aria2c"
SOURCE_NOTE = TOOLS_DIR / "aria2c.SOURCE.txt"

# ── 引擎来源（★ 照抄 Motrix 的 scripts/engine.lock.json）──
ENGINE = {
    "repo": "motrixapp/aria2",
    "tag": "v1.37.0-motrix.14",
    "file": "aria2c-1.37.0-motrix.14-win32-x64.zip",
    "bin": "aria2c.exe",
    "archiveSha256": "36334f45437c3e3aa6733825b0d93976b4121f91aee83b2ecfe4bc210b8398df",
    "binarySha256": "82e367164513a86eb5c52411fb77ad6cb2f6ca9766e6ddb8bd9526010f7fdf73",
}


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _write_source_note():
    TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    SOURCE_NOTE.write_text(
        "aria2c 二进制来源（★ 不是官方 mingw 构建）\n"
        "=========================================\n"
        "为什么不用官方版：官方 aria2-1.37.0-win-64bit-build1 是 mingw + WinTLS\n"
        "构建，在目标机器上所有 HTTPS 请求都失败：\n"
        "    [WinTLSContext.cc:188] Failed to initialize WinTLS context handle\n"
        "（默认 / --check-certificate=false / --ca-certificate / --min-tls-version\n"
        " 四种写法全试过，都不行）→ https:// tracker 全废、BT 找不到 peer。\n"
        "Motrix 正是为此自带 aria2 构建。\n\n"
        f"repo   : {ENGINE['repo']}\n"
        f"tag    : {ENGINE['tag']}\n"
        f"asset  : {ENGINE['file']}\n"
        f"url    : https://github.com/{ENGINE['repo']}/releases/download/"
        f"{ENGINE['tag']}/{ENGINE['file']}\n"
        f"archiveSha256: {ENGINE['archiveSha256']}\n"
        f"binarySha256 : {ENGINE['binarySha256']}\n"
        "（校验和取自参考项目 Motrix 的 scripts/engine.lock.json）\n\n"
        "复原办法：python tools/fetch_aria2.py\n",
        encoding="utf-8")


def _ensure_cacert() -> bool:
    """OpenSSL 版 aria2 需要 CA 证书包，否则 https tracker 握手失败。"""
    dst = TOOLS_DIR / "cacert.pem"
    if dst.exists() and dst.stat().st_size > 10000:
        return True
    for src in (Path(sys.prefix) / "Lib/site-packages/certifi/cacert.pem",
                Path(__file__).resolve().parent.parent / ".venv/Lib/"
                "site-packages/certifi/cacert.pem"):
        if src.exists():
            shutil.copy2(src, dst)
            print(f"✅ CA 证书包已就绪: {dst}")
            return True
    try:
        import certifi
        shutil.copy2(certifi.where(), dst)
        print(f"✅ CA 证书包已就绪（取自 certifi）: {dst}")
        return True
    except Exception as e:
        print(f"⚠ 拿不到 CA 证书包（https tracker 会握手失败）: {e}")
        print("  可手动放一份 PEM 到", dst)
        return False


def verify() -> bool:
    if not TARGET.exists():
        print("✗ 没装:", TARGET)
        return False
    got = _sha256(TARGET)
    ok = got == ENGINE["binarySha256"]
    print(("✅ " if ok else "✗ ") + f"{TARGET.name} sha256={got[:16]}… "
          f"(期望 {ENGINE['binarySha256'][:16]}…)")
    return ok


def install(force: bool = False) -> int:
    TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    _write_source_note()
    _ensure_cacert()
    if TARGET.exists() and not force and verify():
        print("已经装好了，不用重装（要强制重装加 --force）")
        return 0

    url = (f"https://github.com/{ENGINE['repo']}/releases/download/"
           f"{ENGINE['tag']}/{ENGINE['file']}")
    print("下载:", url)
    # 用项目自己的 download 工具（分段 + 断点续传），不另造一套
    sys.path.insert(0, str(PROJECT_ROOT))
    from tools import download_tool as DL
    r = DL.download_url(url, filename=ENGINE["file"], bucket="tools", max_mb=60)
    if not r.get("success"):
        print("✗ 下载失败:", r.get("error"))
        return 1
    zp = Path(r["file_path"])

    got = _sha256(zp)
    if got != ENGINE["archiveSha256"]:
        print(f"✗ 压缩包校验和不符：{got}\n  期望 {ENGINE['archiveSha256']}")
        zp.unlink(missing_ok=True)
        return 2
    print("✅ 压缩包校验通过")

    with zipfile.ZipFile(zp) as z:
        inner = [n for n in z.namelist() if n.lower().endswith(ENGINE["bin"])]
        if not inner:
            print("✗ 包内没有", ENGINE["bin"], z.namelist())
            return 3
        data = z.read(inner[0])
    bio = hashlib.sha256(data).hexdigest()
    if bio != ENGINE["binarySha256"]:
        print(f"✗ 二进制校验和不符：{bio}\n  期望 {ENGINE['binarySha256']}")
        return 4
    print("✅ 二进制校验通过")
    TARGET.write_bytes(data)
    zp.unlink(missing_ok=True)
    print(f"✅ 已安装 {TARGET}（{TARGET.stat().st_size} 字节）")
    return 0


if __name__ == "__main__":
    a = sys.argv[1:]
    if "--check" in a:
        _write_source_note()
        sys.exit(0 if verify() else 1)
    sys.exit(install(force="--force" in a))
