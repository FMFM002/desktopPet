"""1.6.0 GUI 层自测：ChatWidget 侧栏 / 图片预览 / 文件卡片路由 / 形象状态机（不启动完整主窗口）"""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("DEEPSEEK_API_KEY", "test")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import src.core.database as _dbmod
_TMP = Path(__file__).resolve().parents[2] / "_test_tmp_dbs"
_TMP.mkdir(exist_ok=True)
_dbmod.DB_DIR = _TMP

ok = True


def check(name, cond, extra=""):
    global ok
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (f"  {extra}" if extra else ""))
    if not cond:
        ok = False


from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt
app = QApplication(sys.argv)

BASE = Path(__file__).resolve().parents[2]

# ── 1. 侧栏 = 聊天窗口左侧外面的独立窗口（不影响聊天窗口尺寸/内容） ──
print("== 1. 侧栏（窗外独立窗口） ==")
from src.ui.chat_widget import ChatWidget
w = ChatWidget()
w.resize(420, 640)
w.move(400, 200)
# offscreen 环境没有窗口焦点，会触发"失焦自动隐藏"，测试里关掉（真机由 MainWindow 控制）
w.set_hide_on_focus_out(False)
w.show()
for _ in range(30):
    app.processEvents()
    time.sleep(0.01)
check("★1.9.1 聊天窗口里已经没有侧栏窗口了（SidebarWindow 已删）",
      not hasattr(w, "_sidebar_win")
      and not hasattr(w, "_sidebar_visible")
      and not hasattr(w, "_toggle_sidebar_btn"))
check("聊天窗口宽度不受任何侧栏影响", w.width() == 420, f"w={w.width()}")
chat_w_expanded = w._chat_display.width()

w.move(600, 260)
for _ in range(20):
    app.processEvents()
    time.sleep(0.01)

w.resize(500, 700)
for _ in range(20):
    app.processEvents()
    time.sleep(0.01)
check("★1.9.1 聊天窗口缩放后尺寸被如实采用（没有侧栏宽度参与加减）",
      (w.width(), w.height()) == (500, 700), f"{w.width()}x{w.height()}")

# ★ 1.9.1: 原来这里是一大段"侧栏收起/展开/跟随/贴合"的断言（约 50 行）。
#   左侧栏整个删除后它们全部失去对象，替换成下面这组"新布局"断言：
#   左上角 = 形象名 + tokens；右上角 = 开启新话题 / 总结旧话题 / ＋ / ≡。
print("== 2. 标题栏新布局（形象名 + tokens / 话题按钮在最前）==")
w.set_conversation_info("deepseek", "聊聊天气", 12, 3456)
check("左上角显示形象名", "deepseek" in w._id_label.text(), w._id_label.text())
check("左上角显示 tokens 计数（★ 1.9.1 起带 k/M 单位，3456 → 3.5k）",
      "3.5k tokens" in w._id_label.text(), w._id_label.text())
check("★★token 数为 0 时也要显示 tokens，不许退化成「N 条消息」"
      "（你 01:4x 报的 bug：刚开始显示的是「0 条消息」）",
      (lambda: (w.set_conversation_info("x", "", 7, 0),
                "条消息" not in w._id_label.text()
                and w._id_label.text() == "x · 0 tokens")[1])(),
      w._id_label.text())
check("★消息条数没丢，挪进了 tooltip",
      "7 条消息" in w._id_label.toolTip(), w._id_label.toolTip())
w.set_conversation_info("deepseek", "聊聊天气", 12, 3456)
check("左上角不再显示话题名（话题名只进 tooltip）",
      "聊聊天气" not in w._id_label.text() and "聊聊天气" in w._id_label.toolTip())
check("话题按钮存在且文案正确",
      w._new_topic_btn.text() == "开启新话题"
      and w._summarize_topic_btn.text() == "总结旧话题")
check("★两个话题按钮在右上角按钮组里，且排在 ＋ 和 ≡ 之前",
      w._new_topic_btn.parent() is not None
      and w._summarize_topic_btn.parent() is not None
      and w._new_topic_btn.y() <= w._summarize_topic_btn.y()
      and w._menu_btn.y() <= w._new_topic_btn.y() + 40)
_seen = []
w.new_topic_request.connect(lambda: _seen.append("new"))
w._new_topic_btn.click()
w.summarize_topic_request.connect(lambda: _seen.append("sum"))
w._summarize_topic_btn.click()
check("两个按钮点击会发出对应的请求信号", _seen == ["new", "sum"], str(_seen))
check("★书签相关的东西全没了（列表/跳转/删除信号）",
      not hasattr(w, "_topic_list")
      and not hasattr(w, "topic_navigate_request")
      and not hasattr(w, "topic_delete_request")
      and not hasattr(w, "update_topic_list")
      and not hasattr(w, "scroll_to_topic_mark"))

# 聊天区 # 分割线仍然保留（只是不再可点跳转）
w.clear_chat()
w.append_topic_divider("当前话题", topic_id="CUR")
w._append_message("user", "测试消息")
text = w._chat_display.toPlainText()
check("分割线使用 # 样式", "#" in text)
check("★分割线里的隐形跳转标记也去掉了（书签删了就不再写）",
      "⟦TCUR⟧" not in text)

