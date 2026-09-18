"""
终末地工具 v2.0 —— 登录(扫码) / 角色 / 签到 / 公告

★ 1.6.0: 按上游 nonebot-plugin-endfield / endfield-plugin 的接口重写：
  · 服务级凭证：X-API-Key（.env 里的 ENDFIELD_API_KEY）
  · 账号级凭证：X-Framework-Token（扫码登录后拿到，才能查角色/签到）
  · 扫码流程：GET /login/endfield/qr → 轮询 /login/endfield/qr/status
              → POST /login/endfield/qr/confirm 拿最终 framework_token
  · 角色列表：GET /api/endfield/note（X-API-Key + X-Framework-Token）
  · 签到：POST /api/endfield/attendance
  · 公告：GET /api/announcements/latest（只需 X-API-Key）
"""
import os
import json
import time
import logging
import threading
from pathlib import Path
from datetime import datetime

import requests
from dotenv import load_dotenv

_log = logging.getLogger("Tool.Endfield")

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

ENDFIELD_API_KEY = os.getenv("ENDFIELD_API_KEY", "")
ENDFIELD_API_BASE = os.getenv("ENDFIELD_API_BASEURL", "https://end-api.shallow.ink").rstrip("/")

# 账号凭证落盘位置（不进 .env，避免手改；也不放项目根）
TOKEN_FILE = BASE_DIR / "data" / "桌宠私有data" / "endfield_login.json"

_UA = "PRTS-Desktop-Pet/1.6.0"


# ★ 1.9.2（解耦调研的产物，对应遗留项 D1）：扫码登录状态**不能放模块级**。
#
# 原因：本工具是 LAZY 策略，空闲 30 秒就被 ToolManager 卸载
# （src/core/tool_manager.py 的 IDLE_UNLOAD_TIMEOUT），卸载时会把
# `_prts_tool_endfield` 从 sys.modules 里删掉；下次调用重新 import 出**一份
# 全新的模块**，于是模块级的 `_login_state = {}` 也变成一个新的空 dict。
#
# 而扫码登录会开后台轮询线程（下面 _poll_login，deadline 长达几分钟），
# 用户慢慢扫码很容易超过 30 秒 —— 那时：
#     · 轮询线程还在往【旧 dict】写结果；
#     · 新的 `action=login` / `action=status` 读到的是【空的新 dict】；
#   → "明明扫了码，却还说我未登录"，正是你报的"登录状态机不稳定"。
#
# 机械复现（旧写法）：
#     加载后 _login_state id=2144863097088 {'running': True, ...}
#     卸载 → 重载后 id=2144863095680 {}      ← 换了对象，状态全丢
#
# 修法：状态与锁都放进 **tools 包级登记表**（包只被导入一次，不随工具重载重建），
# 和已有的 LOGIN_CALLBACKS 是同一个套路。下面两行拿到的是稳定对象，
# 全文其它地方照旧写 `_login_state[...]` / `with _login_lock:`，不用改。
def _session() -> dict:
    """取（或首次创建）本工具的包级长流程会话：{"state": {...}, "lock": Lock}"""
    try:
        from tools import get_session
        return get_session("endfield")
    except Exception:
        # 极端情况下拿不到包登记表 → 退回模块级（至少不比旧行为差）
        globals().setdefault("_fallback_session",
                             {"state": {}, "lock": threading.Lock()})
        return _fallback_session


_SESSION = _session()
# 扫码登录的轮询状态（★ 包级单例，卸载重载后仍是同一份）
_login_state: dict = _SESSION["state"]
_login_lock = _SESSION["lock"]
# 登录成功后回调（由 MainWindow 注册，用于把结果发到聊天窗口）
_on_login_done = None

requests.packages.urllib3.disable_warnings()


# ══════════════════════════════════════════════════════════════
# 底层请求
# ══════════════════════════════════════════════════════════════

def _load_framework_token() -> str:
    """读取已保存的账号凭证"""
    try:
        if TOKEN_FILE.exists():
            d = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
            return (d.get("framework_token") or "").strip()
    except Exception as e:
        _log.debug(f"读取 endfield 凭证失败: {e}")
    return ""


