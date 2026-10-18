"""
哔哩哔哩 (B站) 视频信息查询与下载工具

v1.3.2: 直调 PlayURL API 获取 CDN 直链 → aiohttp 下载 → ffmpeg 合并。
        支持 DASH（音视频分离）/ durl（合并流）双格式。
        yt-dlp 仅作回退方案。
        移植自 DownKyi 风格的 B站 API 直链方案。
"""
import json
import hashlib
import re
import time
import subprocess
import asyncio
import logging
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode, urlparse, unquote

_log = logging.getLogger("Tool.Bilibili")

# ★ 1.8.10: B 站 CDN 镜像主机 —— 下载失败时按顺序轮换重试。
#   同一份路径在不同镜像主机上通常都能取到。
_CDN_MIRRORS = (
    "upos-sz-mirrorcos",
    "upos-sz-mirrorali",
    "upos-sz-mirrorhw",
    "upos-sz-mirror08c",
)

TOOL_NAME = "bilibili_video"
TOOL_DESCRIPTION = (
    "B站视频信息查询与下载工具。当用户分享B站链接、BV号、b23.tv短链接时使用。"
    "支持: info(查询视频信息)、cover(下载封面到本地并显示在聊天窗口)、"
    "download(下载视频)。"
    # ★★★ 1.9.4（用户 2026-10-18 真机反馈，附截图）：
    #   他问"开通B站功能"，模型回了"绑定账号、开通大会员之类的账号权限
    #   那铃兰这边做不到" —— **这是错的**。1.9.4 已经做了
    #   **设置 → 账号开通 → 一键开通（登录一次）**，cookie 会被存好，
    #   本工具之后就会带着 cookie 工作。
    #   ⇒ 模型之所以说"做不到"，是因为**描述里根本没写这件事**。
    #     所以必须在这里写明，并明确禁止它再说"做不到"。
    "★ 需要登录才有权限的内容（大会员清晰度、收藏/关注、个人相关、"
    "部分受限视频等）：**不要说做不到** —— cookie 是可以配的。"
    "让用户打开「桌宠右键 → 设置 → 账号开通」，点「一键开通（登录一次）」，"
    "在弹出的浏览器里登录一次 B站就好；配好后本工具会自动带 cookie 工作。"
)
TOOL_PARAMETERS = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["info", "cover", "download"],
            "description": ("info=查询视频信息, cover=下载封面（会自动显示在聊天窗口）, "
                            "download=下载视频"),
            "default": "info",
        },
        "bv_id": {
            "type": "string",
            "description": "BV号(不含BV前缀)，如 1k24y1B7Ef",
        },
        "raw_message": {
            "type": "string",
            "description": "原始消息文本，用于自动提取BV号/短链接",
        },
    },
    "required": [],
}

# ==================== 路径 ====================
PROJECT_ROOT = Path(__file__).resolve().parent.parent
COOKIES_FILE = PROJECT_ROOT / "data" / "cookies" / "cookies.txt"

# 下载目录（懒加载）
_download_dir: Optional[Path] = None

# ==================== WbiSign 签名算法（移植自 DownKyi） ====================
_MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
BVID_RE = re.compile(r'BV[a-zA-Z0-9]{10}', re.IGNORECASE)
BVID_STRICT = re.compile(r'BV[a-zA-Z0-9]{10}', re.IGNORECASE)
SHORT_URL_RE = re.compile(r'b23\.tv/([a-zA-Z0-9]+)')

# Wbi 密钥缓存
_wbi_img_key: Optional[str] = None
_wbi_sub_key: Optional[str] = None
_wbi_key_time: float = 0.0
_WBI_KEY_TTL = 3600.0  # 1小时

# Cookie 缓存
_cookie_str: Optional[str] = None


# ==================== 配置 & Cookie ====================

def _get_download_dir() -> Path:
    """获取下载目录，优先使用配置，默认 data/downloads/bilibili"""
    global _download_dir
    if _download_dir is not None:
        return _download_dir
    try:
        from src.utils.config import get_config
        cfg = get_config()
        base = cfg.tools.download_path
        d = Path(base) if base else PROJECT_ROOT / "data" / "downloads"
    except Exception:
        d = PROJECT_ROOT / "data" / "downloads"
    _download_dir = d / "bilibili"
    _download_dir.mkdir(parents=True, exist_ok=True)
    return _download_dir


