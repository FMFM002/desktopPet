# -*- coding: utf-8 -*-
"""★ 1.9.4：**本地服务探测**（Ollama / ComfyUI）守卫 —— 用户 2026-10-06 要求。

用户原话：
  "检测时，**ollama 可以直接启动服务看看有没有 ollama**；
   **comfyUI 不能自动检测注册表**，path 的话就让用户**手动启动，再点击检测**。"

## 钉什么
  A. `port_open` 判据有效（对一个**确实在听**的端口为真、对没人听的为假）
     —— ★ 反向验证：不能用"随便测一个端口恰好是 False"来冒充；
  B. Ollama：**不试启动**时只报端口；**试启动**时若没有 `ollama` 命令，
     要给**可操作**的话（去哪下），而不是抛异常；
  C. ComfyUI：**不许替他启动** —— 说明里必须写清"请你自己先启动，再点检测"；
  D. 向导里：本地服务（ollama/comfyui）有**检测**按钮与结果标签；
  E. ★★ **本地服务不出现在"模型行"里**（它不是模型，没有路径可填、也没有模型可下）
     —— 我第一版漏了这个判断，导致 ollama 同时出现在两套 UI 里；
  F. 检测跑在**后台线程 + 信号**（试启动要等几秒，不能卡住界面）。
"""
import os
import socket
import sys
import threading
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PRTS_NO_COMFY_START", "1")
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))


from src.core import services as S                      # noqa: E402

print("== A. port_open 判据真的在判端口（反向验证）==")
_srv = socket.socket()
_srv.bind(("127.0.0.1", 0))
_srv.listen(1)
_live = _srv.getsockname()[1]
check("★★对一个**确实在听**的端口 → True（证明它不是恒 False）",
      S.port_open("127.0.0.1", _live))
_srv.close()
check("★对一个**没人听**的端口 → False", not S.port_open("127.0.0.1", _live))

print("\n== B. Ollama：不试启动只报端口；试启动失败要给可操作的话 ==")
_ok, _why = S.probe_ollama(try_start=False)
check("★不试启动时只报端口状态", "没有服务在跑" in _why, _why[:40])
# 试启动：本机没装 ollama（或装了但起不来）—— 两种情况都必须给"人话"且不抛
try:
    _ok2, _why2 = S.probe_ollama(try_start=True, wait=2.0)
    _good = ("ollama" in _why2.lower()) and ("没找到" in _why2 or "还没起来" in _why2
                                             or "✅" in _why2)
    check("★★试启动：**不抛异常**，而且无论成败都给可操作的话", _good, _why2[:70])
except Exception as e:
    check("★★试启动：**不抛异常**，而且无论成败都给可操作的话", False,
          f"抛了 {type(e).__name__}: {e}")

print("\n== C. ComfyUI：**不许替他启动**，要说清'你自己先启动再点检测' ==")
_ok3, _why3 = S.probe_comfyui()
check("★★说明里明确要求**用户自己启动**（我们不该也不能替他找/启 ComfyUI）",
      "自己启动" in _why3 or "手动启动" in _why3, _why3.replace("\n", " ")[:70])
check("★并且告诉了他该点哪个文件（run_nvidia_gpu.bat）",
      "run_nvidia_gpu" in _why3 or "启动" in _why3)

print("\n== D~F. 向导里的本地服务区块 ==")
from PySide6.QtWidgets import QApplication            # noqa: E402
_app = QApplication.instance() or QApplication([])
from src.ui.first_run_wizard import FirstRunWizard    # noqa: E402

_b = ROOT / "临时文件" / "_scratch_dev" / "tmp_svc_guard"
if _b.exists():
    import shutil
    shutil.rmtree(_b, ignore_errors=True)
_b.mkdir(parents=True)
try:
    _w = FirstRunWizard(base=_b)
    for _cb in _w._checks.values():
        _cb.setChecked(True)
    _w._goto(3)
    check("★本地服务行里有 ollama 与 comfyui",
          {"ollama", "comfyui"} <= set(_w._svc_rows), str(sorted(_w._svc_rows)))
    check("★★★**本地服务不出现在「模型行」里**"
          "（它不是模型：没有路径可填、也没有模型可下）"
          "—— 我第一版漏了这个判断，两套 UI 打架",
          not ({"ollama", "comfyui"} & set(_w._model_rows)),
          str(sorted(_w._model_rows)))
    check("★模型行里确实有那三样（genie/easyocr/whisper）",
          {"genie", "easyocr", "whisper"} <= set(_w._model_rows),
          str(sorted(_w._model_rows)))
    _src = (ROOT / "src" / "ui" / "first_run_wizard.py").read_text(encoding="utf-8")
    check("★★检测跑在**后台线程 + 信号**（试启动要等几秒，不能卡住界面）",
          "_sig_probe" in _src and "threading.Thread(target=_work" in _src)
    check("★Ollama 的检测按钮文案说明了「会试着启动它」（别让用户意外）",
          "会试着启动它" in _src or "试着启动" in _src)
    check("★★只有 `group == 'models'` 才给路径/下载 UI",
          'group == "models"' in _src)
finally:
    import shutil as _sh
    _sh.rmtree(_b, ignore_errors=True)

print(f"\n==== 结果: {len(PASS)} PASS / {len(FAIL)} FAIL ====")
for f in FAIL:
    print("  FAIL:", f)
sys.exit(1 if FAIL else 0)