def _save_framework_token(token: str, role: dict | None = None):
    """保存账号凭证（含可选的角色信息）"""
    try:
        TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "framework_token": token,
            "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        if role:
            data["role"] = role
            data["role_id"] = str(role.get("role_id") or role.get("roleId") or "")
            data["server_id"] = str(role.get("server_id") or role.get("serverId") or "")
            data["nickname"] = role.get("nickname") or ""
        TOKEN_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                              encoding="utf-8")
        _log.info(f"终末地凭证已保存: {TOKEN_FILE}")
    except Exception as e:
        _log.warning(f"保存 endfield 凭证失败: {e}")


def _load_role() -> dict:
    try:
        if TOKEN_FILE.exists():
            return json.loads(TOKEN_FILE.read_text(encoding="utf-8")).get("role", {}) or {}
    except Exception:
        pass
    return {}


def _api(method: str, path: str, *, need_token: bool = False,
         token: str = "", params: dict | None = None, body: dict | None = None,
         timeout: int = 20) -> dict:
    """统一请求：自动带 X-API-Key（+ 可选 X-Framework-Token）"""
    if not ENDFIELD_API_KEY:
        return {"code": -100, "message": "未配置 ENDFIELD_API_KEY（请到 https://end.shallow.ink 获取并写入 .env）"}
    url = ENDFIELD_API_BASE + path
    headers = {
        "User-Agent": _UA,
        "X-API-Key": ENDFIELD_API_KEY,
        "Accept": "application/json",
    }
    ft = token or (_load_framework_token() if need_token else "")
    if need_token and not ft:
        return {"code": -101, "message": "尚未登录终末地账号（请先扫码登录）"}
    if ft:
        headers["X-Framework-Token"] = ft
    if body is not None:
        headers["Content-Type"] = "application/json"
    try:
        resp = requests.request(method, url, headers=headers, params=params,
                                json=body, timeout=timeout, verify=False)
        if resp.status_code == 200:
            try:
                return resp.json()
            except Exception:
                return {"code": -1, "message": "响应不是 JSON"}
        hint = {
            401: "API Key 无效或未配置",
            403: "缺少/失效的账号凭证（X-Framework-Token），请重新扫码登录",
            404: "接口不存在（上游可能改版）",
            429: "请求过于频繁，稍后再试",
        }.get(resp.status_code, "")
        return {"code": resp.status_code,
                "message": f"HTTP {resp.status_code}" + (f" —— {hint}" if hint else "")}
    except requests.exceptions.Timeout:
        return {"code": -1, "message": "请求超时"}
    except requests.exceptions.ConnectionError:
        return {"code": -1, "message": "网络连接失败（可能需要代理）"}
    except Exception as e:
        return {"code": -1, "message": f"{type(e).__name__}: {e}"}


# ══════════════════════════════════════════════════════════════
# 扫码登录
# ══════════════════════════════════════════════════════════════

def set_login_callback(cb):
    """注册登录完成回调：cb(ok: bool, message: str, role: dict)

    ★ 1.8.10: 同时写进 tools 包的登记表 —— ToolManager 把工具文件加载成
    私有模块名 `_prts_tool_endfield`，而 MainWindow 注册回调时 import 的是
    `tools.endfield_tool`，两者是**不同的 module 对象**。
    只设本模块的全局变量，真正在跑的那份模块读到的仍然是 None，
    于是"扫码成功"永远推不到聊天窗口。
    """
    global _on_login_done
    _on_login_done = cb
    try:
        from tools import LOGIN_CALLBACKS
        # 用字面量而不是 TOOL_NAME：后者定义在文件末尾（第 613 行），
        # 虽然调用时已存在，但依赖声明顺序太脆。
        LOGIN_CALLBACKS["endfield"] = cb
    except Exception:
        pass


def _get_login_callback():
    """优先读 tools 包登记表（跨模块重载有效），退回本模块全局。"""
    try:
        from tools import LOGIN_CALLBACKS
        cb = LOGIN_CALLBACKS.get("endfield")
        if cb is not None:
            return cb
    except Exception:
        pass
    return _on_login_done


def _finish_login(ok: bool, message: str, role: dict | None = None):
    with _login_lock:
        _login_state["running"] = False
        _login_state["result"] = message
    cb = _get_login_callback()
    if callable(cb):
        try:
            cb(ok, message, role or {})
        except Exception as e:
            _log.warning(f"登录回调失败: {e}")