def _get_cookie_string() -> str:
    """从 cookies 文件载入 Cookie（支持 Netscape/JSON 格式），拼接为请求头"""
    global _cookie_str
    if _cookie_str is not None:
        return _cookie_str

    def _parse_netscape(path: Path) -> list[str]:
        result = []
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) >= 7:
                    name, value = parts[5], parts[6]
                    if name and value:
                        result.append(f"{name}={value}")
        return result

    def _parse_json(path: Path) -> list[str]:
        result = []
        data = json.loads(path.read_text(encoding="utf-8"))
        # 支持 {"url":"...","cookies":[...]} 和 [...] 两种格式
        items = data.get("cookies", []) if isinstance(data, dict) else data
        for c in items:
            name = c.get("name", "")
            value = c.get("value", "")
            # 跳过过期 Cookie
            exp = c.get("expirationDate")
            if exp and time.time() > exp:
                continue
            if name and value:
                result.append(f"{name}={value}")
        return result

    cookies = []
    try:
        # 优先搜索 JSON 格式 Cookie（含完整登录态）
        _cookie_dir = PROJECT_ROOT / "data" / "cookies"
        json_files = sorted(
            [f for f in _cookie_dir.iterdir()
             if f.suffix.lower() == ".json"
             and ("bilibili" in f.name.lower() or f.name == "exported-cookies.json")],
            # www.bilibili.com 开头的优先，因为它可能从 EditThisCookie 导出（含 SESSDATA）
            key=lambda p: (0 if p.name.startswith("www.bilibili.com") else 1, p.name),
        ) if _cookie_dir.exists() else []
        for jf in json_files:
            parsed = _parse_json(jf)
            if parsed:
                has = any(c.startswith("SESSDATA=") for c in parsed)
                _log.info(f"从 JSON 文件加载 Cookie: {jf.name} ({len(parsed)} 条{'，含 SESSDATA' if has else ''})")
                cookies = parsed
                if has:
                    break  # 找到含 SESSDATA 的就不继续

        if not cookies and COOKIES_FILE.exists():
            cookies = _parse_netscape(COOKIES_FILE)
            _log.info(f"从 Netscape 文件加载 Cookie: cookies.txt ({len(cookies)} 条)")
    except Exception as e:
        _log.warning(f"加载 Cookie 文件失败: {e}")

    _cookie_str = "; ".join(cookies) if cookies else ""
    has_sess = any(c.startswith("SESSDATA=") for c in cookies)
    if _cookie_str and not has_sess:
        _log.warning("Cookie 中未找到 SESSDATA，B站可能只能获取低画质")
    return _cookie_str


# ==================== WbiSign ====================

def _get_mixin_key(origin: str) -> str:
    return "".join(origin[i] for i in _MIXIN_KEY_ENC_TAB)[:32]


async def _fetch_wbi_keys() -> tuple:
    """获取 WbiSign 密钥（带1h缓存）"""
    global _wbi_img_key, _wbi_sub_key, _wbi_key_time
    import aiohttp
    now = time.time()
    if _wbi_img_key and _wbi_sub_key and (now - _wbi_key_time) < _WBI_KEY_TTL:
        return _wbi_img_key, _wbi_sub_key
    try:
        headers = {"User-Agent": USER_AGENT, "Referer": "https://www.bilibili.com"}
        async with aiohttp.ClientSession() as s:
            async with s.get("https://api.bilibili.com/x/web-interface/nav",
                             headers=headers) as resp:
                data = await resp.json()
        wbi_img = data.get("data", {}).get("wbi_img", {})
        img_url = wbi_img.get("img_url", "")
        sub_url = wbi_img.get("sub_url", "")
        if img_url and sub_url:
            import os.path as _osp
            _wbi_img_key = _osp.splitext(img_url.split("/")[-1])[0]
            _wbi_sub_key = _osp.splitext(sub_url.split("/")[-1])[0]
            _wbi_key_time = now
            return _wbi_img_key, _wbi_sub_key
    except Exception as e:
        _log.debug(f"[WbiSign] 获取密钥失败: {e}")
    return "", ""


