# -*- coding: utf-8 -*-
"""★★ 1.9.4：**发布装配器** —— 从工作区挑出"该发的东西"，放进一个干净的目录。

## 为什么需要它（而不是手动复制）
发布包必须**只含该发的**。手动复制一定会漏 —— 会把下面的东西一起发出去：
    · `.env`（**你的 API Key**）
    · `data/avatars/user_avatar.jpg`（**你的头像**）
    · `data/DBs` / `data/cache`（**你的聊天记录与记忆**）
    · `logs/`（**你的运行日志**）
    · `临时文件/`（我的草稿与调试图）
    · 测试临时目录、`__pycache__`、`.git`
**发出去就收不回来了** —— 所以这件事必须交给脚本，并且脚本要**逐条写明排除理由**。

## 与"发布形态"无关
不管最后是「便携 zip」还是「PyInstaller exe」还是「安装包」，
**都需要先有这么一个干净的目录**。所以本脚本不受那个决策影响。

## 用法
    python tools/build_release.py --dry-run          # 只看会挑哪些、总大小（**不写盘**）
    python tools/build_release.py                    # 真的装配到 dist/PTRTS-<版本>/
    python tools/build_release.py --include-models   # 连 whisper/easyocr 模型一起带（包会大 600MB+）
    python tools/build_release.py --keep-persona     # 保留角色人设（默认**清成通用**，见下）
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.version import APP_ID, __version__          # noqa: E402

# ══════════════════════════════════════════════════════════════
# 排除表：**(路径, 为什么不能发)** —— 每一条都要有理由
# ══════════════════════════════════════════════════════════════
EXCLUDE_DIRS = {
    ".git": "版本库本体（91MB，且发布应该只有代码快照）",
    ".idea": "IDE 配置，与用户无关",
    "临时文件": "我的草稿/探针/调试图，不是产品的一部分",
    "开发历史": "历史归档，不进发布包",
    "logs": "★你的运行日志（含聊天片段、路径）",
    "data/DBs": "★★你的聊天记录与长期记忆（数据库）",
    "data/cache": "★运行时缓存（含聊天图片预览）",
    "data/cookies": "★你的登录 cookie",
    # ★★ 1.9.4（我差点漏掉的**泄露点**）：一键开通 B站/P站 时，
    #   Playwright 会把**整个 Edge 浏览器 profile** 和导出的
    #   `bili_cookies.json` 写在这里 —— 那是**真实登录凭据 + 浏览器数据**，
    #   而且体积可能是几十上百 MB。**绝不能随包发出去。**
    "data/account_login": "★★浏览器 profile 与真实登录凭据（一键开通 B站/P站 时产生）",
    "data/downloads": "★你下载的文件",
    "data/桌宠私有data": "★★你的私有文件夹（工具能读写的地方）",
    "data/chat_clips": "★粘贴图的磁盘缓存",
    "tools/dev_tests": "★回归套件（开发用；用户不需要，还会被误当功能入口）",
    "data/whisper": "模型（465MB）—— 默认按需获取，见 --include-models",
    "data/models": "模型（easyocr 100MB）—— 同上",
    # ★★ 这才是发布包"为什么 1.9GB"的真凶：GENIE 的 **TTS 模型**藏在 src 里。
    #   代码（src/genie_tts/*.py，约 4.5MB）要随包；模型（assets/genie，约 1855MB）不该。
    "src/genie_tts/assets": "★★TTS 模型 1855MB（GENIE）—— 默认不带，见 --include-tts-models",
}
EXCLUDE_FILES = {
    # ★ `.env` = **用户自己的**运行文件（可能含他自己的 Key 与覆盖）→ 绝不随包。
    #   ★ 与之相对：`.env.preset` = **出厂预设**（用户 2026-10-05 拍板选 C：
    #     "这部分key作为出厂默认预设。熟人使用的小项目，不担心被刷爆。"）
    #     → **故意随包**，见下面 INCLUDE 里那一条。两者是**不同**的文件，别混。
    ".env": "★★用户自己的 .env（可能含他自己的 Key）—— 只发 .env.example 与 .env.preset",
    # ★★ 1.9.4（用户 2026-10-04 拍板）：**头像与角色人设都作为「出厂预设」随包发布** ——
    #   所以 `data/avatars/user_avatar.jpg` 与 `data/桌宠形象/*/settings.json` 里的
    #   `性格.free_text` **不再排除/不再清空**。
    #   注：`铃兰` 的人设文件里自己写着「明日方舟·铃兰」—— 这是**已记录的版权风险点**，
    #   用户知情并选择作为预设发布；若将来要公开分发，这是唯一需要重新评估的地方。
    "data/ui_state.json": "★你的窗口状态",
    "data/app_paths.json": "★你机器上的路径记录",
    "config.json": "★★你的个人配置；发布的是 config.default.json（出厂版）",
}

# 顶层要带的（文件/目录）
INCLUDE = [
    "main.py", "requirements.txt", "README.md",
    # ★★ 1.9.4：给"桌宠自己"看的运行手册 —— 系统提示词会指路，桌宠用 file_view 读它。
    #   便携版/exe 版都要带，否则模型回答"我怎么工作"时只能编。
    "桌宠自述.md",
    # ★ `.env.example` = 给用户看的模板；`.env.preset` = **出厂预设密钥**
    #   （用户 2026-10-05 拍板选 C，**故意随包**，让熟人直接用天气/搜索/终末地）
    ".env.example", ".env.preset", "config.default.json",
    "src", "tools", "data",
    # ★★ 1.9.4（收尾时发现）：这几份**根目录的文档**以前没进 INCLUDE，
    #   于是**没被打进 zip** —— 包括用户必看的发布说明、以及他明确要求写的
    #   施工说明书。用户拿到包却看不到说明，等于没写。
    "发布说明_1.9.4.txt",
    "B站与P站凭据获取指南_1.9.4.md",
    "施工说明书_ComfyUI集成_1.9.4.txt",
    "评估_画图工具自部署_1.9.4.txt",
]

# 「工具里要剔掉的开发物」
TOOL_EXCLUDE = ["__pycache__"]


def _should_skip(rel: Path, include_models: bool,
                 include_tts: bool = False) -> tuple[bool, str]:
    """按排除表判断；返回 (跳过?, 原因)。"""
    s = rel.as_posix()
    for pat, why in EXCLUDE_FILES.items():
        if s == pat:
            return True, why
    for pat, why in EXCLUDE_DIRS.items():
        if s == pat or s.startswith(pat + "/"):
            if include_models and pat in ("data/whisper", "data/models"):
                continue
            if include_tts and pat == "src/genie_tts/assets":
                continue
            return True, why
    if any(part in TOOL_EXCLUDE for part in rel.parts):
        return True, "Python 字节码缓存"
    if rel.suffix in (".pyc", ".pyo"):
        return True, "字节码"
    return False, ""


def _clear_persona(src: Path, dst: Path) -> list[str]:
    """★ 把角色人设清成**通用默认**（发布红线：铃兰那份明确是「明日方舟」IP 角色）。

    只改 `性格.free_text`（人设正文）与 `性格.extra`（个人附加约束）；
    GIF/头像/输出控制等**产品结构**保留 —— 那是"桌宠长什么样"，不是个人信息。

    ★★ 结构是 `桌宠形象/<角色>/settings.json`（**深一层**）。
       我第一版写成遍历 `src.iterdir()`，拿到的是角色**目录** → 遇到目录就 continue
       → **一个 settings.json 都没处理到**，而断言把它抓了出来。
       现在用 `rglob` 精确找 settings.json。
    只在**已复制好的产物**上做改写（不负责复制别的文件）。
    """
    cleared = []
    if not src.exists() or not dst.exists():
        return cleared
    for p in src.rglob("settings.json"):
        tgt = dst / p.relative_to(src)
        if not tgt.exists():
            continue
        try:
            # ★ 必须用 utf-8-sig：这些 settings.json **带 BOM**。
            #   用 utf-8 读会让 json.loads 抛错（本项目早就记过"文件带 BOM，别弄丢"）。
            d = json.loads(p.read_text(encoding="utf-8-sig"))
        except Exception as e:
            print(f"    [warn] 人设解析失败，保持原样（**注意可能夹带个人信息**）: "
                  f"{p.name} / {e}")
            continue
        xing = d.get("性格")
        if isinstance(xing, dict):
            # 只清人设正文。★ 注意 `extra` 不在 性格 下（它在 **输出控制** 下，
            # 是"严禁隐瞒工具返回的数据"那类**行为规则**，属于产品调校，不属于个人信息，
            # 故有意保留）。我第一版把它写在 性格 下 —— 那是个不存在的字段，白写。
            if xing.get("free_text"):
                xing["free_text"] = ""    # 清空 → 程序回落到它内置的默认人设
                cleared.append(f"{p.parent.name}.性格.free_text")
        tgt.write_text(json.dumps(d, ensure_ascii=False, indent=2) + "\n",
                       encoding="utf-8")
    return cleared


def build(out: Path, dry: bool, include_models: bool, clear_persona: bool,
          include_tts: bool = False) -> int:
    picked: list[tuple[Path, Path]] = []
    skipped: list[tuple[str, str]] = []
    total = 0

    for top in INCLUDE:
        src = ROOT / top
        if not src.exists():
            print(f"  [warn] 顶层项不存在，跳过: {top}")
            continue
        items = [src] if src.is_file() else [p for p in src.rglob("*")]
        for p in items:
            rel = p.relative_to(ROOT)
            skip, why = _should_skip(rel, include_models, include_tts)
            if skip:
                if p.is_file() or (p.is_dir() and not any(p.iterdir())):
                    skipped.append((rel.as_posix(), why))
                continue
            if p.is_file():
                picked.append((p, out / rel))
                total += p.stat().st_size

    print(f"  版本      : {__version__}")
    print(f"  输出目录  : {out}")
    print(f"  要发的文件: {len(picked)} 个，合计 {total/1024/1024:.1f} MB"
          + (f"（含模型）" if include_models else "（不含模型）"))
    print(f"  已排除    : {len(skipped)} 个路径")
    # 排除表里真正命中的（按理由去重，方便肉眼核对）
    _hits: dict[str, int] = {}
    for _rel, _why in skipped:
        _hits[_why] = _hits.get(_why, 0) + 1
    for _why, _n in sorted(_hits.items(), key=lambda x: -x[1]):
        print(f"     - {_n:>5} 个: {_why}")

    if dry:
        print("\n  （--dry-run：没有写任何文件）")
        return 0

    if out.exists():
        shutil.rmtree(out)
    for s, d in picked:
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(s, d)

    # 角色人设：默认清成通用（发布红线）
    if clear_persona:
        cleared = _clear_persona(ROOT / "data" / "桌宠形象", out / "data" / "桌宠形象")
        print(f"  已清空人设字段: {len(cleared)} 处  {cleared}")
    else:
        print("  角色人设：**按出厂预设随包**（用户拍板，未清空）")

    print(f"\n  装配完成: {out}")
    return 0


_LAUNCHER_BAT = """@echo off
chcp 65001 >nul
REM ══════════════════════════════════════════════════════════════
REM  铃兰桌宠（PRTS Desktop Pet）—— 便携版启动器
REM  ★ 双击我就能跑。第一次会自动装依赖（需要联网）。
REM ══════════════════════════════════════════════════════════════
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo.
    echo   [X] 没找到 python。请先安装 Python 3.10~3.12 并勾选 "Add to PATH"。
    echo       下载: https://www.python.org/downloads/
    echo.
    pause
    exit /b 1
)

