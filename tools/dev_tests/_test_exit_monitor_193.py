# -*- coding: utf-8 -*-
"""★ 1.9.3 第 32 轮：队列 Q5（退出硬超时）+ Q2（监控重启不冻界面）。

【为什么只做这两条、以及为什么不按原样做另外几条 —— 先把"检查结论"也钉进测试里】
  · a) MALFORMED_RESPONSE：**全日志 0 次出现**，底层"工具参数非法 JSON"已在第 18 轮
       用 `_repair_json_args` 兜住 → **无证据，不必按原方案做**。
  · Q3「粘贴不再自动 OCR / 描述不拼进用户消息」：1.9.0 起是**刻意设计**
       （`_on_clip_image_pasted` 的注释写明理由：重启后预览只剩占位，
       而"这张图里有什么"要留在对话历史里）→ **改它等于推翻后来的设计，需用户拍板**。
  · Q4「探针：UI 卡顿 + 形象时不时抽一下」：**体感问题**，脚本测不了 → 需要用户确认。
"""
import os
import re
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE))

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))


SRC = (BASE / "src" / "ui" / "main_window.py").read_text(encoding="utf-8")

print("\n== A. Q5：退出收尾要有**全局硬超时**（原来 5+5+6 最坏 16 秒）==")
check("★定义了总预算常量 EXIT_TOTAL_BUDGET",
      re.search(r"^EXIT_TOTAL_BUDGET\s*=\s*[\d.]+", SRC, re.M) is not None,
      str(re.search(r"^EXIT_TOTAL_BUDGET.*", SRC, re.M).group(0) if re.search(r"^EXIT_TOTAL_BUDGET", SRC, re.M) else ""))
_budget = float(re.search(r"^EXIT_TOTAL_BUDGET\s*=\s*([\d.]+)", SRC, re.M).group(1))
check("★总预算是个「像样」的小数字（<=8 秒，别又变成十几秒）", 0 < _budget <= 8.0,
      f"{_budget}s")
check("★两个 join 不再是写死的 timeout=5，而是从 deadline 扣",
      "t1.join(timeout=max(0.0, _deadline - _t_exit.time()))" in SRC
      and "t1.join(timeout=5)" not in SRC)
check("★JM 下载等待也从同一个预算扣（不再单独 6 秒）",
      "_jm_left = max(0.5, _deadline - _t_exit.time())" in SRC
      and "_wait_jm(timeout=6.0)" not in SRC)
check("★超预算时**如实告警并继续**（不无限等）", "收尾线程超过" in SRC)
check("★关数据库仍在收尾之后（原有顺序不能被破坏）",
      SRC.index("self.db.close()") > SRC.index("t2.join(timeout=max(0.0"))

print("\n== B. Q2：改监控设置时不再冻住 GUI（重启挪到后台线程）==")
_m = re.search(r"def _do_restart_monitor_timed\(self.*?\n(.*?)(?=\n    def )", SRC, re.S)
_body = _m.group(1) if _m else ""
check("★重启监控跑在后台线程里", "threading.Thread(target=_work" in _body,
      _body[:80].replace("\n", " "))
check("★仍然打印耗时（好排查）", "重启完成，耗时" in _body)
check("★线程起不来时退回同步（宁可卡一下也要让监控生效）",
      "退回同步重启" in _body and "_work()" in _body)
check("★1.9.2 的「没改就不重启」仍然保留（别把它改掉）",
      "监控设置未变化 → 跳过重启" in SRC)

print("\n== C. 检查结论也钉住：a 不该按原方案做 ==")
check("★全仓没有 MALFORMED_RESPONSE 的处理分支（因为它从没发生过）",
      "MALFORMED_RESPONSE" not in (BASE / "src" / "core" / "session.py").read_text(encoding="utf-8"))
check("★第 18 轮的参数容错还在（这才是那类问题的真兜底）",
      "_repair_json_args" in (BASE / "src" / "core" / "session.py").read_text(encoding="utf-8"))

print(f"\n==== 结果: {len(PASS)} PASS / {len(FAIL)} FAIL ====")
for f in FAIL:
    print("  FAIL:", f)
sys.exit(1 if FAIL else 0)
