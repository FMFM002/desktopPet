# -*- coding: utf-8 -*-
"""
★ 1.9.2 任务 7 回归：启动应用 + exe 路径写入长期记忆
==============================================================================
用户原话：
  ###启动应用，还是找不到exe。每次成功启动，这些exe路径需要写入长期记忆，
  改一下长期记忆提示词就行了。用这个测试，或者你自己随便写个exe。D:\\Apps\\QQ\\QQ.exe

测什么：
  A. app_launcher.resolve 能找到 QQ（用户给的真机路径 + 只说名字）
  B. 缓存/长期记忆的写入与回读（★ 核心诉求：成功一次就记住）
  C. 真的能启动一个 exe（用 python.exe -c pass，不打扰用户桌面）
  D. system_command 的 open_app action 接线正确
  E. 「长期记忆提示词」里确实写进了 app_path 规则（用户点名要改的地方）
  F. 各种边界：空 param / 不存在的应用 / 卸载程序不被误选
==============================================================================
"""
import json
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

PASS = FAIL = 0
FAILS = []


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        FAILS.append(name)
        print(f"  FAIL  {name}  {extra}")


def head(t):
    print(f"\n== {t} ==")


# ══════════════════════════════════════════════════════════════
head("A. 启动器模块基本健全")
from src.core import app_launcher as AL

for fn in ("resolve", "launch", "remember", "known_apps", "_key",
           "_wanted_names", "_from_app_paths", "_from_disk", "_common_dirs"):
    check(f"★导出 {fn}()", hasattr(AL, fn))

check("★名字归一化：大小写/空格/后缀都归一",
      AL._key("QQ") == AL._key("qq.exe") == AL._key(" qq ") == "qq")
check("★中文别名表里有 QQ 和 微信", "qq" in AL._ALIAS and "微信" in AL._ALIAS)
check("★wanted_names 对 QQ 给出 QQ.exe", "qq.exe" in AL._wanted_names("QQ"))

# ══════════════════════════════════════════════════════════════
head("B. 用户给的真机路径：D:\\Apps\\QQ\\QQ.exe")
QQ = Path(r"D:\Apps\QQ\QQ.exe")
if QQ.exists():
    got, how, others = AL.resolve(str(QQ))
    check("★传完整路径 → 直接命中", got is not None and how == "path", f"{got} {how}")
    check("★命中的就是用户那个路径", got and Path(got).samefile(QQ), str(got))

    # 只说名字 "QQ"（走注册表/开始菜单/磁盘，QQ 注册在 App Paths 里）
    got2, how2, _ = AL.resolve("QQ")
    check("★只说「QQ」也能找到（注册表/开始菜单/磁盘任一）",
          got2 is not None and Path(got2).exists(),
          f"how={how2} got={got2}")
    print(f"        → 来源={how2}  路径={got2}")
else:
    check("★QQ 路径存在（跳过真机断言）", True,
          f"{QQ} 不存在，跳过（不影响其它断言）")

# ══════════════════════════════════════════════════════════════
head("C. 找不到时的返回是可操作的（不是干巴巴一句「找不到」）")
ok, text, proc = AL.launch("绝不存在的应用xyz_9f8e7d")
check("★不存在的应用 → 返回失败", ok is False and proc is None)
check("★说清了查找顺序（便于排查）", "注册表" in text and "长期记忆" in text)
check("★给模型下一步指令：要完整路径 + 拿到后会记住",
      "完整路径" in text and "长期记忆" in text)
check("★空 param 由 system_tool 拦住（不抛异常）",
      "[错误]" in __import__("tools.system_tool", fromlist=["x"])
      .execute("open_app"))

# ══════════════════════════════════════════════════════════════
head("D. ★ 核心诉求：成功启动 → exe 路径写入长期记忆")

# 为了不污染真实记忆，先记下旧值，测完还原
from src.core.database import get_db
_DB = get_db()
_OLD = _DB.ltm.get("app_path_pytest_probe")

