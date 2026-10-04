# -*- coding: utf-8 -*-
"""★ 1.8.10 回归：全仓 bug 审计修出来的问题。

三路审计（src/core、src/ui、tools+utils）共 19 个真 bug，这个文件覆盖其中
可以离线断言的部分：

A. [HIGH] 用户画像提取整体失效 —— ProfileExtractor 把 summarize() 的
   list[str] 返回值当 JSON 用，isinstance(item,dict) 永远为假，
   一条画像都写不进去却照样打印成功日志。
B. [MED] 总结后台线程直接改 QWidget —— session.on_topic_changed /
   on_input_blocked 必须走信号回主线程。
C. [MED] Phase4 抛异常 → Phase5 永不执行（heavy_ready.emit 必须在 finally）。
D. [MED] Phase4 完成前发消息会 AttributeError 丢消息。
E. [MED] 设置窗口尺寸控件不刷新 → 保存时把用户刚拖好的尺寸冲掉。
F. [MED] 工具回调收不到（ToolManager 用私有模块名加载，工具里的
   模块级 global 设的是"另一个模块"）。
G. 其余 LOW：shell_exec 的 PROJECT_ROOT、jmcomic 属性名、
   bilibili CDN 轮换、web_search 状态码、vision 检测用错 Key、
   CHARACTERS_DIR 未导入、ffprobe 无视频流、keep_new 丢记忆、
   非工具脚本被注册成工具。

用法: python tools/dev_tests/_test_audit_fixes_1810.py
"""
import os
import re
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


def read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def code_only(src: str) -> str:
    """去掉注释与 docstring，只留可执行代码。

    这次的修复注释里会**引用旧写法**（"原来是 xxx"），
    直接对全文做 `not in` 断言会被自己的注释坑到 —— 那是在断言注释，
    不是在断言代码。所以"旧写法还在不在"类断言一律用这个。
    """
    src = re.sub(r'"""[\s\S]*?"""', "", src)
    src = re.sub(r"'''[\s\S]*?'''", "", src)
    out = []
    for line in src.split("\n"):
        i = line.find("#")
        out.append(line if i < 0 else line[:i])
    return "\n".join(out)


# ── A. 用户画像提取 ──
print("== A. 用户画像提取（HIGH）==")
from src.core.user_profile import ProfileExtractor  # noqa: E402

check("SummarizerClient 有 complete_raw（返回原始文本）",
      "def complete_raw" in read("src/core/llm_client.py"))
up_src = read("src/core/user_profile.py")
check("extract_from_text 不再用 summarize() 的结果当 JSON",
      "self._llm.summarize(prompt)" not in code_only(up_src))
check("extract_from_text 改用 complete_raw",
      "self._llm.complete_raw(prompt" in up_src)

# _parse_items 的健壮性
pi = ProfileExtractor._parse_items
check("解析纯 JSON 数组",
      pi('[{"key":"a","value":"b"}]') == [{"key": "a", "value": "b"}])
check("解析 ```json 围栏",
      pi('```json\n[{"key":"a","value":"b"}]\n```') == [{"key": "a", "value": "b"}])
check("解析带前后废话的 JSON",
      pi('好的，结果如下：[{"key":"a","value":"b"}] 就这些') == [{"key": "a", "value": "b"}])
check("解析失败返回 [] 而不是抛异常", pi("完全不是 JSON") == [])
check("空输入返回 []", pi("") == [])
check("非数组返回 []", pi('{"key":"a"}') == [])

# 真正跑一遍：给一个假的 LLM，确认画像真的被写进去
class _FakeProfile:
    def __init__(self):
        self.traits = []

    def set_trait(self, **kw):
        self.traits.append(kw)


class _FakeLlm:
    def complete_raw(self, *a, **kw):
        return '[{"key":"user_name","value":"FM","confidence":0.9,"category":"identity"}]'

    def summarize(self, *a, **kw):
        return ["- 这条是记忆条目，不是 JSON"]      # 老代码会拿到这个


pe = ProfileExtractor.__new__(ProfileExtractor)
pe._llm = _FakeLlm()
pe._profile = _FakeProfile()
pe._long_term = None
pe._lock = None
import threading as _th  # noqa: E402
pe._lock = _th.Lock()
pe.extract_from_text("我叫 FM")
check("画像真的被写进 UserProfile", len(pe._profile.traits) == 1,
      str(pe._profile.traits))
check("写入的 key/value 正确",
      pe._profile.traits and pe._profile.traits[0].get("key") == "user_name")

