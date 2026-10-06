# -*- coding: utf-8 -*-
"""★★ 1.9.4：**"本地已有模型"路径扫描 + 向导内下载** 的守卫（用户 2026-10-06 要求）。

## 用户原话
    "5，的下载log希望显示在引导里面，让用户看见正在下载模型。
     下载之前希望先询问用户有没有所需文件，要求填写路径，
     **扫描路径（深度为1）**，确认是正确的模型就把路径写到配置里面。"

目的很实际：GENIE 那套 1855MB，**谁本地已经有一份就不该再下一次**。

## 钉什么
  A. `models.probe_dir()` 的**深度 1**语义：
       直指模型根 ✅ ／ **指它的上一级**（模型在子目录里）✅ ／
       空目录 ❌ ／ 不存在的路径 ❌ ／ **指到一个文件**（不是目录）❌
     ★ 其中"指上一级"正是 depth=1 存在的意义 —— 用户习惯各不相同。
  B. `models.bind_path()` 写的是**现成的配置槽**（不新增键）：
       genie→tts.genie_data_dir/genie_character_dir · whisper→stt.whisper_cli_path/
       whisper_model_path · easyocr→**没有槽**（目录写死）→ 复制过去。
  C. 向导里每个缺失的模型都有：路径输入框 / 「扫描并写入」/ 「下载」/ **日志框**；
  D. ★★ **下载线程真的被 start 了** —— 我第一版写了 `_work` 却忘了
     `Thread(...).start()`，那样按钮点了**毫无反应**。这种错只能靠断言盯。
  E. 下载日志经**信号**回 GUI 线程（Qt 规矩：控件只能在 GUI 线程碰）；
  F. 扫描路径为空时给提示而不是静默。
"""
import os
import shutil
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


from src.core import models as M                        # noqa: E402

_B = ROOT / "临时文件" / "_scratch_dev" / "tmp_probe_guard"
if _B.exists():
    shutil.rmtree(_B, ignore_errors=True)
_B.mkdir(parents=True)

print("== A. probe_dir 的**深度 1** 语义 ==")
# ① 直指模型根
_d1 = _B / "d1"
(_d1 / "GenieData").mkdir(parents=True)
(_d1 / "CharacterModels").mkdir()
# ② 指上一级（模型在子目录里）★ 这就是 depth=1 的意义
_d2 = _B / "d2" / "genie"
(_d2 / "GenieData").mkdir(parents=True)
# ③ 空目录
_d3 = _B / "d3"
_d3.mkdir()
(_d3 / "readme.txt").write_text("x", encoding="utf-8")
# ④ 一个文件
_f = _B / "afile.txt"
_f.write_text("x", encoding="utf-8")

_ok1, _p1, _ = M.probe_dir("genie", str(_d1))
check("★① 直指模型根 → 认得出来", _ok1 and _p1 == _d1, str(_p1))
_ok2, _p2, _why2 = M.probe_dir("genie", str(_B / "d2"))
check("★★② 指**上一级**（模型在子目录里）→ 也能认出来（**这正是 depth=1 的意义**）",
      _ok2 and _p2 == _d2, f"{_p2}")
check("★③ 空目录 → 不认（并说明期望的结构）",
      not M.probe_dir("genie", str(_d3))[0] and "GenieData" in M.probe_dir("genie", str(_d3))[2])
check("★④ 指到一个文件（不是目录）→ 不认", not M.probe_dir("genie", str(_f))[0])
check("★⑤ 不存在的路径 → 不认", not M.probe_dir("genie", str(_B / "nope"))[0])
check("★⑥ 未知模型 key → 不认", not M.probe_dir("不存在的模型", str(_d1))[0])

_w = _B / "whisper"
_w.mkdir()
(_w / "ggml-small.bin").write_bytes(b"x")
(_w / "whisper-cli.exe").write_bytes(b"x")
check("★whisper：ggml-*.bin / whisper-cli.exe 都算特征", M.probe_dir("whisper", str(_w))[0])

