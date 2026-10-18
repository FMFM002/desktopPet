# -*- coding: utf-8 -*-
"""★ 1.9.4：**设置窗口里的「账号开通」页** 守卫。

## 用户要求（2026-10-18）
  "参考项目 bili_pixiv_toolkit 可以做到开通 P站 和 B站 的功能。
   如果用户要开通这两个功能，就**引导用户登陆一次**就可以了。"

## 钉什么
  A. 设置窗口的侧栏里**有「账号开通」这一项**，且页面真的建出来了；
  B. ★★ 登录必须跑在**后台线程**（Playwright + gppt 会弹浏览器并**阻塞**，
     放主线程会让设置窗口假死）；
  C. ★★★ 线程里**绝不能碰 Qt 控件** —— 必须经 **Signal** 回主线程
     （1.9.2 那次"在工具线程里碰 Qt 控件直接硬崩、没有 Python 栈"的教训）；
  D. ★★ 结束时**按钮要恢复可点**（否则用户点一次就卡死了）；
  E. ★ 页面会**刷新状态**（B站/P站 各自 已开通/未开通），
     且缺依赖时把**安装命令**摆出来（还要提醒"装进桌宠自己的 Python"）。
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


_s = (ROOT / "src" / "ui" / "settings_window.py").read_text(encoding="utf-8")

print("== A. 侧栏有「账号开通」，页面能建出来 ==")
check("★★CATEGORIES 里有 accounts", '("accounts", "账号开通"' in _s)
check("★注册了页面构造", '_category_pages["accounts"]' in _s)
check("★页面构造函数存在", "def _create_accounts_page(" in _s)

print("\n== B/C. 后台线程 + Signal 回主线程 ==")
check("★★登录跑在**后台线程**（弹浏览器会阻塞，主线程会假死）",
      "threading.Thread(target=_work" in _s and "daemon=True" in _s)
check("★★★用 **Signal** 把结果送回主线程（线程里绝不能碰 Qt 控件）",
      "class _Bridge(QObject)" in _s and "line = Signal(str)" in _s
      and "done = Signal(bool, str)" in _s)
check("★进度与结束都走 bridge，而不是直接改控件",
      "bridge.line.emit" in _s and "bridge.done.emit" in _s)

print("\n== D. 结束后按钮恢复 ==")
check("★★`_finish_account_login` 里**重新启用按钮**（否则点一次就卡死）",
      "btn.setEnabled(True)" in _s)
check("★并且把按钮文字改回来", "一键开通（登录一次）" in _s)

print("\n== E. 状态刷新 + 缺依赖提示 ==")
check("★有 `_refresh_account_status`（只读文件/环境变量，不起浏览器）",
      "def _refresh_account_status(" in _s)
check("★状态文案分「已开通 / 未开通」",
      "✅ 已开通" in _s and "❌ 未开通" in _s)
check("★缺依赖时把**安装命令**摆出来", "install_hint()" in _s)
check("★并提醒「装进桌宠自己的 Python」（混用别的解释器会 import 不到）",
      "桌宠自己的 Python" in _s)
check("★还说明 playwright 用系统 Edge、**不需要** playwright install",
      "playwright install" in _s)

print("\n== F. 实测：窗口能建、账号页在里面、刷状态不崩 ==")
from PySide6.QtWidgets import QApplication            # noqa: E402
_app = QApplication.instance() or QApplication([])
from src.ui.settings_window import CATEGORIES, SettingsWindow   # noqa: E402

check("★★CATEGORIES 里确实有 accounts", any(c[0] == "accounts" for c in CATEGORIES))
_w = SettingsWindow()
check("★★账号页真的建出来了", "accounts" in _w._category_pages)
try:
    _w._refresh_account_status()
    _ok = True
except Exception as e:
    _ok, _err = False, f"{type(e).__name__}: {e}"
check("★★刷新状态**不崩**", _ok, "" if _ok else _err)
_refs = getattr(_w, "_acc_refs", {})
check("★两个平台各有状态引用（bilibili / pixiv）",
      {"bilibili", "pixiv"} <= set(_refs), str(sorted(_refs)))

print("\n== G. ★★★ 模型必须**知道**这个功能（真机截图暴露的坑）==")
# 用户 2026-10-18 截图：他问"开通B站功能"，模型回
#   "绑定账号、开通大会员之类的账号权限那铃兰这边做不到" —— **错**。
# 根因：功能做了，但**工具描述和系统提示词里都没写** → 模型不知道自己有。
#   ⇒ 教训：**能力做了不等于模型会用**，必须在它看得见的地方写明。
_s_bi = (ROOT / "tools" / "bilibili_tool.py").read_text(encoding="utf-8")
_s_px = (ROOT / "tools" / "pixiv_tool.py").read_text(encoding="utf-8")
_s_lc = (ROOT / "src" / "core" / "llm_client.py").read_text(encoding="utf-8")
check("★★★B站工具描述里写了「不要说做不到」+ 指路到设置→账号开通",
      "不要说做不到" in _s_bi and "设置 → 账号开通" in _s_bi)
check("★★★P站工具描述里也写了（原来只写'需要先配置'，没说怎么配）",
      "不要说做不到" in _s_px and "设置 → 账号开通" in _s_px)
check("★★★系统提示词里**单独有一段** —— 因为模型可能**不调工具**就凭常识回答",
      "B站 / P站 的账号权限" in _s_lc)
check("★★提示词里明确写了「一键开通（登录一次）」这个入口名",
      "一键开通（登录一次）" in _s_lc)
check("★并说明「配好之后可能要重启桌宠才会生效」（工具会缓存凭据）",
      "重启桌宠" in _s_lc)

print(f"\n==== 结果: {len(PASS)} PASS / {len(FAIL)} FAIL ====")
for f in FAIL:
    print("  FAIL:", f)
sys.exit(1 if FAIL else 0)