# ── B. 总结后台线程 → UI ──
print("\n== B. session 回调必须走信号（MED）==")
mw = read("src/ui/main_window.py")
check("on_topic_changed 走 relay 信号",
      "self.session.on_topic_changed = self._task_relay.topic_changed.emit" in mw)
check("on_input_blocked 走 relay 信号",
      "self.session.on_input_blocked = self._task_relay.input_blocked.emit" in mw)
check("不再直接把 QWidget 方法赋给 session 回调",
      "self.session.on_input_blocked = self.chat.set_input_blocked" not in mw
      and "self.session.on_topic_changed = self._on_session_topic_changed" not in mw)
check("relay 声明了 topic_changed / input_blocked 信号",
      "topic_changed = Signal(int)" in mw and "input_blocked = Signal(bool)" in mw)
check("两个信号都连到了真实槽",
      "self._task_relay.topic_changed.connect(self._on_session_topic_changed)" in mw
      and "self._task_relay.input_blocked.connect(self.chat.set_input_blocked)" in mw)

# ── C/D. Phase4/Phase5 与消息丢失 ──
print("\n== C/D. Phase 顺序与消息丢失（MED）==")
check("heavy_ready.emit 在 finally 里",
      re.search(r"finally:\s*\n(?:\s*#.*\n)*\s*self\._task_relay\.heavy_ready\.emit\(\)", mw)
      is not None)
check("heavy_ready 仍连到 _init_finalize",
      "self._task_relay.heavy_ready.connect(self._init_finalize)" in mw)
_um = mw.split("def _on_user_message", 1)[1].split("def _on_abort_chat", 1)[0]
check("_on_user_message 软访问 idle_talker", 'getattr(self, "idle_talker", None)' in _um)
check("_on_user_message 软访问 profile_extractor",
      'getattr(self, "profile_extractor", None)' in _um)
check("_inject_user_profile_context 软访问 user_profile",
      'getattr(self, "user_profile", None)' in mw)

# 真跑一次：属性缺失时不应抛异常，且消息仍然发出
class _Sess:
    def __init__(self):
        self.sent = []

    def send_message(self, t):
        self.sent.append(t)


class _Chat:
    def append_system_msg(self, *a):
        pass

    def reset_stream(self):
        pass

    def set_hide_on_focus_out(self, *a):
        pass


class _Pet:
    def on_user_message(self):
        pass


class _Probe:
    """故意不设 idle_talker / profile_extractor / user_profile"""

    def __init__(self):
        self.session = _Sess()
        self.chat = _Chat()
        self.pet_mgr = _Pet()
        self._last_assistant_text = ""

    def _is_profile_enabled(self):
        return True

    def _inject_user_profile_context(self):
        # 用真实实现（它内部软访问）
        from src.ui.main_window import MainWindow
        return MainWindow._inject_user_profile_context(self)


from src.ui.main_window import MainWindow  # noqa: E402

try:
    p = _Probe()
    MainWindow._on_user_message(p, "提早发的消息")
    check("Phase4 未完成时发消息不再抛异常", True)
    check("消息确实发给了 session", p.session.sent == ["提早发的消息"], str(p.session.sent))
except Exception as e:
    check("Phase4 未完成时发消息不再抛异常", False, f"{type(e).__name__}: {e}")

# ── E. 设置窗口尺寸刷新 ──
print("\n== E. 设置窗口尺寸不再冲掉拖拽结果（MED）==")
sw = read("src/ui/settings_window.py")
check("SettingsWindow 新增 showEvent", "def showEvent(self, event):" in sw)
_se = sw.split("def showEvent", 1)[1][:700]
check("showEvent 里同步了宽/高",
      "_ui_chat_width.setValue" in _se and "_ui_chat_height.setValue" in _se)
check("保存时仍用控件值（保持原逻辑）",
      "cfg.ui.chat_window_width = self._ui_chat_width.value()" in sw)

# 真跑一次：改 config → showEvent 后控件应跟着变
from src.utils.config import get_config  # noqa: E402
from src.ui.settings_window import SettingsWindow  # noqa: E402

_cfg_text = (ROOT / "config.json").read_text(encoding="utf-8")
try:
    cfg = get_config()
    cfg.ui.chat_window_width = 377
    cfg.ui.chat_window_height = 555
    w = SettingsWindow()
    w.show()
    app.processEvents()
    check("showEvent 把 config 的新尺寸同步进了控件",
          w._ui_chat_width.value() == 377 and w._ui_chat_height.value() == 555,
          f"{w._ui_chat_width.value()}x{w._ui_chat_height.value()}")
finally:
    (ROOT / "config.json").write_text(_cfg_text, encoding="utf-8")

