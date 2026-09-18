# -*- coding: utf-8 -*-
"""★ 1.8.2 回归：设置界面「加载 ↔ 保存」双向对称性 + 真正落盘。

覆盖的 bug：
  D1  _load_values 用字符串拼属性名，ui_bg_color/chat_bg_color 拼错 →
      底部两种底色加载不进控件、保存时又被写回默认值（每次启动被刷新）
  D2  vision.api_key 被 config.json 丢弃且不进 .env → 每次启动被 LLM Key 覆盖
  D3  config.save() 无锁并发写同一个 .tmp → 偶发保存失败

用法: python tools/dev_tests/_test_settings_persist_182.py
"""
import json
import os
import shutil
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication(sys.argv)

CFG = ROOT / "config.json"
ENV = ROOT / ".env"
BAK = ROOT / "tools" / "dev_tests" / "_persist_bak"
BAK.mkdir(parents=True, exist_ok=True)

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))


def backup():
    if CFG.exists():
        shutil.copy2(CFG, BAK / "config.json")
    if ENV.exists():
        shutil.copy2(ENV, BAK / ".env")
    chars = ROOT / "data" / "桌宠形象"
    if chars.exists():
        (BAK / "形象").mkdir(exist_ok=True)
        for d in chars.iterdir():
            sf = d / "settings.json"
            if sf.exists():
                shutil.copy2(sf, BAK / "形象" / f"{d.name}__settings.json")


def restore():
    if (BAK / "config.json").exists():
        shutil.copy2(BAK / "config.json", CFG)
    if (BAK / ".env").exists():
        shutil.copy2(BAK / ".env", ENV)
    for f in (BAK / "形象").glob("*__settings.json") if (BAK / "形象").exists() else []:
        name = f.name.replace("__settings.json", "")
        target = ROOT / "data" / "桌宠形象" / name / "settings.json"
        if target.parent.exists():
            shutil.copy2(f, target)


print("== 0. 备份 config.json / .env / 角色 settings.json ==")
backup()
print("   已备份到", BAK)

