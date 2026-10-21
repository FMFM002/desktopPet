# -*- coding: utf-8 -*-
"""★ 1.9.4：**版本一致性守卫** —— 版本号只允许有一个来源。

## 为什么要有这一条
1.9.4 之前版本号是**散落硬编码**的，而且实测互相矛盾：

    README.md:1              # 铃兰桌宠（PRTS Desktop Pet）v1.8.9
    main.py:79               log.info("铃兰桌宠 v1.5.0 启动中...")
    src/ui/main_window.py:2  主窗口 - 整合所有组件 v1.2.0

三个数字、三个值，谁也不知道装的是哪个。用户看到会直接懵。

## 本套件钉住
  A. `src/version.py` 存在、`__version__` 形如 `X.Y.Z`、`version_tuple()` 能拆；
  B. **README 标题里的版本 == `__version__`**（README 是用户第一眼看到的地方，
     最容易漂移，所以它必须被机器核对）；
  C. **`main.py` 的启动日志不再写字面量**，必须走 `src.version.STARTUP_LINE`；
  D. 全仓扫 `vX.Y.Z` 字面量：**除白名单外，一律必须等于当前版本**。
     ★ 这里刻意只扫 **带 `v` 前缀** 的写法 —— 因为源码注释里大量出现
       "★ 1.9.3 第 50 轮：…" 这类**变更历史标记**，那是记录、不是"当前版本"，
       全扫会把它们误判成漂移（我第一版就差点这么干）。
       带 `v` 前缀的才是"给人看的版本号"。

## 白名单怎么加
只有下面两类可以进白名单，且必须**逐条写清理由**：
  ① 第三方组件的版本（aria2 的 `v1.37.0-motrix.14`）；
  ② 变更历史标记（`v1.3.2: 直调 PlayURL API…` 这种"从哪版开始这么做"）。
★ 绝不允许为了"让套件变绿"而把**自己应用的版本号**加进来 ——
  那就等于把这道闸废掉。
"""
import os
import re
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))


# ── 扫描范围：只扫我们自己的代码/文档 ──
SKIP_PARTS = {".git", "node_modules", "__pycache__", "临时文件", "日志", "logs",
              "data", "_vendor", "开发历史", "genie_tts", "dist",
              # ★ 测试产物目录：**回归会把每个套件的输出写到这里**，
              #   而本守卫的输出恰好会回显它白名单里的旧版本号 →
              #   于是"守卫扫到了自己的输出"，报了一个假红。
              #   测试产物不是源码，一律不扫。
              "_sweep_out", "_fixtures_193", "_cfg_guard", "_cfg_guard_auto",
              "_persist_bak", "_winsize_bak",
              # ★ 1.9.5：`.dshwolf/` 是**开发代理的工作笔记**（memory/STATUS/hooks），
              #   不是产品代码 —— 它按时间记录"当时在哪个版本做了什么"，
              #   必然留着旧版本号（如 `v1.9.4`）。纳入扫描只会得到假红。
              ".dshwolf"}
V_RE = re.compile(r"v(\d+\.\d+\.\d+)")

# ── 白名单：{相对路径: 理由}（只允许第三方版本 / 变更历史标记）──
ALLOW = {
    "src/version.py":
        "版本源自己 —— 它的 docstring 引用旧值作为'为什么要有这个文件'的证据",
    "main.py":
        "注释里引用旧值（v1.5.0 / v1.8.9）作为'这里原来写错了'的证据；"
        "代码本身已改用 STARTUP_LINE",
    "tools/fetch_aria2.py":
        "第三方：aria2 的 tag=v1.37.0-motrix.14",
    "tools/dev_tests/_test_bt_download_192.py":
        "第三方：断言里核对 aria2 的 tag=v1.37.0-motrix.14",
    "tools/bilibili_tool.py":
        "变更历史标记：'v1.3.2: 直调 PlayURL API…'（记录从哪版开始这么做）",
    "tools/dev_tests/_test_version_193.py":
        "本守卫自己 —— 它的 docstring 引用旧值（v1.8.9/v1.5.0/v1.2.0）"
        "作为'为什么要做这道闸'的证据，与第三方版本同理",
    # ── 变更历史文档：它们的**职责**就是记录版本变迁，必然引用旧版本号 ──
    #   ★ 属于白名单的第②类（变更历史标记）。这不是"为了变绿而放水"：
    #     开发日志里写"原来是 v1.8.9 / v1.5.0，互相矛盾"本身就是本次修复的证据，
    #     把它改成当前版本号反而是**篡改历史**。
    "1.9.3wsl开发日志.txt":
        "变更历史文档 —— 记录版本变迁，必然引用旧版本号（如六十六节里的取证）",
    "发布说明_1.9.5.txt":
        "变更历史文档（面向用户的发布说明）—— 第五节要说明"
        "『以前 README 写 v1.8.9、启动日志写 v1.5.0』这个事实，"
        "必然引用旧版本号；把它改成当前版本反而是**篡改历史**",
}

