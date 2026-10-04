# -*- mode: python ; coding: utf-8 -*-
"""★★ 1.9.4 阶段5(B)：**PyInstaller 打包配置**（发布顺序 A → B → C 的第二步）。

## B 形态是什么
A（便携 zip）要求用户**自己有 Python**；B 把这个前提去掉 ——
打出一个 `PRTS.exe`，**双击就能跑**（内部自带 Python 运行时与依赖）。

## 构建
    python tools/build_release.py                      # 先装配出干净目录（可选）
    python -m PyInstaller PRTS.spec --noconfirm        # 再打包
    产物: dist/PRTS/PRTS.exe

## ★★ 三条硬约束（都在下面落实了）
  1. **绝不把 1.8 GB 的 TTS 模型打进去**。
     模型是 `data`，不是被 import 的模块 —— 所以**只要不写进 `datas` 就不会进去**。
     它走 `src/core/models.py` 的集中管理，按需获取（`tools/model_manager.py`）。
     同理排除 `data/whisper`、`data/models`。
  2. **不带用户数据**：`.env`、`config.json`、`data/DBs`、`data/cache`、`logs/`、
     `data/桌宠私有data` 一律不进包（发布了就收不回来）。
  3. **exe 不需要 `data/桌宠形象` 之外的东西也能启动** —— 缺模型只是少功能，不是崩
     （这条由 `_test_standalone_194.py` 盯着）。

## 需要显式 `datas` 的东西（它们不是 Python 模块，PyInstaller 猜不到）
    data/桌宠形象  —— 角色（GIF / settings.json / 头像）：**产品本体**
    data/memes     —— 表情包库
    data/assets    —— 程序内用到的图片
    data/China-City-List-latest.csv —— 天气工具要的城市表
    config.default.json —— 首次运行据此生成用户配置
    .env.example   —— 告诉用户要填哪些密钥

## 关于体积与杀软（如实说）
  · 带上 Python 运行时 + PySide6 + onnxruntime + scipy… 通常在 **300~600 MB**；
  · `console=False` 的单文件 exe **常被杀软误报**（PyInstaller 的固有现象）。
    所以这里用 **onedir（默认）而不是 onefile** —— 误报更少、启动更快。
"""
import os
import sys
from pathlib import Path

ROOT = Path(SPECPATH).resolve()          # noqa: F821  (PyInstaller 注入)
sys.path.insert(0, str(ROOT))

# ★★ 必须在 Analysis **之前**设好这两个环境变量。
#   为什么：PyInstaller 的分析阶段会**真的 import 我们的模块**来找依赖，
#   而内置的 `src/genie_tts/Resources.py` 一旦发现 `GENIE_DATA_DIR` 没设，
#   就会**弹交互式询问**"要不要从 HuggingFace 下载？"（还会顺手去下 nltk 数据）
#   —— 第一次打包就是这么失败的（几十行 `Would you like to download it?` 刷屏）。
#   生产环境同理：**导入顺序一变化就会踩到**，所以 main.py 里也提前设了一份。
_FROZEN_ROOT = ROOT
os.environ.setdefault("GENIE_DATA_DIR",
                      str(_FROZEN_ROOT / "src" / "genie_tts" / "assets"
                          / "genie" / "GenieData"))
os.environ.setdefault("HF_HUB_ENABLE_PROGRESS_BAR", "0")
os.environ.setdefault("HF_HUB_OFFLINE", "1")      # ★ 分析期绝不许联网拉模型
os.environ.setdefault("NLTK_DATA", str(_FROZEN_ROOT / "data" / "nltk_data"))

from src.version import APP_ID, __version__              # noqa: E402

# ── 要随包的数据（**注意：这里没有 models**）──
DATAS = []
for rel in (
    "data/桌宠形象",                      # 角色本体
    "data/memes",                        # 表情包
    "data/assets",                       # 程序图片
    "data/China-City-List-latest.csv",   # 天气城市表
    "config.default.json",               # 出厂配置（首启据此生成 config.json）
    ".env.example",                      # 密钥模板
    "requirements.txt",                  # 让用户看得见依赖
    "README.md",
):
    p = ROOT / rel
    if p.exists():
        # 目录 → 连目录名一起带；文件 → 放到同名位置
        DATAS.append((str(p), rel if p.is_dir() else str(Path(rel).parent)))

# ── 隐藏导入：这些是"运行时才 import"或"被别的库间接用"的 ──
HIDDEN = [
    "src.core.models",                   # 模型集中管理（运行时被解析）
    "src.core.first_run",
    "src.ui.first_run_wizard",
    "fitz", "pptx", "docx", "openpyxl",  # 文档读取（file_ops 里延迟导入）
    "win32gui", "win32process", "win32com.client", "pythoncom",   # Windows 能力
    "PIL._tkinter_finder",
]

# ── 明确排除：装了大但用不到的东西，能显著减小体积 ──
# ★★ 第一版实测产物 **3776 MB / 7479 文件** —— 比便携 zip（77MB）大 49 倍！
#    元凶是 `easyocr` 把整个科学计算栈拖了进来（torch / scipy / numba / sklearn /
#    spacy / transformers …）。所以这里**必须**排除干净，否则 B 形态不可发布。
#    ★ OCR 是【可选能力】：程序对它是延迟导入 + 缺了就优雅报错
#      （见 `_test_standalone_194.py` 的口径），所以不打进去没有功能性问题。
EXCLUDES = [
    "tkinter", "matplotlib", "pandas", "IPython", "jupyter", "notebook",
    "pytest", "setuptools", "pip",
    # ── 由 easyocr / 其它可选依赖带进来的重家伙（第一版就是被这些撑到 3.7GB） ──
    "easyocr", "torch", "torchvision", "torchaudio", "numba", "llvmlite",
    "sklearn", "scikit-image", "scipy", "spacy", "thinc", "transformers",
    "networkx", "sympy", "pyarrow", "fsspec", "nltk", "cv2", "shapely",
    "onnxruntime",          # ★ GENIE 走它 —— 但 TTS 模型按需，缺了就不启 TTS
    "fastapi", "uvicorn",   # src/genie_tts/Server.py 我们不跑
    "datasets", "huggingface_hub",   # 只在下载模型时用；exe 里不需要
    # ★★ 第二轮实测 3776→820MB，但最大的两块仍是内置 GENIE 的**多语言 G2P**带的：
    #    sudachidict_core 207MB（日语词典）+ paddle 195MB（PaddleOCR）。
    #    我们只用中文/英文 TTS（feibi 角色），日韩 G2P 用不上 → 一并排除。
    "sudachidict_core", "sudachipy", "paddle", "paddleocr", "paddlex",
    "pyopenjtalk", "g2pk2", "eunjeon", "jamo", "ko_pron", "mecab",
    "pycld2", "polyglot", "icu",
]

a = Analysis(
    [str(ROOT / "main.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=DATAS,
    hiddenimports=HIDDEN,
    hookspath=[],
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
)
pyz = PYZ(a.pure)

# ★ onedir（不是 onefile）：启动更快、杀软误报更少。
#   名称里带版本，方便与旧包区分。
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name=f"{APP_ID}-{__version__}",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,               # ★ UPX 压缩会被更多杀软盯上，关掉
    console=False,           # 图形程序；日志走 logs/ 文件，不弹黑窗
    disable_windowed_traceback=False,
)
coll = COLLECT(
    exe, a.binaries, a.datas,
    strip=False, upx=False, name=f"{APP_ID}-{__version__}",
)
