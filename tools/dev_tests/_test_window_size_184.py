# -*- coding: utf-8 -*-
"""★ 1.8.3 回归：聊天窗口尺寸/侧栏显隐被"每次启动刷新"的根因。

真实证据：logs/prts_20260915_203645.log 第 269 行
  [MainWindow] DEBUG: 聊天窗口尺寸已保存: 420x640
但随后重启的新实例在 20:43:59 把 config.json 写成了 chat_window_width = 340
（而且那个实例的日志是 0 字节，看不到过程 —— 见本文件第 4 节）。

根因：_on_chat_resized 在 _sidebar_visible=True 时减掉
  SIDEBAR_WIDTH(120) + STRIP_W(18) + 20 = 158
→ max(MIN_WIDTH, 420 - 158) = max(340, 262) = 340 ← 正好 MIN_WIDTH。
但侧栏从 1.6.0 起就是聊天窗口**外面**的独立窗口（UI几何模型.txt 第 3 条），
聊天窗口宽度从来不包含它，所以这 158 是凭空的。

★ 1.9.1 更新：**左侧栏整个删除了**（用户方案 2：删掉左侧栏、删掉书签、
  左上角换成「形象名 + tokens」、话题按钮挪到右上角最前面）。
  于是 SIDEBAR_WIDTH / STRIP_W / _sidebar_visible 全都不存在了 ——
  这个 158px 的 bug 从已经被修好升级成结构上不可能再犯。
  本文件第 1、2 节保留（守住尺寸不被凭空减宽），
  第 3、4 节改成侧栏连字段带方法确实都没了 + 尺寸落盘往返。

用法: python tools/dev_tests/_test_window_size_184.py
"""
import json
import os
import shutil
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication(sys.argv)

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))


# ── 1. 复现日志里的那次覆盖 ──
print("== 1. 复现：侧栏显示时宽度被写成 340 ==")
from src.ui.chat_widget import ChatWidget  # noqa: E402
from src.ui.main_window import MainWindow  # noqa: E402


class _UI:
    chat_window_width = 420
    chat_window_height = 640


class _Cfg:
    ui = _UI()


class _Stub:
    """只提供 _on_chat_resized 需要的成员"""

    def __init__(self):
        self.config = _Cfg()
        self.chat = ChatWidget()          # 真实控件，拿真实的 MIN_WIDTH
        # ★ 1.9.1: 左侧栏删了 → `_sidebar_visible` / `SIDEBAR_WIDTH` / `STRIP_W`
        #   全都不存在了，这个"减 158px"的 bug 从此**结构上不可能**再犯。
        #   （1.8.3 时这里要手动置 _sidebar_visible=True 才能复现。）
        self.saved = None

    def _reposition_windows(self):
        pass

    def _save_chat_size(self):
        self.saved = (self.config.ui.chat_window_width,
                      self.config.ui.chat_window_height)


stub = _Stub()
stub.chat._ignore_resize_report = True   # 不让控件的 resizeEvent 参与

# 启动阶段：配置里存的是 420，控件也确实被 resize 到 420
stub.config.ui.chat_window_width = 420
MainWindow._on_chat_resized(stub, 420, 640)

check("侧栏显示时，420 仍然存成 420（不再被减成 340）",
      stub.config.ui.chat_window_width == 420,
      f"got={stub.config.ui.chat_window_width}")
check("不会再撞上 MIN_WIDTH=340",
      stub.config.ui.chat_window_width != stub.chat.MIN_WIDTH)
check("高度照旧", stub.config.ui.chat_window_height == 640)

# 用户拖到很窄 → 仍然被 MIN_WIDTH 兜底
MainWindow._on_chat_resized(stub, 200, 700)
check("比最小宽度还窄时仍兜底到 MIN_WIDTH",
      stub.config.ui.chat_window_width == stub.chat.MIN_WIDTH,
      f"got={stub.config.ui.chat_window_width}")

print("\n== 2. 源码里不该再出现那 158px 的减法 ==")
src_mw = (ROOT / "src" / "ui" / "main_window.py").read_text(encoding="utf-8")
resize_body = src_mw.split("def _on_chat_resized", 1)[1].split("def _save_chat_size", 1)[0]
check("_on_chat_resized 里没有 int(width) - extra", "int(width) - extra" not in resize_body)
check("_on_chat_resized 里没有 SIDEBAR_WIDTH 参与加减", "SIDEBAR_WIDTH" not in resize_body)
check("仍然保留了 MIN_WIDTH 兜底", "self.chat.MIN_WIDTH" in resize_body)

