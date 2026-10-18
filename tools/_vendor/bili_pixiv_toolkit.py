# -*- coding: utf-8 -*-
"""
B站 + Pixiv 一体化爬虫工具（单文件）

【直接运行 - 交互菜单】
    py bili_pixiv_toolkit.py

【作为模块导入 - Agent 调用】
    from bili_pixiv_toolkit import (
        # B站 Cookie
        bili_login, bili_export,
        # B站爬虫
        bili_search, bili_detail, bili_comments,
        bili_download_cover, bili_download_video,
        # Pixiv Token
        pixiv_login,
        # Pixiv 爬虫
        pixiv_search, pixiv_detail, pixiv_download,
    )

    # B站
    bili_login()                                     # 首次：打开浏览器登录
    videos = bili_search("明日方舟", limit=5)         # 搜索
    info   = bili_detail(videos[0]["bvid"])          # 详情（含点赞）
    comms  = bili_comments(videos[0]["bvid"], 10)    # 评论
    bili_download_cover(videos[0]["bvid"])           # 下载封面
    bili_download_video(videos[0]["bvid"])           # 下载视频

    # Pixiv
    pixiv_login()                                    # 首次：获取 refresh_token
    results = pixiv_search("初音ミク", limit=5)
    info    = pixiv_detail(results[0]["id"])
    pixiv_download(results[0]["id"])

【依赖安装】
    py -m pip install playwright requests pixivpy3 gppt
"""
from __future__ import annotations

import json
import hashlib
import re
import sys
import time
import urllib.parse
from pathlib import Path

# ============================== 依赖检查 ==============================
try:
    import requests
except ImportError:
    requests = None

try:
    from pixivpy3 import AppPixivAPI
except ImportError:
    AppPixivAPI = None

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None


# ============================== 全局配置 ==============================
BASE = Path(__file__).resolve().parent

# B站
BILI_COOKIE_FILE = BASE / "bili_cookies.json"
BILI_STATE_FILE = BASE / "bili_state.json"
BILI_PROFILE = BASE / "edge_bili_profile"
BILI_DOWNLOAD_DIR = BASE / "bili_downloads"

# Pixiv
PIXIV_TOKEN_FILE = Path.home() / ".config" / "gppt" / "default.token.json"
PIXIV_DOWNLOAD_DIR = BASE / "pixiv_downloads"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0"
)
BILI_HEADERS = {
    "User-Agent": UA,
    "Referer": "https://www.bilibili.com/",
    "Origin": "https://www.bilibili.com",
}

MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
]


# ============================== 日志 ==============================
def log(msg):
    print(f"[*] {msg}")


def err(msg):
    print(f"[!] {msg}")


# ============================== B站：Cookie 获取 ==============================
def _run_browser(headless: bool, wait_login: bool):
    """通用：启动 Edge，可选等待用户登录，然后导出 B站 cookie。"""
    if sync_playwright is None:
        err("未安装 playwright，请运行：py -m pip install playwright")
        return None

    with sync_playwright() as p:
        try:
            context = p.chromium.launch_persistent_context(
                user_data_dir=str(BILI_PROFILE),
                channel="msedge",
                headless=headless,
                viewport={"width": 1280, "height": 900},
                locale="zh-CN",
            )
        except Exception as e:
            err(f"启动浏览器失败: {e}")
            return None

        try:
            page = context.new_page()
            try:
                page.goto(
                    "https://www.bilibili.com",
                    wait_until="domcontentloaded",
                    timeout=60000,
                )
            except Exception as e:
                err(f"打开 B站失败: {e}")

            if wait_login:
                print("请在弹出的 Edge 里登录 B站，登录完成后回终端按回车...")
                input()

            cookies = context.cookies("https://www.bilibili.com")
            data = {
                c["name"]: c["value"]
                for c in cookies
                if "bilibili.com" in c["domain"]
            }
            BILI_COOKIE_FILE.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            context.storage_state(path=str(BILI_STATE_FILE))
            log(f"B站 cookie 已保存: {BILI_COOKIE_FILE}")
            for k in ["SESSDATA", "bili_jct", "DedeUserID"]:
                print(f"  {k}: {'有' if data.get(k) else '无'}")
            return data
        finally:
            try:
                context.close()
            except Exception:
                pass


