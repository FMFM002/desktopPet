# -*- coding: utf-8 -*-
"""★ 1.9.4：**发布前自检的守卫**。

`tools/preflight.py` 是"发布前跑一条命令就知道还差什么"的脚本。
它的**价值全在"如实"**上 —— 一旦它把没验证的说成验证过了，就会误导发布决策。
所以这条守卫专门盯它的**诚实性与覆盖度**。

## 钉什么
  A. 能跑、退出码 0、`--json` 也能出结构；
  B. ★★ **三项"只有人能做的确认"必须在待办里**（A 的真机演练 / B 的 exe 双击 /
     C 的安装程序编译）—— 这三项是本版本最大的未验证面，**不许被悄悄忽略**；
  C. ★★ **没验证过的不许说成验证过了**：B/C 那两条的文案里必须出现"未验证"字样；
     A 那条必须说明"只验到弹出引导、没真正填 Key 聊过天"；
  D. 覆盖度：版本一致性 / 个人信息不随包 / 独立运行 / 模型状态 / 三种产物 ——
     每一块都要有对应条目；
  E. ★ 给出**可执行的下一步**（不是光报"待办"，得告诉人怎么做）；
  F. 它是**只读**的：不构建、不写盘（跑一次前后比较工作区的"关键产物"是否变化）。
"""
import json
import os
import subprocess
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PRTS_NO_COMFY_START", "1")
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from _fixtures_193 import no_window_kwargs   # noqa: E402

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))


def _run(*args):
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "preflight.py"), *args],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", cwd=str(ROOT), timeout=420, **no_window_kwargs())
    return r.returncode, (r.stdout or "") + (r.stderr or "")


print("== A. 能跑、退出码 0、--json 可解析 ==")
_rc, _txt = _run()
check("★preflight.py 退出码 0（自检不该以失败告终）", _rc == 0, str(_rc))
check("★人类可读输出里有汇总行", "待办" in _txt and "已就绪" in _txt)

_rc2, _json = _run("--json")
_data = None
try:
    _data = json.loads(_json)
except Exception as e:
    check("★--json 输出可解析", False, str(e)[:80])
if _data:
    check("★--json 含 version / counts / items", all(
        k in _data for k in ("version", "counts", "items")),
        f"version={_data.get('version')} counts={_data.get('counts')}")

_names = " ".join(i["name"] for i in (_data or {}).get("items", []))
_actions = " ".join(i.get("action", "") for i in (_data or {}).get("items", []))

print("\n== B. ★★ 三项'只有人能做的确认'必须在待办里 ==")
for _must in ("A 的真机启动演练", "B 的 exe 双击", "C 的安装程序编译"):
    check(f"★★自检里包含「{_must}」", _must in _names)
_todos = [_i for _i in (_data or {}).get("items", []) if _i["status"] == "TODO"]
check("★★而且它们的状态确实是 **TODO**（不是被标成 OK）",
      len(_todos) >= 3, f"TODO 共 {len(_todos)} 条")

print("\n== C. ★★ 没验证过的不许说成验证过了（诚实性）==")
# ★ 1.9.4 收尾：B 从"从未运行过"变成了"**已用 --selftest 跑到 ok=true**"，
#   但"真 Key 聊天 / 图形桌面双击"仍然只有人能确认 —— 所以判据改成
#   「既要写清验到哪一步，又要把没验的部分留在待办里」，而不是笼统的"未验证"。
check("★★B 那条写明了「已用 --selftest 验到哪一步」",
      "--selftest" in _txt)
check("★★B 那条**仍如实列出没验的部分**（真 Key 聊天 / 双击），"
      "不许因为自检过了就说成全验过",
      ("真 Key" in _txt or "填 Key" in _txt) and "双击" in _txt)
check("★★C 那条写了『未验证』", "未验证" in _txt,
      f"'未验证' 出现 {_txt.count('未验证')} 次")
check("★★A 那条说明了『没真正填 Key 聊过天』（不能只说'已验证'）",
      "没有真正填 Key" in _txt or "没真正填 Key" in _txt)
check("★明确说了『自动自检代替不了人眼确认』",
      "人眼" in _txt or "代替不了" in _txt)

print("\n== D. 覆盖度：关键几块都要有条目 ==")
for _kw in ("版本信息一致", "个人信息", "独立运行", "模型状态",
            "A 便携 zip", "B exe", "C 安装脚本"):
    check(f"★自检覆盖「{_kw}」", _kw in _names)

print("\n== E. 待办必须给**可执行的下一步** ==")
check("★每个 TODO 都带 action（不是只报'待办'就完事）",
      all(_i.get("action") for _i in _todos), str([_i["name"] for _i in _todos
                                                   if not _i.get("action")]))
check("★action 里有真能跑的命令（如 build_release / PyInstaller / ISCC）",
      any(k in _actions for k in ("build_release", "PyInstaller", "ISCC", "启动.bat")))

print("\n== F. 自检必须是**只读**的 ==")
_probe = [ROOT / "dist" / "PRTS-DesktopPet-1.9.4-portable.zip",
          ROOT / "PRTS.spec", ROOT / "installer" / "PRTS.iss"]
_before = [(p, p.stat().st_mtime_ns if p.exists() else None) for p in _probe]
_run()
_after = [(p, p.stat().st_mtime_ns if p.exists() else None) for p in _probe]
check("★★跑一次自检不会改动产物/spec/iss（只读，不构建）", _before == _after)

print(f"\n==== 结果: {len(PASS)} PASS / {len(FAIL)} FAIL ====")
for f in FAIL:
    print("  FAIL:", f)
sys.exit(1 if FAIL else 0)
