# -*- coding: utf-8 -*-
"""★ 1.8.5 回归：Phase5 从未执行 → 重启后 UI 设置被"刷回默认"。

真实证据（三份日志一致）：只有 Phase1 / Phase4，**没有** "Phase5: 更新定时器..."。
用户描述："重启后回到默认；打开设置，里面的数值是对的；
            要再点一遍保存才会变回想要的样式（颜色/用户头像/窗口边距）。"

根因：main_window._load_heavy 跑在 threading.Thread 里，却用
  QTimer.singleShot(0, self._init_finalize) 回主线程。
子线程没有 Qt 事件循环 → 定时器永不触发 → _init_finalize 从没执行
→ 只写在 _init_finalize 里的 _apply_config_to_ui() 从没在启动时跑过。
（设置页里数值是对的，因为 _load_values 直接读 config；
  点保存才生效，因为 _on_config_changed 会调 _apply_config_to_ui。）

用法: python tools/dev_tests/_test_phase5_startup_185.py
"""
import os
import sys
import threading
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication(sys.argv)
MAIN_TID = threading.get_ident()

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))


def pump(seconds: float):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)


# ── 1. heavy-load 线程能把 Phase5 拉起来（真实机制复现）──
print("== 1. 子线程 → Phase5 的回调真的会触发 ==")
from src.ui.main_window import MainWindow, _TaskSignalRelay  # noqa: E402
from src.ui.chat_widget import ChatWidget  # noqa: E402

relay = _TaskSignalRelay()
fired = []


class _Phase5Probe:
    """与 MainWindow 同形：非 QObject 的普通类 + 绑定方法"""

    def _init_finalize(self):
        fired.append(threading.get_ident())


probe = _Phase5Probe()
relay.heavy_ready.connect(probe._init_finalize)


def _heavy_thread():
    relay.heavy_ready.emit()


t = threading.Thread(target=_heavy_thread)
t.start()
t.join()
pump(0.5)

check("heavy_ready 触发过 Phase5", len(fired) == 1, f"fired={fired}")
check("Phase5 跑在主线程", fired and fired[0] == MAIN_TID, f"main={MAIN_TID} got={fired}")

src = (ROOT / "src" / "ui" / "main_window.py").read_text(encoding="utf-8")
import re  # noqa: E402

heavy_body = re.search(r"def _load_heavy\(\).*?(?=\n    def )", src, re.S).group(0)
check("_load_heavy 里已无 QTimer.singleShot", "QTimer.singleShot" not in heavy_body)
check("_load_heavy 用 heavy_ready.emit 回主线程", "heavy_ready.emit()" in heavy_body)
check("heavy_ready 连到了 _init_finalize",
      "self._task_relay.heavy_ready.connect(self._init_finalize)" in src)
finalize_body = src.split("def _init_finalize", 1)[1][:1200]
check("_init_finalize 里确实调用 _apply_config_to_ui",
      "self._apply_config_to_ui()" in finalize_body)
check("_init_finalize 有明显日志（便于以后一眼看出跑没跑）",
      "Phase5:" in finalize_body)

# ── 2. _apply_config_to_ui 用「配置里的值」而不是默认值 ──
print("\n== 2. _apply_config_to_ui 应用的是配置值（用户核心症状）==")
from src.utils.config import UIConfig  # noqa: E402


class _PetMgr:
    current = ""
    characters = {}


class _Stub:
    def __init__(self):
        self.config = type("C", (), {"ui": UIConfig()})()
        self.chat = ChatWidget()
        self.chat._ignore_resize_report = True
        self.pet_mgr = _PetMgr()

    def _reposition_windows(self):
        pass


stub = _Stub()
u = stub.config.ui
# 一组明显的"非默认值"
u.ui_bg_color = "#aaffff"
u.chat_bg_color = "#112233"
u.user_bubble_color = "#445566"
u.user_text_color = "#778899"
u.bot_bubble_color = "#aabbcc"
u.bot_text_color = "#ddeeff"
u.chat_window_width = 377
u.chat_window_height = 555
u.opacity = 0.66
# ★ 1.9.1: sidebar_width / sidebar_visible 两个字段已从 UIConfig 删除
#   （聊天窗口左侧栏整个移除），这里不再设置。
u.user_avatar_path = ""