def bili_login():
    """打开 Edge 让用户登录 B站并保存 cookie。首次使用。"""
    return _run_browser(headless=False, wait_login=True)


def bili_export():
    """无头模式导出当前 profile 中的 B站 cookie。已登录后用。"""
    return _run_browser(headless=True, wait_login=False)


# ============================== B站：会话与 WBI 签名 ==============================
_bili_session = None
_bili_wbi = {"img_key": None, "sub_key": None}


def _get_bili_session():
    global _bili_session
    if _bili_session is None:
        if requests is None:
            raise RuntimeError("未安装 requests，请运行：py -m pip install requests")
        _bili_session = requests.Session()
        _bili_session.headers.update(BILI_HEADERS)
        if BILI_COOKIE_FILE.exists():
            try:
                cookies = json.loads(BILI_COOKIE_FILE.read_text(encoding="utf-8"))
                for k, v in cookies.items():
                    _bili_session.cookies.set(k, v, domain=".bilibili.com")
                log(f"已加载 {len(cookies)} 个 B站 cookie")
            except Exception as e:
                err(f"加载 B站 cookie 失败: {e}")
        else:
            err(f"未找到 {BILI_COOKIE_FILE}，请先调用 bili_login()")
    return _bili_session


def _bili_refresh_wbi():
    try:
        session = _get_bili_session()
        r = session.get(
            "https://api.bilibili.com/x/web-interface/nav", timeout=10
        )
        data = r.json()
        wbi = data["data"]["wbi_img"]
        _bili_wbi["img_key"] = wbi["img_url"].rsplit("/", 1)[-1].split(".")[0]
        _bili_wbi["sub_key"] = wbi["sub_url"].rsplit("/", 1)[-1].split(".")[0]
        return True
    except Exception as e:
        err(f"刷新 WBI key 失败: {e}")
        return False


def _bili_mixin_key():
    orig = _bili_wbi["img_key"] + _bili_wbi["sub_key"]
    return "".join(orig[i] for i in MIXIN_KEY_ENC_TAB)[:32]


def _bili_sign(params):
    mixin_key = _bili_mixin_key()
    params = dict(params)
    params["wts"] = round(time.time())
    params = dict(sorted(params.items()))
    params = {
        k: "".join(filter(lambda ch: ch not in "!'()*", str(v)))
        for k, v in params.items()
    }
    query = urllib.parse.urlencode(params)
    params["w_rid"] = hashlib.md5((query + mixin_key).encode()).hexdigest()
    return params


def _bili_request(url, params=None, wbi=False, timeout=10):
    session = _get_bili_session()
    if params is None:
        params = {}
    if wbi:
        if not _bili_wbi["img_key"]:
            _bili_refresh_wbi()
        params = _bili_sign(params)
    r = session.get(url, params=params, timeout=timeout)
    return r.json()


# ============================== B站：爬虫 ==============================
def bili_search(keyword, limit=10, page=1):
    """搜索 B站视频。
    Returns: list[dict(bvid,title,description,author,play,duration)]
    """
    try:
        data = _bili_request(
            "https://api.bilibili.com/x/web-interface/wbi/search/type",
            params={
                "search_type": "video",
                "keyword": keyword,
                "page": page,
                "page_size": limit,
            },
            wbi=True,
        )
    except Exception as e:
        err(f"搜索失败: {e}")
        return []
    if data.get("code") != 0:
        err(f"搜索接口错误: {data.get('code')} {data.get('message')}")
        return []

    result = data.get("data", {}).get("result", []) or []
    videos = []
    for item in result:
        try:
            videos.append({
                "bvid": item.get("bvid", ""),
                "title": re.sub(r"<.*?>", "", item.get("title", "")),
                "description": item.get("description", ""),
                "author": item.get("author", ""),
                "play": item.get("play", 0),
                "duration": item.get("duration", ""),
            })
        except Exception as e:
            err(f"解析搜索项失败: {e}")
    return videos


