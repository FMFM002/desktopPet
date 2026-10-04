"""终末地：扫码登录 + 公告 + 未登录时的角色/签到提示（不发真实请求到聊天窗口）"""
import os
import sys
import json
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE))
os.environ.setdefault("DEEPSEEK_API_KEY", "test")

ok = True
def check(name, cond, extra=""):
    global ok
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (f"  {extra}" if extra else ""))
    if not cond:
        ok = False


from tools import endfield_tool as ef

print("== 1. 工具元数据 ==")
check("TOOL_NAME = endfield", ef.TOOL_NAME == "endfield")
actions = ef.TOOL_PARAMETERS["properties"]["action"]["enum"]
for a in ("announce", "login", "login_status", "logout", "char_list", "signin", "help"):
    check(f"暴露 action={a}", a in actions, str(actions))

print("== 2. 公告（只需 API Key） ==")
r = json.loads(ef.execute(action="announce"))
# ★ 接管修正（2026-09-17 22:20）：这条打的是**第三方服务器**，会被它限流。
#   实测遇到过 `HTTP 429 —— 请求过于频繁` —— 那是外部状态，不是代码坏了，
#   判成 FAIL 会让"全量常绿"变成看运气的活儿。限流/网络类错误一律 SKIP。
_err = str(r.get("error") or "")
if not r.get("success") and ("429" in _err or "请求过于频繁" in _err
                            or "timeout" in _err.lower() or "timed out" in _err.lower()):
    print(f"  SKIP  公告接口被限流/超时（外部原因，不算失败）：{_err[:70]}")
else:
    check("公告可用", r.get("success") is True, str(r)[:110])
if r.get("success"):
    check("有标题", bool(r.get("title")), str(r.get("title"))[:60])
    check("带 need_web_search 标记", "need_web_search" in r)
    print(f"       标题={r.get('title')} | 正文 {r.get('content_length')} 字 | "
          f"需要联网补充={r.get('need_web_search')}")

print("== 3. 未登录时的角色/签到提示 ==")
ef.logout()
r = json.loads(ef.execute(action="char_list"))
check("char_list 明确要求先登录", r.get("need_login") is True, str(r)[:90])
r = json.loads(ef.execute(action="signin"))
check("signin 明确要求先登录", r.get("need_login") is True, str(r)[:90])

print("== 4. 登录发起（扫码优先，不可用则自动走网页授权） ==")
r = json.loads(ef.execute(action="login"))
check("login 返回成功", r.get("success") is True, str(r)[:130])
mode = r.get("mode")
check("模式为 qr 或 web", mode in ("qr", "web"), f"mode={mode}")
if mode == "web":
    check("网页授权给了链接", str(r.get("auth_url", "")).startswith("https://"),
          str(r.get("auth_url"))[:80])
qr = r.get("qr_image_path") or ""
check("登录码已落盘为 PNG", bool(qr) and Path(qr).exists(), qr)
if qr and Path(qr).exists():
    png = Path(qr).read_bytes()
    check("是合法 PNG（魔数）", png[:8] == b"\x89PNG\r\n\x1a\n", str(png[:8]))
    # ★ 1.8.10: 原来断言 len(png) > 1024 是错的 —— 256×256 的调色板二维码
    #   压缩后只有约 650 字节。改成真正有意义的校验：能解码、尺寸合理、
    #   深浅像素比例像二维码（不是一张空白图）。
    try:
        from PIL import Image
        with Image.open(Path(qr)) as _im:
            _w, _h = _im.size
            _px = list(_im.convert("L").getdata())
        _dark = sum(1 for v in _px if v < 128) / max(1, len(_px))
        check("二维码尺寸合理（≥128 且为正方形）",
              _w >= 128 and _w == _h, f"{_w}×{_h}")
        check("二维码不是空白图（深浅比例 10%~70%）",
              0.10 <= _dark <= 0.70, f"深色占比 {_dark*100:.1f}%")
    except Exception as e:
        check("二维码可被 PIL 解码", False, str(e))
st = ef.login_status()
check("登录会话进行中", st.get("login_running") is True, str(st))
r2 = json.loads(ef.execute(action="login"))
check("重复发起时复用同一会话", r2.get("already_running") is True, str(r2)[:100])

print("== 5. 退出登录清理 ==")
r = json.loads(ef.execute(action="logout"))
check("logout 成功", r.get("success") is True, str(r)[:80])
check("凭证已清除", ef.login_status().get("logged_in") is False)

print("\n结果:", "ALL PASS" if ok else "FAILED")
sys.exit(0 if ok else 1)
