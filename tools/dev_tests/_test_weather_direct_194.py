# -*- coding: utf-8 -*-
"""★★ 1.9.4：**天气工具必须直连**（国内服务别绕代理）守卫。

## 真机证据（2026-10-21 20:23）
    天气API连接失败: HTTPSConnectionPool(
        host='p37byj4xwf.re.qweatherapi.com', port=443) Max retries exceeded
        ... (Caused by SSLError(SSLEOFError(8,
        '[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol')))
    实时天气偶尔能通、**未来预报必挂** —— 因为这两个请求都走了**系统代理**
    （Clash 香港出口）。国内服务绕 Hong Kong 出去，TLS 直接被掐。

## 钉什么
  A. 天气请求走**专用 Session**，`trust_env=False`（不吃系统代理）；
  B. 源码里**没有**裸 `requests.get(` 绕过去；
  C. 需要代理的机器能一键恢复（`PRTS_WEATHER_USE_PROXY=1`）；
  D. 注释里写明真机证据（SSLEOFError / 香港出口），别再被"优化"回去。
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))


_SRC = (ROOT / "tools" / "weather_tool.py").read_text(encoding="utf-8")

print("== A. 源码层面 ==")
check("★★有专用 `_session()`（单独的 requests.Session）",
      "def _session()" in _SRC)
check("★★`trust_env` 默认 False（不吃系统代理），只有环境变量才打开",
      "s.trust_env = (os.environ.get(\"PRTS_WEATHER_USE_PROXY\") == \"1\")" in _SRC)
check("★★两个请求都走 `_session().get(`，没有裸 `requests.get(` 漏网",
      _SRC.count("_session().get(") >= 2 and "requests.get(" not in _SRC,
      f"_session={_SRC.count('_session().get(')} / requests.get={_SRC.count('requests.get(')}")
check("★注释里写明了真机证据（SSLEOFError + 香港出口）",
      "SSLEOFError" in _SRC and "香港" in _SRC)

print("\n== B. 运行时行为 ==")
from tools import weather_tool as W                    # noqa: E402
_s = W._session()
check("★★默认 `trust_env is False`（实测对象）", _s.trust_env is False)
check("★是同一个 Session（不会每次新建、也不丢连接池）", W._session() is _s)

os.environ["PRTS_WEATHER_USE_PROXY"] = "1"
try:
    W._SESSION = None
    check("★★设 `PRTS_WEATHER_USE_PROXY=1` → trust_env 变 True（可恢复代理）",
          W._session().trust_env is True)
finally:
    os.environ.pop("PRTS_WEATHER_USE_PROXY", None)
    W._SESSION = None

print("\n== C. 汇报不许在「工具失败」时说「查好了」==")
_SESS = (ROOT / "src" / "core" / "session.py").read_text(encoding="utf-8")
check("★★这一轮有工具失败时，进度汇报被显式拦掉",
      "round_had_failure" in _SESS
      and "本轮有工具失败（不能编'查好了'）" in _SESS)
check("★★兜底那句也受同一条限制",
      "not round_had_failure and _pb_r.would_accept()[0]" in _SESS)

print(f"\n==== 结果: {len(PASS)} PASS / {len(FAIL)} FAIL ====")
for f in FAIL:
    print("  FAIL:", f)
sys.exit(1 if FAIL else 0)