def bili_detail(bvid):
    """获取 B站视频详情（含点赞、投币、收藏、播放等）"""
    try:
        data = _bili_request(
            "https://api.bilibili.com/x/web-interface/view",
            params={"bvid": bvid},
        )
    except Exception as e:
        err(f"获取视频详情失败 {bvid}: {e}")
        return None
    if data.get("code") != 0:
        err(f"详情接口错误: {data.get('code')} {data.get('message')}")
        return None

    d = data["data"]
    stat = d.get("stat", {})
    return {
        "bvid": d.get("bvid"),
        "aid": d.get("aid"),
        "cid": d.get("cid"),
        "title": d.get("title"),
        "desc": d.get("desc"),
        "pic": d.get("pic"),
        "duration": d.get("duration"),
        "owner": d.get("owner", {}).get("name"),
        "stat": {
            "view": stat.get("view"),
            "like": stat.get("like"),
            "coin": stat.get("coin"),
            "favorite": stat.get("favorite"),
            "danmaku": stat.get("danmaku"),
            "reply": stat.get("reply"),
        },
    }


def bili_comments(bvid, count=10):
    """获取 B站视频的部分评论。"""
    info = bili_detail(bvid)
    if not info:
        return []
    aid = info.get("aid")
    if not aid:
        err(f"未获取到 aid: {bvid}")
        return []

    try:
        data = _bili_request(
            "https://api.bilibili.com/x/v2/reply/wbi/main",
            params={"type": 1, "oid": aid, "mode": 3, "next": 0, "ps": count},
            wbi=True,
        )
    except Exception as e:
        err(f"获取评论失败: {e}")
        return []
    if data.get("code") != 0:
        err(f"评论接口错误: {data.get('code')} {data.get('message')}")
        return []

    replies = data.get("data", {}).get("replies") or []
    comments = []
    for r in replies[:count]:
        try:
            comments.append({
                "user": r.get("member", {}).get("uname"),
                "content": r.get("content", {}).get("message"),
                "like": r.get("like"),
                "rcount": r.get("rcount"),
            })
        except Exception as e:
            err(f"解析评论失败: {e}")
    return comments


def bili_download_cover(bvid):
    """下载 B站视频封面（高清）"""
    info = bili_detail(bvid)
    if not info:
        return None
    url = info.get("pic")
    if not url:
        err(f"无封面 URL: {bvid}")
        return None
    try:
        BILI_DOWNLOAD_DIR.mkdir(exist_ok=True)
        path = BILI_DOWNLOAD_DIR / f"{bvid}_cover.jpg"
        session = _get_bili_session()
        r = session.get(url, timeout=20, stream=True)
        r.raise_for_status()
        with open(path, "wb") as f:
            for chunk in r.iter_content(8192):
                f.write(chunk)
        log(f"封面已下载: {path}")
        return str(path)
    except Exception as e:
        err(f"下载封面失败: {e}")
        return None


