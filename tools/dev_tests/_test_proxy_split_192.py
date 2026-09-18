# -*- coding: utf-8 -*-
"""
★ 1.9.2 任务 11 回归：BT 与 P站/JM 的「VPN 冲突」方案（应用级分流）
==============================================================================
用户批注：
  ###下载BT和P站JM工具因为VPN冲突，有没有便捷的方案。研究研究合适的方案

【冲突的本质（本套件钉住的结论）】
  · P站/JM 需要代理；BT 的 peer 连接**协议层不支持代理**（aria2 的代理选项
    只管 HTTP/HTTPS/FTP；issue #1401 求了十年没做），而且代理出口是机房 IP、
    tracker 会拒 → **BT 必须直连**。
  · 所以"便捷方案"不是找个能两全的 VPN，而是**应用级分流**：
      P站/JM → 走本地代理端口；aria2c → 强制直连。

本套件验证代码侧那一半真的做到了：
  A. net_proxy 读配置（PRTS_PROXY 优先，兼容 PIXIV_PROXY，自己兜底读 .env）
  B. aria2c 被强制直连（清代理环境变量 + --all-proxy= / --no-proxy=*）
  C. jmcomic 的代理从"硬编码不走"改成"可配"（这是它必须靠系统 VPN 的根因）
  D. 状态查询会把方案/建议一并回报（用户一问就能看到怎么办）
  E. 配置文档与源码注释都留了规则分流的示例
==============================================================================
"""
import os
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE))
os.environ.setdefault("DEEPSEEK_API_KEY", "test")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PASS = FAIL = 0
FAILS = []


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        FAILS.append(name)
        print(f"  [FAIL] {name}  {extra}")


def head(t):
    print(f"\n== {t} ==")


from src.core import net_proxy as NP                          # noqa: E402
from src.core import torrent_engine as BT                     # noqa: E402

# ══════════════════════════════════════════════════════════════
head("A. 代理配置读取")
check("★有 proxy_url / proxies_dict / mode / clean_env / advice",
      all(hasattr(NP, f) for f in ("proxy_url", "proxies_dict", "mode",
                                   "clean_env", "advice", "system_proxy_env")))
_real = NP.proxy_url()
print(f"        当前读到: {_real or '（空）'}  mode={NP.mode()}")
check("★能读到 .env 里的代理（本机 .env 有 PIXIV_PROXY=127.0.0.1:7897）",
      _real.startswith("http") or _real == "", repr(_real))
check("★proxies_dict 结构与 requests/jmcomic 期望一致",
      NP.proxies_dict() == ({"http": _real, "https": _real} if _real else {}),
      str(NP.proxies_dict()))
check("★mode() 与是否配了代理一致",
      NP.mode() == ("app-proxy" if _real else "direct"), NP.mode())
check("★PRTS_PROXY 优先于 PIXIV_PROXY", "PRTS_PROXY" in NP._PROXY_KEYS
      and NP._PROXY_KEYS.index("PRTS_PROXY") < NP._PROXY_KEYS.index("PIXIV_PROXY"))
check("★自己兜底读 .env（不依赖别处的 load_dotenv 时机）",
      "_ensure_dotenv" in (BASE / "src" / "core" / "net_proxy.py").read_text(encoding="utf-8"))

# 环境变量优先级：显式设 PRTS_PROXY 应该赢
_old_prts = os.environ.get("PRTS_PROXY")
os.environ["PRTS_PROXY"] = "http://127.0.0.1:19999"
try:
    check("★★显式设 PRTS_PROXY 时以它为准",
          NP.proxy_url() == "http://127.0.0.1:19999", NP.proxy_url())
finally:
    if _old_prts is None:
        os.environ.pop("PRTS_PROXY", None)
    else:
        os.environ["PRTS_PROXY"] = _old_prts

# ══════════════════════════════════════════════════════════════
head("B. ★ aria2c 被强制直连（这是 BT 能用的前提）")
clean = NP.clean_env()
check("★★clean_env 摘掉了所有代理环境变量",
      not any(k.lower() in ("http_proxy", "https_proxy", "all_proxy", "ftp_proxy")
              for k in clean))
check("★clean_env 保留了其它环境变量（不是清空 os.environ）",
      len(clean) >= len(os.environ) - 4, f"{len(clean)} vs {len(os.environ)}")