try:
    from src.utils.config import get_config  # noqa: E402

    cfg = get_config()

    # ── 1. 写入一组「有辨识度的」设置并落盘 ──
    print("\n== 1. 写入可辨识设置并保存 ==")
    cfg.ui.ui_bg_color = "#112233"
    cfg.ui.chat_bg_color = "#445566"
    cfg.ui.user_bubble_color = "#778899"
    cfg.ui.user_text_color = "#aabbcc"
    cfg.ui.bot_bubble_color = "#ddeeff"
    cfg.ui.bot_text_color = "#001122"
    cfg.ui.chat_window_width = 351
    cfg.ui.chat_window_height = 641
    cfg.ui.chat_distance = 17
    # ★ 1.9.1: sidebar_width 已从 UIConfig 删除（聊天窗口左侧栏整个移除）
    cfg.ui.opacity = 0.77
    cfg.idle.enabled = False
    cfg.idle.interval_minutes = 23
    cfg.monitor.sys_interval = 31
    cfg.monitor.mouse_min_move = 9
    cfg.tools.download_path = "D:/tmp/dl_probe"
    cfg.vision.ollama_model = "llava:probe"
    cfg.vision.ocr_lang = "chi_sim+probe"
    cfg.save()

    on_disk = json.loads(CFG.read_text(encoding="utf-8"))
    check("config.json 落盘 ui_bg_color", on_disk["ui"]["ui_bg_color"] == "#112233",
          on_disk["ui"].get("ui_bg_color"))
    check("config.json 落盘 chat_bg_color", on_disk["ui"]["chat_bg_color"] == "#445566")
    check("config.json 落盘 idle.interval_minutes", on_disk["idle"]["interval_minutes"] == 23)
    check("config.json 不落盘 api_key",
          "api_key" not in on_disk.get("vision", {}))
    check("config.json 落盘 tools.download_path",
          on_disk["tools"]["download_path"] == "D:/tmp/dl_probe")

    # ── 2. 打开设置窗口，检查 _load_values 是否把值装进正确的控件属性 ──
    print("\n== 2. 设置窗口 _load_values ↔ _do_save_settings 属性名一致性 ==")
    from src.ui.settings_window import SettingsWindow  # noqa: E402

    sw = SettingsWindow()

    PAIRS = [
        ("ui_bg_color", "_ui_bg_color_val", "_ui_bg_color_btn"),
        ("chat_bg_color", "_ui_chat_bg_color_val", "_ui_chat_bg_color_btn"),
        ("user_bubble_color", "_ui_user_bubble_val", "_ui_user_bubble_btn"),
        ("user_text_color", "_ui_user_text_val", "_ui_user_text_btn"),
        ("bot_bubble_color", "_ui_bot_bubble_val", "_ui_bot_bubble_btn"),
        ("bot_text_color", "_ui_bot_text_val", "_ui_bot_text_btn"),
    ]
    for field, val_attr, btn_attr in PAIRS:
        want = getattr(cfg.ui, field)
        got = getattr(sw, val_attr, None)
        check(f"_load_values 装入 {val_attr} = {field}", got == want, f"want={want} got={got}")
        btn = getattr(sw, btn_attr, None)
        check(f"{btn_attr} 色块已按配置着色",
              btn is not None and want in btn.styleSheet(),
              btn.styleSheet() if btn else "按钮不存在")

    # ── 3. 控件数值是否被正确加载 ──
    print("\n== 3. 数值控件加载 ==")
    for label, got, want in [
        ("chat_window_width", sw._ui_chat_width.value(), 351),
        ("chat_window_height", sw._ui_chat_height.value(), 641),
        ("chat_distance", sw._ui_chat_distance.value(), 17),
        # ★ 1.9.1: 「侧栏宽度」控件已从设置界面删除
        ("opacity*100", sw._ui_opacity.value(), 77),
        ("idle_interval", sw._idle_interval.value(), 23),
        ("idle_enabled", sw._idle_enabled.isChecked(), False),
        ("sys_interval", sw._sys_interval.value(), 31),
        ("mouse_min_move", sw._ms_min_move.value(), 9),
        ("download_path", sw._download_path.text(), "D:/tmp/dl_probe"),
        ("ollama_model", sw._vision_ollama_model_combo.currentText(), "llava:probe"),
    ]:
        check(f"加载 {label}", got == want, f"want={want} got={got}")

    # ── 4. 在 UI 上改成新值 → 保存 → 重新读盘 ──
    print("\n== 4. UI 改值 → 保存 → 重新读盘 ==")
    sw._ui_bg_color_val = "#0a0b0c"
    sw._ui_chat_bg_color_val = "#0d0e0f"
    sw._ui_user_bubble_val = "#101112"
    sw._ui_chat_width_was = None
    sw._ui_chat_width.setValue(333)
    # ★ 1.9.1: 不再设 _ui_sidebar_width（控件和字段都删了）
    sw._ui_opacity.setValue(66)
    sw._download_path.setText("E:/new/dl")

    # 绕开太重/无关的部分（工具扫描、真实信号外发）
    sw._get_tools_policy_changes = lambda: {}
    sw._save_character_settings = lambda *a, **k: None

    sw._do_save_settings(cfg, cfg.llm.provider)
    time.sleep(1.5)

    sw2 = json.loads(CFG.read_text(encoding="utf-8"))
    check("保存 ui_bg_color=#0a0b0c", sw2["ui"]["ui_bg_color"] == "#0a0b0c",
          sw2["ui"].get("ui_bg_color"))
    check("保存 chat_bg_color=#0d0e0f", sw2["ui"]["chat_bg_color"] == "#0d0e0f",
          sw2["ui"].get("chat_bg_color"))
    check("保存 user_bubble_color=#101112", sw2["ui"]["user_bubble_color"] == "#101112")
    check("保存 chat_window_width=333", sw2["ui"]["chat_window_width"] == 333)
    check("★1.9.1: 不再往 config.json 写 sidebar_width",
          "sidebar_width" not in sw2["ui"], str(sorted(sw2["ui"].keys())))
    check("保存 opacity=0.66", abs(sw2["ui"]["opacity"] - 0.66) < 1e-6)
    check("保存 download_path", sw2["tools"]["download_path"] == "E:/new/dl")
    check("保存后 vision.api_key 仍不落 config.json",
          "api_key" not in sw2.get("vision", {}))

    # ── 5. 视觉 Key 单独存 .env ──
    print("\n== 5. 视觉 API Key 持久化到 .env ==")
    os.environ.pop("DEEPSEEK_VISION_API_KEY", None)
    cfg.vision.api_key = "sk-vision-probe"
    cfg.save()
    env_text = ENV.read_text(encoding="utf-8") if ENV.exists() else ""
    check(".env 写入 DEEPSEEK_VISION_API_KEY",
          "DEEPSEEK_VISION_API_KEY=sk-vision-probe" in env_text)
    check("config.json 仍然不含视觉 Key", "sk-vision-probe" not in CFG.read_text(encoding="utf-8"))

    os.environ["DEEPSEEK_VISION_API_KEY"] = "sk-vision-probe"
    from src.utils.config import AppConfig  # noqa: E402
    fresh = AppConfig()
    check("新进程式重新加载后视觉 Key 保留",
          fresh.vision.api_key == "sk-vision-probe", fresh.vision.api_key)

    # 视觉 Key 与 LLM Key 相同时不写专用变量
    cfg.vision.api_key = cfg.llm.api_key or "same-as-llm"
    cfg.save()
    env_text2 = ENV.read_text(encoding="utf-8") if ENV.exists() else ""
    check("视觉 Key == LLM Key 时不写专用变量（回落语义）",
          "DEEPSEEK_VISION_API_KEY=sk-vision-probe" not in env_text2)

    # ── 6. 并发保存不炸 ──
    print("\n== 6. 并发 save() 序列化 ==")
    import threading  # noqa: E402
    errs = []

    def _hammer():
        try:
            for _ in range(8):
                cfg.save()
        except Exception as e:  # noqa: BLE001
            errs.append(repr(e))

    ts = [threading.Thread(target=_hammer) for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    check("4×8 并发 save() 无异常", not errs, str(errs[:3]))
    check("并发后 config.json 仍是合法 JSON",
          isinstance(json.loads(CFG.read_text(encoding="utf-8")), dict))

finally:
    print("\n== 7. 还原 config.json / .env / 角色 settings.json ==")
    restore()
    print("   已还原")

print(f"\n==== 结果: {len(PASS)} PASS / {len(FAIL)} FAIL ====")
if FAIL:
    for f in FAIL:
        print("  FAIL:", f)
sys.exit(1 if FAIL else 0)
