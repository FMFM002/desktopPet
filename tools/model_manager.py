# -*- coding: utf-8 -*-
"""★★ 1.9.4：**模型管理器**（命令行）—— 把散落各处的模型**集中**到 `data/models/`。

用户的要求原话："这些模型散落在文件各个地方，希望集中管理。**和 aria 一样作为工具**。"

## 与 `fetch_aria2.py` 的关系（同一个模式）
`tools/fetch_aria2.py` 管的是 **aria2c 二进制**（`data/tools/aria2c.exe`）：
有目标路径、来源元数据、sha256、`verify()`、`install()`。
本文件管的是**模型资产**，用同一套风格：**注册表（唯一真理）+ 状态 + 搬运 + 校验**。
区别：aria2 是**二进制工具**，模型是**数据** —— 所以模型统一进 `data/models/`，
aria2c 仍留在 `data/tools/`（不硬塞到一起）。

## ⚠ 为什么"搬运"要单独一条命令，而不是程序一启动就自动搬
`data/models/genie` 那个是 **1.8 GB**。自动搬会在启动时卡住用户、还可能搬到一半断电。
所以：**解析路径是自动的**（`src/core/models.py` 优先新位置、回落旧位置），
**真正移动文件必须由人显式触发**（本命令）。

## 用法
    python tools/model_manager.py status            # 看每个模型在不在、在哪
    python tools/model_manager.py status --json     # 给安装程序用
    python tools/model_manager.py migrate --dry-run # 先看会搬什么
    python tools/model_manager.py migrate           # 真搬（旧位置 → data/models/）
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.core.models import (MODELS, MODELS_ROOT, ensure_models_root,  # noqa: E402
                             resolve_dir, status_all, total_size_hint)


def _fmt_mb(n: int) -> str:
    return "—" if n < 0 else f"{n / 1024 / 1024:.1f} MB"


def cmd_status(as_json: bool) -> int:
    rows = status_all()
    if as_json:
        # ★ 给**安装程序**用：它据此问用户"要不要一起下载/安装这些模型"
        print(json.dumps({
            "models_root": str(MODELS_ROOT),
            "total_size_hint": total_size_hint(),
            "models": rows,
        }, ensure_ascii=False, indent=2))
        return 0

    print(f"  模型统一根目录: {MODELS_ROOT}")
    print(f"  全部都要的话  : {total_size_hint()}")
    print()
    for r in rows:
        mark = {True: "✅ 已就位", False: "❌ 缺失"}[r["present"]]
        where = {"canonical": "规范位置", "legacy": "**旧位置（建议迁移）**",
                 "missing": "—"}[r["where"]]
        print(f"  {mark}  {r['label']}")
        print(f"          位置   : {r['path'] or '（没有）'}  [{where}]")
        print(f"          实际大小: {_fmt_mb(r['size_bytes'])}   预期 {r['size_hint']}")
        print(f"          少了它会: 没有「{r['feature']}」")
        if r["suggest_migrate"]:
            print(f"          → 建议搬到: {r['canonical_rel']}"
                  f"（python tools/model_manager.py migrate）")
    print()
    _n_legacy = sum(1 for r in rows if r["suggest_migrate"])
    if _n_legacy:
        print(f"  ★ 有 {_n_legacy} 个模型还在**旧位置**，建议迁移以集中管理")
    return 0


def cmd_migrate(dry: bool, only: str = "") -> int:
    """把还在旧位置的模型搬进 `data/models/`。

    ★ 用 `shutil.move`（跨盘也能用），**先搬进临时名再改名** —— 这样半路失败时
      不会留下"看起来像完整模型"的半成品目录（那会让 `resolve` 误判为已就位）。
    """
    ensure_models_root()
    rows = [r for r in status_all() if r["suggest_migrate"]]
    if only:
        rows = [r for r in rows if r["key"] == only]
    if not rows:
        print("  没有需要迁移的模型（都已集中）")
        return 0

    print(f"  将迁移 {len(rows)} 个模型 → {MODELS_ROOT}")
    rc = 0
    for r in rows:
        src = Path(r["path"])
        dst = ROOT / r["canonical_rel"]
        print(f"\n  · {r['label']}")
        print(f"      {src}  →  {dst}")
        if dst.exists():
            print("      [跳过] 目标已存在（不覆盖，避免弄坏现有模型）")
            continue
        if dry:
            print("      [--dry-run] 没有真搬")
            continue
        tmp = dst.with_name(dst.name + ".migrating")
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            if tmp.exists():
                shutil.rmtree(tmp, ignore_errors=True)
            shutil.move(str(src), str(tmp))     # ① 先搬到临时名
            tmp.rename(dst)                     # ② 完成后再改名（原子性更好）
            print("      ✅ 已迁移")
        except Exception as e:
            print(f"      ❌ 迁移失败: {type(e).__name__}: {e}")
            print(f"         （源仍在 {src}；残留临时目录 {tmp} 可手动删）")
            rc = 1
    print()
    if not dry:
        print("  迁移后请跑一次回归确认没搬坏：& tools\\dev_tests\\_sweep190.ps1")
    return rc


def cmd_verify() -> int:
    """核对：注册表里每个模型都能不能解析到。缺失不算错误（是可选项），只报告。"""
    bad = 0
    for r in status_all():
        p, where = resolve_dir(r["key"])
        ok = p is not None
        print(f"  {'✅' if ok else '❌'} {r['key']:<10} {r['feature']:<14} "
              f"{'→ ' + str(p) if ok else '（缺失：该功能不可用，其余照常）'}")
        if not ok:
            bad += 1
    print(f"\n  缺失 {bad} / {len(MODELS)} 个 —— "
          f"★ 缺失**不影响程序启动**（1.9.4 起是可选能力，见 _test_standalone_194.py）")
    return 0        # ★ 缺失不返回错误码：这是"可选能力没有"，不是故障


def main() -> int:
    ap = argparse.ArgumentParser(description="PTRTS 模型管理器（集中管理 data/models/）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("status", help="看每个模型在不在、在哪")
    s.add_argument("--json", action="store_true", help="输出 JSON（给安装程序用）")
    m = sub.add_parser("migrate", help="把旧位置的模型搬进 data/models/")
    m.add_argument("--dry-run", action="store_true")
    m.add_argument("--only", default="", help="只迁移某一个（key）")
    sub.add_parser("verify", help="核对能否解析到每个模型")
    a = ap.parse_args()
    if a.cmd == "status":
        return cmd_status(a.json)
    if a.cmd == "migrate":
        return cmd_migrate(a.dry_run, a.only)
    return cmd_verify()


if __name__ == "__main__":
    raise SystemExit(main())
