"""
Pixiv（P站）工具（★ 1.9.0 新增）

参考实现已**内置**在 `tools/_vendor/pixivpy3/`（public domain，见
`tools/_vendor/pixivpy3_LICENSE`）。只对它做了两处**最小补丁**：
  · cloudscraper 变成可选（缺了就退回普通 requests.Session）
  · requests-toolbelt 变成可选（只有 SNI 绕过用得到）
这样在没装这两个包的机器上也能用主 API。

────────────────────────────────────────────────────────────────
【必须先配刷新令牌】
Pixiv 现在必须登录才能用 app-api。在项目根目录 `.env` 里加一行：

    PIXIV_REFRESH_TOKEN=你的刷新令牌

没配时所有 action 都会返回一段**可操作的提示**（怎么拿令牌），
而不是干巴巴一句报错。

【国内网络两种办法，任选其一】
  1) 走代理（推荐）：.env 里加
         PIXIV_PROXY=http://127.0.0.1:7890
  2) SNI 绕过：先 `pip install requests-toolbelt`，再在 .env 里加
         PIXIV_BYPASS=1

────────────────────────────────────────────────────────────────
action:
  info      作品详情           illust_id（支持整段链接）
  search    关键词搜索         keyword, limit
  ranking   排行榜             mode=day|week|month|day_male|day_female|week_original|week_rookie|day_manga
  user      画师的作品         user_id, limit
  download  下载原图           illust_id, pages（如 "1" / "1-3"，默认全部）

下载目录：data/downloads/pixiv/<作品ID>_<标题>/
下载完成后会通过 FILE_READY_CALLBACKS["pixiv"] 把图推给聊天窗口
（和 jmcomic 同一套机制，走统一附件通道 → 会落库、重启后仍在）。
"""
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

_log = logging.getLogger("Tool.Pixiv")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

# ★ 内置的 pixivpy3
_VENDOR = Path(__file__).resolve().parent / "_vendor"
if str(_VENDOR) not in sys.path:
    sys.path.insert(0, str(_VENDOR))

TOOL_NAME = "pixiv"
TOOL_DESCRIPTION = (
    "Pixiv（P站）插画工具。支持: info(作品详情)、search(关键词搜索)、"
    "ranking(排行榜)、user(画师作品)、download(下载原图，可指定第几页)。"
    "用户给 P站链接/作品ID、或想找某个主题的插画时用它。"
    "下载完成的图会自动发到聊天窗口。需要先在 .env 配置 PIXIV_REFRESH_TOKEN。"
)
TOOL_PARAMETERS = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["info", "search", "ranking", "user", "download"],
            "description": "操作类型: info=作品详情, search=关键词搜索, ranking=排行榜, user=画师作品, download=下载原图",
        },
        "illust_id": {"type": "string", "description": "[info/download] 作品ID或整段 P站链接"},
        "keyword": {"type": "string", "description": "[search] 搜索关键词"},
        "user_id": {"type": "string", "description": "[user] 画师ID或主页链接"},
        "limit": {"type": "integer", "description": "[search/ranking/user] 返回多少条（默认 10）"},
        "mode": {"type": "string", "description": "[ranking] 榜单: day/week/month/day_male/day_female/week_original/week_rookie/day_manga"},
        "pages": {"type": "string", "description": "[download] 只下第几页: \"1\" / \"1-3\" / 空=全部（多图作品才有意义）"},
    },
    "required": ["action"],
}

_DOWNLOAD_SUBDIR = "pixiv"
_MAX_PUSH = 6          # 最多往聊天窗口推几张（多图作品别刷屏）

# 供 MainWindow 注册（与 jmcomic 同一套包级登记表机制）
_FILE_READY = None


def set_file_ready_callback(callback):
    """注册"文件就绪"回调：cb(label, path, size)"""
    global _FILE_READY
    _FILE_READY = callback
    try:
        from tools import FILE_READY_CALLBACKS
        FILE_READY_CALLBACKS["pixiv"] = callback
    except Exception:
        pass


def _get_file_ready_callback():
    try:
        from tools import FILE_READY_CALLBACKS
        cb = FILE_READY_CALLBACKS.get("pixiv")
        if cb:
            return cb
    except Exception:
        pass
    return _FILE_READY


# ── 配置 / 客户端 ──

def _refresh_token() -> str:
    return (os.getenv("PIXIV_REFRESH_TOKEN", "") or "").strip()


