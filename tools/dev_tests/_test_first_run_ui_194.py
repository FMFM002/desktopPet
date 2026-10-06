# -*- coding: utf-8 -*-
"""★ 1.9.4 阶段4(下)：**首次配置引导 · 界面守卫**（offscreen Qt）。

## 钉什么
  A. 向导能构造、四步都在、不抛异常；
  B. **老用户不打扰**：should_show() 判据保守（只差 LLM Key 才算首次）；
  C. **铁律②：没勾的能力绝不写** —— 直接检查 _apply_optional 只记录选择，不写配置；
  D. **铁律①：能探测的不问** —— 自动发现的项目只展示、不做成必答；
  E. 跳得掉（跳过按钮 → reject），且引导失败不影响启动（main.py 里 try 包住）；
  F. 完成页会写出版本号（与 src/version.py 一致）与"Key 写到哪"。
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PRTS_NO_COMFY_START", "1")
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from PySide6.QtWidgets import QApplication, QDialog  # noqa: E402

_app = QApplication.instance() or QApplication([])

from src.core import first_run as fr          # noqa: E402
from src.ui import first_run_wizard as fw     # noqa: E402
from src.version import __version__           # noqa: E402

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))


print("== A. 向导能构造、四步齐全 ==")
w = fw.FirstRunWizard()
check("★能构造且不抛异常", w is not None)
check("★★现在是**五步**（欢迎 / Key / 勾选 / **检测下载** / 完成）—— "
      "用户 2026-10-06 要求「勾选页纯勾选，检测和下载放到下一步」",
      w._stack.count() == 5, f"实际 {w._stack.count()}")
check("★初始在第 1 步", w._step == 0)
check("★第 1 步展示「已自动发现 N 项」（让新用户安心）",
      "自动发现" in w._summary_text() if hasattr(w, "_summary_text")
      else len(w._report.auto_found) >= 1,
      w._report.summary())

print("\n== B. 老用户不打扰（判据保守）==")
check("★★should_show() 与 is_first_run() 一致（同一判据，不另立标准）",
      fw.should_show() == fr.is_first_run(), str(fw.should_show()))
check("★本机已配好 Key → should_show() 为假（不弹）", fw.should_show() is False)

print("\n== C. 铁律②：没勾的能力绝不写 ==")
# ★ 不给任何勾选框打勾 → _apply_optional 应该什么都不写
for cb in w._checks.values():
    cb.setChecked(False)
w._apply_optional()
check("★★一个都没勾时，选中的能力列表为空（不写任何配置）",
      w._optional_picked == [], str(w._optional_picked))

print("\n== D. 铁律①：自动发现的只展示、不做成必答 ==")
check("★自动发现项不出现在 must_fill 里",
      not ({f.key for f in w._report.auto_found}
           & {f.key for f in w._report.must_fill}))
check("★可选能力都有勾选框（想用才问）",
      all(f.key in w._checks for f in w._report.optional),
      f"报告 {len(w._report.optional)} 项 / 勾选框 {len(w._checks)} 个")

print("\n== E. 跳得掉 + 不阻断启动 ==")
check("★有「跳过」按钮（新用户不该被强迫填）",
      hasattr(w, "_btn_skip") and w._btn_skip is not None)
w2 = fw.FirstRunWizard()
w2._btn_skip.click()
check("★点跳过 → 结果是 Rejected（当作没引导过）", w2.result() == QDialog.Rejected,
      str(w2.result()))
_src = (ROOT / "main.py").read_text(encoding="utf-8")
check("★★main.py 里引导被 try 包住（引导坏掉也不许拖垮启动）",
      "first_run_wizard" in _src and "首次配置引导失败（不阻断启动）" in _src)
check("★引导在 MainWindow() **之前**（填的 Key 才能先生效）",
      _src.index("FirstRunWizard()") < _src.index("window = MainWindow()"))

print("\n== F. 完成页内容 ==")
w3 = fw.FirstRunWizard()
w3._key_ok = True
w3._key_saved = "已保存到 .env（不会进入版本库）"
w3._apply_optional()
w3._render_done()
_done = w3.done_page_text()          # ★ 整页文本（别只查一个控件 —— 假红过一次）
check("★完成页写出版本号（与 src/version.py 一致）", __version__ in _done,
      __version__)
check("★完成页说明 Key 写到哪（.env，不进版本库）", ".env" in _done)
check("★完成页提到「在哪能改」（设置）", "设置" in _done)

print("\n== G. 空 Key 不写盘（保护真实 .env）==")
w4 = fw.FirstRunWizard()
w4._key_edit.setText("")
_before = (ROOT / ".env").read_bytes() if (ROOT / ".env").exists() else b""
w4._apply_key()
_after = (ROOT / ".env").read_bytes() if (ROOT / ".env").exists() else b""
check("★★没填 Key 时 _apply_key() 不碰真实 .env", _before == _after)

print(f"\n==== 结果: {len(PASS)} PASS / {len(FAIL)} FAIL ====")
for f in FAIL:
    print("  FAIL:", f)
sys.exit(1 if FAIL else 0)
