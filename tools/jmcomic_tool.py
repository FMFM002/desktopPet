"""
JMComic（禁漫天堂）搜索与下载工具

基于素材项目 jmcomic_tool 移植，核心使用:
- jmcomic.create_option_by_str() 构建配置
- jmcomic.download_album_async() 下载
- JmAsyncDownloader 子类追踪进度
"""
import json
import asyncio
import logging
import threading
from pathlib import Path
from typing import Optional, Callable
from datetime import datetime

_log = logging.getLogger("Tool.JMComic")

# 文件就绪回调（由 main_window 注册，用于自动弹出文件卡片）
_on_file_ready: Optional[Callable[[str, str, str], None]] = None  # (label, path, size)


def set_file_ready_callback(callback: Optional[Callable[[str, str, str], None]]):
    """注册文件就绪回调，当 PDF/视频下载完成时调用

    ★ 1.8.10: 同时写进 tools 包的登记表 —— ToolManager 把工具文件加载成
    私有模块名 `_prts_tool_jmcomic`，而 MainWindow 注册回调时 import 的是
    `tools.jmcomic_tool`，两者是**不同的 module 对象**。
    只设本模块全局变量的话，真正在跑的那份模块读到的仍是 None，
    下载完不会弹文件卡片（register_post_load_hook 也修不了，它调的
    同样是"另一个模块"的 set_file_ready_callback）。
    """
    global _on_file_ready
    _on_file_ready = callback
    try:
        from tools import FILE_READY_CALLBACKS
        FILE_READY_CALLBACKS["jmcomic"] = callback
    except Exception:
        pass


def _get_file_ready_callback():
    """优先读 tools 包登记表（跨模块重载有效），退回本模块全局。"""
    try:
        from tools import FILE_READY_CALLBACKS
        cb = FILE_READY_CALLBACKS.get("jmcomic")
        if cb is not None:
            return cb
    except Exception:
        pass
    return _on_file_ready

TOOL_NAME = "jmcomic"
TOOL_DESCRIPTION = (
    "禁漫天堂(JM/18comic)本子搜索与下载工具。当用户提到JM、禁漫、本子搜索等关键词时使用。"
    "支持: search(按关键词搜索本子)、info(查看本子详情)、download(后台下载指定本子)、progress(查询下载进度)。"
    "注意: 仅供学习研究使用，请遵守当地法律法规。"
)
TOOL_PARAMETERS = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["search", "info", "download", "progress"],
            "description": "操作: search=搜索, info=详情, download=下载, progress=查询进度",
            "default": "search",
        },
        "keyword": {
            "type": "string",
            "description": "搜索关键词",
        },
        "album_id": {
            "type": "string",
            "description": "本子ID",
        },
        "page": {
            "type": "integer",
            "description": "页码",
            "default": 1,
        },
        "limit": {
            "type": "integer",
            "description": "返回条数，默认 10。仅当用户明确要求\"返回 X 条/看 X 个\"时才修改",
            "default": 10,
        },
    },
    "required": ["action"],
}

# 下载目录（懒加载）
_download_dir: Optional[Path] = None

# ★ 1.9.2（解耦调研的产物，与终末地 D1 同一类问题）：
#   本工具是 LAZY 策略，空闲 30 秒被 ToolManager 卸载 → 模块级变量全部重建。
#   而"下载中"是**长流程**：卸载后
#     · `_download_tasks` 变成空的新 dict → 进度查询/去重失效；
#     · `_download_lock` 变成另一把锁 → 临界区形同虚设。
#   所以任务表与锁都放进 tools 包级会话（包只导入一次，不随工具重载重建）。
#   详见 tools/__init__.py 里 get_session 与 LOGIN_SESSIONS 的说明。
try:
    from tools import get_session as _get_session
    _SESSION = _get_session("jmcomic")
except Exception:                                   # 兜底：退回模块级
    _SESSION = {"state": {}, "lock": threading.Lock()}

# 下载进度追踪（★ 包级单例，卸载重载后仍是同一份）
_download_tasks: dict[str, dict] = _SESSION.setdefault("tasks", {})
_download_lock = _SESSION["lock"]

# jmcomic 库（懒加载）
_jm = None