def start_login(timeout_minutes: int = 3) -> dict:
    """发起登录：优先扫码；扫码接口不可用时自动改用「网页授权」链接"""
    with _login_lock:
        if _login_state.get("running"):
            st = _login_state.get("status", "pending")
            return {"success": True, "already_running": True,
                    "qrcode": _login_state.get("qrcode", ""),
                    "qr_image_path": _login_state.get("qr_image_path", ""),
                    "auth_url": _login_state.get("auth_url", ""),
                    "message": f"已有进行中的登录（状态：{st}）"}
        _login_state.clear()
        _login_state["running"] = True
        _login_state["status"] = "pending"

    data = _api("GET", "/login/endfield/qr")
    if data.get("code") == 0:
        return _start_qr_session(data)

    # ★ 1.6.0: 上游扫码服务挂了（实测返回 500「获取扫码ID失败」）时，
    #   自动改走网页授权流程，保证功能仍然可用
    _log.warning(f"扫码接口不可用（{data.get('message')}），改用网页授权流程")
    return _start_web_auth_session(reason=str(data.get("message", "")))


def _start_qr_session(data: dict) -> dict:
    d = data.get("data", {}) or {}
    qr_token = d.get("framework_token", "")
    qrcode = d.get("qrcode", "")
    expire = d.get("expire", 0)
    if not qr_token or not qrcode:
        _finish_login(False, "二维码响应缺少字段")
        return {"success": False, "error": "二维码响应缺少 framework_token/qrcode"}

    qr_path = _save_qr_image(qrcode)
    with _login_lock:
        _login_state["mode"] = "qr"
        _login_state["qr_token"] = qr_token
        _login_state["qrcode"] = qrcode
        _login_state["qr_image_path"] = qr_path
        _login_state["expire"] = expire

    threading.Thread(target=_poll_login, args=(qr_token, 3),
                     daemon=True, name="endfield-login-poll").start()
    _log.info(f"终末地扫码登录已发起，二维码图片: {qr_path}")
    return {"success": True, "mode": "qr", "qrcode": qrcode, "qr_image_path": qr_path,
            "expire": expire,
            "message": "已发给你一张二维码图片，请用终末地/森空岛 App 扫码；"
                       "扫码完成后我会自动确认并保存凭证（约 3 分钟内有效）"}


def _start_web_auth_session(reason: str = "") -> dict:
    """网页授权流程：给用户一个授权链接，授权后自动拿到凭证"""
    body = {
        "client_id": "prts-desktop-pet",
        "client_name": "PRTS 桌宠",
        "client_type": "bot",
        "platform_id": "local",
        "scopes": ["user_info", "binding_info", "game_data", "attendance"],
    }
    data = _api("POST", "/api/v1/authorization/requests", body=body)
    if data.get("code") != 0:
        _finish_login(False, f"获取授权链接失败：{data.get('message')}")
        return {"success": False, "error": data.get("message", "获取授权链接失败")}

    d = data.get("data", {}) or {}
    req_id = d.get("request_id", "")
    auth_url = d.get("auth_url", "")
    if auth_url.startswith("/"):
        auth_url = "https://end.shallow.ink" + auth_url
    if not req_id or not auth_url:
        _finish_login(False, "授权响应缺少字段")
        return {"success": False, "error": "授权响应缺少 request_id/auth_url"}

    qr_path = _make_qr_from_url(auth_url)
    with _login_lock:
        _login_state["mode"] = "web"
        _login_state["request_id"] = req_id
        _login_state["auth_url"] = auth_url
        _login_state["qr_image_path"] = qr_path

    threading.Thread(target=_poll_web_auth, args=(req_id,),
                     daemon=True, name="endfield-webauth-poll").start()
    _log.info(f"终末地网页授权已发起: {auth_url}")
    msg = "扫码接口暂时不可用，已改用网页授权："
    if reason:
        msg += f"（上游报错：{reason}）"
    msg += "\n请点开这个链接完成授权：\n" + auth_url + "\n授权后我会自动保存凭证。"
    return {"success": True, "mode": "web", "auth_url": auth_url,
            "qr_image_path": qr_path, "message": msg}


