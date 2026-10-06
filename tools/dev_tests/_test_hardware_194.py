# -*- coding: utf-8 -*-
"""★ 1.9.4：**硬件检测与按配置推荐** 的守卫（用户 2026-10-06 要求）。

用户原话："根据用户电脑配置来推荐，另外推荐本地语言模型。**电脑配置直接检测**。"

## 钉什么
  A. `detect()` 能跑、**只读**（不装东西、不启动服务、不改配置）；
  B. ★★ **探测不到就如实说"未知"，不猜不编** —— 没有独显时 `has_gpu` 为假、
     建议是"别本地画"；有独显时建议里必须点出**型号与显存**（让用户能核对）；
  C. 推荐结果**随配置变化**（用合成的假配置验档位，而不是只看本机结果）——
     本机是 RTX 5060 8GB，只测本机等于只测了一档；
  D. ★ **磁盘不够要拦住**（本地画图要 ~25GB，这是真会失败的场景）；
  E. 本地语言模型建议随**内存/显存**分档（用户明确要这一项）；
  F. ★ **反向守卫**：造一个"很强"的假配置必须得到高档位、
     造一个"很弱"的必须得到低档位 —— 证明档位不是恒定的一个值。
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PRTS_NO_COMFY_START", "1")
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.core.hardware import Hardware, advise_drawing, advise_local_llm, detect, summary_line  # noqa: E402

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))


print("== A. detect() 能跑，且是只读的 ==")
_cfg_before = (ROOT / "config.json").read_bytes() if (ROOT / "config.json").exists() else b""
_hw = detect()
_cfg_after = (ROOT / "config.json").read_bytes() if (ROOT / "config.json").exists() else b""
check("★detect() 返回 Hardware", isinstance(_hw, Hardware))
check("★★detect() **不写任何东西**（配置没被动过）", _cfg_before == _cfg_after)
check("★摘要行非空且含关键项", all(k in summary_line(_hw) for k in ("CPU", "内存", "磁盘")),
      summary_line(_hw))
check("★CPU 核数探到了（>0）", _hw.cpu_logical > 0, str(_hw.cpu_logical))
check("★内存探到了（>0GB）", _hw.ram_total_gb > 0, f"{_hw.ram_total_gb}GB")
check("★磁盘剩余探到了（>=0）", _hw.disk_free_gb >= 0, f"{_hw.disk_free_gb}GB")
print(f"      本机实测: {summary_line(_hw)}")

print("\n== B. 探测不到就**如实说未知**，不猜不编 ==")
_no_gpu = Hardware(cpu_logical=8, cpu_physical=4, ram_total_gb=16, disk_free_gb=200)
check("★★没有独显时 has_gpu 为假", not _no_gpu.has_gpu)
_a = advise_drawing(_no_gpu)
check("★★没有独显 → 建议**别本地画**，且说明了原因",
      _a.tier == "none" and ("显卡" in _a.why or "独显" in _a.why), _a.why[:50])
check("★并且给了替代方案（云端 / 自己装好我来接管）", bool(_a.picks), str(_a.picks[:1]))

print("\n== C/D. 推荐随配置变化 + 磁盘不够要拦住 ==")
_low = Hardware(gpu_name="RTX 3050", vram_gb=6, ram_total_gb=16, disk_free_gb=200,
                gpu_vendor="nvidia")
_mid = Hardware(gpu_name="RTX 4060", vram_gb=8, ram_total_gb=16, disk_free_gb=200,
                gpu_vendor="nvidia")
_high = Hardware(gpu_name="RTX 4090", vram_gb=24, ram_total_gb=64, disk_free_gb=500,
                 gpu_vendor="nvidia")
_small_disk = Hardware(gpu_name="RTX 4060", vram_gb=8, ram_total_gb=16,
                       disk_free_gb=5, gpu_vendor="nvidia")
check("★6GB 显存 → 低档", advise_drawing(_low).tier == "low", advise_drawing(_low).tier)
check("★8GB 显存 → 中档", advise_drawing(_mid).tier == "mid", advise_drawing(_mid).tier)
check("★24GB 显存 → 高档", advise_drawing(_high).tier == "high", advise_drawing(_high).tier)
check("★★磁盘只剩 5GB → **拦住**（本地画图要 ~25GB，这是真会失败的事）",
      advise_drawing(_small_disk).tier == "none"
      # ★ 断言要照着**实际文案**写：那条 why 里写的是"要预留约 25GB…只剩 5GB"，
      #   并没有"磁盘"两个字（标题才是"磁盘不太够"）。我第一版按想当然写，假红一次。
      and "25GB" in advise_drawing(_small_disk).why
      and "5GB" in advise_drawing(_small_disk).why,
      advise_drawing(_small_disk).why[:60])
check("★有独显时建议里**点出型号与显存**（用户能核对是不是认错了机器）",
      "4060" in advise_drawing(_mid).why and "8" in advise_drawing(_mid).why,
      advise_drawing(_mid).why[:60])

print("\n== E. 本地语言模型建议随内存/显存分档 ==")
_llm_small = Hardware(ram_total_gb=8, disk_free_gb=100)
_llm_mid = Hardware(ram_total_gb=16, vram_gb=8, gpu_name="RTX 4060", disk_free_gb=100)
_llm_big = Hardware(ram_total_gb=64, vram_gb=24, gpu_name="RTX 4090", disk_free_gb=100)
_t = [advise_local_llm(x).tier for x in (_llm_small, _llm_mid, _llm_big)]
check("★8GB 内存 → 低档/不建议；16GB → 中档；64GB → 高档",
      _t[0] in ("low", "none") and _t[1] == "mid" and _t[2] == "high", str(_t))
check("★建议里给了**具体的模型名**（用户才知道去拉哪个）",
      all(advise_local_llm(x).picks for x in (_llm_small, _llm_mid, _llm_big)))

print("\n== F. ★ 反向守卫：档位不是恒定值 ==")
check("★★很强的配置与很弱的配置**得到不同档位**（证明判据真的在按配置算）",
      advise_drawing(_high).tier != advise_drawing(_low).tier,
      f"{advise_drawing(_high).tier} vs {advise_drawing(_low).tier}")
check("★★未知（连内存都测不到）也不崩 —— 一律按最保守走",
      advise_drawing(Hardware()).tier == "none"
      and advise_local_llm(Hardware()).tier == "none")

print(f"\n==== 结果: {len(PASS)} PASS / {len(FAIL)} FAIL ====")
for f in FAIL:
    print("  FAIL:", f)
sys.exit(1 if FAIL else 0)
