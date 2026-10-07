# -*- coding: utf-8 -*-
"""★ 1.9.4：**「假下载」不许再假报成功** 守卫。

## 用户贴的 log（铁证）
    ── 开始获取 easyocr ──
        · OCR 模型（EasyOCR）  约 100 MB
        ✅ 已建目录 —— easyocr 首次使用时会自动下到这里
    ── easyocr 结束（返回码 0）──
  **一个字节都没下**，却报「返回码 0」。
  用户原话：「**首次使用OCR也没有触发下载好像**。」

## 修法
  · **easyocr** → 真的去下：调 `easyocr.Reader(model_storage_directory=...)`
    指向 `ocr_engine` 读取的那个固定目录；没装包就**如实说 + 给装法**，`rc=1`；
  · **whisper** 这类**没有可靠自动源**的 → `rc=2`（**「需要人工」，既不是成功
    也不是崩溃**），并把「要放哪两个文件、放哪、或填路径让我扫描」讲清楚。
    ★ 原来它是 `rc = 0` —— 那等于**把「什么都没做」报成成功**。

## 钉什么（静态，不去真下 100MB）
  A. ★★ easyocr 分支**真的调了** `easyocr.Reader`（不是只 mkdir）；
  A2 ★★ 没装包时给 `rc=1` + 装法，**不是静默成功**；且旧的假成功文案**已删**；
  B. ★★★ 「没有可靠自动源」那条**不再 `rc = 0`**，改成 `rc = 2`；
  B2 ★ 且讲清「放哪两个文件 / 可以填路径让我扫描」；
  C. ★ 注释里留了用户那句原话（日后有人想改回去时能看见为什么）。
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("PRTS_NO_COMFY_START", "1")
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))


_s = (ROOT / "tools" / "model_manager.py").read_text(encoding="utf-8")

print("== A. easyocr 要真的下，不许只 mkdir 就报成功 ==")
check("★★easyocr 分支里真的调用了 easyocr.Reader（真正触发下载）",
      "easyocr.Reader(" in _s)
check("★★而且把模型目录指向固定位置（ocr_engine 读的那个）",
      "model_storage_directory=str(dst)" in _s)
check("★★没装包时给 rc=1 + 装法（不是静默成功）", "pip install easyocr" in _s)
check("★旧的假成功**打印语句**已删"
      "（注释里引用旧行为是**故意**的，不算；要看的是 `print(...)` 还在不在）",
      'print("      ✅ 已建目录' not in _s)

print("\n== B. 没有自动源的，不许把「什么都没做」报成成功 ==")
check("★★★那条分支不再是 rc = 0（原来会让用户看到返回码 0 以为装好了）",
      "rc = 0        # 不当作错误" not in _s)
check("★★改成了 rc = 2，并写明是「需要人工」（区分成功/失败）",
      "rc = 2" in _s and "需要人工" in _s)
check("★并讲清了 whisper 要放哪两个文件",
      "whisper-cli.exe" in _s and "ggml-" in _s)
check("★以及两条出路：填路径让我扫描 / 直接放到规范位置",
      "扫描并写配置" in _s or "把路径填进去" in _s)

print("\n== C. 留档：用户原话在注释里 ==")
check("★注释里保留了用户那句「…也没有触发下载好像」"
      "（★ 别用整句匹配 —— 注释里是**换行拆开**的，整句匹配必然落空，我第一版就这么假红了一次）",
      "也没有触发下载好像" in _s)

print(f"\n==== 结果: {len(PASS)} PASS / {len(FAIL)} FAIL ====")
for f in FAIL:
    print("  FAIL:", f)
sys.exit(1 if FAIL else 0)