def _proxy() -> str:
    """当前代理地址。

    ★★ 1.9.2 真机踩坑（用户 00:09 报"关闭 VPN 后 P站也无法使用"）：
      这里原来直接 `os.getenv("PIXIV_PROXY")` —— **没有 .env 兜底**，
      所以能不能读到完全取决于"别处有没有先 load_dotenv()"。
      真机日志里 P站就在 00:09:18 失败、而 NetProxy 直到 00:09:53 才
      "已按需加载 .env" —— 也就是说那次 P站**读到的是空串 → 请求走了直连**
      → SSL 被掐（错误正是 oauth.secure.pixiv.net 握手 EOF）。
      现在统一走 src/core/net_proxy（它自己会按需读 .env，并兼容
      PRTS_PROXY / PIXIV_PROXY 两个键名）。
    """
    try:
        from src.core.net_proxy import proxy_url
        return proxy_url()
    except Exception:
        return (os.getenv("PIXIV_PROXY", "") or "").strip()


# ★ i.pximg.net 会 403 掉 requests 的默认 UA（实测，见 _build_api 里的说明）
_UA = "PixivAndroidApp/5.0.234 (Android 11; Pixel 5)"

# 常见图片/动图的文件头（用来识破"其实是 403 的 HTML"）
_IMAGE_MAGIC = (
    b"\xff\xd8\xff",              # JPEG
    b"\x89PNG\r\n\x1a\n",         # PNG
    b"GIF87a", b"GIF89a",         # GIF
    b"RIFF",                      # WEBP
    b"BM",                        # BMP
    b"II*\x00", b"MM\x00*",       # TIFF
)


def _looks_like_image(path) -> tuple:
    """返回 (是不是图片, 前 4 字节的说明)。用来识破 403/HTML 之类的假文件"""
    try:
        p = Path(path)
        if not p.exists() or p.stat().st_size <= 0:
            return False, "文件不存在或为空"
        with open(p, "rb") as f:
            head = f.read(8)
    except Exception as e:  # noqa: BLE001
        return False, f"读取失败: {e}"
    if any(head.startswith(m) for m in _IMAGE_MAGIC):
        return True, "ok"
    if head[:1] == b"<":
        return False, "对方返回的是 HTML（多半是 403 防盗链/UA 被拦）"
    return False, f"不是图片（文件头 {head[:4]!r}）"


def _wants_bypass() -> bool:
    return (os.getenv("PIXIV_BYPASS", "") or "").strip() not in ("", "0", "false", "False")


_NO_TOKEN_HINT = (
    "还没配置 Pixiv 刷新令牌，所以用不了 P站接口。\n"
    "请在项目根目录 .env 里加一行：\n"
    "    PIXIV_REFRESH_TOKEN=你的刷新令牌\n"
    "（令牌用 pixivpy 官方的 OAuth 流程获取；国内还需在 .env 里配 "
    "PIXIV_PROXY=http://127.0.0.1:7890 走代理，"
    "或 pip install requests-toolbelt 后设 PIXIV_BYPASS=1 走 SNI 绕过。）"
)


def _build_api():
    """建客户端并认证。返回 (api, None) 或 (None, 错误信息)"""
    token = _refresh_token()
    if not token:
        return None, _NO_TOKEN_HINT
    try:
        import pixivpy3
    except Exception as e:  # pragma: no cover
        return None, f"内置 pixivpy3 不可用: {e}"

    kwargs = {}
    proxy = _proxy()
    if proxy:
        kwargs["proxies"] = {"http": proxy, "https": proxy}

    api = None
    if _wants_bypass():
        if getattr(pixivpy3, "ByPassSniApi", None) is None:
            return None, ("PIXIV_BYPASS=1 需要 requests-toolbelt：\n"
                          "    pip install requests-toolbelt")
        try:
            api = pixivpy3.ByPassSniApi(**kwargs)
            api.require_appapi_hosts()
            _log.info("[Pixiv] 已启用 SNI 绕过")
        except Exception as e:
            _log.warning(f"[Pixiv] SNI 绕过失败，回落直连: {e}")
            api = None
    if api is None:
        api = pixivpy3.AppPixivAPI(**kwargs)

    # ★ 真机联调发现（2026-09-16）：i.pximg.net 会**403 掉 requests 的默认
    #   User-Agent**（`python-requests/2.34.2`）——哪怕 Referer 完全正确。
    #   实测：带 Referer + 默认 UA → 403；换成 App 或浏览器 UA → 200。
    #   pixivpy 默认不发 UA，于是**图片永远下不下来**（还会把 403 的 HTML
    #   当图片存进文件，见 _act_download 的校验）。这里统一补上真实 UA。
    try:
        api.set_additional_headers({
            "User-Agent": _UA,
            "Accept-Language": "zh-cn",
        })
    except Exception as e:  # pragma: no cover
        _log.debug(f"[Pixiv] 设置请求头失败: {e}")

    try:
        api.auth(refresh_token=token)
    except Exception as e:
        msg = str(e)
        hint = ""
        if "403" in msg or "400" in msg or "refresh_token" in msg:
            hint = ("（令牌可能过期/失效；刷新令牌是一次性的，"
                    "每次登录都会换一个新的，建议把 .env 里的值更新成最新的）")
        return None, f"Pixiv 登录失败: {msg[:300]}{hint}"
    _save_rotated_token(api)
    return api, None