python -c "import PySide6, openai" >nul 2>nul
if errorlevel 1 (
    echo.
    echo   首次运行：正在安装依赖（需要联网，几分钟）...
    echo.
    python -m pip install --upgrade pip
    python -m pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo   [X] 依赖安装失败。请检查网络，或手动跑: pip install -r requirements.txt
        echo.
        pause
        exit /b 1
    )
)

echo   启动中...（首次会弹出配置引导，填一个 LLM API Key 就能用了）
python main.py
if errorlevel 1 pause
"""

_FIRST_READ = """══════════════════════════════════════════════════════════════
 铃兰桌宠（PRTS Desktop Pet）v{ver} —— 便携版 · 首次使用必读
══════════════════════════════════════════════════════════════

【怎么跑】
  双击  启动.bat
  第一次会自动装依赖（需要联网）。之后就直接启动了。

【需要什么】
  · Windows 10 / 11
  · Python 3.10 ~ 3.12  —— 没装的话去 https://www.python.org/downloads/
    安装时**务必勾选 "Add Python to PATH"**
  · 一个 LLM API Key —— 去 https://platform.deepseek.com 申请
    （首次启动会弹出配置引导，只有这一项是必填的）

【配置写在哪】
  · .env          —— 密钥（绝不外传）
  · config.json   —— 界面/工具等运行配置（第一次会自动生成）

