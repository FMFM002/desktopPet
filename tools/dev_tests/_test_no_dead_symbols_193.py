# -*- coding: utf-8 -*-
"""★ 1.9.3 第 47 轮：**零引用符号守卫** —— 不让"死代码"再悄悄长回来。

用户要求（2026-10-04）："3，加。"（指这道守卫）

判据：扫描**我们自己的生产代码**里"全仓只出现在定义处"的函数/类 → 不该有新的。
已有的 5 个放进**显式放行名单**，每条都写清为什么留着；以后新增一个就红。

设计上的三个防误伤：
  · 排除 `_vendor/`（第三方）、`dev_tests/`、`临时文件/`；
  · 排除**框架回调**（`paintEvent` / `dropEvent` / `data` … 由 Qt 按名字调用，源码里搜不到引用）；
  · 排除 `_do_*`（`pet_manager` 里的**动态派发**：`getattr(self, f"_do_{cmd}")`）。
  · ★ 另外：放行名单里的符号**一旦被删掉，本套件也会提醒你把它从名单里划掉**
    （避免名单越攒越假）。

★ 这是**会拦人的闸**（不是只报），所以上面三条排除必须做扎实 ——
  否则以后随便加个 Qt 覆写就会红，闸就没人信了。
"""
import ast
import os
import pathlib
import re
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = pathlib.Path(__file__).resolve().parents[2]

SKIP_PARTS = {".git", "node_modules", "__pycache__", "临时文件", "genie_tts",
              "开发历史", "data", "_vendor", "dev_tests"}

# ── 框架按名字调用的回调（源码里当然搜不到引用）──
FRAMEWORK_HOOKS = {
    "paintEvent", "resizeEvent", "mousePressEvent", "mouseMoveEvent",
    "mouseReleaseEvent", "mouseDoubleClickEvent", "keyPressEvent",
    "keyReleaseEvent", "closeEvent", "showEvent", "hideEvent", "enterEvent",
    "leaveEvent", "wheelEvent", "dragEnterEvent", "dragMoveEvent",
    "dropEvent", "contextMenuEvent", "changeEvent", "focusInEvent",
    "focusOutEvent", "eventFilter", "event", "sizeHint", "minimumSizeHint",
    "heightForWidth", "hasHeightForWidth", "headerData", "rowCount",
    "columnCount", "data", "flags", "setData", "index", "beginResetModel",
    "endResetModel", "__init__", "run", "start", "stop", "cleanup",
}

# ── 显式放行：每条都必须写清"为什么留着" ──
ALLOWED = {
    # ↓ 实时麦克风那一套：**不是死代码，是被弄丢的功能**（等用户拍板 a/b）
    "MicrophoneCapture": "实时麦克风采集；stt.enabled=true、设置里有开关、"
                         "465MB 模型已装 —— 触发它的 toggle_stt 早已 0 调用，"
                         "这是'功能被弄丢'，等用户决定接回去还是删掉",
    "EnergyVAD": "实时语音端点检测，同上（实时麦克风那一套）",
    "_realtime_loop": "实时转写主循环，同上（实时麦克风那一套）",
    # ↓ 测试支撑 API：删了会削弱验证能力
    "uninstall_foreground_hook": "真做 UnhookWinEvent 的资源释放；只有测试在调，"
                                 "用于验证'装/卸对称'（生产 install 幂等，进程只活一次）",
    "wait_all": "文档写明'给自测/收尾用'；注释含'别用 empty() 判断'的踩坑结论；"
                "依赖模块私有 _lock/_jobs，搬不进测试",
}

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))


def scan():
    files = [p for p in ROOT.rglob("*.py")
             if not any(s in p.parts for s in SKIP_PARTS)]
    texts = {p: p.read_text(encoding="utf-8", errors="replace") for p in files}
    all_text = "\n".join(texts.values())
    out = {}
    for p, t in texts.items():
        try:
            tree = ast.parse(t)
        except SyntaxError:
            continue
        for n in ast.walk(tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                nm = n.name
                if nm.startswith("__") or nm in FRAMEWORK_HOOKS or nm.startswith("_do_"):
                    continue
                if len(re.findall(rf"(?<![\w]){re.escape(nm)}(?![\w])", all_text)) <= 1:
                    out.setdefault(nm, []).append(
                        f"{p.relative_to(ROOT).as_posix()}:{n.lineno}")
    return out


print(f"== 扫描 {ROOT.name} 的生产代码 ==")
found = scan()

unexpected = {k: v for k, v in found.items() if k not in ALLOWED}
check("★没有**新增**的零引用符号（有新的就说明死代码又长回来了）",
      not unexpected,
      "; ".join(f"{k} @ {v[0]}" for k, v in sorted(unexpected.items())[:5])
      + (f" …共 {len(unexpected)} 个" if len(unexpected) > 5 else ""))

stale = [k for k in ALLOWED if k not in found]
check("★放行名单里没有'过期项'（符号已被删就该把它从名单里划掉）",
      not stale, "; ".join(stale))

print(f"\n  当前零引用符号 {len(found)} 个，全部在放行名单里（{len(ALLOWED)} 条）。")
for k in sorted(found):
    print(f"    · {k:<28} {found[k][0]}")

print(f"\n==== 结果: {len(PASS)} PASS / {len(FAIL)} FAIL ====")
for f in FAIL:
    print("  FAIL:", f)
sys.exit(1 if FAIL else 0)