def _make_qr_from_url(url: str) -> str:
    """把授权链接画成二维码 PNG（有 qrcode 库就画，没有就返回空）"""
    try:
        import qrcode
        img = qrcode.make(url)
        out_dir = BASE_DIR / "data" / "桌宠私有data" / "endfield"
        out_dir.mkdir(parents=True, exist_ok=True)
        p = out_dir / "login_authurl_qrcode.png"
        img.save(str(p))
        return str(p)
    except Exception as e:
        _log.debug(f"生成授权链接二维码失败（可忽略）: {e}")
        return ""


def _poll_web_auth(req_id: str, timeout_minutes: int = 5):
    """轮询网页授权状态，批准后保存凭证"""
    deadline = time.time() + max(1, timeout_minutes) * 60
    while time.time() < deadline:
        time.sleep(3)
        with _login_lock:
            if not _login_state.get("running"):
                return
        st = _api("GET", f"/api/v1/authorization/requests/{req_id}/status")
        if st.get("code") != 0:
            continue
        d = st.get("data", {}) or {}
        status = str(d.get("status", "")).lower()
        with _login_lock:
            _login_state["status"] = status or "pending"
        if status in ("rejected", "expired"):
            _finish_login(False, "授权被拒绝或已过期，请重新发起")
            return
        if status in ("approved", "used") or d.get("framework_token"):
            token = (d.get("framework_token") or "").strip()
            if not token:
                continue
            roles = d.get("available_roles") or []
            info = d.get("binding_info") or {}
            role = {}
            for r in roles:
                if r.get("is_default"):
                    role = r
                    break
            if not role and roles:
                role = roles[0]
            if not role and info:
                role = info
            _save_framework_token(token, role)
            name = role.get("nickname") or info.get("nickname") or "未命名"
            _finish_login(True, f"网页授权成功！绑定角色：{name}", role)
            return
    with _login_lock:
        if _login_state.get("running"):
            _finish_login(False, "授权等待超时，请重新发起登录")


def _save_qr_image(qrcode: str) -> str:
    """把接口返回的 base64 二维码落成 PNG 文件，返回路径（供聊天窗口显示）"""
    try:
        import base64
        import re as _re
        b64 = qrcode
        if b64.startswith("data:image"):
            b64 = b64.split(",", 1)[-1]
        b64 = _re.sub(r"\s+", "", b64)
        raw = base64.b64decode(b64)
        out_dir = BASE_DIR / "data" / "桌宠私有data" / "endfield"
        out_dir.mkdir(parents=True, exist_ok=True)
        p = out_dir / "login_qrcode.png"
        p.write_bytes(raw)
        return str(p)
    except Exception as e:
        _log.warning(f"二维码图片保存失败: {e}")
        return ""


def _poll_login(qr_token: str, timeout_minutes: int):
    """后台轮询扫码状态 → 确认 → 保存凭证"""
    _STATUS_TEXT = {
        "pending": "等待扫码",
        "scanned": "已扫码，等待确认",
        "authed": "已授权，正在确认",
        "expired": "二维码已过期",
        "failed": "登录失败",
    }
    deadline = time.time() + max(1, timeout_minutes) * 60
    while time.time() < deadline:
        time.sleep(2)
        with _login_lock:
            if not _login_state.get("running"):
                return
        st = _api("GET", "/login/endfield/qr/status", params={"framework_token": qr_token})
        if st.get("code") not in (0, 1, 2, 3):
            # 明确的失败/过期
            msg = st.get("message", "轮询失败")
            if any(k in str(msg) for k in ("过期", "expired")):
                _finish_login(False, "二维码已过期，请重新发起登录")
            elif st.get("code") == -2:
                _finish_login(False, "二维码已过期，请重新发起登录")
            elif st.get("code") == -3:
                _finish_login(False, f"登录失败：{msg}")
            continue

        d = st.get("data", {}) or {}
        status = (d.get("status") or "").lower()
        code = d.get("code")
        with _login_lock:
            _login_state["status"] = status or str(code)
        if status in ("expired",) or code == -2:
            _finish_login(False, "二维码已过期，请重新发起登录")
            return
        if status in ("failed",) or code == -3:
            _finish_login(False, f"登录失败：{d.get('msg') or '未知原因'}")
            return
        if status in ("done", "authed") or code in (0, 3):
            break
        _log.debug(f"扫码状态: {status or code} ({_STATUS_TEXT.get(status, '')})")

    with _login_lock:
        if not _login_state.get("running"):
            return

    cf = _api("POST", "/login/endfield/qr/confirm",
              body={"framework_token": qr_token, "platform": "bot"})
    if cf.get("code") != 0:
        _finish_login(False, f"确认登录失败：{cf.get('message')}")
        return
    d = cf.get("data", {}) or {}
    final_token = (d.get("framework_token") or "").strip()
    if not final_token:
        _finish_login(False, "确认登录成功但未拿到凭证")
        return
    roles = d.get("available_roles") or []
    role = {}
    for r in roles:
        if r.get("is_default"):
            role = r
            break
    if not role and roles:
        role = roles[0]
    _save_framework_token(final_token, role)
    name = role.get("nickname") or "未命名"
    _finish_login(True, f"登录成功！绑定角色：{name}（共 {len(roles)} 个可用角色）", role)


