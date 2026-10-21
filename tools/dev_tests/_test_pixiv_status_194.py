# -*- coding: utf-8 -*-
"""★★ 1.9.4：**pixiv 维护探测**守卫（真机 2026-10-21 的关键教训）。

## 背景（我判错过一次，记在这里）
用户报 P站 `HTTP 405: 405 Not Allowed`，我判成"代理出口被 Cloudflare 拦"，
建议他换节点 —— **错了**。他一开浏览器：pixiv 官方维护页
（"Under maintenance … scheduled to end in the early hours of October 22, 2026 (JST)"）。

复测：维护期间 **浏览器 UA** 访问首页/令牌端点都是 `503 + Under maintenance`，
而 **App UA 的 POST** 会被边缘直接挡成 `405`。
⇒ 405/403/503 混在一起，光看状态码**分不出**"维护"和"被挡"。

本套件钉住：
  A. `looks_like_maintenance()` 只认"站点整体不可用 + 维护字样"，
     正常 200 页面里出现 "maintenance" 不算；
  B. 英文/日文维护页都要认出来；
  C. 探测失败（网络异常）**不抛**，返回 under_maintenance=False；
  D. 两个调用点（令牌验证 / pixiv 工具）在 4xx/5xx 时都会先探测，维护中直接给维护话术；
  E. 话术里必须明确"不是令牌失效、别换节点、别重登"。
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PRTS_NO_COMFY_START", "1")
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.core import pixiv_status as ps          # noqa: E402

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))


# 真机上抓到的那张维护页的骨架（英文 + 日文各一份）
EN = ("<html><head><title>pixiv</title></head><body>"
      "<h1>Under maintenance</h1>"
      "<p>pixiv is currently under maintenance. The maintenance is scheduled "
      "to end in the early hours of October 22, 2026 (JST).</p></body></html>")
JA = ("<html><body><h1>メンテナンス中</h1>"
      "<p>pixivはただいまメンテナンス中です。</p></body></html>")
NORMAL = ("<html><head><title>pixiv</title></head><body>"
          "<div>イラストを投稿しよう</div></body></html>")

print("== A. 识别逻辑 ==")
check("★★英文维护页（503）认得出", ps.looks_like_maintenance(503, EN) is True)
check("★★日文维护页（503）认得出", ps.looks_like_maintenance(503, JA) is True)
check("★405 的维护骨架也认得出（App UA 的 POST 就是这个码）",
      ps.looks_like_maintenance(405, EN) is True)
check("★★正常页面（200 + 没有维护字样）**不能**误判",
      ps.looks_like_maintenance(200, NORMAL) is False)
check("★★200 页面里只是提到 maintenance（比如帮助页）也不算",
      ps.looks_like_maintenance(200, "<html>how we do maintenance</html>") is False)
check("★空响应不炸", ps.looks_like_maintenance(503, "") is False)

print("\n== B. 探测函数本身不抛 ==")
_v = ps.check(use_cache=False)
check("★返回 dict 且有约定的三个键",
      isinstance(_v, dict) and {"under_maintenance", "status", "detail"} <= set(_v),
      str(_v)[:90])
check("★没网/没代理时也不抛（只把 detail 写成探测失败）",
      isinstance(_v.get("under_maintenance"), bool))

print("\n== C. 话术 ==")
_h = ps.maintenance_hint()
check("★★明确说「不是令牌失效」", "不是令牌失效" in _h)
check("★★明确说「别换节点」", "换节点" in _h)
check("★★明确说「等恢复再刷新」，不要重登", "刷新状态" in _h or "等它恢复" in _h)

print("\n== D. 两个调用点都先探测 ==")
_ac = (ROOT / "src" / "core" / "account_login.py").read_text(encoding="utf-8")
_px = (ROOT / "tools" / "pixiv_tool.py").read_text(encoding="utf-8")
check("★★令牌验证：405/503 分支里 import 了 pixiv_status 并调用 check()",
      "from src.core.pixiv_status import check" in _ac and "_px_check()" in _ac)
check("★★pixiv 工具：认证失败时同样探测", "from src.core.pixiv_status import check" in _px)
check("★★维护中直接返回 maintenance_hint()（不是笼统的'网络问题'）",
      "maintenance_hint()" in _ac and "maintenance_hint()" in _px)
check("★403 也先探测一次（维护期间 403 很常见）",
      '"403" in _m' in _ac)

print("\n== E. 文档口径（桌宠要知道这条）==")
_doc = (ROOT / "桌宠自述.md").read_text(encoding="utf-8")
check("★★桌宠自述里写了「先怀疑维护」", "先判断 pixiv 是不是在维护" in _doc)
check("★★并写明维护期间 405/403/503 会混着出现", "405 / 403 / 503" in _doc)
check("★给了自动探测的存在（pixiv_status）", "pixiv_status" in _doc)

print(f"\n==== 结果: {len(PASS)} PASS / {len(FAIL)} FAIL ====")
for f in FAIL:
    print("  FAIL:", f)
sys.exit(1 if FAIL else 0)