# ── 3. 侧栏显隐：切换要发信号，恢复不发 ──
print("\n== 3. ★ 1.9.1: 左侧栏已整个删除 ==")
w = ChatWidget()
check("ChatWidget 不再有侧栏窗口/状态/按钮",
      not hasattr(w, "_sidebar_win") and not hasattr(w, "_sidebar_visible")
      and not hasattr(w, "_sidebar_ready") and not hasattr(w, "_toggle_sidebar_btn"))
check("侧栏相关方法和信号也都没了",
      not hasattr(w, "_toggle_sidebar") and not hasattr(w, "set_sidebar_ready")
      and not hasattr(w, "_position_sidebar") and not hasattr(w, "sidebar_toggled")
      and not hasattr(w, "set_sidebar_visible") and not hasattr(w, "set_sidebar_width"))
check("常量 SIDEBAR_WIDTH / STRIP_W 已删除",
      not hasattr(w, "SIDEBAR_WIDTH") and not hasattr(w, "STRIP_W"))
check("书签（列表/跳转/删除）也全没了",
      not hasattr(w, "update_topic_list") and not hasattr(w, "topic_navigate_request")
      and not hasattr(w, "topic_delete_request"))
check("左上角换成了「形象名 + tokens」标签", hasattr(w, "_id_label"))
check("右上角有「开启新话题 / 总结旧话题」两个按钮",
      hasattr(w, "_new_topic_btn") and hasattr(w, "_summarize_topic_btn"))
check("MainWindow 里不再有 _on_sidebar_toggled",
      not hasattr(MainWindow, "_on_sidebar_toggled"))
def _code_only(path: Path) -> str:
    """去掉注释行后的源码。

    ★ 坑：注释里也会出现 set_sidebar_visible / sidebar_visible 这些名字
    （用来说明"这个字段已经删了"），直接对整个文件做 in 判断会被自己的注释绊倒
    （第一版就是这么挂的）。所以只保留非注释行。
    """
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s.startswith("#") or s.startswith('"""') or s.startswith("'''"):
            continue
        out.append(line)
    return "\n".join(out)


check("MainWindow 里不再有侧栏恢复/接线代码",
      all(k not in _code_only(ROOT / "src" / "ui" / "main_window.py")
          for k in ("set_sidebar_visible", "sidebar_toggled",
                    "set_sidebar_ready", "_position_sidebar")))
check("UIConfig 里删掉了 sidebar_width / sidebar_visible 两个字段",
      all(k not in _code_only(ROOT / "src" / "utils" / "config.py")
          for k in ("sidebar_width", "sidebar_visible")))
check("设置界面里也删掉了「侧栏宽度」那一项",
      "_ui_sidebar_width" not in _code_only(ROOT / "src" / "ui" / "settings_window.py"))

# ── 4. config 落盘往返（★ 1.9.1: 只剩聊天窗口尺寸，侧栏字段已删）──
print("\n== 4. chat_window_width 落盘往返 ==")
from src.utils.config import AppConfig  # noqa: E402

CFG = ROOT / "config.json"
BAK = ROOT / "tools" / "dev_tests" / "_winsize_bak"
BAK.mkdir(parents=True, exist_ok=True)
shutil.copy2(CFG, BAK / "config.json")
try:
    cfg = AppConfig()
    cfg.ui.chat_window_width = 420
    cfg.save()
    on_disk = json.loads(CFG.read_text(encoding="utf-8"))
    check("config.json 写入 chat_window_width=420",
          on_disk["ui"].get("chat_window_width") == 420)
    fresh = AppConfig()
    check("重新加载后宽度仍是 420",
          fresh.ui.chat_window_width == 420, repr(fresh.ui.chat_window_width))
finally:
    shutil.copy2(BAK / "config.json", CFG)
    print("   config.json 已还原")

print(f"\n==== 结果: {len(PASS)} PASS / {len(FAIL)} FAIL ====")
for f in FAIL:
    print("  FAIL:", f)
sys.exit(1 if FAIL else 0)