def _save_rotated_token(api):
    """pixiv 会轮换 refresh_token —— 写回 .env，否则下次登录失败（一次性令牌）"""
    try:
        new = (getattr(api, "refresh_token", "") or "").strip()
        old = _refresh_token()
        if not new or new == old:
            return
        env = PROJECT_ROOT / ".env"
        if not env.exists():
            return
        text = env.read_text(encoding="utf-8")
        if re.search(r"^PIXIV_REFRESH_TOKEN=.*$", text, re.M):
            text = re.sub(r"^PIXIV_REFRESH_TOKEN=.*$", f"PIXIV_REFRESH_TOKEN={new}",
                          text, flags=re.M)
        else:
            text = text.rstrip("\n") + f"\nPIXIV_REFRESH_TOKEN={new}\n"
        env.write_text(text, encoding="utf-8")
        _log.info("[Pixiv] 刷新令牌已轮换并写回 .env")
    except Exception as e:
        _log.debug(f"[Pixiv] 写回刷新令牌失败: {e}")


# ── 小工具 ──

def _g(obj, key, default=None):
    """兼容 dict(JsonDict) 与 pydantic 模型两种返回形态"""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _extract_id(s: str) -> str:
    """从 'https://www.pixiv.net/artworks/123456' / 'users/42' / '123456' / '42' 里取 ID

    ★ 注意：画师 ID 可能很短（如 42），所以**纯数字输入一律原样接受**，
      不能统一要求 4 位以上（那样短 ID 会被拒掉）。
    """
    s = (s or "").strip()
    if s.isdigit():
        return s
    m = re.search(r"(?:artworks/|illust_id=|users/|member\.php\?id=)(\d+)", s)
    if m:
        return m.group(1)
    m = re.search(r"(\d{4,})", s)          # 兜底：链接里的长数字
    return m.group(1) if m else ""


