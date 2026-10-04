# -*- coding: utf-8 -*-
"""★ 1.9.3 第 44 轮：**未使用的 import 一律不许存在** —— 把这条规范钉进回归。

为什么加这个套件：
  用户要求"删除冗余代码"。而"冗余"里最客观、最没争议的一类就是**未使用的 import**：
  它不承载任何功能，删掉零风险（compile + 回归全绿即可证明）。
  第 44 轮一次性清掉了 **38 条 import 语句 + 68 个名字**，为了不让它们悄悄长回来，
  这里加一道**机器闸**：以后任何文件里出现"导入了却没人用"的名字，这套件就红。

判据（本套件自己实现，不依赖外部 linter）：
  · 名字在整个文件里出现次数 <= 1（即只有 import 那一处）→ 未使用；
  · 排除 `from __future__ import annotations`（**编译器指令**，不是普通 import）；
  · 排除 `__init__.py`（那里的 import 多是**再导出**，删了会改包接口）；
  · 排除 `tools/_vendor/**`（vendored 第三方）。
"""
import ast
import os
import pathlib
import re
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = pathlib.Path(__file__).resolve().parents[2]

SKIP_PARTS = {".git", "node_modules", "__pycache__", "临时文件", "genie_tts",
              "开发历史", "data", "_vendor"}

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))


def unused_imports(path: pathlib.Path):
    # ★ 用 utf-8-sig 读：本项目有若干文件带 **UTF-8 BOM**（PowerShell 写文件留下的），
    #   用 utf-8 读会把 BOM 当成源码第一个字符 → `ast.parse` 直接报
    #   "invalid non-printable character U+FEFF"，整个文件被跳过（第一版就是这样漏检的）。
    src = path.read_text(encoding="utf-8-sig", errors="replace")
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        return [(0, f"<语法错误: {e}>")]
    imported = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                imported.setdefault((a.asname or a.name).split(".")[0], node.lineno)
        elif isinstance(node, ast.ImportFrom):
            if node.module == "__future__":      # ★ 编译器指令，跳过
                continue
            for a in node.names:
                if a.name == "*":
                    continue
                imported.setdefault(a.asname or a.name, node.lineno)
    # `__all__` 里出现也算用到
    out = []
    for nm, ln in imported.items():
        n = len(re.findall(rf"(?<![\w]){re.escape(nm)}(?![\w])", src))
        if n <= 1:
            out.append((ln, nm))
    return out


files = [p for p in ROOT.rglob("*.py")
         if not any(s in p.parts for s in SKIP_PARTS) and p.name != "__init__.py"]

print(f"== 检查 {len(files)} 个文件的 import ==")
bad = []
for p in files:
    for ln, nm in unused_imports(p):
        bad.append(f"{p.relative_to(ROOT).as_posix()}:{ln} import {nm}")

check("★全仓没有未使用的 import（38 条语句 / 68 个名字已在第 44 轮清掉）",
      not bad, ("; ".join(bad[:5]) + (f" …共 {len(bad)} 个" if len(bad) > 5 else "")))

# 反向守卫：别把"清理"做成"把整个文件删了"
for _must in ("src/core/session.py", "src/ui/main_window.py", "src/core/monitor.py",
              "tools/pptx_tool.py"):
    check(f"★{_must} 仍在", (ROOT / _must).exists())

print(f"\n==== 结果: {len(PASS)} PASS / {len(FAIL)} FAIL ====")
for f in FAIL:
    print("  FAIL:", f)
sys.exit(1 if FAIL else 0)
