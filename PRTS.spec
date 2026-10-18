# -*- mode: python ; coding: utf-8 -*-
"""★★ 1.9.4 阶段5(B)：**PyInstaller 打包配置**（发布顺序 A → B → C 的第二步）。

## B 形态是什么
A（便携 zip）要求用户**自己有 Python**；B 把这个前提去掉 ——
打出一个 `PRTS-DesktopPet-1.9.4.exe`，**双击就能跑**（内部自带 Python 运行时与依赖）。

## 构建
    python -m PyInstaller PRTS.spec --noconfirm
    产物: dist/PRTS-DesktopPet-1.9.4-exe/PRTS-DesktopPet-1.9.4.exe

## ★★ 四条硬约束（都在下面落实了）
  1. **绝不把 1.8 GB 的 TTS 模型打进去**。
     模型是 `data`，不是被 import 的模块 —— 所以**只要不写进 `datas` 就不会进去**。
     它走 `src/core/models.py` 的集中管理，按需获取（`tools/model_manager.py`）。
     同理排除 `data/whisper`、`data/models`。
  2. **不带用户数据**：`.env`、`config.json`、`data/DBs`、`data/cache`、`logs/`、
     `data/桌宠私有data` 一律不进包（发布了就收不回来）。
  3. **exe 不需要模型也能启动** —— 缺模型只是少功能，不是崩
     （这条由 `_test_standalone_194.py` 盯着）。
  4. **不与 A 形态撞目录**：A 的装配目录是 `dist/PRTS-DesktopPet-1.9.4/`，
     所以 B 的 COLLECT 目录名多一个 `-exe` 后缀。
     ★ 这一条是**真踩过的坑**：PyInstaller 带 `--noconfirm` 时会**先删掉同名输出目录**，
       以前两边同名 → 打一次 exe 就把 77MB 的便携装配目录整锅端掉。

## ★★★ `contents_directory="."` —— 让 exe 目录就是"项目根"
PyInstaller 6 默认把运行时塞进 `_internal/`，于是 `sys._MEIPASS` 指向 `_internal`。
而本项目**所有模块**都用 `Path(__file__).resolve().parents[...]` 找根目录
（`main.py` / `src/utils/config.py` / `src/core/tool_manager.py` … 共 40 余处），
冻结后 `__file__` 落在 `_MEIPASS` 下 —— 也就是说：

    · 默认布局 → 用户数据（config.json / .env / logs / data/DBs）会写进 `_internal/`
      （混在 DLL 里、升级时会被整目录替换掉，用户的聊天记录就没了）；
    · `contents_directory="."`（PyInstaller 的 legacy onedir 布局）→ `_MEIPASS` **就是**
      exe 所在目录 → 数据就落在 exe 旁边，与便携 zip 版行为一致。

**一行配置换掉 40 处路径改动**，这是这一版选它的唯一理由。

## 需要显式 `datas` 的东西（它们不是 Python 模块，PyInstaller 猜不到）
    data/桌宠形象  —— 角色（GIF / settings.json / 头像）：**产品本体**
    data/memes     —— 表情包库
    data/assets    —— 程序内用到的图片
    data/tools     —— aria2c.exe（磁力下载引擎，按文件路径找）
    data/China-City-List-latest.csv —— 天气工具要的城市表
    config.default.json —— 首次运行据此生成用户配置
    .env.example / .env.preset —— 密钥模板 / **出厂预设密钥**
    tools/*.py     —— ★ 工具是 `importlib.util.spec_from_file_location()` **按文件路径**
                      加载的（见 `src/core/tool_manager.py`），静态分析**完全看不见**；
                      不复制进包 → exe 里一个工具都没有
    tools/_vendor  —— 账号开通工具包（同样是按文件路径 exec_module 加载）

## 关于体积与杀软（如实说）
  · 带上 Python 运行时 + PySide6 + onnxruntime… 实测约 **400 MB**；
  · `console=False` 的单文件 exe **常被杀软误报**（PyInstaller 的固有现象）。
    所以这里用 **onedir（默认）而不是 onefile** —— 误报更少、启动更快。
  · 排查"双击没反应"的正确姿势：`PRTS-DesktopPet-1.9.4.exe --selftest`
    （把启动过程写进 `logs/selftest_*.txt`，不需要改 console）。
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

# ── 输出名 ──
EXE_NAME = f"{APP_ID}-{__version__}"
# ★ 目录名带 -exe：**绝不能**等于 A 形态的装配目录（--noconfirm 会先删同名目录）
COLLECT_NAME = f"{APP_ID}-{__version__}-exe"

# ── 要随包的数据（**注意：这里没有 models**）──
DATAS = []
for rel in (
    "data/桌宠形象",                      # 角色本体
    "data/memes",                        # 表情包
    "data/assets",                       # 程序图片
    "data/tools",                        # aria2c.exe（磁力下载引擎，按文件路径找）
    "data/China-City-List-latest.csv",   # 天气城市表
    "config.default.json",               # 出厂配置（首启据此生成 config.json）
    ".env.example",                      # 密钥模板
    # ★★ 缺口 1（交接文档第三节）：**出厂预设密钥**。
    #   漏了它 → main.py 首启复制不出 .env → 天气 / Tavily 搜索 / 终末地**全部没配**。
    ".env.preset",
    "requirements.txt",                  # 让用户看得见依赖
    "README.md",
    # 与便携 zip 保持一致的 4 份文档
    "发布说明_1.9.4.txt",
    "B站与P站凭据获取指南_1.9.4.md",
    "施工说明书_ComfyUI集成_1.9.4.txt",
    "评估_画图工具自部署_1.9.4.txt",
):
    p = ROOT / rel
    if p.exists():
        # 目录 → 连目录名一起带；文件 → 放到同名位置
        DATAS.append((str(p), rel if p.is_dir() else str(Path(rel).parent)))
    else:
        print(f"[spec][warn] DATAS 缺文件，已跳过: {rel}")

# ── ★★ tools/*.py：工具是**按文件路径**加载的，必须复制进包 ──
TOOL_SRC = sorted(p for p in (ROOT / "tools").glob("*.py"))
for _p in TOOL_SRC:
    DATAS.append((str(_p), "tools"))
print(f"[spec] tools/*.py: {len(TOOL_SRC)} 个")

# 账号开通的第三方工具包（account_login.py 用 exec_module 按路径加载）
_VENDOR = ROOT / "tools" / "_vendor"
if _VENDOR.is_dir():
    DATAS.append((str(_VENDOR), "tools/_vendor"))

# ── 隐藏导入：这些是"运行时才 import"或"按路径 import"的 ──
HIDDEN = [
    "src.core.models",                   # 模型集中管理（运行时被解析）
    "src.core.first_run",
    "src.ui.first_run_wizard",
    # ★★ 缺口 2（交接文档第三节）：设置页里 `from src.core import account_login`
    #   是**运行时** import → 静态分析扫不到 → 冻结后一进「账号开通」页就 ModuleNotFoundError
    "src.core.account_login",
    "fitz", "pptx", "docx", "openpyxl",  # 文档读取（file_ops 里延迟导入）
    "win32gui", "win32process", "win32com.client", "pythoncom",   # Windows 能力
    "PIL._tkinter_finder",
    # ★ exe 里的模型下载（向导里**在本进程内**跑 model_manager，见 first_run_wizard）：
    #   huggingface_hub 1.x 的 xet 后端是**延迟导入**的，不登记会漏。
    "huggingface_hub", "hf_xet", "filelock", "tqdm", "yaml",
]
# ★★ 工具模块本身也要进 hiddenimports —— 不是为了让"导入它们"，
#   而是为了让 PyInstaller **分析它们的依赖**（pptx / yt_dlp / jmcomic / aiohttp …）。
#   不登记 → 工具代码在包里，但它 import 的库不在包里 → 调用即 ModuleNotFoundError。
HIDDEN += [f"tools.{p.stem}" for p in TOOL_SRC if p.stem != "__init__"]

# ── 明确排除：装了大但用不到的东西，能显著减小体积 ──
# ★★ 第一版实测产物 **3776 MB / 7479 文件** —— 比便携 zip（77MB）大 49 倍！
#    元凶是 `easyocr` 把整个科学计算栈拖了进来（torch / scipy / numba / sklearn /
#    spacy / transformers …）。所以这里**必须**排除干净，否则 B 形态不可发布。
#    ★ OCR 是【可选能力】：程序对它是延迟导入 + 缺了就优雅报错
#      （见 `_test_standalone_194.py` 的口径），所以不打进去没有功能性问题。
EXCLUDES = [
    "tkinter", "matplotlib", "pandas", "IPython", "jupyter", "notebook",
    "pytest", "pip",
    # ★★ 1.9.4 真机踩坑（第一版 exe 双击后**弹一个空白错误框、什么都不发生**）：
    #   我把 `setuptools` 也排除了。结果 PyInstaller 的运行时钩子
    #   `pyi_rth_pkgres.py` 仍然被装上（有包在 import pkg_resources），
    #   而 pkg_resources 需要 setuptools 里的 `jaraco.text` →
    #        ModuleNotFoundError: No module named 'jaraco'
    #   窗口版 bootloader 把这个异常显示成**模态错误框** → 进程既不退出也不启动，
    #   在自动化里表现为"卡住"。所以 `setuptools` **绝不能排**。
    # ── 由 easyocr / 其它可选依赖带进来的重家伙（第一版就是被这些撑到 3.7GB） ──
    "easyocr", "torch", "torchvision", "torchaudio", "numba", "llvmlite",
    "sklearn", "scikit-image", "scipy", "spacy", "thinc", "transformers",
    "networkx", "sympy", "pyarrow", "fsspec", "nltk", "cv2", "shapely",
    "onnxruntime",          # ★ GENIE 走它 —— 但 TTS 模型按需，缺了就不启 TTS
    "fastapi", "uvicorn",   # src/genie_tts/Server.py 我们不跑
    "datasets",             # 只在下载模型时用；exe 里不需要
    # ★ 但 **huggingface_hub 要留着**：exe 里的「首次配置引导 → 获取语音模型」
    #   走 `src/genie_tts/PredefinedCharacter.download_chara()` → `snapshot_download()`。
    #   实测它 + hf_xet + 依赖一共约 20MB，换"exe 用户也能下 TTS 模型"，值。
    # ★★ 第二轮实测 3776→820MB，但最大的两块仍是内置 GENIE 的**多语言 G2P**带的：
    #    sudachidict_core 207MB（日语词典）+ paddle 195MB（PaddleOCR）。
    #    我们只用中文/英文 TTS（feibi 角色），日韩 G2P 用不上 → 一并排除。
    "sudachidict_core", "sudachipy", "paddle", "paddleocr", "paddlex",
    "pyopenjtalk", "g2pk2", "eunjeon", "jamo", "ko_pron", "mecab",
    "pycld2", "polyglot", "icu",
    # ★★ 1.9.4 缺口 3 的取舍（交接文档第三节，按"建议方案"执行）：
    #    「一键开通 B站/P站」要 playwright（含 107MB 浏览器驱动）+ gppt。
    #    exe 里把这三个排掉 → 该功能在界面上**明确提示改用便携 zip 版**，
    #    而不是崩、也不是假报"没装"（见 src/core/account_login.py 的 frozen 分支）。
    #    ★ 想改成支持：删掉这行，并把 playwright 的驱动目录加进 DATAS（+150MB 起）。
    "playwright", "gppt", "pixivpy3",
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
    name=EXE_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,               # ★ UPX 压缩会被更多杀软盯上，关掉
    # ★ console=False 是给用户的；要排查启动失败就设环境变量 PRTS_SPEC_CONSOLE=1 重打包
    console=bool(os.environ.get("PRTS_SPEC_CONSOLE")),
    disable_windowed_traceback=False,
    # ★★★ 关键：把运行时直接铺在 exe 旁边（不建 _internal/），
    #   这样 sys._MEIPASS == exe 目录，用户数据（config.json/.env/logs/data）
    #   全部落在 exe 旁边 —— 与便携 zip 版行为一致。原因见文件头。
    contents_directory=".",
)
coll = COLLECT(
    exe, a.binaries, a.datas,
    strip=False, upx=False, name=COLLECT_NAME,
)