MainWindow._apply_config_to_ui(stub)

p = stub.chat._personalize
check("底色用了配置值", p["ui_bg"] == "#aaffff", p["ui_bg"])
check("聊天区底色用了配置值", p["chat_bg"] == "#112233", p["chat_bg"])
check("用户气泡色用了配置值", p["user_bubble"] == "#445566", p["user_bubble"])
check("Bot 字体色用了配置值", p["bot_text"] == "#ddeeff", p["bot_text"])
css = stub.chat.styleSheet()
check("样式表里真的写进了底色 #aaffff", "#aaffff" in css)
check("样式表里真的写进了聊天区底色 #112233", "#112233" in css)
check("窗口宽度用了配置值 377", stub.chat.width() == 377, str(stub.chat.width()))
check("窗口高度用了配置值 555", stub.chat.height() == 555, str(stub.chat.height()))
check("透明度用了配置值 0.66", abs(stub.chat.windowOpacity() - 0.66) < 0.02,
      str(stub.chat.windowOpacity()))
_apply_body = src.split("def _apply_config_to_ui", 1)[1].split("\n    def ", 1)[0]
# ★ 只看**非注释行**：我特意留了注释说明"这里原来有 set_sidebar_*，
#   现在删了"，直接 in 判断会被自己的注释绊倒（第一版就是这么挂的）。
_apply_code = "\n".join(l for l in _apply_body.splitlines()
                        if not l.strip().startswith("#"))
check("★1.9.1: 侧栏相关配置已不再被 _apply_config_to_ui 读取",
      "sidebar" not in _apply_code, _apply_code[-200:])

# 默认值不该获胜
check("不再是默认底色 #ffffff", stub.chat._personalize["ui_bg"] != "#ffffff")
check("不再是默认宽度 420", stub.chat.width() != 420)

# ── 3. 退出顺序：数据库必须在监控停下之后才关 ──
print("\n== 3. 退出顺序（修 '输入监控落库失败'）==")
quit_body = src.split("def _quit", 1)[1]
stop_lw = quit_body.split("def _stop_lightweight", 1)[1].split("t1 = threading.Thread", 1)[0]
check("_stop_lightweight 里不再关数据库", "self.db.close()" not in stop_lw,
      "db.close 仍在该函数内" if "self.db.close()" in stop_lw else "")
check("db.close() 出现在 join 之后", quit_body.find("self.db.close()") > quit_body.find("t1.join(timeout=5)"),
      f"close@{quit_body.find('self.db.close()')} join@{quit_body.find('t1.join(timeout=5)')}")
check("退出前立了 _shutting_down 旗子", "self._shutting_down = True" in quit_body)
check("_shutting_down 在 __init__ 初始化", "self._shutting_down = False" in src)
imp = src.split("def _on_input_monitor_data", 1)[1][:300]
check("输入监控回调在退出时直接返回", 'getattr(self, "_shutting_down", False)' in imp)

# ── 4. 落库回调在退出中的行为（真跑一次）──
print("\n== 4. 退出中的输入监控回调不再碰数据库 ==")


class _BoomDB:
    class raw:
        write_counter = 0

        @staticmethod
        def add_raw_event(**_kw):
            raise AssertionError("退出中不该再写库")

        @staticmethod
        def enforce_cap(_n):
            raise AssertionError("退出中不该再写库")


class _CBProbe:
    _shutting_down = True
    db = _BoomDB()

    def _build_monitor_text(self, source, data):
        return "剪贴板变化: probe"


try:
    MainWindow._on_input_monitor_data(_CBProbe(), "clipboard", {"content_preview": "x"})
    check("退出中回调安全返回（没碰数据库）", True)
except AssertionError as e:
    check("退出中回调安全返回（没碰数据库）", False, str(e))

print(f"\n==== 结果: {len(PASS)} PASS / {len(FAIL)} FAIL ====")
for f in FAIL:
    print("  FAIL:", f)
sys.exit(1 if FAIL else 0)