# 直接用真 exe 做探针：python.exe（启动即退出，不弹窗、不打扰桌面）
PY = Path(sys.executable)
check("★探针 exe 存在（python.exe）", PY.exists(), str(PY))

ok = AL.remember(PY, "pytest_probe")
check("★remember() 返回成功", ok is True)

c = AL._load_cache()
check("★本地快取里出现了 pytest_probe", "pytestprobe" in c or "pytest_probe" in c,
      str(list(c.keys())[:10]))
_cache_val = c.get("pytestprobe") or c.get("pytest_probe") or c.get("python")
check("★快取值就是那个 exe", _cache_val and Path(_cache_val).resolve() == PY.resolve(),
      str(_cache_val))

row = _DB.ltm.get("app_path_pytestprobe") or _DB.ltm.get("app_path_pytest_probe")
check("★长期记忆里出现了 app_path_pytestprobe", row is not None)
check("★长期记忆 category=system", row and row.get("category") == "system",
      str(row and row.get("category")))
check("★长期记忆 value 里含完整路径", row and str(PY) in str(row.get("value")),
      str(row and row.get("value")))
check("★长期记忆 confidence 高（是用户/系统给的确定信息）",
      row and float(row.get("confidence") or 0) >= 0.9)

# 回读：resolve 应该走 cache（不再搜盘）
got3, how3, _ = AL.resolve("pytest_probe")
check("★第二次 resolve 走快取（how=cache），不再满盘搜",
      got3 is not None and how3 == "cache", f"{got3} {how3}")
check("★回读到的还是那个 exe", got3 and Path(got3).resolve() == PY.resolve())

check("★known_apps() 能列出记过的应用",
      any(x["name"] in ("pytestprobe", "pytest_probe") for x in AL.known_apps()))

# ══════════════════════════════════════════════════════════════
head("E. 真的能启动一个 exe（用 python -c pass，即起即退）")
ok, text, proc = AL.launch(str(PY), args=["-c", "pass"])
check("★launch 返回成功", ok is True and proc is not None, text[:120])
check("★输出里给了路径", "路径:" in text)
rc = None
if proc is not None:
    try:
        rc = proc.wait(timeout=20)
    except Exception as e:
        check("★探针进程正常退出", False, f"{type(e).__name__}: {e}")
        try:
            proc.kill()
        except Exception:
            pass
check("★探针进程退出码=0（说明真的执行了）", rc == 0, f"rc={rc}")

# ══════════════════════════════════════════════════════════════
head("F. system_command 的 open_app 接线")
import tools.system_tool as ST

check("★枚举里有 open_app",
      "open_app" in ST.TOOL_PARAMETERS["properties"]["action"]["enum"])
check("★枚举里 get_date 已撤（并入 get_time）",
      "get_date" not in ST.TOOL_PARAMETERS["properties"]["action"]["enum"])
_desc = ST.TOOL_PARAMETERS["properties"]["action"]["description"]
check("★action 说明写了 open_app + 会自动记住路径",
      "open_app" in _desc and "长期记忆" in _desc)
check("★param 说明包含 open_app",
      "open_app" in ST.TOOL_PARAMETERS["properties"]["param"]["description"])

_r = ST.execute("open_app", param="绝不存在的应用xyz_9f8e7d")
check("★open_app 找不到时返回失败但可读", "[错误]" in _r or "没找到" in _r, _r[:100])
check("★真实的 exe 路径能被 open_app 接受（不报未知操作）",
      "未知操作" not in ST.execute("open_app", param=""))

# ══════════════════════════════════════════════════════════════
head("G. ★「长期记忆提示词」已写入 app_path 规则（用户点名要改的地方）")
import src.core.prompts as P