# ── F. 工具回调登记表 ──
print("\n== F. 工具回调跨模块可达（MED）==")
init_src = read("tools/__init__.py")
check("tools 包有 LOGIN_CALLBACKS", "LOGIN_CALLBACKS" in init_src)
check("tools 包有 FILE_READY_CALLBACKS", "FILE_READY_CALLBACKS" in init_src)
ef = read("tools/endfield_tool.py")
check("endfield set_login_callback 写登记表",
      'LOGIN_CALLBACKS["endfield"] = cb' in ef)
check("endfield _finish_login 读登记表",
      "_get_login_callback()" in ef)
jc = read("tools/jmcomic_tool.py")
check("jmcomic set_file_ready_callback 写登记表",
      'FILE_READY_CALLBACKS["jmcomic"] = callback' in jc)
check("jmcomic 通知处读登记表", "_get_file_ready_callback()" in jc)

# 真跑：注册到 tools.endfield_tool，另一份"私有模块"也要能读到
import importlib.util  # noqa: E402

import tools.endfield_tool as _ef_pub  # noqa: E402

_sent = []
_ef_pub.set_login_callback(lambda ok, msg, role: _sent.append((ok, msg)))
spec = importlib.util.spec_from_file_location(
    "_prts_tool_endfield_probe", ROOT / "tools" / "endfield_tool.py")
_priv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(_priv)
_priv._finish_login(True, "扫码成功", {"nick": "FM"})
check("私有模块实例也能触发 MainWindow 注册的回调",
      _sent == [(True, "扫码成功")], str(_sent))

# ── G. 其余 LOW ──
print("\n== G. 其余 LOW 修复 ==")
se = read("tools/shell_exec.py")
check("shell_exec: PROJECT_ROOT 在 try 之前定义",
      se.index("PROJECT_ROOT = Path(__file__)") < se.index("from tools.file_ops import"))
check("shell_exec: 不再重复定义 PROJECT_ROOT", se.count("PROJECT_ROOT = Path(__file__)") == 1)

check("jmcomic: 用 tags 而不是 tag_list",
      "getattr(album, 'tag_list'" not in code_only(jc) and "getattr(album, 'tags'" in code_only(jc))
check("jmcomic: 用 page_count 而不是 photo_count",
      "photo_count" not in code_only(jc) and "page_count" in code_only(jc))

bib = read("tools/bilibili_tool.py")
check("bilibili: 定义了 _CDN_MIRRORS", "_CDN_MIRRORS = (" in bib)
check("bilibili: 不再用坏的 temp 前缀交换",
      "upos-sz-mirror_temp" not in code_only(bib))
check("bilibili: 用正则整体换主机",
      're.sub(' in bib and "upos-sz-mirror[a-z0-9]*" in bib)

ws = read("tools/web_search_tool.py")
check("web_search: 检查 resp.status_code", "resp.status_code != 200" in ws)
check("web_search: 识别 JSON 里的 detail/error",
      'data.get("detail")' in ws)

check("settings: _detect_models 按 target 取 Key",
      "self._vision_api_key.text().strip()" in sw
      and sw.count("api_key = (self._llm_key.text().strip()") >= 1)
check("settings: 导入了 CHARACTERS_DIR",
      "from src.core.pet_manager import CHARACTERS_DIR" in sw)

# ★ 1.9.0: ffprobe 元信息解析已搬进 tools/file_view.py（聊天统一走文件查看器），
#   守卫改到新位置检查：无视频流时不能对 None 调 .get() 崩。
_fv = read("tools/file_view.py")
check("file_view: ffprobe 无视频流不再崩（★1.9.0 搬到 file_view）",
      "if vids:" in _fv and "vids[0].get(" in _fv
      and "video_stream.get(" not in _fv)

ltm = read("tools/long_term_memory_tool.py")
_kn = ltm.split('if action == "keep_old"', 1)[1][:900]
check("LTM: keep_new 会真的 upsert",
      'if action == "keep_new":' in _kn and "db.ltm.upsert(" in _kn
      and 'elif action in ("replace", "merge", "keep_new")' not in ltm)

tm_src = read("src/core/tool_manager.py")
check("tool_manager: 没有 execute 的文件不再注册成工具",
      "if execute_func is None:" in tm_src)

check("已删除无人引用的 0 字节 src/core/fts_search.py",
      not (ROOT / "src" / "core" / "fts_search.py").exists())

print(f"\n==== 结果: {len(PASS)} PASS / {len(FAIL)} FAIL ====")
for f in FAIL:
    print("  FAIL:", f)
sys.exit(1 if FAIL else 0)
