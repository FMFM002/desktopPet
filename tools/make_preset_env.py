# -*- coding: utf-8 -*-
"""★★ 1.9.4：**出厂预设密钥生成器** —— 生成随包发布的 `.env.preset`。

## 用户拍板（2026-10-05，原话）
    "c。这部分key作为出厂默认预设。熟人使用的小项目，不担心被刷爆。"

背景：用户真机测试时问"天气和搜索，终末地的key，可以用原本的api，反正免费，支持多并发"，
我给了 A/B/C/D 四个选项（A 技术上不行 · B 改走 LLM · **C 内置免费 KEY** · D 其它），
用户**选了 C**。

## 预设什么、不预设什么（这条线很重要）
  ✅ **预设**（辅助服务，注册麻烦、但额度够熟人用）：
        WEATHER_API_HOST / WEATHER_API_KEY      （和风天气）
        TAVILY_API_KEY / TAVILY_API_URL         （联网搜索）
        ENDFIELD_API_KEY / ENDFIELD_API_BASEURL （终末地）
        COMFYUI_HOST / COMFYUI_PORT             （通用默认，非个人）
        PRTS_PET_COMPOSITED=0                   （★ 必须保持 0 的那个坑，见 .env.example）
  ❌ **不预设**：
        DEEPSEEK_API_KEY —— **引导就是问它**，而且它是要花钱的那个；
        用户自己填一次即可（写进他自己的 .env）。
        PIXIV_REFRESH_TOKEN —— 会过期，预设了也是废的。
        PIXIV_PROXY / COMFYUI_PATH —— **机器相关**（绝对路径、本机端口），
        预设到别人机器上是错的。

## 与 `.env` 的关系（发布时的红黑线）
    `.env`        用户自己的运行文件 —— **绝不随包**（可能含他自己的覆盖与密钥）
    `.env.preset` 出厂预设      —— **故意随包**（用户拍板，见上）
  程序**首次运行**没有 `.env` 时，会从 `.env.preset` 复制一份出来。

## 用法
    python tools/make_preset_env.py            # 从 .env 生成 .env.preset
    python tools/make_preset_env.py --check    # 只检查是否与 .env 一致（回归用）
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / ".env"
DEST = ROOT / ".env.preset"

# ★ 白名单：**只挑这些键**进预设。要加新的，必须在这里写明理由。
PRESET_KEYS = {
    "WEATHER_API_HOST": "和风天气的专属 Host（注册后控制台可见）",
    "WEATHER_API_KEY": "和风天气 KEY",
    "TAVILY_API_KEY": "联网搜索 KEY",
    "TAVILY_API_URL": "联网搜索接口（一般不用改）",
    "ENDFIELD_API_KEY": "终末地 KEY",
    "ENDFIELD_API_BASEURL": "终末地接口",
    "COMFYUI_HOST": "ComfyUI 主机（通用默认）",
    "COMFYUI_PORT": "ComfyUI 端口（通用默认）",
    "PRTS_PET_COMPOSITED": "★ 必须保持 0（设 1 会让形象窗口永久不可见，见 .env.example）",
}

_HEADER = """# ══════════════════════════════════════════════════════════════
# 铃兰桌宠 —— **出厂预设的环境变量**（随包发布，程序首启会复制成 .env）
#
# ★ 这份文件是**故意进版本库/随包**的（用户 2026-10-05 拍板"选 C"：
#   "这部分key作为出厂默认预设。熟人使用的小项目，不担心被刷爆。"）。
#   目的：熟人拿到就能直接用**天气 / 联网搜索 / 终末地**，
#   不必自己去注册这三个服务。
#
# ★ **不含 LLM 的 Key** —— 那个每个用户自己填（首次启动的配置引导会问），
#   填好后写进**他自己的** `.env`（不进版本库）。
#
# ★ 由 `python tools/make_preset_env.py` 从开发机的 `.env` 里**挑白名单键**生成，
#   不手抄（手抄迟早漏/写错）。要加键请改那个脚本里的 PRESET_KEYS 并写明理由。
# ══════════════════════════════════════════════════════════════
"""


def _read_env(path: Path) -> dict:
    out = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, _, v = s.partition("=")
        out[k.strip()] = v.strip()
    return out


def build() -> str:
    env = _read_env(SRC)
    lines = [_HEADER.rstrip(), ""]
    for k, why in PRESET_KEYS.items():
        v = env.get(k, "")
        lines.append(f"# {why}")
        lines.append(f"{k}={v}")
        lines.append("")
    lines.append("# ── 以下这些**故意不预设**（写了原因，别照抄进来）──")
    lines.append("# DEEPSEEK_API_KEY    —— 引导会问用户；而且它是要花钱的那个")
    lines.append("# PIXIV_REFRESH_TOKEN —— 会过期，预设也是废的")
    lines.append("# PIXIV_PROXY         —— 机器相关（本机代理端口）")
    lines.append("# COMFYUI_PATH        —— 机器相关（绝对路径）")
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    text = build()
    if "--check" in sys.argv:
        if not DEST.exists():
            print(f"  FAIL  {DEST.name} 不存在（跑 `python tools/make_preset_env.py`）")
            return 1
        if DEST.read_text(encoding="utf-8") != text:
            print(f"  FAIL  {DEST.name} 与开发机 .env 不一致 —— 改了 .env 就重跑生成器")
            return 1
        print(f"  PASS  {DEST.name} 与 .env 一致（{len(PRESET_KEYS)} 个预设键）")
        return 0
    DEST.write_text(text, encoding="utf-8")
    _n = sum(1 for k in PRESET_KEYS if _read_env(SRC).get(k))
    print(f"  已生成 {DEST.name}（{len(PRESET_KEYS)} 个键，其中 {_n} 个有值）")
    print("  ★ 不含 DEEPSEEK_API_KEY（引导会问）；不含机器相关项")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
