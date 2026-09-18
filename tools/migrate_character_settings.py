#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把角色的 settings.json 从旧格式（伪标记文本）迁移到新格式（schema 2）。

★ 1.9.0 配套工具。做了三件事：
  1. 旧文件先备份成 `settings.json.bak190`（已存在就不覆盖，避免盖掉更早的备份）
  2. 按 src/core/character_settings.py 的规则转换并写回（缩进 2、保留中文）
  3. **幂等**：已经是 schema 2 的目录自动跳过

用法:
    python tools/migrate_character_settings.py            # 迁移 data/桌宠形象 下全部角色
    python tools/migrate_character_settings.py --dry-run  # 只看会改什么，不写盘
    python tools/migrate_character_settings.py --dir <角色目录>
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.core import character_settings as cs  # noqa: E402

CHARACTERS_DIR = ROOT / "data" / "桌宠形象"
BACKUP_SUFFIX = ".bak190"


def migrate_one(char_dir: Path, dry_run: bool = False) -> str:
    f = char_dir / "settings.json"
    if not f.exists():
        return f"{char_dir.name}: 没有 settings.json，跳过"
    try:
        raw = json.loads(f.read_text(encoding="utf-8"))
    except Exception as e:
        return f"{char_dir.name}: 读取失败（{e}），跳过"

    if cs.is_v2(raw):
        return f"{char_dir.name}: 已经是 schema 2，跳过"

    new = cs.migrate_old(raw)
    summary = (f"{char_dir.name}: 旧格式 → schema {cs.SCHEMA}  "
               f"(identity {len(new['性格']['identity'])} 项 / "
               f"输出控制 {sum(1 for k, v in new['输出控制'].items() if v not in (False, '', None))} 项开 / "
               f"行为控制 {len(new['行为控制'])} 项)")
    if dry_run:
        return summary + "   [dry-run 未写盘]"

    bak = f.with_suffix(f.suffix + BACKUP_SUFFIX)
    if not bak.exists():
        shutil.copy2(f, bak)
    cs.save(f, new)
    return summary + f"   已写盘（备份 {bak.name}）"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="", help="只处理某个角色目录")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if args.dir:
        dirs = [Path(args.dir)]
    else:
        dirs = sorted([d for d in CHARACTERS_DIR.iterdir() if d.is_dir()])
    if not dirs:
        print("没找到角色目录:", CHARACTERS_DIR)
        return 1
    for d in dirs:
        print("·", migrate_one(d, dry_run=args.dry_run))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