def bili_download_video(bvid):
    """下载 B站视频（DASH 视频流 + 音频流，需要 ffmpeg 合并）"""
    info = bili_detail(bvid)
    if not info:
        return None
    cid = info.get("cid")
    if not cid:
        err(f"未获取到 cid: {bvid}")
        return None

    try:
        data = _bili_request(
            "https://api.bilibili.com/x/player/playurl",
            params={
                "bvid": bvid,
                "cid": cid,
                "qn": 120,
                "fnval": 4048,
                "fourk": 1,
            },
        )
    except Exception as e:
        err(f"获取播放地址失败: {e}")
        return None
    if data.get("code") != 0:
        err(f"playurl 接口错误: {data.get('code')} {data.get('message')}")
        return None

    dash = data.get("data", {}).get("dash")
    if not dash:
        err("未返回 DASH 数据（可能需要大会员或重新登录）")
        return None

    BILI_DOWNLOAD_DIR.mkdir(exist_ok=True)
    video_path = BILI_DOWNLOAD_DIR / f"{bvid}_video.m4s"
    audio_path = BILI_DOWNLOAD_DIR / f"{bvid}_audio.m4s"

    video_url = None
    try:
        videos = sorted(dash.get("video", []),
                        key=lambda x: x.get("id", 0), reverse=True)
        if videos:
            video_url = videos[0]["baseUrl"]
            log(f"选择视频清晰度 id={videos[0]['id']}")
    except Exception as e:
        err(f"解析视频流失败: {e}")

    audio_url = None
    try:
        audios = sorted(dash.get("audio", []),
                        key=lambda x: x.get("id", 0), reverse=True)
        if audios:
            audio_url = audios[0]["baseUrl"]
    except Exception as e:
        err(f"解析音频流失败: {e}")

    session = _get_bili_session()

    def _dl(url, path, label):
        if not url:
            err(f"{label} 无地址")
            return None
        try:
            r = session.get(
                url, timeout=120, stream=True,
                headers={"User-Agent": UA,
                         "Referer": "https://www.bilibili.com/"},
            )
            r.raise_for_status()
            total = 0
            with open(path, "wb") as f:
                for chunk in r.iter_content(65536):
                    f.write(chunk)
                    total += len(chunk)
            log(f"{label} 已下载: {path} ({total/1024/1024:.2f} MB)")
            return str(path)
        except Exception as e:
            err(f"{label} 下载失败: {e}")
            return None

    v = _dl(video_url, video_path, "视频流")
    a = _dl(audio_url, audio_path, "音频流")

    if v and a:
        merged = BILI_DOWNLOAD_DIR / f"{bvid}.mp4"
        log(f"合并命令: ffmpeg -i \"{v}\" -i \"{a}\" -c copy \"{merged}\"")
    return (v, a)


# ============================== Pixiv：Token ==============================
def _load_pixiv_token():
    if not PIXIV_TOKEN_FILE.exists():
        return None
    try:
        data = json.loads(PIXIV_TOKEN_FILE.read_text(encoding="utf-8"))
        return data.get("refresh_token")
    except Exception as e:
        err(f"读取 Pixiv token 失败: {e}")
        return None


def pixiv_login():
    """通过 gppt 获取 Pixiv refresh_token。
    优先调用 gppt Python API，失败则从已有文件读取。
    """
    try:
        from gppt import GetPixivToken
    except ImportError:
        err("未安装 gppt，请运行：py -m pip install gppt")
        return _load_pixiv_token()

    # 方式1：gppt Python API
    try:
        g = GetPixivToken(headless=False)
        res = g.login()
        if res and res.get("refresh_token"):
            log("Pixiv 登录成功")
            print(f"  refresh_token: {res['refresh_token'][:20]}...")
            return res["refresh_token"]
    except TypeError:
        # 兼容不同 gppt 版本
        try:
            g = GetPixivToken()
            res = g.login()
            if res and res.get("refresh_token"):
                log("Pixiv 登录成功")
                return res["refresh_token"]
        except Exception as e:
            err(f"gppt 登录失败: {e}")
    except Exception as e:
        err(f"gppt 登录失败: {e}")

    # 方式2：兜底读文件
    token = _load_pixiv_token()
    if token:
        log("已从现有文件读取 refresh_token")
        return token

    err("Pixiv 登录失败，请手动运行: gppt login")
    return None


# ============================== Pixiv：爬虫 ==============================
_pixiv_api = None


def _get_pixiv_api():
    global _pixiv_api
    if _pixiv_api is None:
        if AppPixivAPI is None:
            raise RuntimeError("未安装 pixivpy3，请运行：py -m pip install pixivpy3")
        token = _load_pixiv_token()
        if not token:
            raise RuntimeError("未找到 Pixiv refresh_token，请先调用 pixiv_login()")
        _pixiv_api = AppPixivAPI()
        _pixiv_api.auth(refresh_token=token)
        log("Pixiv API 认证成功")
    return _pixiv_api


