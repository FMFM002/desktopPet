# -*- coding: utf-8 -*-
"""★ 1.9.4：**视觉未配置时不许瞎调工具** + **B站 cookie 目录要被创建** 守卫。

## A. 视觉未配置（用户真机报告）
  真实 transcript 证据：
      粘贴一张图片 → `[视觉API未配置]`
      → `[工具调用] file_ops action=list_downloads`   ← 跟图片**毫无关系**
      → `[工具调用] meme ...`
      → `[临时汇报] 图片没读出来，铃兰正在找它的原始文件～`
  根因：`llm_client._analyze_api()` 在没配视觉时只返回一个**光秃秃的
  `[视觉API未配置]`** —— 模型看到这四个字**不知道该干什么**，于是去瞎调工具。
  ⇒ 改成**可执行的指令**：明确"**别去找、别猜**"，并"**如实告诉用户**怎么开"。

## B. B站 cookie 目录（子代理查出的缺口）
  B站的凭据**存在文件里**（`data/cookies/`），**不是 `.env` 键**。
  可 `ensure_runtime_dirs()` **原来不创建这个目录** → 用户按说明书去放 cookie 时
  会发现"没这个目录"。⇒ 补进首次运行要建的目录清单。

## 钉什么
  A1 ★★ 未配置时返回的不再是光秃秃的标记，而是**含"不要去找"+"如实告诉用户"**的指令；
  A2 ★ **反向守卫**：确认那个旧标记**真的没了**（防止有人改回去）；
  B1 `ensure_runtime_dirs` 的清单里**包含 `data/cookies`**；
  B2 ★ 实测调用一次，`data/cookies` **真的被建出来**（不是只在清单里写了名字）。
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PRTS_NO_COMFY_START", "1")
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))


print("== A. 视觉未配置时，给模型的是**可执行的指令** ==")
_src = (ROOT / "src" / "core" / "llm_client.py").read_text(encoding="utf-8")
check("★★不再是光秃秃的 `[视觉API未配置]`"
      "（模型看到四个字不知道该干什么 → 真机去调了 list_downloads）",
      'return "[视觉API未配置]"' not in _src)
check("★★明确告诉它**别去找这张图、也别猜**",
      "不要**尝试用其它工具去找这张图" in _src or "不要" in _src and "去找这张图" in _src)
check("★★明确要求它**如实告诉用户**（而不是自己瞎折腾）",
      "如实告诉用户" in _src)
check("★并给出了**怎么开**（设置 → 视觉）", "设置 → 视觉" in _src)

print("\n== B. B站 cookie 目录要被创建 ==")
_fr = (ROOT / "src" / "core" / "first_run.py").read_text(encoding="utf-8")
check("★★`ensure_runtime_dirs` 的清单里有 `data/cookies`"
      "（B站凭据存在**文件**里，不是 .env 键；原来这目录不会被创建）",
      '"data/cookies"' in _fr)

print("\n== B2. 实测：真的建得出来 ==")
import shutil                                              # noqa: E402
from src.core import first_run as _FR                      # noqa: E402

_tmp = ROOT / "临时文件" / "_scratch_dev" / "tmp_cookies_dir"
if _tmp.exists():
    shutil.rmtree(_tmp, ignore_errors=True)
_tmp.mkdir(parents=True)
try:
    _made = _FR.ensure_runtime_dirs(base=_tmp)
    check("★★调用后 `data/cookies` **真的存在**（不是只在清单里写了名字）",
          (_tmp / "data" / "cookies").is_dir(), f"新建了 {_made}")
finally:
    shutil.rmtree(_tmp, ignore_errors=True)

print(f"\n==== 结果: {len(PASS)} PASS / {len(FAIL)} FAIL ====")
for f in FAIL:
    print("  FAIL:", f)
sys.exit(1 if FAIL else 0)