print("== A. 版本源本身 ==")
sys.path.insert(0, str(ROOT))
from src.version import __version__, version_tuple, TITLE, STARTUP_LINE  # noqa: E402

check("★src/version.py 能导入，__version__ 形如 X.Y.Z",
      bool(re.fullmatch(r"\d+\.\d+\.\d+", __version__)), __version__)
check("★version_tuple() 能拆成三元组且都是整数",
      isinstance(version_tuple(), tuple) and len(version_tuple()) == 3
      and all(isinstance(x, int) for x in version_tuple()), str(version_tuple()))
check("★TITLE / STARTUP_LINE 里都带上了当前版本",
      __version__ in TITLE and __version__ in STARTUP_LINE,
      f"{TITLE} | {STARTUP_LINE}")

print("\n== B. README 标题必须等于版本源（用户第一眼看到的地方）==")
_readme = (ROOT / "README.md").read_text(encoding="utf-8")
_m = re.search(r"^#\s+.*?v(\d+\.\d+\.\d+)\s*$", _readme, re.M)
check("★README 第一行标题里有版本号", _m is not None,
      (_readme.splitlines()[0] if _readme else ""))
check("★★README 标题的版本 == src/version.py 的版本",
      bool(_m) and _m.group(1) == __version__,
      f"README={_m.group(1) if _m else '?'} vs version.py={__version__}")

print("\n== C. main.py 的启动日志不许再写字面量 ==")


def _strip_py_comments(text: str) -> str:
    """★★ 把 Python 源码里的**注释**去掉，再拿去做文本匹配。

    为什么必须这样：我第一版直接对 `main.py` 全文正则找 `"铃兰桌宠 vX.Y.Z"`，
    结果匹配到了**我自己写的那句注释** ——
        `#   这里原来硬编码 "铃兰桌宠 v1.5.0 启动中...", 而 README 标题写的是 v1.8.9`
    于是这道闸把"证据说明"当成了"违规代码"，**当场假红**。

    ★ 这是本项目第 3 次栽在"字符串搜索被自己的注释/docstring 骗到"
      （第 41 轮 think 面板、第 57 轮零引用守卫、这次版本守卫）。
      所以：**判代码不要用全文文本匹配，先把注释剥掉**。
    """
    import io
    import tokenize
    out = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type == tokenize.COMMENT:
                continue
            out.append(tok.string)
    except Exception:
        return text          # 解析不了就退回原文（宁可多报也不漏报）
    return "\n".join(out)


_main = (ROOT / "main.py").read_text(encoding="utf-8")
_main_code = _strip_py_comments(_main)
check("★main.py 用的是 STARTUP_LINE（从版本源取）", "STARTUP_LINE" in _main_code)
check("★★main.py 的**代码**里没有『铃兰桌宠 vX.Y.Z』这种硬编码启动行"
      "（已剥掉注释，避免被自己的证据注释假红）",
      not re.search(r'"铃兰桌宠\s*v\d+\.\d+\.\d+', _main_code))

print("\n== D. 全仓扫 vX.Y.Z：除白名单外都必须等于当前版本 ==")
_files = []
for _p in ROOT.rglob("*"):
    if not _p.is_file() or _p.suffix.lower() not in (".py", ".md", ".txt", ".json", ".iss", ".spec"):
        continue
    if any(s in _p.parts for s in SKIP_PARTS):
        continue
    _files.append(_p)

_bad, _allowed_hit = [], []
for _p in _files:
    _rel = _p.relative_to(ROOT).as_posix()
    try:
        _txt = _p.read_text(encoding="utf-8", errors="replace")
    except Exception:
        continue
    for _ln, _line in enumerate(_txt.splitlines(), 1):
        for _mm in V_RE.finditer(_line):
            _v = _mm.group(1)
            if _v == __version__:
                continue
            if _rel in ALLOW:
                _allowed_hit.append(_rel)
                continue
            _bad.append(f"{_rel}:{_ln} v{_v}")

check("★★除白名单外，全仓没有'不等于当前版本'的 vX.Y.Z 字面量",
      not _bad, "; ".join(_bad[:6]) + (f" …共 {len(_bad)} 处" if len(_bad) > 6 else ""))
_stale_allow = [k for k in ALLOW if k not in set(_allowed_hit)]
check("★白名单没有'过期项'（文件已不再含旧版本字面量，就该把该条删掉）",
      not _stale_allow,
      f"过期条目: {_stale_allow}" if _stale_allow else "")

print(f"\n  扫描 {len(_files)} 个文件；白名单命中 {len(set(_allowed_hit))} 个文件。")
print(f"  当前版本: {__version__}")

print(f"\n==== 结果: {len(PASS)} PASS / {len(FAIL)} FAIL ====")
for f in FAIL:
    print("  FAIL:", f)
sys.exit(1 if FAIL else 0)