def _ensure_jmcomic():
    """延迟导入 jmcomic，初始化域名自动更新和代理设置"""
    global _jm
    if _jm is not None:
        return _jm

    try:
        import jmcomic as _jm
        from jmcomic import JmModuleConfig
        # 自动更新域名（内置域名可能过期）
        JmModuleConfig.FLAG_API_CLIENT_AUTO_UPDATE_DOMAIN = True
        # ★ 1.9.2 任务 11（用户批注："下载BT和P站JM工具因为VPN冲突，有没有便捷的方案"）：
        #   这里原来是硬编码的 `DEFAULT_PROXIES = None`（注释写"禁用系统代理，
        #   避免走本地代理导致下载失败"）。后果是 JM **只能**靠系统级 VPN 才能上 ——
        #   而系统级 VPN 一开，BT 就完了（BT 的 peer 连接协议层不支持代理，
        #   且代理出口是机房 IP、tracker 会拒），这就是你遇到的冲突。
        #
        #   现在改成**可配置的应用级代理**：读 .env 的 PRTS_PROXY（兼容
        #   PIXIV_PROXY），配了就只让 JM 自己走这个本地代理端口，
        #   于是**不用开系统 VPN**，BT 也能直连。原理见 src/core/net_proxy.py。
        try:
            from src.core.net_proxy import proxies_dict as _pd
            _proxies = _pd()
            JmModuleConfig.DEFAULT_PROXIES = _proxies or None
            _log.info("JMComic 代理: %s",
                      _proxies.get("http") if _proxies else "直连（未配 PRTS_PROXY）")
        except Exception as _e:
            JmModuleConfig.DEFAULT_PROXIES = None
            _log.debug("读代理配置失败，JM 走直连: %s", _e)
        _log.info("JMComic 加载成功 (v%s)", getattr(_jm, '__version__', 'unknown'))
        return _jm
    except ImportError as e:
        # ★ 1.6.0: 把真实原因打出来 —— 以前任何 ImportError 都报"未安装"，
        #   实际可能是 jmcomic 自身的子模块导入失败（例如缺 pyyaml / curl_cffi），
        #   导致误判成没装库、让用户白跑 pip install
        import traceback
        _log.error("jmcomic 导入失败: %s: %s", type(e).__name__, e)
        _log.debug("导入失败详情:\n%s", traceback.format_exc())
        return None
    except Exception as e:
        import traceback
        _log.error("jmcomic 初始化失败: %s: %s", type(e).__name__, e)
        _log.debug("初始化失败详情:\n%s", traceback.format_exc())
        return None


def _get_download_dir() -> Path:
    """获取下载目录"""
    global _download_dir
    if _download_dir is not None:
        return _download_dir
    try:
        from src.utils.config import get_config
        cfg = get_config()
        base = cfg.tools.download_path
        d = Path(base) if base else Path(__file__).resolve().parent.parent / "data" / "downloads"
    except Exception:
        d = Path(__file__).resolve().parent.parent / "data" / "downloads"
    _download_dir = d / "jmcomic"
    _download_dir.mkdir(parents=True, exist_ok=True)
    return _download_dir


def _build_option(download_dir: Path) -> Optional[object]:
    """
    通过 YAML 字符串动态创建 JmOption 配置。
    路径用正斜杠，避免 Windows 反斜杠导致 YAML 转义错误。
    """
    jm = _ensure_jmcomic()
    if not jm:
        return None

    download_dir.mkdir(parents=True, exist_ok=True)
    yml_dir = str(download_dir).replace("\\", "/")

    yml_str = f"""
dir_rule:
  rule: Bd_Aid
  base_dir: "{yml_dir}"

download:
  image:
    suffix: .jpg
    decode: true
  thread: 5
  chapter_thread: 2
  retry_times: 5
  skip_existing: true

client:
  impl: api
  domain: ""
  timeout: 90

log:
  level: INFO
"""
    try:
        option = jm.create_option_by_str(yml_str)
        _log.debug("JmOption 构建完成: %s", yml_dir)
        return option
    except Exception as e:
        _log.error("构建 JmOption 失败: %s", e)
        return None


# ==================== 搜索 ====================