async def _wbi_sign_params(params: dict) -> dict:
    """对参数进行 WbiSign 签名，失败时只加 wts 降级"""
    img_key, sub_key = await _fetch_wbi_keys()
    if not img_key or not sub_key:
        result = {k: str(v) for k, v in params.items() if v is not None}
        result["wts"] = str(int(time.time()))
        return result
    mixin_key = _get_mixin_key(img_key + sub_key)
    result = {k: str(v) for k, v in params.items() if v is not None}
    result["wts"] = str(int(time.time()))
    result = dict(sorted(result.items(), key=lambda x: x[0]))
    # 过滤特殊字符
    result = {k: v.translate(str.maketrans("", "", "!'()*")) for k, v in result.items()}
    query = urlencode(result)
    result["w_rid"] = hashlib.md5((query + mixin_key).encode()).hexdigest()
    return result


# ==================== BV号提取 ====================

def _extract_bvid(text: str) -> Optional[str]:
    """从文本中提取 BV 号"""
    if not text:
        return None
    m = BVID_RE.search(text)
    if m:
        return m.group(0)
    return None


# ==================== API 调用（异步） ====================

async def _get_video_info(bvid: str) -> dict:
    """获取视频信息（WbiSign 新接口 → 旧接口回退）"""
    import aiohttp
    pure = bvid[2:] if bvid.upper().startswith("BV") else bvid

    async def _try_wbi():
        params = await _wbi_sign_params({"bvid": f"BV{pure}"})
        url = f"https://api.bilibili.com/x/web-interface/wbi/view?{urlencode(params)}"
        headers = {"User-Agent": USER_AGENT, "Referer": "https://www.bilibili.com",
                   "Origin": "https://www.bilibili.com"}
        async with aiohttp.ClientSession() as s:
            async with s.get(url, headers=headers) as resp:
                if resp.status == 200:
                    return await resp.json()
        return None

    async def _try_old():
        url = f"https://api.bilibili.com/x/web-interface/view?bvid=BV{pure}"
        headers = {"User-Agent": USER_AGENT, "Referer": "https://www.bilibili.com"}
        async with aiohttp.ClientSession() as s:
            async with s.get(url, headers=headers) as resp:
                if resp.status == 200:
                    return await resp.json()
        return None

    try:
        data = await _try_wbi()
        if data is None:
            data = await _try_old()
        if data and data.get("code") == 0:
            v = data["data"]
            stat = v.get("stat", {})
            owner = v.get("owner", {})
            dur = v.get("duration", 0)
            desc = (v.get("desc") or "").strip()[:500]
            return {
                "success": True,
                "title": v.get("title", ""),
                "author": owner.get("name", ""),
                "description": desc,
                "cover_url": v.get("pic", ""),
                "duration": f"{dur // 60}:{dur % 60:02d}",
                "view": stat.get("view", 0),
                "like": stat.get("like", 0),
                "coin": stat.get("coin", 0),
                "favorite": stat.get("favorite", 0),
                "share": stat.get("share", 0),
                "comment": stat.get("reply", 0),
                "danmaku": stat.get("danmaku", 0),
                "pubdate": time.strftime("%Y-%m-%d", time.localtime(v.get("pubdate", 0))),
                "bvid": f"BV{pure}",
                "tname": v.get("tname", ""),
                "pages": len(v.get("pages", [])),
            }
        msg = data.get("message", "未知错误") if data else "API 无响应"
        return {"success": False, "error": f"API 返回: {msg}"}
    except Exception as e:
        return {"success": False, "error": str(e)}