def pixiv_search(keyword, limit=5, sort="date_desc"):
    """搜索 Pixiv 插画。
    Args:
        keyword: 关键词
        limit:   返回数量
        sort:    date_desc / date_asc / popular_desc
    Returns:
        list[dict(id,title,author,author_id,tags,url)]
    """
    try:
        api = _get_pixiv_api()
        res = api.search_illust(
            keyword,
            search_target="partial_match_for_tags",
            sort=sort,
        )
    except Exception as e:
        err(f"Pixiv 搜索失败: {e}")
        return []

    results = []
    for illust in res.illusts[:limit]:
        try:
            results.append({
                "id": illust.id,
                "title": illust.title,
                "author": illust.user.name,
                "author_id": illust.user.id,
                "tags": [t.name for t in illust.tags],
                "url": f"https://www.pixiv.net/artworks/{illust.id}",
            })
        except Exception as e:
            err(f"解析插画失败: {e}")
    return results


def pixiv_detail(illust_id):
    """获取 Pixiv 插画详情"""
    try:
        api = _get_pixiv_api()
        res = api.illust_detail(illust_id)
        illust = res.illust
        return {
            "id": illust.id,
            "title": illust.title,
            "author": illust.user.name,
            "view": illust.total_view,
            "like": illust.total_bookmarks,
            "comment": illust.total_comments,
            "page_count": illust.page_count,
            "url": f"https://www.pixiv.net/artworks/{illust.id}",
        }
    except Exception as e:
        err(f"获取 Pixiv 详情失败 {illust_id}: {e}")
        return None


def _pixiv_download_one(url, filename):
    try:
        api = _get_pixiv_api()
        path = PIXIV_DOWNLOAD_DIR / filename
        api.download(url, fname=str(path))
        log(f"已保存: {path}")
        return str(path)
    except Exception as e:
        err(f"下载图片失败 {url}: {e}")
        return None


def pixiv_download(illust_id):
    """下载 Pixiv 插画原图（支持多页）。返回已保存的文件路径列表。"""
    try:
        api = _get_pixiv_api()
        res = api.illust_detail(illust_id)
        illust = res.illust
    except Exception as e:
        err(f"获取 Pixiv 详情失败 {illust_id}: {e}")
        return []

    PIXIV_DOWNLOAD_DIR.mkdir(exist_ok=True)
    page_count = illust.page_count
    title = illust.title
    log(f"开始下载: {title} (共 {page_count} 页)")

    saved = []
    try:
        if page_count > 1 and illust.meta_pages:
            for i, page in enumerate(illust.meta_pages):
                url = page.image_urls.original
                filename = f"{illust_id}_p{i}.jpg"
                p = _pixiv_download_one(url, filename)
                if p:
                    saved.append(p)
                time.sleep(1)
        else:
            url = illust.meta_single_page.original_image_url
            filename = f"{illust_id}_p0.jpg"
            p = _pixiv_download_one(url, filename)
            if p:
                saved.append(p)
    except Exception as e:
        err(f"下载过程异常: {e}")

    return saved


# ============================== 打印辅助 ==============================
def _print_bili_search(videos):
    if not videos:
        err("无结果")
        return
    print("\n" + "=" * 60)
    print(f"搜索结果（{len(videos)} 条）")
    print("=" * 60)
    for i, v in enumerate(videos, 1):
        print(f"\n[{i}] BV号: {v['bvid']}")
        print(f"    标题: {v['title']}")
        print(f"    UP  : {v['author']}  播放: {v['play']}  时长: {v['duration']}")
        desc = v["description"]
        print(f"    简介: {desc[:100]}{'...' if len(desc) > 100 else ''}")


def _print_bili_detail(info):
    print("\n" + "=" * 60)
    print("视频详情")
    print("=" * 60)
    print(f"  标题  : {info['title']}")
    print(f"  UP    : {info['owner']}")
    print(f"  时长  : {info['duration']} 秒")
    print(f"  aid   : {info['aid']}   cid: {info['cid']}")
    s = info["stat"]
    print(f"  播放  : {s['view']}")
    print(f"  点赞  : {s['like']}")
    print(f"  投币  : {s['coin']}")
    print(f"  收藏  : {s['favorite']}")
    print(f"  弹幕  : {s['danmaku']}")
    print(f"  评论  : {s['reply']}")


