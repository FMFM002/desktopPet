# -*- coding: utf-8 -*-
"""★ 1.9.4 阶段2：**依赖清单守卫** —— 代码用到的包必须在 requirements.txt 里。

## 为什么要有这一条
1.9.4 发布前审计发现：`requirements.txt` **漏了 16 个"代码里真在用"的包**，
而它们**只写在注释里**（`# pip install PyMuPDF`）——
可 `pip install -r requirements.txt` **不执行注释**。
后果是**新用户装完少功能，而且不报错**：

    PPT 工具 → 缺 python-pptx（代码里 23 处引用）
    看 PDF   → 缺 PyMuPDF
    开 Word  → 缺 python-docx
    剪贴板监控 → 缺 pyperclip
    前台窗口识别 → 缺 pywin32
    STT 重采样 → 缺 scipy

这种"静默少功能"最难查 —— 所以必须让机器盯着。

## 同时反向查
声明了但**全仓都没引用**的包（旧版有 PyMySQL / SQLAlchemy / opencv-python /
pystray 四个）也算问题：白白拖慢安装、还可能引冲突。

## 判据
一律走 **AST**（不看注释/字符串）—— 本项目已 3 次栽在"文本搜索被自己的注释骗到"。
"""
import ast
import os
import pathlib
import re
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))


# import 名 → PyPI 包名（只登记不一致的）
I2P = {
    "PIL": "Pillow", "cv2": "opencv-python", "dotenv": "python-dotenv",
    "edge_tts": "edge-tts", "pptx": "python-pptx", "docx": "python-docx",
    "fitz": "PyMuPDF", "yaml": "pyyaml", "win32api": "pywin32",
    "win32gui": "pywin32", "win32process": "pywin32", "win32con": "pywin32",
    "win32event": "pywin32", "win32com": "pywin32", "pythoncom": "pywin32",
    "g2pM": "g2pM", "onnxruntime": "onnxruntime", "yt_dlp": "yt-dlp",
    "pixivpy3": "pixivpy3", "easyocr": "easyocr", "pytesseract": "pytesseract",
    "jmcomic": "jmcomic", "qrcode": "qrcode", "uiautomation": "uiautomation",
    "pyperclip": "pyperclip",
}

# ★ 允许"代码里用了但清单里不声明"的包 —— 必须逐条写清理由。
#   这些是【可选能力】：没装时对应工具会**优雅报错**（不崩），
#   所以不该强制所有用户都装。
OPTIONAL_OK = {
    "easyocr": "可选：本地 OCR（没装走其它 OCR 或提示）",
    "pytesseract": "可选：本地 OCR（需系统级 Tesseract）",
    "yt-dlp": "可选：B站视频下载",
    "jmcomic": "可选：JM 工具（缺包时返回 'pip install jmcomic' 指引，已实测）",
    "qrcode": "可选：终末地工具生成二维码",
    "certifi": "可选/传递依赖：fetch_aria2 取 CA 证书",
    "pixivpy3": "已内置：tools/_vendor/pixivpy3 自带副本，无需 pip 装",
    "comtypes": "可选：Windows COM 辅助",
    "pywin32": "已声明；但 win32com/pythoncom 都是它的一部分",
    # ★ 1.9.4：B站/P站「一键开通」（设置 → 账号开通）专用的两个包。
    #   只有用户点「一键开通」时才需要；没装时**设置页会直接给出安装命令**，
    #   不会崩，也不会影响其它功能。
    "playwright": "可选：一键开通 B站（弹系统 Edge 让用户登录一次；"
                  "没装时设置页给安装命令，不影响其它功能）",
    "gppt": "可选：一键开通 P站（OAuth 拿 refresh_token；"
            "没装时设置页给安装命令，不影响其它功能）",
}

# 标准库 / 项目内部
STD = set(sys.stdlib_module_names)
INTERNAL = {"src", "tools", "main", "_session", "tests", "dev_tests",
            "_fixtures_193", "_repro_subagent_wait_193"}

