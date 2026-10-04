# -*- coding: utf-8 -*-
"""★ 1.9.4 阶段5(A)：**便携 zip 守卫**（用户定的发布顺序是 A → B → C，这是 A）。

## A 形态是什么
"便携 zip" = 解压出来、**双击 `启动.bat`** 就能跑（启动器会自己装依赖）。
面向的用户是"机器上有 Python、但不想折腾"的人。

## 钉什么
  A. `--zip` 真能产出 `<产物名>-portable.zip`；
  B. zip 里**顶层带一层目录名**（解压出来不会把 300 多个文件撒在桌面上）；
  C. zip 里有**启动器**与**首次使用必读**；
  D. ★★ zip 里**没有**密钥/DB/日志/私有文件（发布了就收不回来，这是红线）；
  E. 启动器内容要点：检测 python、自动装依赖、失败要 pause（否则窗口一闪而过，
     用户根本看不到错误 —— 这是小白最容易卡住的地方）；
  F. 必读里写清了"必填只有 LLM Key""模型是可选能力""两个环境地雷"。
"""
import os
import sys
import zipfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PRTS_NO_COMFY_START", "1")
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))


# ★ 不在这里跑装配（那条已由 _test_release_pack_194 覆盖，跑一次要几十秒）。
#   本套件只验"启动器与打包逻辑"的产物是否合格 —— 用**已有的 zip 或现造一个小的**。
_OUT = ROOT / "临时文件" / "_scratch_dev" / "tmp_zipprobe_194"
_ZIP = _OUT.parent / f"{_OUT.name}-portable.zip"
import shutil                                              # noqa: E402

print("== 0. 造一个最小产物并打 zip（只为验启动器与打包逻辑，不跑全量装配）==")
if _OUT.exists():
    shutil.rmtree(_OUT, ignore_errors=True)
_OUT.mkdir(parents=True)
(_OUT / "main.py").write_text("print('x')", encoding="utf-8")
(_OUT / "requirements.txt").write_text("PySide6\n", encoding="utf-8")

from src.version import __version__                        # noqa: E402
from tools.build_release import _make_zip, _write_launcher  # noqa: E402

_write_launcher(_OUT)
check("★启动器 启动.bat 已写入", (_OUT / "启动.bat").is_file())
check("★首次使用必读.txt 已写入", (_OUT / "首次使用必读.txt").is_file())

_z = _make_zip(_OUT, include_models=False)
check("★--zip 产出了便携包", _z.is_file() and _z.stat().st_size > 0,
      _z.name if _z.exists() else "")

print("\n== B/C/D. zip 内部结构 ==")
with zipfile.ZipFile(_z) as z:
    names = z.namelist()
    tops = {n.split("/")[0] for n in names}
    check("★★zip 里**顶层只有一层目录名**（解压不会把几百个文件撒满桌面）",
          len(tops) == 1 and "" not in tops, str(sorted(tops)))
    check("★zip 里有 启动.bat", any(n.endswith("启动.bat") for n in names))
    check("★zip 里有 首次使用必读.txt",
          any(n.endswith("首次使用必读.txt") for n in names))
    _bad = [n for n in names if n.endswith(".env") or "/data/DBs/" in n
            or "/logs/" in n or "桌宠私有data" in n]
    check("★★zip 里没有 .env / DBs / logs / 私有文件（发布红线）",
          not _bad, str(_bad))

print("\n== E. 启动器的要点（小白最容易卡在这几处）==")
_bat = (_OUT / "启动.bat").read_text(encoding="utf-8")
check("★检测 python 是否存在（并给出下载地址）",
      "where python" in _bat and "python.org" in _bat)
check("★首次自动装依赖（pip install -r requirements.txt）",
      "pip install -r requirements.txt" in _bat)
check("★★失败时 pause（否则窗口一闪而过，用户看不到错误）",
      _bat.count("pause") >= 2, f"pause 出现 {_bat.count('pause')} 次")
check("★按脚本所在目录执行（cd /d %~dp0 —— 双击时才不会跑错地方）",
      "%~dp0" in _bat)
check("★有 chcp 65001（否则中文提示会是乱码）", "chcp 65001" in _bat)

print("\n== F. 必读里写清了关键信息 ==")
_rd = (_OUT / "首次使用必读.txt").read_text(encoding="utf-8")
check("★写了版本号", __version__ in _rd)
check("★说了必填只有 LLM Key", "LLM API Key" in _rd)
check("★说了模型是**可选**能力（没有也能跑）", "可选" in _rd and "models" in _rd)
check("★留了两个环境地雷的排查线索（GBK 的 .pth）", ".pth" in _rd)
check("★给了设置入口（右键桌宠 → 设置）", "设置" in _rd)

shutil.rmtree(_OUT, ignore_errors=True)
if _ZIP.exists():
    _ZIP.unlink()

print(f"\n==== 结果: {len(PASS)} PASS / {len(FAIL)} FAIL ====")
for f in FAIL:
    print("  FAIL:", f)
sys.exit(1 if FAIL else 0)