def _search(keyword: str, page: int = 1, limit: int = 10) -> dict:
    if not keyword:
        return {"success": False, "error": "请提供搜索关键词"}

    jm = _ensure_jmcomic()
    if not jm:
        return {"success": False, "error": "jmcomic 库未加载。执行: pip install jmcomic"}

    try:
        client = jm.JmOption.default().build_jm_client()
        search_page = client.search_site(search_query=keyword.strip(), page=page)

        if search_page.is_single_album:
            album = search_page.single_album
            album_info = {
                "id": album.album_id,
                "title": album.name[:80],
                "author": ", ".join(album.authors) if album.authors else "未知",
                "tags": list(album.tags)[:10] if album.tags else [],
                "page_count": album.page_count,
            }
            return {"success": True, "keyword": keyword, "page": page,
                    "total": 1, "albums": [album_info]}

        albums = []
        if hasattr(search_page, 'content'):
            for aid, ainfo in search_page.content:
                tags = ainfo.get('tags', [])
                if not tags:
                    cat = ainfo.get('category', {})
                    subcat = ainfo.get('category_sub', {})
                    if isinstance(cat, dict) and cat.get('title'):
                        tags.append(cat['title'])
                    if isinstance(subcat, dict) and subcat.get('title'):
                        tags.append(subcat['title'])
                    author = ainfo.get('author', '')
                    if author:
                        tags.append(author)
                albums.append({
                    "id": str(aid),
                    "title": ainfo.get('name', '')[:80],
                    "tags": tags[:5],
                })
        total = search_page.total if hasattr(search_page, 'total') else len(albums)

        return {"success": True, "keyword": keyword, "page": page,
                "total": total, "albums": albums[:max(1, min(int(limit or 10), 50))]}
    except Exception as e:
        return {"success": False, "error": f"搜索失败: {e}"}


# ==================== 详情 ====================

def _info(album_id: str) -> dict:
    if not album_id:
        return {"success": False, "error": "请提供 album_id（本子ID）"}

    jm = _ensure_jmcomic()
    if not jm:
        return {"success": False, "error": "jmcomic 库未加载"}

    try:
        client = jm.JmOption.default().new_jm_client()
        album = client.get_album_detail(album_id)
        return {
            "success": True,
            "album": {
                "id": str(album.id),
                "title": album.title,
                "author": getattr(album, 'author', '未知'),
                "description": getattr(album, 'description', '')[:300],
                # ★ 1.8.10: JmAlbumDetail 的属性名是 tags / page_count
                #   （__len__ 是章节数）。原来写的 tag_list / photo_count
                #   在库里根本不存在，getattr 又给了默认值 → 静默返回空。
                "tags": getattr(album, 'tags', []),
                "pages": getattr(album, 'page_count', 0),
                "likes": getattr(album, 'likes', 0),
                "views": getattr(album, 'views', 0),
            }
        }
    except Exception as e:
        return {"success": False, "error": f"查询详情失败: {e}"}


# ==================== 下载（核心） ====================