def login_status() -> dict:
    """查询当前登录状态"""
    with _login_lock:
        running = bool(_login_state.get("running"))
        status = _login_state.get("status", "")
        result = _login_state.get("result", "")
    token = _load_framework_token()
    role = _load_role()
    return {
        "logged_in": bool(token),
        "role": role,
        "login_running": running,
        "login_status": status,
        "last_result": result,
    }


def logout() -> dict:
    """清除本地凭证"""
    try:
        if TOKEN_FILE.exists():
            TOKEN_FILE.unlink()
        return {"success": True, "message": "已清除终末地登录凭证"}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ══════════════════════════════════════════════════════════════
# 业务功能
# ══════════════════════════════════════════════════════════════

def _announce() -> dict:
    """最新公告（只需 API Key）"""
    data = _api("GET", "/api/announcements/latest")
    if data.get("code") != 0:
        return {"success": False, "error": data.get("message", "查询失败")}
    d = data.get("data", {}) or {}
    texts = d.get("texts", []) or []
    content = "\n".join(t.get("content", "") for t in texts if t.get("content")).strip()
    images = [i.get("url", "") for i in (d.get("images") or []) if i.get("url")]
    title = d.get("title", "未知")
    item_id = d.get("item_id") or d.get("id") or ""
    link = f"https://www.skland.com/article?id={item_id}" if item_id else ""
    return {
        "success": True,
        "title": title,
        "content": content[:1200],
        "content_length": len(content),
        "images": images[:4],
        "link": link,
        "message": f"终末地最新公告：{title}",
        # ★ 内容过短时让调用方（LLM）去联网补充背景
        "need_web_search": len(content) < 50,
    }


def _char_list(keyword: str = "") -> dict:
    """角色/干员列表（需要登录凭证）"""
    token = _load_framework_token()
    if not token:
        return {"success": False, "need_login": True,
                "error": "尚未登录终末地账号，请先让我发起扫码登录（action=login）"}
    role = _load_role()
    params = {}
    if role.get("roleId") or role.get("role_id"):
        params["roleId"] = str(role.get("roleId") or role.get("role_id"))
    if role.get("serverId") or role.get("server_id"):
        params["serverId"] = str(role.get("serverId") or role.get("server_id"))
    data = _api("GET", "/api/endfield/note", need_token=True,
                params=params or None)
    if data.get("code") != 0:
        msg = str(data.get("message", ""))
        need_login = any(k in msg for k in ("凭证", "登录", "token", "403"))
        return {"success": False, "need_login": need_login, "error": msg or "查询失败"}

    d = data.get("data", {}) or {}
    base = d.get("base", {}) or {}
    chars = d.get("chars", []) or []
    if keyword:
        chars = [c for c in chars if keyword.lower() in str(c.get("name", "")).lower()]
    out = []
    for c in chars[:20]:
        rar = c.get("rarity")
        if isinstance(rar, dict):
            rar = rar.get("value") or rar.get("level") or ""
        prof = c.get("profession")
        if isinstance(prof, dict):
            prof = prof.get("value") or ""
        prop = c.get("property")
        if isinstance(prop, dict):
            prop = prop.get("value") or ""
        out.append({
            "name": c.get("name", ""),
            "level": c.get("level") or c.get("charLevel") or "",
            "rarity": rar,
            "profession": prof,
            "element": prop,
        })
    return {
        "success": True,
        "account": {
            "nickname": base.get("name", ""),
            "level": base.get("level", ""),
            "world_level": base.get("worldLevel", ""),
            "server": base.get("serverName", ""),
            "char_num": base.get("charNum", len(chars)),
            "last_login": base.get("lastLoginTime") or base.get("lastOnline") or "",
        },
        "characters": out,
        "total": base.get("charNum") or len(chars),
        "keyword": keyword,
        "message": f"终末地角色共 {base.get('charNum') or len(chars)} 位"
                   + (f"（已按「{keyword}」筛选，显示前 {len(out)} 位）" if keyword else f"（显示前 {len(out)} 位）"),
    }