async def _get_video_cid(bvid: str) -> Optional[int]:
    """获取视频第一P的 cid"""
    import aiohttp
    pure = bvid[2:] if bvid.upper().startswith("BV") else bvid
    try:
        params = await _wbi_sign_params({"bvid": f"BV{pure}"})
        url = f"https://api.bilibili.com/x/web-interface/wbi/view?{urlencode(params)}"
        headers = {"User-Agent": USER_AGENT,
                   "Referer": f"https://www.bilibili.com/video/BV{pure}"}
        async with aiohttp.ClientSession() as s:
            async with s.get(url, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("code") == 0:
                        pages = data["data"].get("pages", [])
                        if pages:
                            return pages[0].get("cid")
    except Exception as e:
        _log.debug(f"获取 cid 失败: {e}")
    # 回退旧接口
    try:
        url = f"https://api.bilibili.com/x/web-interface/view?bvid=BV{pure}"
        async with aiohttp.ClientSession() as s:
            async with s.get(url, headers={
                "User-Agent": USER_AGENT,
                "Referer": f"https://www.bilibili.com/video/BV{pure}"
            }) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("code") == 0:
                        pages = data["data"].get("pages", [])
                        if pages:
                            return pages[0].get("cid")
    except Exception:
        pass
    return None


# ==================== PlayURL 直链获取 ====================

async def _get_video_playurl(bvid: str, cid: int, quality: int = 127) -> Optional[dict]:
    """
    直调 B站 /x/player/wbi/playurl 获取视频流直链。
    fnval=4048: 请求 DASH(音视频分离) + 8K + HDR + 杜比 + AV1
    qn=127: 请求最高画质，B站会自动返回当前账号/视频支持的最高画质
    """
    import aiohttp
    pure = bvid[2:] if bvid.upper().startswith("BV") else bvid
    params = {
        "bvid": f"BV{pure}", "cid": str(cid), "qn": str(quality),
        "fnval": "4048", "fnver": "0", "fourk": "1", "platform": "web",
    }
    signed = await _wbi_sign_params(params)
    api_url = f"https://api.bilibili.com/x/player/wbi/playurl?{urlencode(signed)}"

    headers = {
        "User-Agent": USER_AGENT,
        "Referer": f"https://www.bilibili.com/video/BV{pure}",
        "Origin": "https://www.bilibili.com",
    }
    cookie = _get_cookie_string()
    if cookie:
        headers["Cookie"] = cookie

    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(api_url, headers=headers,
                             timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    _log.warning(f"[PlayURL] 状态码: {resp.status}")
                    return None
                data = await resp.json()

        if data.get("code") != 0:
            _log.warning(f"[PlayURL] code={data.get('code')} msg={data.get('message')}")
            return None

        rd = data.get("data", {})
        accept_q = rd.get("accept_quality", [])
        actual_q = rd.get("quality")
        _log.info(f"[PlayURL] 请求画质={quality}, 返回画质={actual_q}, 支持画质={accept_q}")

        # ---- DASH 格式 ----
        dash = rd.get("dash")
        if dash:
            videos = dash.get("video", [])
            audios = dash.get("audio", [])
            if videos and audios:
                # 按画质 id 降序，同画质按带宽降序，优先选清晰度高、码率大的流
                videos = sorted(videos, key=lambda v: (v.get("id", 0), v.get("bandwidth", 0)), reverse=True)
                audios = sorted(audios, key=lambda a: (a.get("id", 0), a.get("bandwidth", 0)), reverse=True)
                bv = videos[0]
                ba = audios[0]
                qid = bv.get("id", quality)
                _log.info(f"[PlayURL] DASH 可用画质: {[v.get('id') for v in videos]}, "
                          f"选择: {qid} ({bv.get('width')}x{bv.get('height')}, "
                          f"bandwidth={bv.get('bandwidth', 0)})")
                return {
                    "format": "dash",
                    "video_urls": [bv.get("baseUrl", bv.get("base_url", ""))],
                    "video_backup_urls": bv.get("backupUrl", bv.get("backup_url", [])),
                    "audio_url": ba.get("baseUrl", ba.get("base_url", "")),
                    "audio_backup_urls": ba.get("backupUrl", ba.get("backup_url", [])),
                    "width": bv.get("width", 1920),
                    "height": bv.get("height", 1080),
                    "quality": qid,
                    "codecs": bv.get("codecs", ""),
                    "bandwidth": bv.get("bandwidth", 0),
                }

        # ---- durl 格式（合并流） ----
        durl_list = rd.get("durl")
        if durl_list and len(durl_list) > 0:
            best = durl_list[0]
            qid = rd.get("quality", quality)
            _log.info(f"[PlayURL] durl 合并流，实际画质: {qid} ({best.get('width')}x{best.get('height')})")
            return {
                "format": "durl",
                "video_urls": [best.get("url", "")],
                "video_backup_urls": best.get("backup_url", []) if isinstance(best.get("backup_url"), list) else [],
                "audio_url": "",
                "audio_backup_urls": [],
                "width": best.get("width", 1920),
                "height": best.get("height", 1080),
                "quality": qid,
                "codecs": "",
                "bandwidth": best.get("size", 0),
            }

        _log.warning("[PlayURL] 既无 dash 也无 durl")
        return None

    except Exception as e:
        _log.error(f"[PlayURL] 异常: {e}")
        return None


# ==================== 下载 & 合并 ====================

def _find_ffmpeg() -> Optional[Path]:
    """查找 ffmpeg 可执行文件"""
    candidates = [
        PROJECT_ROOT / "tools" / "ffmpeg" / "ffmpeg.exe",
        Path("ffmpeg.exe"), Path("ffmpeg"),
    ]
    for c in candidates:
        if c.exists():
            return c
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=5, check=True)
        return Path("ffmpeg")
    except Exception:
        pass
    return None


def _build_download_headers(bvid: str) -> dict:
    """构建下载请求头（Cookie + Referer 防盗链）"""
    headers = {
        "User-Agent": USER_AGENT,
        "Referer": f"https://www.bilibili.com/video/{bvid}",
        "Origin": "https://www.bilibili.com",
        "Accept": "*/*",
        "Accept-Encoding": "identity",
    }
    cookie = _get_cookie_string()
    if cookie:
        headers["Cookie"] = cookie
    return headers


async def _download_segment(url: str, dest: Path, headers: dict,
                            label: str = "", max_retries: int = 2) -> bool:
    """aiohttp 分块下载（1MB chunks），失败时切换 CDN 重试"""
    import aiohttp
    for attempt in range(max_retries + 1):
        dl_url = url
        if attempt > 0:
            # ★ 1.8.10: 换真实镜像主机。
            #   旧写法是
            #     replace("upos-sz-mirrorcos","upos-sz-mirror_temp")
            #     replace("upos-sz-mirror","upos-sz-mirrorcos")
            #     replace("upos-sz-mirror_temp","upos-sz-mirror")
            #   但所有 B 站 CDN 主机都以 "upos-sz-mirror" 开头（cos/ali/hw/08c…），
            #   第二步的替换模式是第一步模式的**前缀**，temp 交换根本回不去：
            #     upos-sz-mirrorcos → upos-sz-mirror_temp → upos-sz-mirrorcos_temp
            #   结果重试请求的是一个不存在的主机，必然失败，重试预算白费。
            if "upos-sz-mirror" in dl_url:
                dl_url = re.sub(
                    r"upos-sz-mirror[a-z0-9]*",
                    _CDN_MIRRORS[attempt % len(_CDN_MIRRORS)],
                    dl_url, count=1)
            _log.info(f"[Download] {label} 重试{attempt+1}, 切换CDN...")

        try:
            timeout = aiohttp.ClientTimeout(total=300, connect=15, sock_read=30)
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.get(dl_url, headers=headers) as resp:
                    if resp.status not in (200, 206):
                        _log.warning(f"[Download] {label} HTTP {resp.status} (尝试{attempt+1})")
                        if attempt < max_retries:
                            await asyncio.sleep(1)
                            continue
                        return False

                    total_size = resp.content_length
                    downloaded = 0
                    last_log = 0
                    with open(dest, "wb") as f:
                        async for chunk in resp.content.iter_chunked(1024 * 1024):
                            f.write(chunk)
                            downloaded += len(chunk)
                            if total_size and total_size > 0:
                                pct = downloaded / total_size * 100
                                if pct - last_log >= 20:
                                    _log.info(
                                        f"[Download] {label}: {pct:.0f}% "
                                        f"({downloaded / 1024 / 1024:.1f}/{total_size / 1024 / 1024:.1f}MB)")
                                    last_log = pct

                    file_mb = dest.stat().st_size / 1024 / 1024
                    _log.info(f"[Download] {label} 完成: {file_mb:.1f}MB")
                    return True

        except asyncio.TimeoutError:
            _log.warning(f"[Download] {label} 超时(尝试{attempt+1})")
        except Exception as e:
            _log.error(f"[Download] {label} 异常(尝试{attempt+1}): {e}")
            if attempt < max_retries:
                await asyncio.sleep(2)
    return False


async def _merge_video_audio(video_path: Path, audio_path: Path,
                             output_path: Path, ffmpeg_path: Path) -> bool:
    """ffmpeg 合并音视频（-c copy 无损）"""
    cmd = [
        str(ffmpeg_path), "-i", str(video_path), "-i", str(audio_path),
        "-c:v", "copy", "-c:a", "copy", "-movflags", "+faststart",
        "-y", str(output_path),
    ]
    _log.info("[FFmpeg] 合并中...")
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            err = stderr.decode("utf-8", errors="replace")[-500:]
            _log.error(f"[FFmpeg] 合并失败 (code={proc.returncode}): {err}")
            return False
        if output_path.exists() and output_path.stat().st_size > 0:
            _log.info(f"[FFmpeg] 合并完成: {output_path.stat().st_size / 1024 / 1024:.1f}MB")
            return True
    except Exception as e:
        _log.error(f"[FFmpeg] 异常: {e}")
    return False


# ==================== 主下载流程（异步） ====================

async def _download_video(bvid: str) -> dict:
    """
    下载视频：
    1. PlayURL API 获取 CDN 直链（DASH/durl）
    2. aiohttp 下载
    3. DASH 用 ffmpeg 合并
    4. yt-dlp 回退
    """
    dl_dir = _get_download_dir()
    folder = dl_dir / f"video_{bvid.replace('BV', '')}"
    folder.mkdir(parents=True, exist_ok=True)
    output = folder / f"{bvid}.mp4"

    # 本地已有则直接返回
    if output.exists():
        size_mb = output.stat().st_size / (1024 * 1024)
        _log.info(f"视频已缓存: {output} ({size_mb:.1f}MB)")
        return {"success": True, "file_path": str(output),
                "file_size_mb": round(size_mb, 1), "bvid": bvid}

    ffmpeg_path = _find_ffmpeg()

    # ---- 方案1: PlayURL 直链下载 ----
    cid = await _get_video_cid(bvid)
    if cid:
        playurl = await _get_video_playurl(bvid, cid)
        if playurl and playurl.get("video_urls") and playurl["video_urls"][0]:
            dl_headers = _build_download_headers(bvid)
            fmt = playurl.get("format", "dash")

            if fmt == "durl":
                # 合并流，直接下载
                _log.info(f"[Download] durl 合并流 (画质={playurl.get('quality')})")
                tmp = folder / f"{bvid}_tmp.mp4"
                ok = await _download_segment(playurl["video_urls"][0], tmp, dl_headers, "视频(durl)")
                if ok:
                    tmp.rename(output)
                    size_mb = output.stat().st_size / (1024 * 1024)
                    return {"success": True, "file_path": str(output),
                            "file_size_mb": round(size_mb, 1), "bvid": bvid,
                            "resolution": f"{playurl.get('width', 0)}x{playurl.get('height', 0)}",
                            "quality": playurl.get("quality")}

            elif fmt == "dash" and playurl.get("audio_url"):
                # DASH 分离流
                if not ffmpeg_path:
                    _log.warning("ffmpeg 未找到，DASH 格式无法合并")
                    # 继续回退到 yt-dlp
                else:
                    _log.info(f"[Download] DASH 流: {playurl['width']}x{playurl['height']}")
                    v_tmp = folder / f"{bvid}_video.m4s"
                    a_tmp = folder / f"{bvid}_audio.m4s"

                    results = await asyncio.gather(
                        _download_segment(playurl["video_urls"][0], v_tmp, dl_headers, "视频流"),
                        _download_segment(playurl["audio_url"], a_tmp, dl_headers, "音频流"),
                    )
                    if results[0] and results[1]:
                        merged = await _merge_video_audio(v_tmp, a_tmp, output, ffmpeg_path)
                        for t in [v_tmp, a_tmp]:
                            try:
                                t.unlink(missing_ok=True)
                            except Exception:
                                pass
                        if merged:
                            size_mb = output.stat().st_size / (1024 * 1024)
                            return {"success": True, "file_path": str(output),
                                    "file_size_mb": round(size_mb, 1), "bvid": bvid,
                                    "resolution": f"{playurl.get('width', 0)}x{playurl.get('height', 0)}",
                                    "quality": playurl.get("quality")}
                        else:
                            _log.warning("[Download] 合并失败，回退 yt-dlp...")
                    else:
                        _log.warning("[Download] 部分下载失败，回退 yt-dlp...")
                        for t in [v_tmp, a_tmp]:
                            try:
                                t.unlink(missing_ok=True)
                            except Exception:
                                pass
            else:
                _log.warning("[Download] 无可用的 playurl 数据，回退 yt-dlp...")
        else:
            _log.warning("[Download] 无法获取 playurl，回退 yt-dlp...")
    else:
        _log.warning("[Download] 无法获取 cid，回退 yt-dlp...")

    # ---- 方案2: yt-dlp 回退 ----
    return await _download_via_ytdlp(bvid, output)


async def _download_via_ytdlp(bvid: str, output: Path) -> dict:
    """yt-dlp 回退下载"""
    try:
        import yt_dlp
    except ImportError:
        return {"success": False, "error": "yt-dlp 未安装且直链下载失败。请执行: pip install yt-dlp"}

    try:
        folder = output.parent
        folder.mkdir(parents=True, exist_ok=True)
        ydl_opts = {
            "outtmpl": str(output),
            "format": "bestvideo+bestaudio/best",
            "merge_output_format": "mp4",
            "quiet": True, "no_warnings": True,
            "retries": 5, "sleep_interval": 3, "max_sleep_interval": 8,
        }
        if COOKIES_FILE.exists():
            ydl_opts["cookiefile"] = str(COOKIES_FILE)
        ffmpeg_path = _find_ffmpeg()
        if ffmpeg_path:
            ydl_opts["ffmpeg_location"] = str(ffmpeg_path)

        loop = asyncio.get_running_loop()

        def _dl():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([f"https://www.bilibili.com/video/{bvid}"])

        await loop.run_in_executor(None, _dl)

        if output.exists():
            size_mb = output.stat().st_size / (1024 * 1024)
            return {"success": True, "file_path": str(output),
                    "file_size_mb": round(size_mb, 1), "bvid": bvid}
        return {"success": False, "error": "yt-dlp 下载后未生成文件"}
    except Exception as e:
        return {"success": False, "error": f"yt-dlp 下载失败: {e}"}


async def _resolve_short_url(short_id: str) -> Optional[str]:
    """解析 b23.tv 短链接，获取 BV 号"""
    import aiohttp
    try:
        url = f"https://b23.tv/{short_id}"
        headers = {"User-Agent": USER_AGENT}
        async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10)) as s:
            async with s.get(url, headers=headers, allow_redirects=True) as resp:
                final_url = str(resp.url)
                m = re.search(r'BV([a-zA-Z0-9]{10})', final_url)
                if m:
                    return f"BV{m.group(1)}"
    except Exception as e:
        _log.debug(f"短链接解析失败: {e}")
    return None