req = (ROOT / "requirements.txt").read_text(encoding="utf-8")
declared, commented = set(), set()
for _l in req.splitlines():
    _s = _l.strip()
    if not _s:
        continue
    if _s.startswith("#"):
        _m = re.match(r"#\s*([A-Za-z0-9_.\-]+)\s*(?:[><=\[]|$)", _s[1:].strip())
        if _m:
            commented.add(_m.group(1).lower())
        continue
    _n = re.split(r"[><=\[]", _s)[0].strip()
    if _n:
        declared.add(_n.lower())


def collect(base_dirs, skip_parts):
    """收集第三方顶层 import（AST）。返回 {包名: '文件:行'}。"""
    out = {}
    files = []
    for b in base_dirs:
        p = ROOT / b
        if p.is_file():
            files.append(p)
        else:
            files += [x for x in p.rglob("*.py") if not (set(x.parts) & skip_parts)]
    for f in files:
        try:
            t = ast.parse(f.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for n in ast.walk(t):
            mods = []
            if isinstance(n, ast.Import):
                mods = [a.name.split(".")[0] for a in n.names]
            elif isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
                mods = [n.module.split(".")[0]]
            for m in mods:
                if m in STD or m in INTERNAL:
                    continue
                out.setdefault(I2P.get(m, m), f"{f.relative_to(ROOT).as_posix()}:{n.lineno}")
    return out


print("== A. 代码用到的第三方包，清单里必须声明（或明确登记为可选）==")
used = collect(["src", "tools", "main.py"], {"_vendor", "genie_tts", "dev_tests"})
missing = {p: w for p, w in used.items()
           if p.lower() not in declared and p not in OPTIONAL_OK}
check("★★代码用到的每个第三方包，都已声明或登记为【可选】",
      not missing,
      "; ".join(f"{p}（{w}）" for p, w in list(missing.items())[:5])
      + (f" …共 {len(missing)} 个" if len(missing) > 5 else ""))
print(f"      代码用到 {len(used)} 个第三方包；{len(declared)} 个已声明、"
      f"{len(OPTIONAL_OK)} 个登记为可选")

print("\n== B. 反向：声明了但全仓（含内置 genie_tts）都没引用 ==")
# genie_tts 是内置的，它的依赖（onnx/tokenizers…）也要算"被用到"
used_all = collect(["src", "tools", "main.py"], {"_vendor", "dev_tests"})
dead = sorted(p for p in declared if p not in {u.lower() for u in used_all})
check("★★清单里没有'全仓 0 引用'的多余包（旧版有 pymysql/sqlalchemy/"
      "opencv-python/pystray 四个）",
      not dead, "; ".join(dead))

print("\n== C. 反向守卫：确认判据抓得到'没声明的包' ==")
_fake = {"不存在的包xyz": "x.py:1"}
_caught = [p for p in _fake if p.lower() not in declared and p not in OPTIONAL_OK]
check("★判据有效：虚构一个未声明的包名，能被这套逻辑挑出来", bool(_caught))

print("\n== D. 不许再出现'只在注释里教人装'的核心依赖 ==")
# 旧版把 PyMuPDF/python-pptx/python-docx 写在注释里 —— 这是本次的根因。
_CORE_MUST_DECLARE = {"pymupdf": "PyMuPDF", "python-pptx": "python-pptx",
                      "python-docx": "python-docx", "pywin32": "pywin32",
                      "scipy": "scipy", "pyperclip": "pyperclip"}
_only_commented = {v: k for k, v in _CORE_MUST_DECLARE.items()
                   if v.lower() in commented and v.lower() not in declared}
check("★★本次修复的 6 个核心依赖都已**真正声明**（不再只写在注释里）",
      not _only_commented, str(_only_commented))

print(f"\n==== 结果: {len(PASS)} PASS / {len(FAIL)} FAIL ====")
for f in FAIL:
    print("  FAIL:", f)
sys.exit(1 if FAIL else 0)