def _download(album_id: str) -> dict:
    """后台下载本子 — 使用 download_album_async + JmAsyncDownloader"""
    if not album_id:
        return {"success": False, "error": "请提供 album_id"}

    jm = _ensure_jmcomic()
    if not jm:
        return {"success": False, "error": "jmcomic 库未加载"}

    album_id = str(album_id).strip()
    if not album_id.isdigit():
        return {"success": False, "error": f"album_id 必须为纯数字（当前: {album_id}）"}

    # 检查是否正在下载
    with _download_lock:
        existing = _download_tasks.get(album_id)
        if existing and existing.get("status") == "downloading":
            t = existing
            done = t.get("done_photos", 0)
            total = t.get("total_photos", 0)
            return {
                "success": True,
                "album_id": album_id,
                "message": f"⏳ 该本子正在下载中（{done}/{total} 章），可使用 progress 查询进度",
                "status": "downloading",
            }

    task_id = f"jm_{album_id}_{int(datetime.now().timestamp())}"
    save_dir = _get_download_dir()

    with _download_lock:
        _download_tasks[album_id] = {
            "task_id": task_id,
            "album_id": album_id,
            "status": "downloading",
            "album_name": "",
            "total_photos": 0,
            "done_photos": 0,
            "current_photo_name": "",
            "photo_index": 0,
            "started_at": datetime.now().strftime("%H:%M:%S"),
            "save_dir": str(save_dir),
        }

    def _do_download():
        album_name = ""
        try:
            option = _build_option(save_dir)
            if not option:
                with _download_lock:
                    _download_tasks[album_id]["status"] = "failed"
                    _download_tasks[album_id]["error"] = "构建 JmOption 失败"
                return

            _downloaded_album = None

            # 创建带进度追踪的下载器（继承 JmAsyncDownloader）
            class ProgressDownloader(jm.JmAsyncDownloader):
                async def before_album(self, album):
                    nonlocal _downloaded_album
                    _downloaded_album = album
                    await super().before_album(album)
                    with _download_lock:
                        if album_id in _download_tasks:
                            _download_tasks[album_id].update({
                                "album_name": album.name,
                                "total_photos": len(album),
                                "total_images": getattr(album, 'page_count', 0),
                                "done_photos": 0,
                            })
                    _log.info("下载开始: %s (%s 章, %s 页)",
                              album.name, len(album), getattr(album, 'page_count', 0))

                async def after_album(self, album):
                    await super().after_album(album)
                    with _download_lock:
                        if album_id in _download_tasks:
                            _download_tasks[album_id]["status"] = "merging"
                            _download_tasks[album_id]["finished_at"] = datetime.now().strftime("%H:%M:%S")
                    _log.info("图片下载完成: %s，开始合成 PDF...", album.name)

                async def before_photo(self, photo):
                    await super().before_photo(photo)
                    with _download_lock:
                        if album_id in _download_tasks:
                            _download_tasks[album_id].update({
                                "current_photo_name": photo.name,
                                "photo_index": photo.index,
                            })
                    _log.info("  章节 [%s/%s] %s (%s 张)",
                              photo.index, len(photo.from_album), photo.name, len(photo))

                async def after_photo(self, photo):
                    await super().after_photo(photo)
                    with _download_lock:
                        if album_id in _download_tasks:
                            _download_tasks[album_id]["done_photos"] += 1

            # 在独立事件循环中运行异步下载
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                album, downloader = loop.run_until_complete(
                    jm.download_album_async(
                        album_id,
                        option=option,
                        downloader=ProgressDownloader,
                    )
                )
            finally:
                loop.close()

            # ---- 下载完成后自动合成 PDF ----
            if album:
                album_name = getattr(album, 'name', '') or getattr(album, 'title', '')
                _synthesize_pdf(album, save_dir, album_id)

        except Exception as e:
            # 即使下载异常，也尝试为已下载的图片生成 PDF
            if album_name:
                try:
                    _synthesize_pdf_from_dir(save_dir, album_name, album_id)
                except Exception:
                    pass
            with _download_lock:
                _download_tasks[album_id]["status"] = "failed"
                _download_tasks[album_id]["error"] = str(e)[:300]
            _log.error("JM下载失败: %s: %s", album_id, e)

    threading.Thread(target=_do_download, daemon=True).start()

    return {
        "success": True,
        "task_id": task_id,
        "album_id": album_id,
        "message": f"已开始后台下载本子 {album_id}，下载完成后可在 {save_dir} 查看",
        "save_dir": str(save_dir),
    }


# ==================== PDF 合成 ====================

def _find_downloaded_dir(base_dir: Path, album_name: str, album_id: str) -> Optional[Path]:
    """根据 dir_rule(Bd_Aid) 查找下载目录。Bd_Aid 即直接以 album_id 为目录名"""
    # 直接匹配 album_id
    direct = base_dir / album_id
    if direct.is_dir():
        return direct
    # 兼容命名规则带名字的目录
    safe_name = album_name.replace('/', '_').replace('\\', '_').replace(':', '_').replace('?', '_').strip()
    for candidate in (
        base_dir / f"{safe_name}_{album_id}",
        base_dir / f"{album_name}_{album_id}",
    ):
        if candidate.is_dir():
            return candidate
    # 模糊搜索
    for d in sorted(base_dir.iterdir()):
        if d.is_dir() and (d.name == album_id or d.name.endswith(f"_{album_id}")):
            return d
    return None


def _collect_images(album_dir: Path) -> list[Path]:
    """递归收集目录下所有图片文件并按文件名排序"""
    imgs: list[Path] = []
    for ext in ("*.jpg", "*.jpeg", "*.png", "*.webp", "*.bmp"):
        imgs.extend(album_dir.rglob(ext))
    # 按路径排序（保证章节顺序）
    imgs.sort(key=lambda p: (p.parent.name, p.name))
    return imgs


def _synthesize_pdf(album, base_dir: Path, album_id: str):
    """从 JM album 对象找到下载目录并合成 PDF"""
    album_name = getattr(album, 'name', '') or getattr(album, 'title', '')
    _synthesize_pdf_from_dir(base_dir, album_name, album_id)