def _signin() -> dict:
    """森空岛每日签到（需要登录凭证）"""
    token = _load_framework_token()
    if not token:
        return {"success": False, "need_login": True,
                "error": "尚未登录终末地账号，请先扫码登录（action=login）"}
    role = _load_role()
    params = {}
    if role.get("roleId") or role.get("role_id"):
        params["role_id"] = str(role.get("roleId") or role.get("role_id"))
    if role.get("serverId") or role.get("server_id"):
        params["server_id"] = str(role.get("serverId") or role.get("server_id"))
    data = _api("POST", "/api/endfield/attendance", need_token=True,
                params=params or None)
    if data.get("code") != 0:
        msg = str(data.get("message", ""))
        if "已" in msg and "签" in msg:
            return {"success": True, "already_signed": True, "message": "今日已完成森空岛签到"}
        need_login = any(k in msg for k in ("凭证", "登录", "token", "403"))
        return {"success": False, "need_login": need_login, "error": msg or "签到失败"}

    d = data.get("data", {}) or {}
    if d.get("already_signed"):
        return {"success": True, "already_signed": True, "message": "今日已完成森空岛签到"}
    # 解析奖励名
    rewards = []
    rmap = d.get("resourceInfoMap") or {}
    for aw in (d.get("awardIds") or []):
        rid = str(aw.get("id"))
        info = rmap.get(rid, {}) or {}
        rewards.append(f"{info.get('name', rid)}×{aw.get('count', 1)}")
    return {
        "success": True,
        "rewards": rewards,
        "message": "森空岛签到成功" + (f"：{'、'.join(rewards)}" if rewards else ""),
    }


def _help() -> dict:
    st = login_status()
    return {
        "success": True,
        "message": "终末地工具使用帮助",
        "logged_in": st["logged_in"],
        "role": st["role"].get("nickname", ""),
        "commands": {
            "登录": "action=login（我会发一张二维码图片给你扫，扫完自动保存凭证）",
            "登录状态": "action=login_status",
            "退出登录": "action=logout",
            "公告": "action=announce（无需登录）",
            "角色列表": "action=char_list, keyword=可选筛选词（需登录）",
            "签到": "action=signin（需登录）",
        },
    }


def execute(action: str = "announce", keyword: str = "", **kwargs) -> str:
    """执行终末地工具操作"""
    _log.info(f"终末地工具: {action}")
    handlers = {
        "announce": _announce,
        "char_list": lambda: _char_list(keyword),
        "signin": _signin,
        "login": start_login,
        "login_status": login_status,
        "logout": logout,
        "help": _help,
    }
    handler = handlers.get(action)
    if handler is None:
        result = {"success": False,
                  "error": f"未知操作: {action}，支持: " + "/".join(handlers.keys())}
    else:
        try:
            result = handler()
        except Exception as e:
            _log.error(f"终末地工具执行失败: {e}", exc_info=True)
            result = {"success": False, "error": str(e)}
    return json.dumps(result, ensure_ascii=False, indent=2)


# 工具元数据（供 ToolManager 扫描）
TOOL_NAME = "endfield"
TOOL_DESCRIPTION = (
    "明日方舟：终末地 游戏数据查询工具。支持: "
    "announce(最新公告，无需登录)、"
    "login(扫码登录，会发二维码图片到聊天窗口)、login_status(登录状态)、logout(退出登录)、"
    "char_list(角色列表，需登录，keyword 可选筛选)、"
    "signin(森空岛每日签到，需登录)、help(帮助)。"
    "首次使用查角色/签到需要先登录。"
)
TOOL_PARAMETERS = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["announce", "login", "login_status", "logout",
                     "char_list", "signin", "help"],
            "description": "操作类型，默认 announce",
            "default": "announce",
        },
        "keyword": {
            "type": "string",
            "description": "[char_list] 可选：按角色名筛选",
        },
    },
    "required": ["action"],
}