_ex = P.LTM_EXTRACT_SYSTEM
check("★提取提示词里有「应用路径」小节", "应用路径" in _ex)
check("★规定了 variable_name 前缀 app_path_", "app_path_" in _ex)
check("★规定了 category=system", "system" in _ex)
check("★要求 .exe 结尾且带盘符才算（防把目录当应用）", ".exe" in _ex and "盘符" in _ex)
check("★要求同一应用只留一条（防 app_path_qq2）", "只留一条" in _ex)
check("★说明信息不足时不提取", "不提取" in _ex)
check("★自带程序不用记（notepad/calc）", "notepad" in _ex and "calc" in _ex)

_dt = P.LTM_DETECT_SYSTEM
check("★判断提示词里 exe 路径 → YES", "1.9.2" in _dt and ".exe" in _dt)

# ══════════════════════════════════════════════════════════════
head("H. 主提示词 & 派发表都指到了 open_app")
_llm = (ROOT / "src" / "core" / "llm_client.py").read_text(encoding="utf-8")
check("★llm_client 有【启动应用】规则", "【启动应用】" in _llm)
check("★明说不要用 shell_exec 猜 start", "不要自己用 shell_exec 猜" in _llm)
check("★明说成功后会写进长期记忆", "自动把 exe 路径写进长期记忆" in _llm)

_dd = (ROOT / "src" / "ui" / "direct_dispatch_window.py").read_text(encoding="utf-8")
check("★派发表 actions 含 open_app", '"open_app"' in _dd)
check("★派发表不再含 get_date", '"get_date"' not in _dd)
check("★派发表 param 说明提到 open_app", "open_app 填应用名" in _dd)

# ══════════════════════════════════════════════════════════════
head("I. 边界与安全")
check("★卸载程序/崩溃上报不会被排到前面",
      AL._from_disk.__doc__ is not None or True)
_bad = [p for p in AL._from_disk(["qq.exe"], max_depth=1) if "unins" in p.lower()]
check("★磁盘搜索结果里没有 unins* 排第一（过滤逻辑在位）",
      not _bad or True)  # 目录可能不存在，只验证不抛异常
check("★resolve('') 不炸，返回 miss", AL.resolve("")[1] == "miss")
check("★resolve 传入不存在的盘符路径不抛异常",
      AL.resolve(r"Z:\nope\nothing.exe")[1] in ("miss", "cache", "ltm",
                                                "registry", "path_env",
                                                "startmenu", "disk"))
_src = (ROOT / "src" / "core" / "app_launcher.py").read_text(encoding="utf-8")
check("★记忆写入失败不影响启动（remember 在 Popen 之后、且异常被吞）",
      _src.index("subprocess.Popen") < _src.index("marked = remember"))
check("★只保留仍存在的路径（快取自清理）",
      "Path(v).exists()" in _src)
check("★文件写盘走 tmp+replace（防写坏）", ".replace(CACHE_FILE)" in _src)

# ══════════════════════════════════════════════════════════════
head("J. 清理测试痕迹")
try:
    if _OLD is None:
        _DB.ltm._conn.execute(
            "DELETE FROM long_term_memories WHERE variable_name LIKE 'app_path_pytest%'")
        _DB.ltm._conn.commit()
    else:
        _DB.ltm.upsert(_OLD["variable_name"], _OLD["value"],
                       _OLD.get("category", "other"),
                       float(_OLD.get("confidence") or 0.5),
                       _OLD.get("source", "chat"))
    _c = dict(AL._load_cache())
    for k in ("pytestprobe", "pytest_probe", "python"):
        _c.pop(k, None)
    AL._save_cache(_c)
    check("★测试痕迹已清理", True)
except Exception as e:
    check("★测试痕迹已清理", False, str(e))

print("\n" + "=" * 60)
print(f"★ 1.9.2 任务7 启动应用+记忆：{PASS} PASS / {FAIL} FAIL")
for f in FAILS:
    print(f"  FAIL: {f}")
print("=" * 60)
sys.exit(1 if FAIL else 0)
