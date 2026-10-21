# -*- coding: utf-8 -*-
"""★★ 1.9.4：**LLM 请求默认直连**（绕开系统代理）守卫。

## 真机证据（2026-10-21 20:10 日志）
`api.deepseek.com` 的请求被系统代理（Clash 127.0.0.1:7897）接管后反复失败：

    start_tls.failed  ConnectTimeout('_ssl.c:993: The handshake operation timed out')
    start_tls.failed  ConnectError(SSLEOFError(8, '[SSL: UNEXPECTED_EOF...]'))
    receive_response_body.failed  ReadTimeout('The read operation timed out')

表现：聊天卡住 → 回一句「(唔…我好像卡住了，稍后再试试吧)」。
而同一时刻**直连是通的**（DeepSeek 的 API 国内外都可直连）。
⇒ LLM 默认 `trust_env=False`（不吃系统代理）；需要代理的机器设
   `PRTS_LLM_USE_PROXY=1` 即可恢复。

★ 只影响 LLM：pixiv / B站 / 下载 那些**仍然**走系统代理（它们需要）。
"""
import importlib
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("DEEPSEEK_API_KEY", "test")
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))


print("== A. 默认直连 ==")
os.environ.pop("PRTS_LLM_USE_PROXY", None)
from src.core import llm_client as L                    # noqa: E402
importlib.reload(L)
check("★★默认 `_LLM_DIRECT` 为真（不吃系统代理）", L._LLM_DIRECT is True)
_c = L._llm_http_client(30.0)
check("★★直连客户端 `trust_env=False`",
      _c is not None and getattr(_c, "trust_env", True) is False,
      type(_c).__name__)

print("\n== B. 需要代理时能一键恢复 ==")
os.environ["PRTS_LLM_USE_PROXY"] = "1"
try:
    importlib.reload(L)
    check("★设 `PRTS_LLM_USE_PROXY=1` → `_LLM_DIRECT` 为假（走 SDK 默认/系统代理）",
          L._LLM_DIRECT is False)
    check("★这时 `_llm_http_client()` 返回 None（交给 SDK）",
          L._llm_http_client(30.0) is None)
finally:
    os.environ.pop("PRTS_LLM_USE_PROXY", None)
    importlib.reload(L)

print("\n== C. 所有 OpenAI 客户端都接上了（不能漏一个）==")
_src = (ROOT / "src" / "core" / "llm_client.py").read_text(encoding="utf-8")
_n_openai = _src.count("OpenAI(")
_n_hc = _src.count("http_client=_llm_http_client(")
check(f"★★`OpenAI(...)` 共 {_n_openai} 处，全部带 http_client",
      _n_openai > 0 and _n_openai == _n_hc, f"{_n_hc}/{_n_openai}")
check("★注释里写明了真机证据（SSL handshake timeout / EOF）与开关名",
      "PRTS_LLM_USE_PROXY" in _src and "handshake" in _src)
check("★只影响 LLM：pixiv/B站仍走系统代理（注释里明确写了）",
      "只影响" in _src and "仍然各自走系统代理" in _src)

print(f"\n==== 结果: {len(PASS)} PASS / {len(FAIL)} FAIL ====")
for f in FAIL:
    print("  FAIL:", f)
sys.exit(1 if FAIL else 0)
