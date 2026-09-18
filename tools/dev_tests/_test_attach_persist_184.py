# -*- coding: utf-8 -*-
"""★ 1.8.4 回归：
  A. 卡片放大 1.5 倍（3×1 → 4.5×1.5 cm）
  B. 文件卡片改成"靠头像、一条消息一个头像"（和图片同款行布局）
  C. 图片/文件卡片随消息落库 → 重启后能重建（用户反馈"重启后预览没了"）
  D. 退出时把设置（全局 + 当前角色）强制落盘；切角色前先存旧角色

用法: python tools/dev_tests/_test_attach_persist_184.py
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
PX_PER_CM = 96 / 2.54


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))


from src.ui.chat_widget import ChatWidget  # noqa: E402

w = ChatWidget()
w.resize(420, 640)
w.show()
app.processEvents()

# ── A. 尺寸 1.5 倍 ──
print("== A. 卡片尺寸放大 1.5 倍 ==")
want_w, want_h = w.card_size_px()
print(f"  {want_w}×{want_h}px = {want_w/PX_PER_CM:.2f}cm × {want_h/PX_PER_CM:.2f}cm")
check("CARD_WIDTH_CM == 4.5", ChatWidget.CARD_WIDTH_CM == 4.5, str(ChatWidget.CARD_WIDTH_CM))
check("CARD_HEIGHT_CM == 1.5", ChatWidget.CARD_HEIGHT_CM == 1.5, str(ChatWidget.CARD_HEIGHT_CM))
check("像素宽 ≈ 170", abs(want_w - 170) <= 3, str(want_w))
check("像素高 ≈ 57", abs(want_h - 57) <= 3, str(want_h))

target = ROOT / "data" / "桌宠私有data" / "office" / "pptx" / "deepseek_intro.pptx"
assert target.exists(), target

# ── B/C. 标记 → 渲染 ──
print("\n== B. 文件卡片与消息同排、共用一个头像 ==")
marker = ChatWidget.file_marker(target.resolve(), "15.0KB", "用户上传")
html = w._render_message_html("user", f"{marker}\n[上传文件: {target.name}]\n内容预览...")
check("渲染结果里不含原始标记", "[[FILE:" not in html, html[:80])
# ★ 1.9.0: 卡片底色不再由 HTML 画（Qt 只能画方角），改用「显式高度的卡片表格」
#   作为识别标志 —— border="0" 只有卡片表格有（消息外壳表是 cellpadding="3"）。
check("渲染结果里有卡片表格（★1.9.0 底色改自绘）", 'border="0"' in html)
check("卡片尺寸写进了 HTML", f'width="{want_w}" height="{want_h}"' in html)
check("整条消息只有一个头像", html.count("<img src=") == 1, f"img 数={html.count('<img src=')}")
check("用户消息：卡片在头像左侧的单元格",
      html.find('border="0"') < html.find("AVATAR")
      or html.find('border="0"') < html.rfind("<img src="),
      "卡片在头像之前")
# 方向：用户版靠右（先内容单元格再头像单元格）
check("用户版是「内容在前、头像在后」",
      html.find('align="right"') < html.find("<img src="))
bot_html = w._render_message_html("assistant", f"{marker}\n桌宠发来的文件")
check("桌宠版是「头像在前、内容在后」",
      bot_html.find("<img src=") < bot_html.find('border="0"'),
      f"img@{bot_html.find('<img src=')} card@{bot_html.find('border=\"0\"')}")

print("\n== C. 标记随消息落库 → 重启后能重建 ==")
check("file_marker 生成 [[FILE:...]] 格式", marker.startswith("[[FILE:") and marker.endswith("]]"))
check("标记里带绝对路径", str(target.resolve()) in marker)
check("标记里带大小", "15.0KB" in marker)
clean, files = ChatWidget._split_file_markers(f"abc {marker} def")
check("解析回 (path,size,label)", len(files) == 1 and files[0][1] == "15.0KB", str(files))
check("解析后正文只剩其它文字",
      clean.replace("\u0000FILE\u0000", "").strip() == "abc  def", repr(clean))

# 文件不存在 → 友好提示而不是原始标记
gone = ChatWidget.file_marker(ROOT / "data" / "_not_exist_184.pptx", "1KB", "用户上传")
gh = w._render_message_html("user", gone)
check("文件已不在时给出提示", "文件已不在" in gh and "[[FILE:" not in gh, gh[:120])

# 图片标记（markdown）随消息落库
img = ROOT / "data" / "avatars" / "user_avatar.jpg"
if img.exists():
    ih = w._render_message_html("user", f"![{img.name}]({img.as_uri()})\n看看这个")
    check("图片标记渲染成 <img> 缩略图", "<img src=" in ih and "file:///" in ih)
    check("图片标记不会以原文显示", "![user_avatar" not in ih)

# ── D. 退出落盘 ──
print("\n== D. 退出时设置强制落盘 ==")
# ★ 安全第一：全程只在**临时副本**上操作，绝不碰 data/桌宠形象 里的真实数据。
#   （上一版这里 rmtree 了真实目录，而运行中的程序正锁着 GIF，
#    结果删掉了 11 个文件 —— 已从备份恢复。这个教训写在这里，别再犯。）
import src.core.pet_manager as _pm  # noqa: E402
from src.utils.config import get_config  # noqa: E402
from src.ui.settings_window import SettingsWindow  # noqa: E402

REAL_CHARS = _pm.CHARACTERS_DIR
CFG_PATH = ROOT / "config.json"
CFG_TEXT_BAK = CFG_PATH.read_text(encoding="utf-8")   # 内存备份，最稳
# 用工作区内的临时目录（系统 temp 在沙箱下不可写）
TMP_ROOT = ROOT / "tools" / "dev_tests" / "_tmp_chars_184"
if TMP_ROOT.exists():
    shutil.rmtree(TMP_ROOT, ignore_errors=True)
TMP_CHARS = TMP_ROOT / "桌宠形象"
shutil.copytree(REAL_CHARS, TMP_CHARS)

_check_done = False
try:
    _pm.CHARACTERS_DIR = TMP_CHARS          # 只影响本次测试进程
    sw = SettingsWindow()
    char = sw._selected_character
    print(f"  当前选中角色: {char}（临时副本: {TMP_CHARS}）")
    check("设置窗口有 flush_current_character_settings",
          callable(getattr(sw, "flush_current_character_settings", None)))
    check("设置窗口有 flush_all_on_exit",
          callable(getattr(sw, "flush_all_on_exit", None)))

    # 改人设但**不点保存**，直接走退出落盘
    sw._pet_prompt_edit.setPlainText("PROBE_PERSONA_184")
    sw._pet_scale.setValue(123)
    sw.flush_all_on_exit()

    sfile = TMP_CHARS / char / "settings.json"
    saved = json.loads(sfile.read_text(encoding="utf-8"))
    check("未点保存的改动被退出落盘救回",
          (saved.get("性格", {}) or {}).get("free_text") == "PROBE_PERSONA_184",
          repr((saved.get("性格", {}) or {}).get("free_text"))[:60])
    check("缩放也落盘了", abs(float(saved.get("scale", 0)) - 1.23) < 1e-6,
          str(saved.get("scale")))
    check("config.json 里同步了角色字段",
          get_config().pet.character == char, get_config().pet.character)

    # 真实角色目录必须毫发无损
    real_persona = json.loads(
        (REAL_CHARS / char / "settings.json").read_text(encoding="utf-8")
    ).get("persona_prompt", "")
    check("真实的 data/桌宠形象 没被写脏",
          "PROBE_PERSONA_184" not in real_persona, real_persona[:40])

    # 切角色前先存旧角色
    calls = []
    orig_flush = sw.flush_current_character_settings

    def _spy():
        calls.append(sw._selected_character)
        return orig_flush()

    sw.flush_current_character_settings = _spy
    names = list(sw._character_cards.keys())
    if len(names) >= 2:
        other = [n for n in names if n != char][0]
        sw._on_character_card_clicked(other)
        check("点另一个角色卡片时会先 flush 旧角色", calls == [char], str(calls))
        check("确实切到了新角色", sw._selected_character == other, sw._selected_character)
        calls.clear()
        sw._on_character_card_clicked(other)
        check("点同一个角色不重复 flush", calls == [], str(calls))
    else:
        print("  (只有一个角色，跳过切角色 flush 检查)")
    _check_done = True
finally:
    # 逐步还原，任何一步失败都不影响后面的还原
    try:
        _pm.CHARACTERS_DIR = REAL_CHARS
    except Exception:
        pass
    try:
        CFG_PATH.write_text(CFG_TEXT_BAK, encoding="utf-8")
    except Exception as e:
        print(f"  !! config.json 还原失败: {e}")
    shutil.rmtree(TMP_ROOT, ignore_errors=True)
    print("  已还原：config.json（内存备份） + CHARACTERS_DIR 指向；临时副本已删")

# 真实数据必须完好
real_count = len(list(REAL_CHARS.rglob("*.*")))
check("真实 data/桌宠形象 文件数正常", real_count >= 20, f"{real_count} 个文件")
check("config.json 里没有探针残留",
      "PROBE_PERSONA_184" not in CFG_PATH.read_text(encoding="utf-8"))

# _quit 里必须调用 flush
src_mw = (ROOT / "src" / "ui" / "main_window.py").read_text(encoding="utf-8")
quit_body = src_mw.split("def _quit", 1)[1][:900]
check("_quit 调用了 settings_win.flush_all_on_exit",
      "flush_all_on_exit()" in quit_body)
check("_quit 有 config.save() 兜底", "self.config.save()" in quit_body)

print(f"\n==== 结果: {len(PASS)} PASS / {len(FAIL)} FAIL ====")
for f in FAIL:
    print("  FAIL:", f)
sys.exit(1 if FAIL else 0)