def _print_bili_comments(comments):
    if not comments:
        err("无评论或获取失败")
        return
    print("\n" + "=" * 60)
    print(f"部分评论（{len(comments)} 条）")
    print("=" * 60)
    for i, c in enumerate(comments, 1):
        print(f"\n[{i}] {c['user']}  赞 {c['like']}  回复 {c['rcount']}")
        content = c["content"] or ""
        print(f"    {content[:200]}{'...' if len(content) > 200 else ''}")


def _print_pixiv_search(results):
    if not results:
        err("无结果")
        return
    print("\n" + "=" * 60)
    print(f"搜索结果（{len(results)} 条）")
    print("=" * 60)
    for i, r in enumerate(results, 1):
        tags = ", ".join(r["tags"][:5]) if r["tags"] else "-"
        print(f"\n[{i}] ID: {r['id']}")
        print(f"    标题: {r['title']}")
        print(f"    作者: {r['author']} (uid={r['author_id']})")
        print(f"    标签: {tags}")
        print(f"    链接: {r['url']}")


def _print_pixiv_detail(detail):
    if not detail:
        return
    print("\n" + "=" * 60)
    print("插画详情")
    print("=" * 60)
    print(f"  标题  : {detail['title']}")
    print(f"  作者  : {detail['author']}")
    print(f"  观看  : {detail['view']}")
    print(f"  收藏  : {detail['like']}")
    print(f"  评论  : {detail['comment']}")
    print(f"  页数  : {detail['page_count']}")
    print(f"  链接  : {detail['url']}")


# ============================== 交互菜单 ==============================
def _interactive_menu():
    print("=" * 60)
    print("B站 + Pixiv 一体化工具")
    print("=" * 60)

    while True:
        print("\n[菜单]")
        print("  B站")
        print("    1. 登录 B站（打开浏览器，首次用）")
        print("    2. 无头导出 B站 Cookie（已登录后用）")
        print("    3. 搜索 B站视频")
        print("    4. 查看视频详情 + 评论")
        print("    5. 下载视频封面")
        print("    6. 下载视频（DASH）")
        print("  Pixiv")
        print("    7. 登录 Pixiv（获取 refresh_token）")
        print("    8. 搜索 Pixiv 插画")
        print("    9. 查看插画详情")
        print("   10. 下载插画原图")
        print("    0. 退出")

        choice = input("\n请选择: ").strip()

        try:
            if choice == "0":
                break
            elif choice == "1":
                bili_login()
            elif choice == "2":
                bili_export()
            elif choice == "3":
                kw = input("关键词: ").strip()
                if kw:
                    _print_bili_search(bili_search(kw, limit=10))
            elif choice == "4":
                bvid = input("BV号: ").strip()
                if bvid:
                    info = bili_detail(bvid)
                    if info:
                        _print_bili_detail(info)
                        _print_bili_comments(bili_comments(bvid, count=10))
            elif choice == "5":
                bvid = input("BV号: ").strip()
                if bvid:
                    bili_download_cover(bvid)
            elif choice == "6":
                bvid = input("BV号: ").strip()
                if bvid:
                    bili_download_video(bvid)
            elif choice == "7":
                pixiv_login()
            elif choice == "8":
                kw = input("关键词: ").strip()
                if kw:
                    _print_pixiv_search(pixiv_search(kw, limit=5))
            elif choice == "9":
                s = input("插画 ID: ").strip()
                if s.isdigit():
                    _print_pixiv_detail(pixiv_detail(int(s)))
                else:
                    err("请输入数字 ID")
            elif choice == "10":
                s = input("插画 ID: ").strip()
                if s.isdigit():
                    pixiv_download(int(s))
                else:
                    err("请输入数字 ID")
            else:
                print("无效选择")
        except KeyboardInterrupt:
            print("\n已取消")
        except Exception as e:
            err(f"执行出错: {e}")


# ============================== 入口 ==============================
if __name__ == "__main__":
    _interactive_menu()