【哪些功能需要额外下载模型】
  ★ 以下都是**可选的**：没有它们程序照常跑，只是对应功能不可用。
     语音合成 / 语音识别 / 本地 OCR / 本地画图（ComfyUI）
  看当前状态:  python tools\\model_manager.py status
  集中管理说明: 所有模型统一放在  data/models/  下

【遇到问题】
  1. 先看  logs\\prts_*.log  （最新那个）
  2. 设置入口:  右键桌宠 → 设置
  3. ⚠ 如果连 python 都跑不起来，看这个:
     `python -c "print(1)"` 若直接报错 → 你的 Python 安装里可能有
     GBK 编码的 .pth 文件（某些中文项目会留），Python 读它会崩。
     去  Python安装目录\\Lib\\site-packages\\  找 *.pth 排查。

══════════════════════════════════════════════════════════════
"""


def _write_launcher(out: Path) -> None:
    """往产物里写**便携版的启动器与必读**（A 形态：解压即用）。"""
    (out / "启动.bat").write_text(_LAUNCHER_BAT, encoding="utf-8")
    (out / "首次使用必读.txt").write_text(_FIRST_READ.format(ver=__version__),
                                          encoding="utf-8")


def _make_zip(out: Path, include_models: bool) -> Path:
    """把产物打成便携 zip。★ 文件名里带版本与是否含模型，免得拿错。"""
    tag = "-with-models" if include_models else "-portable"
    zpath = out.parent / f"{out.name}{tag}.zip"
    if zpath.exists():
        zpath.unlink()
    import zipfile
    n = 0
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for p in sorted(out.rglob("*")):
            if p.is_file():
                z.write(p, p.relative_to(out.parent))   # 顶层带一层目录名，解压不散
                n += 1
    print(f"  便携包: {zpath}")
    print(f"          {zpath.stat().st_size/1024/1024:.1f} MB / {n} 个文件")
    return zpath


def main() -> int:
    ap = argparse.ArgumentParser(description="PTRTS 发布装配器")
    ap.add_argument("--out", default=None, help="输出目录（默认 dist/<APP_ID>-<版本>）")
    ap.add_argument("--dry-run", action="store_true", help="只报告，不写盘")
    ap.add_argument("--include-models", action="store_true",
                    help="把 whisper/easyocr 模型也带上（会大 600MB+）")
    ap.add_argument("--clear-persona", action="store_true",
                    help="把角色人设清成通用（默认**保留为出厂预设**，用户 2026-10-04 拍板）")
    ap.add_argument("--include-tts-models", action="store_true",
                    help="带上 GENIE 的 TTS 模型（src/genie_tts/assets，约 1855MB）")
    ap.add_argument("--zip", action="store_true",
                    help="★ A 形态：装配后打成**便携 zip**（含启动器与必读）")
    a = ap.parse_args()
    out = Path(a.out) if a.out else (ROOT / "dist" / f"{APP_ID}-{__version__}")
    rc = build(out, a.dry_run, a.include_models, a.clear_persona,
               a.include_tts_models)
    if rc == 0 and not a.dry_run:
        _write_launcher(out)
        print(f"  已写入便携版启动器: 启动.bat / 首次使用必读.txt")
        if a.zip:
            _make_zip(out, a.include_models)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
