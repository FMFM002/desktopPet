"""
通用下载工具（★ 1.9.0 新增；★ 1.9.2 按用户要求参照 Motrix 升级）

由来：1.9.0 开发日志里两条"失败"其实是同一个根因 ——
  ① 联网搜索拿不到图片；
  ② B站封面"说是只返回了 url，没有实现下载"。
两条都只差"把 URL 变成文件并送进聊天窗口"这一步，所以做成一个通用工具。

================================================================================
★ 1.9.2 升级（用户原话：「升级download工具，参考项目 …\\Motrix-main下载器\\Motrix-main」）
================================================================================
Motrix 本体是 Electron + aria2 的完整下载器（种子/DHT/多任务/限速面板…），
整个搬过来没意义。这里只取它对**单 URL 下载**真正有用的四件事，逐条对着源码：

  1) 【分段并发下载】 ← `src/core/download/segment-downloader.ts` +
     `aria2-segment-client.ts`
     · 先探 Content-Length 与 `Accept-Ranges: bytes`；
     · 够大（≥1MB）且支持 Range → 切成 N 段，多线程各写自己的偏移区间；
     · 单线程慢的根因就是"一个连接拉到底"，分段是最有效的提速手段。

  2) 【断点续传】 ← `src/core/task/final-name-picker.ts` 里的
     `INCOMPLETE_SUFFIX` / `.aria2` 控制文件思路
     · 下载写进 `xxx.part`（**不是**最终文件名）→ 完成才改名；
       这样"下到一半的文件"永远不会被误当成成品（Motrix 用 `.motrix`，
       这里用更通用的 `.part`，并配 `xxx.part.aria2` 控制文件）；
     · 控制文件记 URL / 总长 / 已完成分段 → 中断后重跑只补没下完的段。

  3) 【失败重试 + 退避】 ← aria2 的 `max-tries` / `retry-wait`
     · 每段独立重试（默认 3 次），等待 1.5s → 3s → 6s 指数退避；
     · 单个分段失败不会毁掉整次下载（Motrix 的 `task-error/descriptor` 也是
       "分段级失败 + 可诊断描述"的思路）。

  4) 【文件名确定与去重】 ← `final-name-picker.ts`
     · 优先 Content-Disposition（含 RFC 5987 的 `filename*=UTF-8''…`），
       其次 URL 路径，再次 Content-Type 推断扩展名；
     · 重名不覆盖：`base (1).ext`（照 Motrix 的 ` (n)` 格式）。

用法（LLM 侧）：
  download(url="https://…/a.jpg")                   → data/downloads/websearch/
  download(url="…", filename="封面.jpg")            → 指定文件名
  download(url="…", bucket="bilibili")              → data/downloads/bilibili/
  download(url="…", threads=8)                      → 指定分段并发数（0=自动）
  download(url="…", resume=False)                   → 不续传，从头下

下载完成后：
  · 返回 {"success": true, "file_path": …, "message": …}
  · MainWindow._maybe_show_file_card 的「通用」分支会自动把它推给聊天窗口
    （图片走左侧内联缩略图，其它走文件卡片），并★写进历史，重启后仍在。
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlparse, unquote

import requests

_log = logging.getLogger("Tool.Download")

PROJECT_ROOT = Path(__file__).resolve().parent.parent

TOOL_NAME = "download"
TOOL_DESCRIPTION = (
    "把任意 http/https 链接（图片、封面、PDF、压缩包、音视频等）下载到本地，"
    "并自动发送到聊天窗口。"
    "★ 支持多线程分段下载 + 断点续传（照 Motrix）：大文件会切成多段并发拉取，"
    "下到一半中断的话，再下同一个 URL 会接着下（不会从头再来）。"
    "★ 也支持 BT：磁力链（magnet:?xt=urn:btih:…）和 .torrent 种子，"
    "交给 aria2c 后台下载（BT 很慢，会立刻返回 job_id，之后问进度就行）。"
    "联网搜索拿到图片链接后要用它把图取回来；B站封面、网页上的附件同理。\n"
    "参数: url(必填); filename(可选，含扩展名); bucket(可选，默认 websearch，"
    "即 data/downloads/<bucket>/); threads(可选，分段并发数 1-8，默认自动); "
    "resume(可选，默认 true，断点续传); max_mb(可选，默认 512，超过就拒绝); "
    "action(可选: 空=下载 | bt_status=看 BT 任务进度 | bt_stop=停止，配 job_id)。"
)
TOOL_PARAMETERS = {
    "type": "object",
    "properties": {
        "url": {"type": "string",
                "description": "要下载的 http/https 链接，或磁力链 magnet:?xt=…，"
                               "或 .torrent 种子（本地路径/网址）"},
        "filename": {"type": "string",
                     "description": "保存的文件名（含扩展名）；不填则自动推断"},
        "bucket": {"type": "string",
                   "description": "保存到 data/downloads/ 下的哪个子目录，默认 websearch"},
        "threads": {"type": "integer",
                    "description": "分段并发数 1-8；0 或不填=自动（按文件大小定）"},
        "resume": {"type": "boolean",
                   "description": "断点续传，默认 true；下次下同一个 URL 会接着下"},
        "max_mb": {"type": "integer",
                   "description": "文件大小上限（MB），默认 512"},
        "referer": {"type": "string",
                    "description": "防盗链站点需要（如 B站封面填 https://www.bilibili.com）"},
        "action": {"type": "string",
                   "enum": ["", "bt_status", "bt_stop", "bt_engine"],
                   "description": "空=正常下载；bt_status=查 BT 任务进度；"
                                  "bt_stop=停止某任务(配 job_id)；bt_engine=看 BT 引擎状态；"
                                  "cleanup=清理下载缓存(超 800MB 时删最旧的)"},
        "job_id": {"type": "string",
                   "description": "[action=bt_stop] 要停止的 BT 任务 id"},
    },
    "required": ["url"],
}

# ── 分段下载参数（对着 Motrix aria2 的 max-tries / retry-wait / -x 取值）──
DEFAULT_THREADS = 4            # 未指定时按大小自动在 1..4 之间取
MAX_THREADS = 8
SEGMENT_MIN_BYTES = 1 * 1024 * 1024      # 小于 1MB 不分段（分段开销不划算）
DEFAULT_RETRIES = 3
RETRY_WAIT = 1.5               # 退避基数 → 1.5s / 3s / 6s
DEFAULT_MAX_MB = 512

PART_SUFFIX = ".part"
CTRL_SUFFIX = ".aria2"         # 控制文件（照 Motrix 的 .aria2 命名）
CHUNK = 256 * 1024

DEFAULT_TIMEOUT = 30
PROBE_TIMEOUT = 15

_IMG_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico")
_BAD_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')

_CTYPE_EXT = {
    "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png",
    "image/gif": ".gif", "image/webp": ".webp", "image/bmp": ".bmp",
    "image/x-icon": ".ico", "application/pdf": ".pdf",
    "application/zip": ".zip", "text/plain": ".txt",
    "video/mp4": ".mp4", "audio/mpeg": ".mp3",
    "application/octet-stream": "",
}

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")


# ══════════════════════════════════════════════════════════════════
# 文件名
# ══════════════════════════════════════════════════════════════════

def _safe_name(name: str) -> str:
    """清洗文件名：去掉路径分隔符与 Windows 非法字符，防目录穿越"""
    name = _BAD_CHARS.sub("_", (name or "").strip())
    name = name.replace("..", "_").strip(". ")
    return name[:120]


def _from_content_disposition(cd: str) -> str:
    """★ 1.9.2：从 Content-Disposition 取文件名（含 RFC 5987 的 filename*）。"""
    if not cd:
        return ""
    # filename*=UTF-8''%E5%B0%81%E9%9D%A2.jpg（优先，带编码信息）
    m = re.search(r"filename\*\s*=\s*([^;]+)", cd, re.I)
    if m:
        v = m.group(1).strip().strip('"')
        if "''" in v:
            _cs, _, enc = v.partition("''")
            try:
                return unquote(enc)
            except Exception:
                pass
        try:
            return unquote(v)
        except Exception:
            return v
    m = re.search(r'filename\s*=\s*"([^"]+)"', cd, re.I)
    if m:
        return m.group(1)
    m = re.search(r"filename\s*=\s*([^;]+)", cd, re.I)
    if m:
        return m.group(1).strip().strip('"')
    return ""


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
    """同名不覆盖：追加 (1) / (2) …（照 Motrix final-name-picker 的 ` (n)` 格式）"""
    if not dest.exists():
        return dest
    stem, suf, i = dest.stem, dest.suffix, 1
    while i <= 9999:
        cand = dest.with_name(f"{stem} ({i}){suf}")
        if not cand.exists():
            return cand
        i += 1
    return dest.with_name(f"{stem}-{int(time.time())}{suf}")


def _fmt_size(n) -> str:
    try:
        n = int(n)
    except Exception:
        return "?"
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    if n < 1024 ** 3:
        return f"{n / 1024 / 1024:.1f} MB"
    return f"{n / 1024 ** 3:.2f} GB"


def download_dir(bucket: str = "websearch") -> Path:
    """data/downloads/<bucket>/（不存在则创建）"""
    b = _safe_name(bucket).lower() or "websearch"
    d = PROJECT_ROOT / "data" / "downloads" / b
    d.mkdir(parents=True, exist_ok=True)
    return d


# ══════════════════════════════════════════════════════════════════
# ★ 1.9.2（内存/磁盘调研结论）：data/downloads 清理策略
# ══════════════════════════════════════════════════════════════════
#
# 调研实测：`data/downloads` 已经 509 MB / 840 个文件，而且**只增不减** ——
# 网页配图、B站封面、P站图、下载的安装包全往里堆。
#
# 策略（保守，只动"我们自己下载的缓存"）：
#   · 只在总量超过 MAX_DOWNLOADS_MB（默认 800，可用环境变量
#     PRTS_DOWNLOADS_MAX_MB 调）时才开始清；
#   · 按 mtime 从**最旧**开始删，直到降到上限的 80%（留点余量，别每下一次都清）；
#   · **永远保留**：最新的 KEEP_RECENT 个文件、`tools/` 整个目录
#     （BT 引擎 aria2c 与 CA 证书在那儿）、任何 `.part`/`.aria2`
#     （那是断点续传的现场，删了等于让人重下）。
#
# ⚠ 这是**唯一一处会主动删文件**的逻辑，所以：只删 data/downloads 下、
#   我们自己下载的东西；不碰用户的任何其他目录。
MAX_DOWNLOADS_MB = 800
KEEP_RECENT = 30
_PURGE_THROTTLE = 600.0        # 同一进程内最多 10 分钟清一次（别每次下载都扫盘）
_last_purge = 0.0
_PURGE_EXCLUDE_DIRS = {"tools"}


def cleanup_downloads(max_mb: int = 0, force: bool = False) -> dict:
    """把 data/downloads 清到上限以内。返回统计（供日志/工具结果用）。"""
    global _last_purge
    now = time.time()
    if not force and (now - _last_purge) < _PURGE_THROTTLE:
        return {"skipped": "throttled"}
    _last_purge = now

    try:
        cap_mb = int(max_mb or os.environ.get("PRTS_DOWNLOADS_MAX_MB")
                     or MAX_DOWNLOADS_MB)
    except Exception:
        cap_mb = MAX_DOWNLOADS_MB
    cap = cap_mb * 1024 * 1024
    base = PROJECT_ROOT / "data" / "downloads"
    if not base.is_dir():
        return {"skipped": "no_dir"}

    files = []
    total = 0
    for p in base.rglob("*"):
        if not p.is_file():
            continue
        # 保护：BT 引擎目录、以及续传现场
        if any(part in _PURGE_EXCLUDE_DIRS for part in
               p.relative_to(base).parts[:-1]):
            continue
        if p.suffix.lower() in (".part", ".aria2"):
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        files.append((st.st_mtime, st.st_size, p))
        total += st.st_size

    if total <= cap:
        return {"total_mb": round(total / 1024 / 1024, 1), "cap_mb": cap_mb,
                "deleted": 0}

    files.sort()                       # 最旧在前
    protected = {p for _m, _s, p in files[-KEEP_RECENT:]}   # 最新 N 个不动
    target = int(cap * 0.8)
    deleted, freed = 0, 0
    for _mtime, size, p in files:
        if total <= target:
            break
        if p in protected:
            continue
        try:
            p.unlink()
            deleted += 1
            freed += size
            total -= size
        except OSError:
            continue
    if deleted:
        _log.info(f"[Download] 清理下载缓存：删了 {deleted} 个最旧文件、"
                  f"释放 {_fmt_size(freed)}（现在 {_fmt_size(total)}，"
                  f"上限 {cap_mb}MB）")
    return {"total_mb": round(total / 1024 / 1024, 1), "cap_mb": cap_mb,
            "deleted": deleted, "freed": _fmt_size(freed)}


# ══════════════════════════════════════════════════════════════════
# 探测
# ══════════════════════════════════════════════════════════════════

def _headers(referer: str = "") -> dict:
    h = {"User-Agent": _UA}
    if referer:
        h["Referer"] = referer
    return h


def _probe(url: str, headers: dict, timeout: int = PROBE_TIMEOUT) -> dict:
    """探大小 / 是否支持 Range / 真实文件名。先 HEAD，不行退化成 GET 取头。"""
    info = {"total": None, "ranges": False, "ctype": "", "filename": "",
            "final_url": url}
    for method in ("head", "get"):
        try:
            if method == "head":
                r = requests.head(url, headers=headers, timeout=timeout,
                                  allow_redirects=True)
            else:
                r = requests.get(url, headers=headers, timeout=timeout,
                                 stream=True, allow_redirects=True)
            if r.status_code >= 400:
                if method == "get":
                    return info
                continue
            h = r.headers
            info["final_url"] = str(r.url or url)
            info["ctype"] = (h.get("Content-Type") or "").split(";")[0].strip().lower()
            info["ranges"] = "bytes" in (h.get("Accept-Ranges") or "").lower()
            info["filename"] = _from_content_disposition(h.get("Content-Disposition") or "")
            try:
                cl = int(h.get("Content-Length") or 0)
                if cl > 0:
                    info["total"] = cl
            except Exception:
                pass
            if method == "get":
                r.close()
            # HEAD 被拒（有些站只认 GET）或拿不到长度 → 再试 GET
            if method == "head" and (info["total"] is None or not info["ranges"]):
                continue
            return info
        except Exception as e:
            _log.debug(f"[Download] {method} 探测失败: {e}")
            continue
    return info


# ══════════════════════════════════════════════════════════════════
# 分段 / 控制文件
# ══════════════════════════════════════════════════════════════════

def _plan_segments(total: int, threads: int) -> list:
    """均分成 N 段 [(start, end_inclusive), …]（照 aria2 的 -x/-s 分段思路）"""
    if not total or total <= 0:
        return []
    n = max(1, min(int(threads or 1), MAX_THREADS))
    if total < SEGMENT_MIN_BYTES:
        n = 1
    step = total // n
    segs = []
    for i in range(n):
        s = i * step
        e = total - 1 if i == n - 1 else (s + step - 1)
        segs.append((s, e))
    return segs


def _auto_threads(total) -> int:
    """按文件大小自动定并发数（小文件别开线程，纯开销）。"""
    if not total:
        return 1
    if total < SEGMENT_MIN_BYTES:
        return 1
    if total < 16 * 1024 * 1024:
        return 2
    if total < 128 * 1024 * 1024:
        return DEFAULT_THREADS
    return 6


def _ctrl_path(part: Path) -> Path:
    return part.with_name(part.name + CTRL_SUFFIX)


def _load_ctrl(part: Path, url: str, total) -> set:
    """读控制文件 → 已完成的分段下标集合（URL/总长对不上就作废重下）。"""
    try:
        cp = _ctrl_path(part)
        if not cp.exists():
            return set()
        d = json.loads(cp.read_text(encoding="utf-8"))
        if d.get("url") != url:
            _log.info("[Download] 控制文件的 URL 不匹配 → 重新开始")
            return set()
        if total and int(d.get("total") or 0) != int(total):
            _log.info("[Download] 控制文件的总长不匹配 → 重新开始")
            return set()
        return set(int(x) for x in (d.get("done") or []))
    except Exception as e:
        _log.debug(f"[Download] 读控制文件失败: {e}")
        return set()


def _save_ctrl(part: Path, url: str, total, done: set, segs: list):
    try:
        _ctrl_path(part).write_text(json.dumps({
            "url": url, "total": int(total or 0),
            "done": sorted(done),
            "segments": [[int(s), int(e)] for s, e in segs],
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        }, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as e:
        _log.debug(f"[Download] 写控制文件失败: {e}")


# ══════════════════════════════════════════════════════════════════
# 单段 / 单流下载
# ══════════════════════════════════════════════════════════════════

class _Progress:
    """各段已下字节（进度与"是否真的在动"的判断都靠它）"""

    def __init__(self):
        self._lock = threading.Lock()
        self.bytes = {}
        self.errors = []

    def set(self, idx: int, n: int):
        with self._lock:
            self.bytes[idx] = n

    def add(self, idx: int, n: int):
        with self._lock:
            self.bytes[idx] = self.bytes.get(idx, 0) + n

    def fail(self, idx: int, msg: str):
        with self._lock:
            self.errors.append((idx, msg))

    def total(self) -> int:
        with self._lock:
            return sum(self.bytes.values())


def _seg_worker(url: str, headers: dict, part: Path, idx: int, start: int,
                end: int, retries: int, prog: _Progress,
                timeout: int) -> bool:
    """下一段 [start, end]（含端点），写进 .part 的对应偏移。

    ★ 每段独立重试 + 指数退避（照 aria2 的 max-tries / retry-wait）。
    ★ 每段用自己的文件句柄 seek，互不干扰（同一文件不同区间是安全的）。
    """
    expect = end - start + 1
    for attempt in range(retries + 1):
        got = 0
        try:
            h = dict(headers)
            h["Range"] = f"bytes={start + prog.bytes.get(idx, 0)}-{end}"
            with requests.get(url, headers=h, stream=True, timeout=timeout) as r:
                if r.status_code not in (200, 206):
                    raise RuntimeError(f"HTTP {r.status_code}")
                # 服务器忽略 Range 返回 200（整份）时，只有 0 段能用它
                if r.status_code == 200 and start != 0:
                    raise RuntimeError("服务器不支持 Range（返回 200 全量）")
                with open(part, "r+b") as f:
                    f.seek(start + prog.bytes.get(idx, 0))
                    for chunk in r.iter_content(chunk_size=CHUNK):
                        if not chunk:
                            continue
                        f.write(chunk)
                        got += len(chunk)
                        prog.add(idx, len(chunk))
            if prog.bytes.get(idx, 0) >= expect:
                return True
            raise RuntimeError(
                f"只下到 {prog.bytes.get(idx, 0)}/{expect} 字节")
        except Exception as e:
            # 回退本次已计数的字节（下次重试从原位置继续）
            if got:
                with prog._lock:
                    prog.bytes[idx] = max(0, prog.bytes.get(idx, 0) - got)
            if attempt >= retries:
                prog.fail(idx, f"第{idx}段失败: {e}")
                return False
            wait = RETRY_WAIT * (2 ** attempt)
            _log.info(f"[Download] 第{idx}段第{attempt + 1}次失败（{e}），"
                      f"{wait:.1f}s 后重试")
            time.sleep(wait)
    return False


def _stream_worker(url: str, headers: dict, part: Path, retries: int,
                   prog: _Progress, timeout: int) -> bool:
    """不支持 Range 时的退化为单流下载（带重试，成功后至少留个完整文件）。"""
    for attempt in range(retries + 1):
        try:
            with requests.get(url, headers=headers, stream=True,
                              timeout=timeout) as r:
                if r.status_code != 200:
                    raise RuntimeError(f"HTTP {r.status_code}")
                with open(part, "wb") as f:
                    for chunk in r.iter_content(chunk_size=CHUNK):
                        if not chunk:
                            continue
                        f.write(chunk)
                        prog.add(0, len(chunk))
            return True
        except Exception as e:
            if attempt >= retries:
                prog.fail(0, str(e))
                return False
            # 单流没法续传，重来就重来（清掉半截）
            try:
                part.write_bytes(b"")
            except Exception:
                pass
            with prog._lock:
                prog.bytes[0] = 0
            wait = RETRY_WAIT * (2 ** attempt)
            _log.info(f"[Download] 单流第{attempt + 1}次失败（{e}），"
                      f"{wait:.1f}s 后重试")
            time.sleep(wait)
    return False


# ══════════════════════════════════════════════════════════════════
# 主入口
# ══════════════════════════════════════════════════════════════════

def download_url(url: str, filename: str = "", bucket: str = "websearch",
                 referer: str = "", timeout: int = DEFAULT_TIMEOUT,
                 threads: int = 0, resume: bool = True,
                 max_mb: int = DEFAULT_MAX_MB, retries: int = DEFAULT_RETRIES,
                 progress_cb=None) -> dict:
    """下载一个 URL 到 data/downloads/<bucket>/。返回 dict（可直接当工具结果）。

    ★ 1.9.2：分段并发 + 断点续传 + 重试退避 + Content-Disposition 文件名。
    只允许 http/https；有大小上限；重名自动加序号，不覆盖已有文件。
    """
    u = (url or "").strip()
    if not u:
        return {"success": False, "error": "url 不能为空"}
    if not u.lower().startswith(("http://", "https://")):
        return {"success": False, "error": "只支持 http/https 链接"}

    try:
        limit = max(1, int(max_mb)) * 1024 * 1024
    except Exception:
        limit = DEFAULT_MAX_MB * 1024 * 1024

    headers = _headers(referer)

    # ── 1) 探测 ──
    info = _probe(u, headers, min(timeout, PROBE_TIMEOUT))
    total = info["total"]
    ranges = bool(info["ranges"])
    if total and total > limit:
        return {"success": False, "url": u,
                "error": f"文件 {_fmt_size(total)} 超过上限 "
                         f"{_fmt_size(limit)}，已取消（可用 max_mb 调大）"}

    dest_dir = download_dir(bucket)
    name = _safe_name(filename) if (filename or "").strip() else ""
    if not name:
        name = _safe_name(info["filename"]) or _guess_name(u)
    if not name or name in (".", "_"):
        name = "download"
    dest = dest_dir / name
    if not dest.suffix:
        ext = _CTYPE_EXT.get(info["ctype"], "")
        if ext:
            dest = dest.with_suffix(ext)
    dest = _unique(dest)

    part = dest.with_name(dest.name + PART_SUFFIX)
    # 续传前提：控制文件里的 URL/总长都对得上
    done = _load_ctrl(part, u, total) if resume else set()
    if not resume:
        try:
            if part.exists():
                part.unlink()
            cp = _ctrl_path(part)
            if cp.exists():
                cp.unlink()
        except Exception:
            pass

    threads = int(threads or 0)
    if threads <= 0:
        threads = _auto_threads(total)
    segs = _plan_segments(total, threads) if ranges else []

    prog = _Progress()
    t0 = time.time()
    mode = ""

    try:
        if segs and len(segs) > 1:
            # ── 2a) 分段并发 ──
            mode = f"{len(segs)} 段并发"
            # 预分配文件（每段各自 seek 写入）
            if not part.exists() or part.stat().st_size != total:
                with open(part, "wb") as f:
                    f.truncate(total)
            # 已完成的分段直接按整段计入进度（续传）
            for i, (s, e) in enumerate(segs):
                if i in done:
                    prog.set(i, e - s + 1)
            todo = [(i, s, e) for i, (s, e) in enumerate(segs) if i not in done]
            if todo:
                _log.info(f"[Download] 分段下载 {len(todo)}/{len(segs)} 段 "
                          f"(共 {_fmt_size(total)})")
            workers = min(len(todo), MAX_THREADS) or 1
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futs = {ex.submit(_seg_worker, info["final_url"], headers, part,
                                  i, s, e, retries, prog, timeout): i
                        for i, s, e in todo}
                for fut in as_completed(futs):
                    i = futs[fut]
                    try:
                        if fut.result():
                            done.add(i)
                            _save_ctrl(part, u, total, done, segs)
                    except Exception as e:
                        prog.fail(i, str(e))
                    if progress_cb:
                        try:
                            progress_cb(prog.total(), total)
                        except Exception:
                            pass
            if prog.errors:
                _save_ctrl(part, u, total, done, segs)
                return {"success": False, "url": u,
                        "error": ("分段下载未完成: "
                                  + "; ".join(m for _i, m in prog.errors[:3])),
                        "partial_file": str(part),
                        "downloaded": _fmt_size(prog.total()),
                        "resumable": True,
                        "hint": "再调用一次同一条 download 即可续传"}
        else:
            # ── 2b) 单流（不支持 Range 或文件太小）──
            mode = "单流"
            if part.exists() and not resume and not ranges:
                try:
                    part.unlink()
                except Exception:
                    pass
            if not _stream_worker(info["final_url"], headers, part, retries,
                                  prog, timeout):
                return {"success": False, "url": u,
                        "error": "下载失败: " + "; ".join(
                            m for _i, m in prog.errors[:3]),
                        "partial_file": str(part) if part.exists() else "",
                        "resumable": bool(ranges)}
    except Exception as e:
        return {"success": False, "url": u, "error": f"下载异常: {e}",
                "partial_file": str(part) if part.exists() else "",
                "resumable": bool(ranges)}

    got = prog.total()
    if got == 0:
        for p in (part, _ctrl_path(part)):
            try:
                if p.exists():
                    p.unlink()
            except Exception:
                pass
        return {"success": False, "url": u, "error": "下载内容为空"}
    if total and got < total:
        _save_ctrl(part, u, total, done, segs)
        return {"success": False, "url": u,
                "error": f"文件不完整：{_fmt_size(got)}/{_fmt_size(total)}"
                         f"（已保留 .part，可续传）",
                "partial_file": str(part), "resumable": True,
                "hint": "再调用一次同一条 download 即可续传"}
    if total and got > total:
        # 服务器 Content-Length 撒谎 → 按实际大小截断
        try:
            with open(part, "r+b") as f:
                f.truncate(total)
            got = total
        except Exception:
            pass

    # ── 3) 完成：.part → 最终文件名 ──
    try:
        if dest.exists():
            dest = _unique(dest)
        part.replace(dest)
    except Exception as e:
        return {"success": False, "url": u,
                "error": f"下载完成但改名失败: {e}",
                "partial_file": str(part)}
    for p in (_ctrl_path(part),):
        try:
            if p.exists():
                p.unlink()
        except Exception:
            pass

    dt = max(0.001, time.time() - t0)
    speed = got / dt
    # ★★ 1.9.2（用户："###删除这个功能"）：**不再自动清理下载缓存**。
    #   原来这里会调 cleanup_downloads()（超 800MB 就删最旧的文件）。
    #   用户明确不要这个行为 —— "删文件"这种事不该由工具背着他做。
    #   下面那些常量与函数保留但**不再被调用**（想恢复把这两行接回去）：
    #       try: cleanup_downloads()
    #       except Exception as e: _log.debug(f"[Download] 缓存清理跳过: {e}")
    is_img = dest.suffix.lower() in _IMG_EXTS
    size_str = _fmt_size(got)
    _log.info(f"[Download] 完成: {dest} ({size_str}, {mode}, "
              f"{_fmt_size(speed)}/s)")
    return {
        "success": True,
        "file_path": str(dest),
        "file_name": dest.name,
        "file_size": size_str,
        "bytes": got,
        "is_image": is_img,
        "url": u,
        "mode": mode,
        "threads": len(segs) if segs else 1,
        "seconds": round(dt, 2),
        "speed": f"{_fmt_size(speed)}/s",
        "message": (f"🖼 图片下载完成 · {dest.name} · {size_str}"
                    if is_img else
                    f"📎 下载完成 · {dest.name} · {size_str} · {mode}"),
    }


def execute(url: str, filename: str = "", bucket: str = "websearch",
            referer: str = "", threads: int = 0, resume: bool = True,
            max_mb: int = DEFAULT_MAX_MB, action: str = "",
            job_id: str = "", **kwargs) -> str:
    """下载 URL → 返回 JSON。主窗口会自动把它推送到聊天窗口。

    ★ 1.9.2（用户批注「希望可以下载BT」）：
      · action="" 且 url 是磁力链/.torrent → 交给 aria2c 后台下载，立刻返回 job_id
      · action="bt_status" → 看 BT 任务进度
      · action="bt_stop"   → 停某个任务
      · action="bt_engine" → 看 BT 引擎（aria2c）找不找得到
    """
    act = (action or "").strip().lower()

    # ── BT 相关动作（不需要 url）──
    try:
        from src.core import torrent_engine as BT
    except Exception as e:
        BT = None
        _log.debug(f"[Download] BT 模块不可用: {e}")

    if act == "bt_engine":
        if BT is None:
            return json.dumps({"success": False,
                               "error": "BT 模块不可用"}, ensure_ascii=False, indent=2)
        info = BT.engine_info()
        info["success"] = True
        info["message"] = (f"BT 引擎：{'已找到 ' + info['path'] if info['found'] else '未找到'}"
                           + ("" if info["found"] else "\n" + info["hint"]))
        return json.dumps(info, ensure_ascii=False, indent=2)

    if act == "bt_status":
        if BT is None:
            return json.dumps({"success": False, "error": "BT 模块不可用"},
                              ensure_ascii=False, indent=2)
        return json.dumps(BT.job_status(job_id or ""), ensure_ascii=False, indent=2)

    if act == "bt_stop":
        if BT is None:
            return json.dumps({"success": False, "error": "BT 模块不可用"},
                              ensure_ascii=False, indent=2)
        if not job_id:
            return json.dumps({"success": False, "error": "bt_stop 需要 job_id"},
                              ensure_ascii=False, indent=2)
        return json.dumps(BT.stop_bt(job_id), ensure_ascii=False, indent=2)

    u = (url or "").strip()

    # ── ★ BT（磁力 / 种子）：交给 aria2c 后台跑，不阻塞工具调用 ──
    if BT is not None and BT.is_bt_target(u):
        if not BT.find_aria2():
            return json.dumps({
                "success": False, "bt": True,
                "error": ("这是一个 BT 链接，但本机没有 BT 下载引擎。\n"
                          + BT.aria2_hint()),
            }, ensure_ascii=False, indent=2)
        dest = download_dir(bucket) / "bt"
        r = BT.start_bt(u, dest)
        return json.dumps(r, ensure_ascii=False, indent=2)

    result = download_url(url, filename=filename or "",
                          bucket=bucket or "websearch",
                          referer=referer or "", threads=threads or 0,
                          resume=bool(resume), max_mb=max_mb or DEFAULT_MAX_MB)
    return json.dumps(result, ensure_ascii=False, indent=2)
