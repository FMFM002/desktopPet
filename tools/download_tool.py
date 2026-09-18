"""
通用下载工具（★ 1.9.0 新增）

由来：1.9.0 开发日志里两条"失败"其实是同一个根因 ——
  ① 联网搜索拿不到图片；
  ② B站封面"说是只返回了 url，没有实现下载"。
两条都只差"把 URL 变成文件并送进聊天窗口"这一步，所以做成一个通用工具，
以后网页附件、搜索结果配图、封面图都走它。

用法（LLM 侧）：
  download(url="https://…/a.jpg")            → 存到 data/downloads/websearch/
  download(url="…", filename="封面.jpg")     → 指定文件名
  download(url="…", bucket="bilibili")       → 存到 data/downloads/bilibili/

下载完成后：
  · 返回 {"success": true, "file_path": …, "message": …}
  · MainWindow._maybe_show_file_card 的「通用」分支会自动把它推给聊天窗口
    （图片走左侧内联缩略图，其它走文件卡片），并★写进历史，重启后仍在。
"""
import json
import logging
import re
from pathlib import Path
from urllib.parse import urlparse, unquote

import requests

_log = logging.getLogger("Tool.Download")

PROJECT_ROOT = Path(__file__).resolve().parent.parent

TOOL_NAME = "download"
TOOL_DESCRIPTION = (
    "把任意 http/https 链接（图片、封面、PDF、压缩包、音视频等）下载到本地，"
    "并自动发送到聊天窗口。"
    "联网搜索拿到图片链接后要用它把图取回来；B站封面、网页上的附件同理。"
    "参数: url(必填); filename(可选，含扩展名); bucket(可选，data/downloads 下的子目录，默认 websearch)。"
)
TOOL_PARAMETERS = {
    "type": "object",
    "properties": {
        "url": {"type": "string", "description": "要下载的 http/https 链接"},
        "filename": {"type": "string",
                     "description": "保存的文件名（含扩展名）；不填则从 URL 推断"},
        "bucket": {"type": "string",
                   "description": "保存到 data/downloads/ 下的哪个子目录，默认 websearch"},
    },
    "required": ["url"],
}

# 单文件上限（与聊天窗口「用户上传 50MB 上限」保持一致）
MAX_BYTES = 50 * 1024 * 1024
DEFAULT_TIMEOUT = 30

_IMG_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico")
_BAD_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')

_CTYPE_EXT = {
    "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png",
    "image/gif": ".gif", "image/webp": ".webp", "image/bmp": ".bmp",
    "image/x-icon": ".ico", "application/pdf": ".pdf",
    "application/zip": ".zip", "text/plain": ".txt",
    "video/mp4": ".mp4", "audio/mpeg": ".mp3",
}


def _safe_name(name: str) -> str:
    """清洗文件名：去掉路径分隔符与 Windows 非法字符，防目录穿越"""
    name = _BAD_CHARS.sub("_", (name or "").strip())
    name = name.replace("..", "_").strip(". ")
    return name[:120]


def _guess_name(url: str) -> str:
    """从 URL 推断文件名（含扩展名）"""
    try:
        path = unquote(urlparse(url).path)
        base = Path(path).name
    except Exception:
        base = ""
    name = _safe_name(base)
    if not name or name in (".", "_"):
        return "download"
    return name


def _unique(dest: Path) -> Path:
    """同名不覆盖：追加 -1 / -2 …"""
    if not dest.exists():
        return dest
    stem, suf, i = dest.stem, dest.suffix, 1
    while True:
        cand = dest.with_name(f"{stem}-{i}{suf}")
        if not cand.exists():
            return cand
        i += 1


def _fmt_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / 1024 / 1024:.1f} MB"


def download_dir(bucket: str = "websearch") -> Path:
    """data/downloads/<bucket>/（不存在则创建）"""
    b = _safe_name(bucket).lower() or "websearch"
    d = PROJECT_ROOT / "data" / "downloads" / b
    d.mkdir(parents=True, exist_ok=True)
    return d


def download_url(url: str, filename: str = "", bucket: str = "websearch",
                 referer: str = "", timeout: int = DEFAULT_TIMEOUT) -> dict:
    """下载一个 URL 到 data/downloads/<bucket>/。返回 dict（可直接当工具结果）。

    只允许 http/https；有大小上限；同名自动加序号，不覆盖已有文件。
    """
    u = (url or "").strip()
    if not u:
        return {"success": False, "error": "url 不能为空"}
    if not u.lower().startswith(("http://", "https://")):
        return {"success": False, "error": "只支持 http/https 链接"}

    headers = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/120.0 Safari/537.36"),
    }
    if referer:
        headers["Referer"] = referer

    dest_dir = download_dir(bucket)
    name = _safe_name(filename) if (filename or "").strip() else _guess_name(u)
    dest = dest_dir / (name or "download")

    total = 0
    try:
        with requests.get(u, headers=headers, stream=True, timeout=timeout) as r:
            if r.status_code != 200:
                return {"success": False, "url": u,
                        "error": f"HTTP {r.status_code}（下载失败）"}
            # URL 推断不出扩展名时，用 Content-Type 补一个
            if not dest.suffix:
                ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                ext = _CTYPE_EXT.get(ctype, "")
                if ext:
                    dest = dest.with_suffix(ext)
            dest = _unique(dest)
            too_big = False
            with open(dest, "wb") as f:
                for chunk in r.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > MAX_BYTES:
                        too_big = True
                        break
                    f.write(chunk)
            if too_big:
                try:
                    dest.unlink()
                except Exception:
                    pass
                return {"success": False, "url": u,
                        "error": f"文件超过 {MAX_BYTES // 1024 // 1024}MB 上限，已取消下载"}
    except Exception as e:
        try:
            if dest.exists() and dest.stat().st_size == 0:
                dest.unlink()
        except Exception:
            pass
        return {"success": False, "url": u, "error": f"下载异常: {e}"}

    if total == 0:
        try:
            dest.unlink()
        except Exception:
            pass
        return {"success": False, "url": u, "error": "下载内容为空"}

    is_img = dest.suffix.lower() in _IMG_EXTS
    size_str = _fmt_size(total)
    _log.info(f"[Download] 完成: {dest} ({size_str})")
    return {
        "success": True,
        "file_path": str(dest),
        "file_name": dest.name,
        "file_size": size_str,
        "bytes": total,
        "is_image": is_img,
        "url": u,
        "message": (f"🖼 图片下载完成 · {dest.name}" if is_img
                    else f"📎 下载完成 · {dest.name}"),
    }


def execute(url: str, filename: str = "", bucket: str = "websearch", **kwargs) -> str:
    """下载 URL → 返回 JSON。主窗口会自动把它推送到聊天窗口。"""
    result = download_url(url, filename=filename or "", bucket=bucket or "websearch")
    return json.dumps(result, ensure_ascii=False, indent=2)
