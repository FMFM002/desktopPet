# -*- coding: utf-8 -*-
"""★ 1.9.4：**出厂配置生成器** —— 生成随包发布的 `config.default.json`。

## 为什么要"生成"而不是手写
`config.json` 有 100+ 个键，还在不断新增。手写一份出厂配置会**立刻过期**
（新键没人补）。所以出厂配置必须**从代码默认值生成**，这样：
  · 新增配置项时，重跑本脚本即可同步；
  · 出厂值与"程序没读文件时会用什么"**天然一致**，不会出现两套真理。

## 出厂 vs 个人（本脚本的取舍）
`config.json` 里有**个人的东西**（用户自己的人设、头像路径、界面颜色、窗口位置）。
出厂配置**只保留项目级结构**，个人项一律回到代码默认：

    个人项                        出厂值
    pet.personality_prompt    →  ""（空 = 用程序内置默认人设）
    ui.user_avatar_path       →  ""（无个人头像）
    ui.pet_position           →  [100, 100]
    ui.ui_bg_color 等配色      →  代码默认
    llm.api_key               →  不写进 config.json（密钥在 .env，见下）

    项目级结构（**保留**，不是个人信息）
    enabled_tools             →  全部工具（不然新用户没有工具可用）
    tool_policies             →  各工具的懒加载策略

## 用法
    python tools/make_default_config.py            # 生成/更新 config.default.json
    python tools/make_default_config.py --check    # 只检查是否与代码默认一致（CI/回归用）
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEST = ROOT / "config.default.json"

# ── 出厂要保留的"项目级结构"（从当前 config.json 抄，不是个人信息）──
#   enabled_tools / tool_policies 是"项目支持哪些工具"，与用户无关。
STRUCTURAL_FROM_CURRENT = ("enabled_tools", "tool_policies")

# ── 出厂时**必须**是这些值的个人项（防手滑把个人信息写进出厂文件）──
FORCED = {
    "pet.personality_prompt": "",
    "ui.user_avatar_path": "",
    "ui.pet_position": [100, 100],
}


def _flatten(o, pre=""):
    if isinstance(o, dict):
        for k, v in o.items():
            yield from _flatten(v, f"{pre}.{k}" if pre else k)
    else:
        yield pre, o


def build() -> dict:
    """生成出厂配置：**代码默认值** + 项目级结构。"""
    from src.utils.config import AppConfig

    # ★ 用工作区内目录当临时路径 —— DSH 沙箱**禁止写系统 temp**（踩过）。
    tmpd = ROOT / "临时文件" / "_scratch_dev" / "tmp_factory_gen"
    tmpd.mkdir(parents=True, exist_ok=True)
    tmp_path = tmpd / "config.json"
    if tmp_path.exists():
        tmp_path.unlink()

    cfg = AppConfig(config_path=tmp_path)
    cfg.save()                                   # 落到磁盘，拿到"程序会写出的默认值"
    data = json.loads(tmp_path.read_text(encoding="utf-8"))
    try:
        del tmp_path                             # 用一下就删，别留垃圾
    except Exception:
        pass

    # 项目级结构：从当前 config.json 取（它代表"本项目支持的工具与策略"）
    cur_path = ROOT / "config.json"
    if cur_path.exists():
        try:
            cur = json.loads(cur_path.read_text(encoding="utf-8"))
            for k in STRUCTURAL_FROM_CURRENT:
                if k in cur:
                    data[k] = cur[k]
        except Exception as e:
            print(f"  [warn] 读当前 config.json 失败（结构项用代码默认）: {e}")

    # 强制个人项为出厂值
    for dotted, val in FORCED.items():
        head, _, tail = dotted.partition(".")
        if head in data and isinstance(data[head], dict):
            data[head][tail] = val

    # 密钥绝不写出（万一代码默认里带了）
    if isinstance(data.get("llm"), dict):
        data["llm"].pop("api_key", None)
    if isinstance(data.get("vision"), dict):
        data["vision"].pop("api_key", None)
    return data


def main() -> int:
    check_only = "--check" in sys.argv
    data = build()
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"

    if check_only:
        if not DEST.exists():
            print(f"  FAIL  {DEST.name} 不存在（跑 `python tools/make_default_config.py` 生成）")
            return 1
        old = DEST.read_text(encoding="utf-8")
        if old != text:
            print(f"  FAIL  {DEST.name} 与代码默认值不一致 —— "
                  f"配置项有增减/默认值变了，请重跑生成器")
            return 1
        print(f"  PASS  {DEST.name} 与代码默认值一致（{len(list(_flatten(data)))} 个叶子键）")
        return 0

    DEST.write_text(text, encoding="utf-8")
    print(f"  已生成 {DEST.relative_to(ROOT)}（{len(text.encode('utf-8'))} 字节，"
          f"{len(list(_flatten(data)))} 个叶子键）")
    print("  ★ 出厂个人项：", ", ".join(f"{k}={v!r}" for k, v in FORCED.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