def _synthesize_pdf_from_dir(base_dir: Path, album_name: str, album_id: str):
    """从下载目录收集图片并合成 PDF"""
    album_dir = _find_downloaded_dir(base_dir, album_name, album_id)
    if not album_dir:
        _log.warning("未找到下载目录，跳过 PDF 合成")
        with _download_lock:
            if album_id in _download_tasks:
                _download_tasks[album_id]["status"] = "completed"
                _download_tasks[album_id]["pdf"] = ""
        return

    imgs = _collect_images(album_dir)
    if not imgs:
        _log.warning("下载目录中无图片，跳过 PDF 合成: %s", album_dir)
        with _download_lock:
            if album_id in _download_tasks:
                _download_tasks[album_id]["status"] = "completed"
                _download_tasks[album_id]["pdf"] = ""
        return

    from src.utils.img2pdf_convert import images_to_pdf

    pdf_dir = base_dir / "PDFs"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    safe_name = album_name.replace('/', '_').replace('\\', '_').replace(':', '_').replace('?', '_').strip()
    pdf_path = pdf_dir / f"{safe_name}_{album_id}.pdf"

    _log.info("开始合成 PDF: %s (%s 张图片)", pdf_path.name, len(imgs))

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(images_to_pdf(imgs, pdf_path))
    finally:
        loop.close()

    pdf_size = pdf_path.stat().st_size / (1024 * 1024) if pdf_path.exists() else 0
    _log.info("PDF 合成完成: %s (%.1f MB)", pdf_path.name, pdf_size)

    size_str = f"{pdf_size:.1f} MB" if pdf_size >= 1 else f"{pdf_size * 1024:.0f} KB"
    # 通知 UI 弹出文件卡片
    # ★ 1.8.10: 走 _get_file_ready_callback()（包级登记表优先），
    #   否则本模块全局在 ToolManager 的私有模块里始终是 None。
    _cb = _get_file_ready_callback()
    if _cb:
        try:
            _cb(f"📚 {safe_name}", str(pdf_path), size_str)
        except Exception as ex:
            _log.debug(f"文件就绪回调失败: {ex}")

    with _download_lock:
        if album_id in _download_tasks:
            _download_tasks[album_id]["status"] = "completed"
            _download_tasks[album_id]["pdf"] = str(pdf_path)
            _download_tasks[album_id]["pdf_size_mb"] = round(pdf_size, 1)
            _download_tasks[album_id]["image_dir"] = str(album_dir)


# ==================== 进度查询 ====================

def _progress() -> dict:
    with _download_lock:
        tasks = dict(_download_tasks)

    active_tasks = []
    for album_id, task in tasks.items():
        if task["status"] in ("downloading", "merging"):
            total_p = task.get("total_photos", 0) or 1
            done_p = task.get("done_photos", 0)
            st = "🔄 下载中" if task["status"] == "downloading" else "📦 合成PDF中"
            active_tasks.append({
                "task_id": task.get("task_id", ""),
                "album_id": album_id,
                "album_name": task.get("album_name", ""),
                "status": st,
                "progress": f"{done_p}/{total_p} 章",
                "current": task.get("current_photo_name", ""),
                "started_at": task.get("started_at", ""),
            })

    if not active_tasks:
        completed = []
        for aid, t in list(tasks.items())[-5:]:
            if t["status"] not in ("completed", "failed"):
                continue
            entry = {
                "task_id": t.get("task_id", ""),
                "album_id": aid,
                "album_name": t.get("album_name", ""),
                "status": t["status"],
                "save_dir": t.get("save_dir", ""),
            }
            if t["status"] == "completed":
                entry["pdf"] = t.get("pdf", "")
                entry["pdf_size_mb"] = t.get("pdf_size_mb", 0)
                entry["image_dir"] = t.get("image_dir", "")
            elif t["status"] == "failed":
                entry["error"] = t.get("error", "")
            completed.append(entry)
        return {"success": True, "active_tasks": [], "completed_tasks": completed,
                "message": "没有进行中的下载任务"}

    return {"success": True, "active_tasks": active_tasks,
            "message": f"共 {len(active_tasks)} 个下载任务进行中"}


# ==================== 入口 ====================

def execute(action: str = "search", keyword: str = "", album_id: str = "",
            page: int = 1, limit: int = 10, **kwargs) -> str:
    _log.info("JMComic工具: %s", action)

    handlers = {
        "search": lambda: _search(keyword, page, limit),
        "info": lambda: _info(album_id),
        "download": lambda: _download(album_id),
        "progress": _progress,
    }

    handler = handlers.get(action)
    if handler is None:
        result = {"success": False, "error": f"未知操作: {action}"}
    else:
        try:
            result = handler()
        except Exception as e:
            _log.error("JMComic 工具执行失败: %s", e)
            result = {"success": False, "error": str(e)}

    return json.dumps(result, ensure_ascii=False, indent=2)