# ==================== 入口 ====================

def _cover_filename(bvid: str, cover_url: str) -> str:
    """从封面 URL 推断扩展名（B站封面常带 @320w_200h 之类后缀，取不到就用 .jpg）"""
    suf = ""
    try:
        path = unquote(urlparse(cover_url).path)
        suf = Path(path).suffix.lower()
    except Exception:
        suf = ""
    if suf not in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"):
        suf = ".jpg"
    return f"{bvid}_封面{suf}"


def _cover_result(info: dict, bvid: str) -> dict:
    """★ 1.9.0: cover → 真正把封面下载到本地，返回 file_path。

    以前这里只回 {"cover_url": …}，而主窗口的「通用文件卡片」分支只认
    file_path / pdf / save_dir，于是界面上什么都不出现 —— 用户反馈的
    「封面下载失败，说是只返回了 url，没有实现下载」就是这个原因。
    现在复用通用 download 工具：返回 file_path 后，主窗口会自动把封面
    作为图片显示在聊天窗口左侧（桌宠侧），并写进历史，重启后仍在。
    """
    if not info.get("success") or not info.get("cover_url"):
        return info
    cover_url = info["cover_url"]
    title = (info.get("title") or "").strip() or bvid
    try:
        from tools.download_tool import download_url
    except Exception as e:
        return {"success": False, "error": f"下载模块不可用: {e}", "cover_url": cover_url}
    r = download_url(
        cover_url,
        filename=_cover_filename(bvid, cover_url),
        bucket="bilibili",
        referer=f"https://www.bilibili.com/video/{bvid}",
    )
    if not r.get("success"):
        # 下载失败也把直链带回去，至少模型能给用户一个可点的链接
        r.setdefault("cover_url", cover_url)
        r.setdefault("title", title)
        r.setdefault("bvid", bvid)
        return r
    r["cover_url"] = cover_url
    r["title"] = title
    r["bvid"] = bvid
    r["message"] = f"🖼 B站封面 · {title}"
    return r