def _safe_name(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", (name or "").strip())
    return (name.replace("..", "_").strip(". ") or "untitled")[:60]


def _image_urls(illust: dict) -> list:
    """取作品的所有原图 URL（多图用 meta_pages，单图用 meta_single_page）"""
    urls = []
    pages = _g(illust, "meta_pages") or []
    for p in pages:
        u = _g(_g(p, "image_urls"), "original")
        if u:
            urls.append(u)
    if not urls:
        single = _g(_g(illust, "meta_single_page"), "original_image_url")
        if single:
            urls.append(single)
    if not urls:
        u = _g(_g(illust, "image_urls"), "large")
        if u:
            urls.append(u)
    return urls


def _brief(illust: dict) -> dict:
    user = _g(illust, "user") or {}
    return {
        "id": str(_g(illust, "id", "")),
        "title": _g(illust, "title", ""),
        "type": _g(illust, "type", ""),
        "user": _g(user, "name", ""),
        "user_id": str(_g(user, "id", "")),
        "pages": int(_g(illust, "page_count", 0) or 0),
        "tags": [(_g(t, "name", "") if not isinstance(t, str) else t)
                 for t in (_g(illust, "tags") or [])],
        "create_date": _g(illust, "create_date", ""),
        "bookmarks": _g(illust, "total_bookmarks", 0),
        "views": _g(illust, "total_view", 0),
        "x_restrict": _g(illust, "x_restrict", 0),
        "url": f"https://www.pixiv.net/artworks/{_g(illust, 'id', '')}",
    }


def _download_dir(illust_id: str, title: str) -> Path:
    try:
        base = None
        from src.utils.config import get_config
        base = (get_config().tools.download_path or "").strip()
    except Exception:
        base = ""
    root = Path(base) if base else (PROJECT_ROOT / "data" / "downloads")
    d = root / _DOWNLOAD_SUBDIR / f"{illust_id}_{_safe_name(title)}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _parse_pages(spec: str, total: int) -> list:
    spec = (spec or "").strip()
    if not spec:
        return list(range(1, total + 1))
    out = []
    for part in re.split(r"[,，\s]+", spec):
        m = re.match(r"^(\d+)\s*[-~]\s*(\d+)$", part)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if a > b:
                a, b = b, a
            out.extend(range(a, b + 1))
        elif part.isdigit():
            out.append(int(part))
    seen, pages = set(), []
    for n in out:
        if 1 <= n <= total and n not in seen:
            seen.add(n)
            pages.append(n)
    return pages or list(range(1, total + 1))


# ── 各 action ──

def _fmt_list(items: list, max_desc: int = 60) -> str:
    lines = []
    for i, b in enumerate(items, 1):
        tags = "/".join(b["tags"][:5])
        lines.append(f"{i}. [{b['id']}] {b['title'][:max_desc]} —— {b['user']} "
                     f"({b['pages']}页, ♥{b['bookmarks']})"
                     + (f"  #{tags}" if tags else ""))
    return "\n".join(lines)


def _act_info(api, illust_id: str) -> dict:
    iid = _extract_id(illust_id)
    if not iid:
        return {"success": False, "error": "没解析出作品ID（给作品链接或纯数字ID）"}
    res = api.illust_detail(iid)
    illust = _g(res, "illust")
    if not illust:
        return {"success": False, "error": f"取不到作品 {iid}（可能已删除/需登录/被限流）"}
    b = _brief(illust)
    return {"success": True, "action": "info", **b,
            "text": (f"[P站作品 {b['id']}] {b['title']}\n"
                     f"作者: {b['user']} (id={b['user_id']})\n"
                     f"页数: {b['pages']}  收藏: {b['bookmarks']}  浏览: {b['views']}\n"
                     f"标签: {'/'.join(b['tags'][:8])}\n"
                     f"发布: {b['create_date']}\n链接: {b['url']}")}


def _act_search(api, keyword: str, limit: int) -> dict:
    if not keyword:
        return {"success": False, "error": "search 需要 keyword"}
    res = api.search_illust(keyword)
    illusts = _g(res, "illusts") or []
    items = [_brief(i) for i in illusts[:max(1, limit)]]
    return {"success": True, "action": "search", "keyword": keyword,
            "count": len(items), "items": items,
            "text": f"P站搜索「{keyword}」前 {len(items)} 条：\n" + _fmt_list(items)}


def _act_ranking(api, mode: str, limit: int) -> dict:
    mode = (mode or "day").strip()
    res = api.illust_ranking(mode=mode)
    illusts = _g(res, "illusts") or []
    items = [_brief(i) for i in illusts[:max(1, limit)]]
    return {"success": True, "action": "ranking", "mode": mode,
            "count": len(items), "items": items,
            "text": f"P站「{mode}」排行榜前 {len(items)} 条：\n" + _fmt_list(items)}


def _act_user(api, user_id: str, limit: int) -> dict:
    uid = _extract_id(user_id)
    if not uid:
        return {"success": False, "error": "user 需要 user_id 或画师主页链接"}
    res = api.user_illusts(uid)
    illusts = _g(res, "illusts") or []
    items = [_brief(i) for i in illusts[:max(1, limit)]]
    return {"success": True, "action": "user", "user_id": uid,
            "count": len(items), "items": items,
            "text": f"画师 {uid} 的作品前 {len(items)} 条：\n" + _fmt_list(items)}


def _act_download(api, illust_id: str, pages_spec: str) -> dict:
    iid = _extract_id(illust_id)
    if not iid:
        return {"success": False, "error": "没解析出作品ID"}
    res = api.illust_detail(iid)
    illust = _g(res, "illust")
    if not illust:
        return {"success": False, "error": f"取不到作品 {iid}"}
    b = _brief(illust)
    urls = _image_urls(illust)
    if not urls:
        return {"success": False, "error": f"作品 {iid} 没有可下载的图片地址"}
    sel = _parse_pages(pages_spec, len(urls))
    out_dir = _download_dir(iid, b["title"])
    files, failed = [], []
    bad_reason = ""
    for n in sel:
        url = urls[n - 1]
        ext = Path(url.split("?")[0]).suffix or ".jpg"
        name = f"{iid}_p{n}{ext}"
        dest = out_dir / name
        try:
            if dest.exists() and _looks_like_image(dest)[0]:
                files.append(str(dest))
                continue
            # ★ pixivpy 的 download() 不检查状态码，会把 403 的 HTML 直接写成
            #   .jpg。所以下完必须验一遍"是不是真图片"，假的删掉并记原因。
            api.download(url, path=str(out_dir), name=name)
            ok, why = _looks_like_image(dest)
            if ok:
                files.append(str(dest))
            else:
                failed.append(n)
                bad_reason = bad_reason or why
                try:
                    if dest.exists():
                        dest.unlink()
                except Exception:  # noqa: BLE001
                    pass
                _log.warning(f"[Pixiv] 第 {n} 页不是有效图片：{why}")
        except Exception as e:
            _log.warning(f"[Pixiv] 第 {n} 页下载失败: {e}")
            failed.append(n)
    if not files:
        hint = f"（{bad_reason}）" if bad_reason else ""
        return {"success": False,
                "error": f"下载失败（{len(sel)} 页都没成功，可能是网络/防盗链{hint}；"
                         f"国内建议配 PIXIV_PROXY）"}
    # 推给聊天窗口（最多 _MAX_PUSH 张，走统一附件通道）
    cb = _get_file_ready_callback()
    pushed = 0
    if cb:
        for fp in files[:_MAX_PUSH]:
            try:
                sz = Path(fp).stat().st_size
                cb(f"🖼 P站 · {b['title'][:20]}", fp,
                   f"{sz/1024:.0f} KB" if sz < 1024 * 1024 else f"{sz/1024/1024:.1f} MB")
                pushed += 1
            except Exception as e:
                _log.debug(f"[Pixiv] 推送图片失败: {e}")
    return {"success": True, "action": "download", **b,
            "downloaded": len(files), "failed_pages": failed,
            "files": files, "save_dir": str(out_dir),
            "pushed": pushed,
            "file_path": files[0],       # 给主窗口的通用分支兜底（同路径会去重）
            "message": f"🖼 P站下载完成 · {b['title'][:20]}（{len(files)} 张）",
            "text": (f"[P站作品 {b['id']}] {b['title']} —— {b['user']}\n"
                     f"已下载 {len(files)}/{len(urls)} 张到 {out_dir}"
                     + (f"（失败页: {failed}）" if failed else ""))}


def execute(action: str = "", **kwargs) -> str:
    """工具入口（同步）"""
    action = (action or "").strip().lower()
    limit = int(kwargs.get("limit") or 10)
    if action not in ("info", "search", "ranking", "user", "download"):
        return json.dumps({"success": False,
                           "error": f"未知 action: {action or '(空)'}。"
                                    f"支持 info/search/ranking/user/download"},
                          ensure_ascii=False)
    api, err = _build_api()
    if api is None:
        return json.dumps({"success": False, "error": err}, ensure_ascii=False)
    try:
        if action == "info":
            r = _act_info(api, kwargs.get("illust_id") or kwargs.get("path") or "")
        elif action == "search":
            r = _act_search(api, kwargs.get("keyword") or "", limit)
        elif action == "ranking":
            r = _act_ranking(api, kwargs.get("mode") or "day", limit)
        elif action == "user":
            r = _act_user(api, kwargs.get("user_id") or "", limit)
        else:
            r = _act_download(api, kwargs.get("illust_id") or "", kwargs.get("pages") or "")
    except Exception as e:
        _log.error(f"[Pixiv] {action} 失败: {e}", exc_info=True)
        # ★ 1.9.1：网络/SSL 类失败时带上自救提示 —— 真机（17:28 日志）直传
        #   pixiv.search 时代理隧道被掐，聊天里只甩了一屏 SSLEOF 堆栈，
        #   用户不知道要检查代理。堆栈截短 + 给出两条出路。
        msg = str(e)
        net_kw = ("SSL", "SSLError", "EOF", "Max retries", "Connection",
                  "timed out", "timeout", "reset", "ProxyError")
        if any(k.lower() in msg.lower() for k in net_kw):
            short = msg.split("Caused by")[-1].strip()[:160] or msg[:160]
            r = {"success": False,
                 "error": f"{action} 失败（网络）: {short}",
                 "hint": ("P站接口连不上/被掐。检查 .env 里的 PIXIV_PROXY 代理"
                          "是否在跑（当前: {p}）；或改用 PIXIV_BYPASS=1 走 SNI "
                          "绕过（需 pip install requests-toolbelt）。"
                          ).format(p=_proxy() or "未配置")}
        else:
            r = {"success": False, "error": f"{action} 失败: {e}"}
    return json.dumps(r, ensure_ascii=False, indent=2)


if __name__ == "__main__":   # 手动排查用
    logging.basicConfig(level=logging.INFO)
    import sys as _s
    if len(_s.argv) > 1:
        act = _s.argv[1]
        kw = {}
        for a in _s.argv[2:]:
            if "=" in a:
                k, v = a.split("=", 1)
                kw[k] = v
        print(execute(act, **kw))
    else:
        print("用法: python tools/pixiv_tool.py search keyword=风景 limit=3")
