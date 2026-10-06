# -*- coding: utf-8 -*-
"""★ 1.9.4：**本地语言模型一键获取**（Ollama）守卫 —— 用户拍板 Q4「只做这个」。

## 用户的决定（2026-10-06，用提问工具选的）
  Q4 范围 → **② 只自动拉模型**：
      "没装 Ollama 就引导你去官网装…装好后我自动 `ollama pull` 推荐模型。
       **不替你装软件**，风险低。"
  选哪个模型 → **按硬件自动选**：
      "拉之前会把型号和体积告诉我，**确认了再拉**。"

## 钉什么
  A. 推荐**真的按硬件变**（强/弱/未知三种假配置必须得不同结果 —— 反向守卫）；
  B. ★★ **不替用户装 Ollama 本体**：拉取前若 Ollama 没在跑，要给
     "去官网装 + 启动"的**可操作**的话，并**指出下载地址**；**绝不自己去下安装包**；
  C. ★ **不重复下载**（用户反复强调"不要重复下载"）：已装同款要**跳过**；
  D. 拉取**不抛异常**，失败要返回可读原因（界面要能显示）；
  E. ★★ **拉之前必须确认**（用户明确要求）：向导里那条路径必须有确认框；
  F. 成功后**写进现成的配置槽** `vision.ollama_model`（不新增配置键）；
  G. 跨平台：Windows 上起子进程要 `CREATE_NO_WINDOW`（否则弹黑框，用户报过）。
"""
import os
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


from src.core import llm_local as L                     # noqa: E402
from src.core.hardware import Hardware                  # noqa: E402

print("== A. 推荐随硬件变（反向守卫：别拿本机凑）==")
_weak = L.recommend(Hardware(ram_total_gb=8, disk_free_gb=100))
_mid = L.recommend(Hardware(ram_total_gb=16, vram_gb=8, gpu_name="RTX 4060",
                            disk_free_gb=100))
_strong = L.recommend(Hardware(ram_total_gb=64, vram_gb=24, gpu_name="RTX 4090",
                               disk_free_gb=500))
check("★弱机器 → 小模型（3b 级）", "3b" in _weak.model or _weak.tier in ("low", "none"),
      f"{_weak.model} / {_weak.tier}")
check("★中档 → 7b 级", "7b" in _mid.model, _mid.model)
check("★强机器 → 大模型", "14b" in _strong.model or "32b" in _strong.model,
      _strong.model)
check("★★强/中必须**给出不同**的模型（证明判据真在按硬件算）",
      _strong.model != _mid.model, f"{_strong.model} vs {_mid.model}")
check("★每一项都带体积提示（用户要「拉之前知道多大」）",
      all(p.size_hint for p in (_weak, _mid, _strong)),
      f"{_weak.size_hint} / {_mid.size_hint} / {_strong.size_hint}")
check("★并且带了理由（为什么推荐它）", all(p.why for p in (_weak, _mid, _strong)))

print("\n== B. ★★ 不替用户装 Ollama 本体 ==")
_src = (ROOT / "src" / "core" / "llm_local.py").read_text(encoding="utf-8")
check("★★拉取前先看 Ollama 在不在跑", "port_open" in _src)
check("★★没在跑时给出**下载地址**（引导他自己装）",
      "OLLAMA_DOWNLOAD_URL" in _src and "ollama.com/download" in _src)
check("★★★**绝不**自己下载/安装 Ollama 本体"
      "（用户选的是「只自动拉模型」，不替他装软件）",
      "installer" not in _src.lower()
      and "winsetup" not in _src.lower()
      and ".exe" not in _src)

print("\n== C. ★ 不重复下载（用户反复强调）==")
check("★★拉之前先 `ollama list` 查一遍", "has_model" in _src and "ollama\", \"list" in _src)
check("★已装同款 → **跳过**并报成功（不当成失败）",
      "跳过（不重复下载）" in _src)
_ok, _why = L.pull("qwen2.5:7b")          # 本机没 Ollama → 走"引导去装"分支
check("★★拉取**不抛异常**且给可读原因", isinstance(_why, str) and bool(_why), _why[:40])
check("★提示里指向了下载页", "ollama.com/download" in _why)

print("\n== D/E/F/G. Ollama 的入口**从引导挪走了**（用户要求）==")
_w_src = (ROOT / "src" / "ui" / "first_run_wizard.py").read_text(encoding="utf-8")
check("★★成功后写进现成配置槽 `vision.ollama_model`（不新增键）",
      "vision.ollama_model" in _src)
check("★Windows 上起子进程隐藏窗口（用户报过闪黑框）",
      "0x08000000" in _src)
check("★★`llm_local` 模块**本身仍然完整**（供设置窗口调用）",
      all(k in _src for k in ("recommend", "list_installed", "has_model",
                              "pull", "write_back")))
# ★★ 1.9.4 真机修复（用户 2026-10-06）：
#   "我的建议是**首次配置，不提及 comfyUI 和 Ollama**。
#    **ollama 在设置里面**检测，下载，扫描，选择。"
#   ⇒ 引导里**不该再有** Ollama 的拉取按钮/确认框（那套 UI 要挪到设置窗口；
#     本轮先把引导清干净，设置窗口的接线下一轮做）。
#   ★ 「引导里没有 ollama」这条**在下面用运行时行为验**（看勾选表），
#     **不按源码文本判** —— 我第一版按 `"Ollama" not in 源码` 判，
#     而注释里正常会提到它 → **假红一次**。断言要看行为，别看字符串。

_b = ROOT / "临时文件" / "_scratch_dev" / "tmp_llm_guard"
if _b.exists():
    import shutil
    shutil.rmtree(_b, ignore_errors=True)
_b.mkdir(parents=True)
from PySide6.QtWidgets import QApplication            # noqa: E402
_app = QApplication.instance() or QApplication([])
from src.ui.first_run_wizard import FirstRunWizard    # noqa: E402

try:
    _w_now = FirstRunWizard(base=_b)
    _w_checks_now = set(_w_now._checks)
    check("★★★引导的可选项里**没有 ollama**（用户要求：挪去设置窗口）",
          "ollama" not in _w_checks_now, str(sorted(_w_checks_now)))
    check("★★引导也**没有 _svc_rows / _on_pull_llm** 这种东西了（那套 UI 已删）",
          not hasattr(_w_now, "_svc_rows")
          and not hasattr(_w_now, "_on_pull_llm"))
finally:
    import shutil as _sh
    _sh.rmtree(_b, ignore_errors=True)

print(f"\n==== 结果: {len(PASS)} PASS / {len(FAIL)} FAIL ====")
for f in FAIL:
    print("  FAIL:", f)
sys.exit(1 if FAIL else 0)
