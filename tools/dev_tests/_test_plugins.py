"""插件离线自检：office / file_ops / system / weather / web_search / knowledge_graph / jm(不联网部分)

每个用例打印 [PASS]/[FAIL]，不依赖网络的操作全部真实执行。
"""
import os
import sys
import json
import shutil
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE))
os.environ.setdefault("DEEPSEEK_API_KEY", "test")

ok = True
failures = []


def check(name, cond, extra=""):
    global ok
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (f"  {extra}" if extra else ""))
    if not cond:
        ok = False
        failures.append(name)


def run(tool: str, **kwargs):
    from src.core.tool_manager import get_tool_manager
    tm = get_tool_manager()
    return tm.execute_tool(tool, **kwargs)


PRIV = BASE / "data" / "桌宠私有data"
OFFICE = PRIV / "office"
print("== office 工具 ==")
try:
    # ★ 1.8.10: 文件名加唯一后缀，并且**开跑前先清掉上一次留下的**。
    #   以前用固定的「自检表格.xlsx」，第一次能过、第二次就被 officecli
    #   以 "File already exists" 拒掉，于是这套测试只能过一次。
    #   收尾时删文件在 Windows 上常被 officecli 的句柄挡住（PermissionError），
    #   所以改成"下次开跑前清"，保证不会越积越多。
    import os as _os
    import time as _time
    if OFFICE.exists():
        for _stale in OFFICE.rglob("自检*_*_*.*"):
            try:
                _stale.unlink()
            except Exception:
                pass
    _tag = f"_{_os.getpid()}_{int(_time.time())}"
    _names = {"xlsx": f"自检表格{_tag}.xlsx",
              "docx": f"自检文档{_tag}.docx",
              "pptx": f"自检幻灯{_tag}.pptx"}
    results = {}
    for fmt, fname in _names.items():
        results[fmt] = str(run("office", action="create", format=fmt, file=fname))
    for fmt, res in results.items():
        check(f"create {fmt}",
              ("已创建" in res or "success" in res.lower() or "✅" in res)
              and "创建失败" not in res, res[:110])
    found = [p for p in OFFICE.rglob(f"自检*{_tag}*")] if OFFICE.exists() else []
    check("文件已落盘到 桌宠私有data/office", len(found) >= 3,
          str([p.name for p in found]))
    r = str(run("office", action="view", file=_names["pptx"], mode="outline"))
    check("view pptx", len(r) > 20 and "未知操作" not in r, r[:110])
    r = str(run("office", action="add", file=_names["pptx"], type="slide",
                props={"title": "自检标题"}))
    check("add slide", "success" in r.lower() or "✅" in r or "已" in r, r[:110])
except Exception as e:
    check("office 工具可用", False, f"{type(e).__name__}: {e}")

print("== file_ops 工具 ==")
try:
    tgt = PRIV / "自检文件.txt"
    r = run("file_ops", action="write", path=str(tgt.relative_to(BASE)),
            content="hello 自检\n第二行")
    check("write", "✅" in str(r), str(r)[:100])
    r = run("file_ops", action="read", path=str(tgt.relative_to(BASE)))
    check("read", "hello" in str(r), str(r)[:100])
    r = run("file_ops", action="edit", path=str(tgt.relative_to(BASE)),
            old_string="第二行", new_string="第二行(已改)")
    check("edit", "✅" in str(r), str(r)[:100])
    r = run("file_ops", action="read", path=str(tgt.relative_to(BASE)))
    check("edit 生效", "已改" in str(r))
    r = run("file_ops", action="list", path=str(PRIV.relative_to(BASE)))
    check("list", "自检" in str(r), str(r)[:80])
    r = run("file_ops", action="search", pattern="hello", path=str(PRIV.relative_to(BASE)))
    check("search", "自检文件" in str(r) or "hello" in str(r), str(r)[:80])
    # 沙箱：写项目根之外必须被拒（返回拒绝文案或明确错误，不允许真的写进去）
    r = str(run("file_ops", action="write", path="C:/Windows/Temp/prts_should_fail.txt",
                content="x"))
    check("沙箱拒绝写系统盘（可读文案）",
          "拒绝" in r and "只读" in r, r[:130])
    tgt.unlink(missing_ok=True)
except Exception as e:
    check("file_ops 工具可用", False, f"{type(e).__name__}: {e}")

print("== system / weather / web_search / knowledge_graph ==")
try:
    r = run("system_command", action="get_local_ip")
    check("system get_local_ip", "192.168" in str(r) or "." in str(r), str(r)[:80])
except Exception as e:
    check("system_command 可用", False, f"{type(e).__name__}: {e}")

try:
    r = run("weather_query", city="重庆")
    check("weather 离线不崩（可能报网络错误）", r is not None, str(r)[:90])
except Exception as e:
    check("weather 调用未抛异常", False, f"{type(e).__name__}: {e}")

try:
    r = str(run("web_search", query="测试"))
    check("web_search 不崩（可能无 key）", len(r) > 0, r[:90])
except Exception as e:
    check("web_search 调用未抛异常", False, f"{type(e).__name__}: {e}")

try:
    r = str(run("knowledge_graph", action="stats"))
    check("knowledge_graph stats", len(r) > 5 and "未知操作" not in r, r[:90])
except Exception as e:
    check("knowledge_graph 调用未抛异常", False, f"{type(e).__name__}: {e}")

print("== shell_exec 沙箱 ==")
try:
    r = run("shell_exec", command="echo prts_ok")
    check("shell 正常命令", "prts_ok" in str(r), str(r)[:90])
    r = run("shell_exec", command="cmd /c del C:\\Windows\\Temp\\x.txt")
    check("shell 拦截破坏性命令", "拦截" in str(r) or "拒绝" in str(r) or "❌" in str(r),
          str(r)[:110])
except Exception as e:
    check("shell_exec 可用", False, f"{type(e).__name__}: {e}")

print("\n结果:", "ALL PASS" if ok else f"FAILED ({len(failures)}): {failures}")
sys.exit(0 if ok else 1)