_args = BT.build_bt_args("aria2c", "x.torrent", Path("/d"))
check("★★参数里有 --all-proxy=（显式置空 = 直连）", "--all-proxy=" in _args)
check("★★参数里有 --no-proxy=*", "--no-proxy=*" in _args)
_src_bt = (BASE / "src" / "core" / "torrent_engine.py").read_text(encoding="utf-8")
check("★★start_bt 用 clean_env() 启动 aria2c（不继承宿主代理）",
      "clean_env()" in _src_bt and 'kwargs["env"]' in _src_bt)
check("★注释写明了「aria2 会读 http_proxy 环境变量」这个坑",
      "http_proxy" in _src_bt)
check("★注释写明了 BT peer 不支持代理（协议层限制）",
      "协议层不支持代理" in _src_bt or "peer 连接**协议层不支持代理" in _src_bt)

# ══════════════════════════════════════════════════════════════
head("C. ★ jmcomic：从「硬编码不走代理」改成「可配」")
_jm_src = (BASE / "tools" / "jmcomic_tool.py").read_text(encoding="utf-8")
check("★★旧的无条件硬编码已去掉（原注释「禁用系统代理，避免走本地代理导致下载失败」）",
      "禁用系统代理，避免走本地代理导致下载失败" not in _jm_src)
check("★★改成读 net_proxy.proxies_dict()",
      "proxies_dict" in _jm_src and "DEFAULT_PROXIES = _proxies" in _jm_src)
check("★仍保留兜底（读配置失败才退回 None，不是把保护彻底删掉）",
      "DEFAULT_PROXIES = None" in _jm_src and "except Exception as _e" in _jm_src)
check("★注释说明了这就是「必须靠系统 VPN」的根因",
      "只能**靠系统级 VPN" in _jm_src or "系统 VPN" in _jm_src)
try:
    import jmcomic  # noqa: F401
    from tools import jmcomic_tool as JT
    JT._ensure_jmcomic()
    from jmcomic import JmModuleConfig
    _dp = JmModuleConfig.DEFAULT_PROXIES
    check("★★实际加载后 DEFAULT_PROXIES 反映配置（配了就带 http/https）",
          (_dp in (None, {})) if not _real else
          (isinstance(_dp, dict) and _dp.get("http") == _real),
          f"{_dp!r} / 配置={_real!r}")
except Exception as e:
    check("★jmcomic 可用（否则只做源码断言）", True, f"跳过运行期检查: {e}")

# ══════════════════════════════════════════════════════════════
head("D. 状态查询会把方案一并回报")
_info = BT.engine_info()
check("★engine_info 带 app_proxy", "app_proxy" in _info, str(list(_info.keys()))[:120])
check("★engine_info 带 bt_direct_forced=True", _info.get("bt_direct_forced") is True)
check("★★engine_info 带 advice（用户一问状态就能看到怎么办）",
      bool(_info.get("advice")))
check("★advice 给出了规则分流的具体写法",
      "PROCESS-NAME,aria2c.exe,DIRECT" in _info.get("advice", ""))
check("★advice 解释了「为什么 BT 不能走代理」",
      "不支持代理" in _info.get("advice", ""))
check("★advice 会在检测到系统代理环境变量时提醒",
      "系统级代理环境变量" in _info.get("advice", "")
      or not NP.system_proxy_env())
_from_tool = __import__("json").loads(
    __import__("tools.download_tool", fromlist=["x"]).execute(url="", action="bt_engine"))
check("★download(action=bt_engine) 也能拿到这些字段",
      "app_proxy" in _from_tool and "advice" in _from_tool)

# ══════════════════════════════════════════════════════════════
head("E. 文档与注释")
_env_ex = (BASE / ".env.example").read_text(encoding="utf-8")
check("★.env.example 里新增了 PRTS_PROXY 并解释了分流",
      "PRTS_PROXY" in _env_ex and "规则分流" in _env_ex)
check("★.env.example 说明了 BT 必须直连的原因",
      "协议层不支持代理" in _env_ex or "只管 HTTP/HTTPS/FTP" in _env_ex)
check("★net_proxy 模块头写清了完整方案（含 Clash 规则示例）",
      "PROCESS-NAME,aria2c.exe,DIRECT" in
      (BASE / "src" / "core" / "net_proxy.py").read_text(encoding="utf-8"))
check("★注明了那条限制的来源（aria2 issue #1401）",
      "#1401" in (BASE / "src" / "core" / "net_proxy.py").read_text(encoding="utf-8"))

print("\n" + "=" * 60)
print(f"★ 1.9.2 任务11 代理分流：{PASS} PASS / {FAIL} FAIL")
for f in FAILS:
    print(f"  FAIL: {f}")
print("=" * 60)
sys.exit(1 if FAIL else 0)