# ── 3. 图片内联预览（用户上传同款路径） ──
print("== 3. 图片预览 ==")
img = None
for cand in (BASE / "data" / "桌宠形象").rglob("*.jpg"):
    img = cand
    break
check("找到测试图片", img is not None, str(img))
if img:
    w.append_image_preview(str(img))
    html = w._chat_display.toHtml()
    check("预览插入图片资源", "img" in html.lower())
    check("预览不含多余文字", "点击查看原图" not in w._chat_display.toPlainText())

# ── 3b. ★ 相对路径也必须能预览（LLM 工具返回的是相对路径） ──
print("== 3b. 相对路径图片预览（回归日志 14:25:58） ==")
rel = "data/桌宠形象/deepseek/头像.jpg"
if (BASE / rel).exists():
    before_len = len(w._chat_display.toPlainText())
    w.append_image_preview(rel)     # 相对路径，旧版会抛 as_uri 错误
    html2 = w._chat_display.toHtml()
    check("相对路径未报错并插入图片", "img" in html2.lower())
    check("未出现'图片预览失败'提示",
          "预览失败" not in w._chat_display.toPlainText()[before_len:])
else:
    check("相对路径测试图片存在", False, rel)

# ── 3c. ★ 桌宠发的图片必须在左侧（与它的文字消息同侧） ──
print("== 3c. 图片左右分侧 ==")
if img:
    w.clear_chat()
    w.append_image_preview(str(img), from_bot=True)
    html_bot = w._chat_display.toHtml()
    w.clear_chat()
    w.append_image_preview(str(img), from_bot=False)
    html_user = w._chat_display.toHtml()
    check("桌宠图：头像单元格在图片前（左对齐）",
          html_bot.find("<td") < html_bot.find("<img") and "left" in html_bot.lower())
    check("用户图：图片在头像前（右对齐）",
          html_user.find("<img") < html_user.rfind("<td") and "right" in html_user.lower())

# ── 4. LLM markdown 图片解析 ──
print("== 4. LLM 图片解析 ==")
if img:
    text = f"给你看这张 {img.as_posix()} 图\n![头像]({img})"
    clean, imgs = ChatWidget._split_inline_images(text)
    check("markdown 图片被拆出", len(imgs) == 1 and imgs[0][0] == "头像")
    check("残留文本不含 ![]", "![" not in clean)
    html = w._render_inline_image_html(str(img), "头像")
    check("渲染出 img 标签", "<img" in html and "file:///" in html)
    html_bad = w._render_inline_image_html(str(BASE / "不存在.png"), "x")
    check("缺失图片给提示", "图片不可用" in html_bad)

# ── 5. 形象状态机：启动 GIF 只播一遍 → 待机，且待机 tick 能推进 ──
print("== 5. 形象状态机 ==")
from src.core.pet_manager import PetManager, CHARACTERS_DIR


class FakePet:
    def __init__(self):
        self.calls = []

    def set_character(self, n):
        self.calls.append(("character", n))

    def set_gif_file(self, p, once=False):
        self.calls.append(("gif", Path(p).name, once))

    def set_scale(self, s):
        self.calls.append(("scale", s))

    def set_behavior(self, b):
        self.calls.append(("behavior", b))


mgr = PetManager(FakePet())
mgr._do_scan(None)
names = list(mgr.characters.keys())
check("扫描到角色", len(names) >= 1, str(names))
deep = "deepseek" if "deepseek" in names else names[0]
info = mgr.characters[deep]
check("deepseek 有待机列表", len(info.settings.get("idle_gifs", []) or []) >= 1,
      str(info.settings.get("idle_gifs")))

mgr._do_switch_character((deep, False))
check("启动 GIF 以 once=True 播放",
      any(c[0] == "gif" and len(c) == 3 and c[2] is True for c in mgr.pet.calls),
      str(mgr.pet.calls[:3]))
check("处于等待播完状态", mgr._startup_pending is True)
mgr._do_startup_gif_finished(None)
check("播完后退出等待", mgr._startup_pending is False)
check("播完后切到待机 GIF",
      len(mgr.pet.calls) >= 2 and mgr.pet.calls[-1][0] == "gif",
      str(mgr.pet.calls[-1]))
check("待机轮播已排程", mgr._idle_timer is not None)
# 手工触发一次 tick。
# ★ 1.9.1：待机计时器改成"范围内随机"挑（不再按下标顺序推进），
#   所以断言换成**行为**：确实切了一张 GIF，而不是看内部下标。
_n_before = len(mgr.pet.calls)
mgr._idle_tick()
check("idle tick 切了一张待机 GIF（范围内随机挑）",
      len(mgr.pet.calls) > _n_before and mgr.pet.calls[-1][0] == "gif",
      str(mgr.pet.calls[-1]))
check("idle tick 排了下一个定时器", mgr._idle_timer is not None)
# 二次调用不应再重复 once
mgr._do_startup_gif_finished(None)
check("重复完成事件被忽略", mgr._startup_pending is False)

print("\n结果:", "ALL PASS" if ok else "FAILED")
mgr._stop_idle_timer()
w.close()
app.quit()
sys.exit(0 if ok else 1)