def execute(action: str = "info", bv_id: str = "", raw_message: str = "",
            **kwargs) -> str:
    """
    B站工具入口。同步接口，内部用 asyncio.run() 执行异步操作。
    """
    # 提取 BV 号
    bvid = bv_id.strip()
    if bvid.upper().startswith("BV") and len(bvid) > 2:
        pass  # 已有完整 BV 号
    elif bvid:
        bvid = f"BV{bvid}"
    elif raw_message:
        # 先检查短链接
        short_m = SHORT_URL_RE.search(raw_message)
        if short_m:
            _log.info(f"检测到 b23.tv 短链接: {short_m.group(1)}")
            bvid = asyncio.run(_resolve_short_url(short_m.group(1)))
        if not bvid:
            bvid = _extract_bvid(raw_message)

    if not bvid:
        bvid = kwargs.get("bvid", "") or kwargs.get("bv", "")
        if bvid and not bvid.upper().startswith("BV"):
            bvid = f"BV{bvid}"

    if not bvid:
        return json.dumps({
            "success": False,
            "error": "未找到 BV 号，请提供 bv_id 或B站链接",
        }, ensure_ascii=False)

    _log.info(f"B站工具: {action} {bvid}")

    try:
        if action == "info":
            result = asyncio.run(_get_video_info(bvid))
        elif action == "cover":
            # ★ 1.9.0: 下载封面到本地（以前只返回 cover_url）
            result = _cover_result(asyncio.run(_get_video_info(bvid)), bvid)
        elif action == "download":
            result = asyncio.run(_download_video(bvid))
        else:
            result = {"success": False, "error": f"未知操作: {action}"}
    except RuntimeError as e:
        if "cannot be called from a running event loop" in str(e):
            _log.warning("检测到已有事件循环，在新线程中执行")
            import threading
            result_holder = {}

            def _run():
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    if action == "download":
                        result_holder["r"] = loop.run_until_complete(_download_video(bvid))
                    elif action == "info":
                        result_holder["r"] = loop.run_until_complete(_get_video_info(bvid))
                    elif action == "cover":
                        # ★ 1.9.0: 同样走"下载到本地"的路径
                        result_holder["r"] = _cover_result(
                            loop.run_until_complete(_get_video_info(bvid)), bvid)
                finally:
                    loop.close()

            t = threading.Thread(target=_run, daemon=True)
            t.start()
            t.join(timeout=180)
            result = result_holder.get("r", {"success": False, "error": "内部线程超时"})
        else:
            raise

    return json.dumps(result, ensure_ascii=False, indent=2)