print("\n== B. bind_path 写的是**现成配置槽**（不新增键）==")
from src.utils.config import get_config              # noqa: E402

_cfg = get_config()
_snap = (_cfg.stt.whisper_cli_path, _cfg.stt.whisper_model_path)
try:
    _ok, _msg = M.bind_path("whisper", _w)
    check("★whisper 写进 stt.whisper_cli_path / whisper_model_path",
          _ok and _cfg.stt.whisper_cli_path.endswith("whisper-cli.exe")
          and _cfg.stt.whisper_model_path.endswith("ggml-small.bin"), _msg[:50])
    _ok2, _msg2 = M.bind_path("genie", _d1)
    check("★genie 写进 tts.genie_data_dir / genie_character_dir",
          _ok2 and _cfg.tts.genie_data_dir.endswith("GenieData")
          and _cfg.tts.genie_character_dir.endswith("CharacterModels"), _msg2[:50])
    _ok3, _msg3 = M.bind_path("whisper", _d3)        # 空目录
    check("★绑一个不像的目录 → 拒绝，不乱写配置", not _ok3, _msg3[:40])
finally:
    _cfg.stt.whisper_cli_path, _cfg.stt.whisper_model_path = _snap
    _cfg.tts.genie_data_dir = ""
    _cfg.tts.genie_character_dir = ""
    _cfg.save()                                       # ★ 别把测试值留在真实配置里

print("\n== C~F. 向导里的模型区块 ==")
from PySide6.QtWidgets import QApplication            # noqa: E402
_app = QApplication.instance() or QApplication([])
from src.ui.first_run_wizard import FirstRunWizard    # noqa: E402

_w2 = FirstRunWizard(base=_B)                         # 空目录 = 没有模型
try:
    # ★★ 1.9.4：模型行现在在**第 4 页（检测/下载页）**，而且**只列出勾了的**。
    #   所以要先勾上、再进那一页，内容才会生成（这正是用户要的"按勾选排版"）。
    for _k2 in list(_w2._checks):
        if _k2 in ("whisper", "easyocr"):
            _w2._checks[_k2].setChecked(True)
    _w2._goto(3)
    check("★★每个缺失的模型都有：路径框 / 扫描 / 下载",
          bool(_w2._model_rows) and all(
              k in r for r in _w2._model_rows.values()
              for k in ("edit", "btn_dl", "res")),
          str(sorted(_w2._model_rows)))
    check("★★**只为你勾了的**项目出内容（没勾的一个字都不显示）",
          set(_w2._model_rows) <= {"whisper", "easyocr"},
          str(sorted(_w2._model_rows)))
    check("★★有**下载日志框**（用户要求『显示在引导里面，让用户看见正在下载』）",
          hasattr(_w2, "_dl_log"))
    check("★★下载日志经**信号**回 GUI 线程（Qt 规矩：控件只能在 GUI 线程碰）",
          hasattr(_w2, "_sig_line") and hasattr(_w2, "_sig_done"))

    _k = sorted(_w2._model_rows)[0]
    _w2._on_scan(_k)
    check("★路径为空时给**提示**（不是静默什么都不做）",
          "请先填写" in _w2._model_rows[_k]["res"].text(),
          _w2._model_rows[_k]["res"].text()[:20])

    _src = (ROOT / "src" / "ui" / "first_run_wizard.py").read_text(encoding="utf-8")
    check("★★★下载线程**真的被 start 了**"
          "（我第一版写了 _work 却忘了 start —— 那样按钮点了毫无反应）",
          "threading.Thread(target=_work" in _src
          and ".start()" in _src.split("threading.Thread(target=_work")[1][:120])
    check("★用了 daemon 线程（窗口关了它不该挂着进程）",
          "daemon=True" in _src)
finally:
    shutil.rmtree(_B, ignore_errors=True)

print(f"\n==== 结果: {len(PASS)} PASS / {len(FAIL)} FAIL ====")
for f in FAIL:
    print("  FAIL:", f)
sys.exit(1 if FAIL else 0)
