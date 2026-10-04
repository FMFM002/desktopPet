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
# ★★ 1.9.3 第 57 轮：**口径从"文本出现次数"改成"AST 真被引用"后，冒出来 10 个**。
#   旧口径是 `全仓文本里这个名字出现 <= 1 次` —— 于是**注释/docstring 里提一句**
#   就算"有人用"，把一批真死的全盖住了（第 41 轮我记过这个坑："字符串搜索会被
#   自己的注释骗到"，当时只用来查 think 面板，没回头修这里）。
#   ★ 现在这 10 个是"**生产 + 测试都没人按名字调**"的，需要**逐个人工确认**
#     （有没有 `getattr(obj, "名字")` 这类字符串动态派发 —— 那种 AST 数不到）。
#   ★ 本轮的处置：**如实记进名单当"待查"，不删**（用户没授权整批删，
#     且第 42 轮"按 0 引用一次删 438 行"翻过车）。下一轮逐个查。
ALLOWED = {
    # ★★ 1.9.3 第 58 轮：**清空**。
    #   第 57 轮那 9 条（think_write / think_delta / _note_round_start /
    #   _unload_old_rounds / clear_before_last_user_message / describe_strict /
    #   normalize_persona / speak_now / take_all）已按用户拍板删除 ——
    #   用户原话："12345全部删，都是好久以前决定放弃的功能。"
    #   `take_all` 也删，因为已核实**不影响 LLM 提交后台任务**：
    #     主窗口走 `progress_bus.set_sink()` 直推（`main_window.py:558`），
    #     提交侧是 `report()`；`take_all` 只是"没注册 sink 时的兜底"。
    #   ★ 本名单的正确用法：只登记"**确认要留、但确实没人按名调**"的符号，并写清理由。
    #     空着 = "零引用真的一条都没有"，这是健康状态。
}

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))


def scan():
    """返回 {零引用符号名: [定义处, ...]}。

    ★★ 1.9.3 第 57 轮：**计数口径从"文本出现次数"改成"AST 里真的被用到"**。
       原来是 `len(re.findall(name, all_text)) <= 1` —— 也就是"全仓文本只出现一次"。
       这个口径会被**注释和 docstring 骗到**：
       第 57 轮我在 `pet_animation.py` 的注释里写了 `uninstall_foreground_hook()`
       （就是为了说明"生产从不卸它"），结果它的文本出现次数从 1 变 2 →
       扫描器以为"有人在用" → 反过来把放行名单判成"过期项"。
       ⇒ **正确做法是修扫描器，不是改注释去迎合它**（第 41 轮就记过这个坑：
         "字符串搜索会被自己的注释骗到"）。
       现在只数真正的**使用**：`ast.Name` 的 id、或 `ast.Attribute` 的 attr。
       （已知局限：`getattr(obj, "名字")` 这种字符串动态派发数不到 ——
         所以 `_do_*` 那类已经用前缀规则排除，见上面。）
    """
    files = [p for p in ROOT.rglob("*.py")
             if not any(s in p.parts for s in SKIP_PARTS)]
    parsed = []
    for p in files:
        try:
            parsed.append((p, ast.parse(p.read_text(encoding="utf-8", errors="replace"))))
        except SyntaxError:
            continue

    # ★ "被用到"要**连测试一起数** —— 否则会把"**故意只被测试用**的支撑 API"
    #   （第 43 轮已认定这类要留：wait_all / uninstall_foreground_hook / 状态机断言…）
    #   全判成死代码。本闸要抓的是"**连测试都没人用**"的真死代码。
    # ★★ 1.9.3 第 57 轮：**别名导入也要算进去**。
    #   第一版只收集 `ast.Name` / `ast.Attribute` 的标识符，于是
    #       from tools.jmcomic_tool import wait_for_downloads as _wait_jm
    #       _wait_jm(...)                     ← 按**别名**调用
    #   里 `wait_for_downloads` 完全数不到 → 被误判成"零引用"。
    #   ★ 第 57 轮我差点照着那份假清单去删 —— **这就是"别信扫描器，先验证"的又一例**。
    #   这里建立 `本地名 → 原名` 映射：别名被用到时，原名也算被用到。
    alias_of = {}          # 本地名（含 asname）→ 原始名
    for p in ROOT.rglob("*.py"):
        if any(s in p.parts for s in (".git", "node_modules", "__pycache__",
                                      "临时文件", "genie_tts", "开发历史", "_vendor")):
            continue
        try:
            t2 = ast.parse(p.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for n in ast.walk(t2):
            if isinstance(n, ast.ImportFrom):
                for a in n.names:
                    alias_of[a.asname or a.name] = a.name
            elif isinstance(n, ast.Import):
                for a in n.names:
                    if a.asname:
                        alias_of[a.asname] = a.name.split(".")[-1]

    used = set()
    for p in ROOT.rglob("*.py"):
        if any(s in p.parts for s in (".git", "node_modules", "__pycache__",
                                      "临时文件", "genie_tts", "开发历史", "_vendor")):
            continue
        try:
            t2 = ast.parse(p.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for n in ast.walk(t2):
            if isinstance(n, ast.Name):
                used.add(n.id)
                # 别名被用到 ⇒ 原名也算被用到
                if n.id in alias_of:
                    used.add(alias_of[n.id])
            elif isinstance(n, ast.Attribute):
                used.add(n.attr)

    out = {}
    for p, tree in parsed:
        for n in ast.walk(tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                nm = n.name
                if nm.startswith("__") or nm in FRAMEWORK_HOOKS or nm.startswith("_do_"):
                    continue
                # 定义本身不算"用到" —— 只要有人 Name/Attribute 引用过就算活的
                if nm not in used:
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
