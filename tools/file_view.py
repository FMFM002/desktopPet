"""
统一文件查看器（★ 1.9.0 新增）

【为什么要有它】
以前"LLM 看文件/看图"是两条互不相干的链路：
  · 聊天里用户发的图 → 视觉识别 / 多模态直传（写在 session/main_window 里）
  · LLM 主动看文件   → file_ops.read（图片只回尺寸元信息，等于"没眼睛"）
用户要求统一成**一个文件查看工具**，并按"文件类型 + 大小 + 指定方式"自动选路：

    小文件   → 直接走多模态/视觉，让模型**如实描述**
               （禁思考、禁止编造，提示词强制"只说你真看到的"）
    大文件   → 抽样：PDF 按页、PPT 按张、文本按头尾；也可以指定具体页
    要看原文 → method="text"，本地直接抽取（不花 token、不改写）

【对外只有一个入口】
    view_file(path, method="auto", pages="", max_chars=0, prompt="") -> dict

    method: auto | multimodal | vision | stt | text     （查看方式）
    pages : ""（默认/抽样）| "3" | "3-5" | "1,4,7"      （查看位置）

返回 dict（可直接 JSON 化交给工具层）：
    {"success", "kind", "method", "pages", "text", "note", "meta"}

【可测性】vision_fn / stt_fn 可注入 —— 自测用假函数跑，
不需要真的调视觉模型或 whisper。
"""
import json
import logging
import os
import re
from pathlib import Path
from typing import Callable, Optional

_log = logging.getLogger("Tool.FileView")

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 小文件阈值：小于它就直接整份送多模态/视觉
SMALL_BYTES = 512 * 1024
# 文本原样返回的上限（超过就抽样头尾）
TEXT_FULL_CHARS = 20000
# 文档一次最多看几页（超过就抽样）
MAX_PAGES = 8
# 抽样时默认看几页
SAMPLE_PAGES = 3

DOC_EXTS = {".pdf", ".docx", ".pptx", ".xlsx"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".tiff"}
AUDIO_EXTS = {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".wma"}
VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".wmv"}
TEXT_EXTS = {".txt", ".md", ".py", ".js", ".ts", ".json", ".csv", ".log", ".html",
             ".css", ".xml", ".yml", ".yaml", ".ini", ".cfg", ".bat", ".ps1", ".sh",
             ".c", ".cpp", ".h", ".hpp", ".java", ".go", ".rs", ".sql", ".toml",
             ".gitignore", ".env.example"}

METHODS = ("auto", "multimodal", "vision", "stt", "text")

# ★ 强制"如实描述"的提示词：禁思考、禁编造、禁客套
STRICT_PROMPT = (
    "你是文件内容读取器。只输出你**真实看到**的内容；不要推测、不要补全、不要评价、"
    "不要客套、不要输出思考过程。看不清的地方写「无法辨认」。\n"
    "请严格按下面四段如实输出：\n"
    "1) 类型：照片 / 截图 / 文档扫描 / 图表 / 其它\n"
    "2) 出现的文字：逐条抄录（保留原样，不要翻译、不要改写、不要总结）\n"
    "3) 画面主体：客观描述（人物 / 物体 / 界面元素 / 排版结构）\n"
    "4) 其它可读信息：数字、表格、标题、文件名、时间戳等\n"
)


def _fmt_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / 1024 / 1024:.1f} MB"


def detect_kind(p: Path) -> str:
    """按扩展名判断文件大类"""
    ext = p.suffix.lower()
    if ext in IMAGE_EXTS:
        return "image"
    if ext in AUDIO_EXTS:
        return "audio"
    if ext in VIDEO_EXTS:
        return "video"
    if ext in DOC_EXTS:
        return ext.lstrip(".")          # pdf / docx / pptx / xlsx
    if ext in TEXT_EXTS:
        return "text"
    # 无扩展名或未知：嗅探文件头
    try:
        with open(p, "rb") as f:
            head = f.read(512)
        if head.startswith(b"%PDF"):
            return "pdf"
        if head.startswith(b"\x89PNG") or head.startswith(b"\xff\xd8\xff") \
                or head.startswith(b"GIF8"):
            return "image"
        head.decode("utf-8")
        return "text"
    except Exception:
        return "binary"


def parse_pages(spec: str, total: int) -> list:
    """把 "3" / "3-5" / "1,4,7" 解析成 1-based 页码列表（越界自动忽略）。

    spec 为空或 all → 全部（总数超过 MAX_PAGES 时由调用方决定是否抽样）。
    """
    if total <= 0:
        return []
    s = (spec or "").strip().lower()
    if not s or s in ("all", "*", "全部"):
        return list(range(1, total + 1))
    out = []
    for part in re.split(r"[,，\s]+", s):
        if not part:
            continue
        m = re.match(r"^(\d+)\s*[-~]\s*(\d+)$", part)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if a > b:
                a, b = b, a
            out.extend(range(a, b + 1))
            continue
        if part.isdigit():
            out.append(int(part))
    # 去重 + 保序 + 只留合法页
    seen, pages = set(), []
    for n in out:
        if 1 <= n <= total and n not in seen:
            seen.add(n)
            pages.append(n)
    return pages


def sample_pages(total: int, want: int = SAMPLE_PAGES) -> list:
    """抽样页码：首页 + 均匀分布的中间几页 + 末页（去重保序）"""
    if total <= want:
        return list(range(1, total + 1))
    if want <= 1:
        return [1]
    picks = {1, total}
    for i in range(1, want - 1):
        picks.add(int(round(1 + i * (total - 1) / (want - 1))))
    return sorted(picks)


def _sample_text(text: str, limit: int) -> tuple:
    """大文本抽样：保留头 2/3 + 尾 1/3，中间省略（并说明省略了多少字）"""
    if limit <= 0:
        limit = TEXT_FULL_CHARS
    if len(text) <= limit:
        return text, ""
    head = int(limit * 2 / 3)
    tail = limit - head
    omitted = len(text) - limit
    return (text[:head]
            + f"\n\n…（中间省略 {omitted} 字，需要原文请用 method=\"text\" 并 "
              f"offset/limit 分段读取，或用 pages 指定页）…\n\n"
            + text[-tail:]), f"已抽样（原文 {len(text)} 字）"


# ── 视觉 / STT（默认真实实现，可注入假函数以便自测）──

def effective_vision_mode() -> str:
    """★ 1.9.0: 图片该用哪个后端读 —— 把设置页「多模态」那块**真正接上**。

    规则（写在设置页提示里，也写进开发日志）：
      · 「多模态」开 → 用视觉大模型，具体 api/ollama 由 `multimodal_mode` 决定
      · 「多模态」关 → 听「视觉识别」那块的 `vision.mode`（api / ollama / ocr）
    这样两个开关互不干扰、又都有实际效果：开多模态＝一定走视觉大模型；
    关掉则尊重你在②里选的（可以是本地 OCR，完全不花 API）。
    """
    try:
        from src.utils.config import get_config
        v = get_config().vision
        if bool(getattr(v, "multimodal", False)):
            mm = (getattr(v, "multimodal_mode", "api") or "api").strip().lower()
            return "ollama" if mm == "ollama" else "api"
        return (getattr(v, "mode", "api") or "api").strip().lower()
    except Exception as e:
        _log.debug(f"读取视觉后端配置失败，回落 api: {e}")
        return "api"


def _default_vision(img, prompt: str) -> str:
    """调视觉客户端做**如实描述**（禁思考；后端按 effective_vision_mode 选）"""
    try:
        from src.core.llm_client import get_vision_client
        client = get_vision_client()
        fn = getattr(client, "describe_strict", None)
        if callable(fn):
            return fn(img, prompt, mode=effective_vision_mode())
        return client.analyze(img, prompt)
    except Exception as e:
        _log.warning(f"视觉描述失败: {e}")
        return f"[视觉描述失败: {e}]"


def _default_stt(path: str) -> str:
    try:
        from src.core.stt_engine import get_stt
        stt = get_stt()
        if stt is None:
            return "[STT 未启用或未就绪]"
        text = stt.transcribe_file(str(path))
        return text or "[STT 没有返回内容]"
    except Exception as e:
        _log.warning(f"STT 失败: {e}")
        return f"[STT 失败: {e}]"


def _open_image(p: Path):
    from PIL import Image
    return Image.open(str(p)).convert("RGB")


def _pdf_page_text(p: Path, page_no: int) -> str:
    import fitz
    doc = fitz.open(str(p))
    try:
        return doc[page_no - 1].get_text() or ""
    finally:
        doc.close()


def _pdf_page_image(p: Path, page_no: int, zoom: float = 2.0):
    """把 PDF 某一页渲染成图片（扫描件/图表靠这个走视觉）"""
    import fitz
    from PIL import Image
    import io as _io
    doc = fitz.open(str(p))
    try:
        pix = doc[page_no - 1].get_pixmap(matrix=fitz.Matrix(zoom, zoom))
        return Image.open(_io.BytesIO(pix.tobytes("png"))).convert("RGB")
    finally:
        doc.close()


def _pdf_page_count(p: Path) -> int:
    import fitz
    doc = fitz.open(str(p))
    try:
        return doc.page_count
    finally:
        doc.close()


def _result(success, kind, method, text="", pages=None, meta=None, note="",
            error="") -> dict:
    out = {"success": bool(success), "kind": kind, "method": method,
           "text": text or "", "pages": list(pages or []), "note": note,
           "meta": meta or {}}
    if error:
        out["error"] = error
    return out


def _with_total(meta: dict, total: int) -> dict:
    m = dict(meta or {})
    m["pages_total"] = total
    return m


def _ffprobe(path: str) -> str:
    try:
        import subprocess
        r = subprocess.run(["ffprobe", "-v", "quiet", "-print_format", "json",
                            "-show_format", "-show_streams", path],
                           capture_output=True, text=True, timeout=20)
        if r.returncode != 0:
            return "[ffprobe 不可用]"
        d = json.loads(r.stdout or "{}")
        fmt = d.get("format", {}) or {}
        dur = float(fmt.get("duration") or 0)
        streams = d.get("streams", []) or []
        vids = [s for s in streams if s.get("codec_type") == "video"]
        auds = [s for s in streams if s.get("codec_type") == "audio"]
        head = f"[视频] 时长 {dur:.1f}s"
        if vids:
            head += f"  分辨率 {vids[0].get('width')}x{vids[0].get('height')}"
        return f"{head}  视频流 {len(vids)} 条  音轨 {len(auds)} 条"
    except Exception as e:
        return f"[ffprobe 失败: {e}]"


def _view_pdf(p: Path, m: str, pages_spec: str, limit: int, prompt: str,
              vision_fn, meta: dict) -> dict:
    try:
        total = _pdf_page_count(p)
    except Exception as e:
        return _result(False, "pdf", m, meta=meta,
                       error=f"PDF 打不开（需要 PyMuPDF: pip install pymupdf）: {e}")

    explicit = bool((pages_spec or "").strip())
    if m == "text":
        sel = parse_pages(pages_spec, total)
        if not explicit and len(sel) > MAX_PAGES:
            sel = sample_pages(total)
        parts = []
        for n in sel:
            try:
                parts.append(f"── 第 {n} 页 ──\n{_pdf_page_text(p, n)}")
            except Exception as e:
                parts.append(f"── 第 {n} 页 ──\n[读取失败: {e}]")
        text, note = _sample_text("\n\n".join(parts), limit)
        return _result(True, "pdf", "text", text=text, pages=sel,
                       meta=_with_total(meta, total),
                       note=(note + " " if note else "") + f"PDF 共 {total} 页（原文方式）")

    # auto / multimodal / vision：每页先抽文本；文本太少（扫描件/图表）就渲染成图走视觉
    if explicit:
        sel = parse_pages(pages_spec, total)
        sampled = False
    else:
        sel = sample_pages(total) if total > MAX_PAGES else list(range(1, total + 1))
        sampled = total > MAX_PAGES
    parts = []
    for n in sel:
        try:
            t = (_pdf_page_text(p, n) or "").strip()
        except Exception:
            t = ""
        force_visual = m in ("multimodal", "vision")
        if len(t) >= 30 and not force_visual:
            parts.append(f"── 第 {n} 页（文本抽取）──\n{t}")
            continue
        try:
            d = vision_fn(_pdf_page_image(p, n), prompt or STRICT_PROMPT)
            tag = "视觉补充" if t else "视觉（该页文本很少）"
            parts.append(f"── 第 {n} 页（{tag}）──\n{d}")
        except Exception as e:
            parts.append(f"── 第 {n} 页 ──\n[页面渲染失败: {e}]"
                         + (f"\n文本: {t}" if t else ""))
    text, note = _sample_text("\n\n".join(parts), limit)
    note2 = (note + " " if note else "") + f"PDF 共 {total} 页；本次看第 {sel} 页"
    if sampled:
        note2 += f"（超过 {MAX_PAGES} 页已自动抽样；要看指定页传 pages=\"3\" 或 \"3-5\"）"
    return _result(True, "pdf", "multimodal" if m == "auto" else m, text=text,
                   pages=sel, meta=_with_total(meta, total), note=note2)


def _view_office(p: Path, kind: str, m: str, pages_spec: str, limit: int,
                 meta: dict) -> dict:
    explicit = bool((pages_spec or "").strip())
    try:
        if kind == "docx":
            from docx import Document
            doc = Document(str(p))
            raw = "\n".join(x.text for x in doc.paragraphs)
            text, note = _sample_text(raw, limit)
            return _result(True, kind, "text", text=text, meta=meta,
                           note=(note or "原文直读") + "（Word 无固定分页，pages 不适用）")

        if kind == "pptx":
            from pptx import Presentation
            prs = Presentation(str(p))
            total = len(prs.slides)
            sel = (parse_pages(pages_spec, total) if explicit else
                   (sample_pages(total) if total > MAX_PAGES else list(range(1, total + 1))))
            parts = []
            for n in sel:
                slide = prs.slides[n - 1]
                t = "\n".join(s.text for s in slide.shapes if hasattr(s, "text"))
                parts.append(f"── 第 {n} 张幻灯片 ──\n{t}")
            text, note = _sample_text("\n\n".join(parts), limit)
            return _result(True, kind, "text", text=text, pages=sel,
                           meta=_with_total(meta, total),
                           note=(note + " " if note else "") + f"PPT 共 {total} 张（按张查看）")

        if kind == "xlsx":
            from openpyxl import load_workbook
            wb = load_workbook(str(p), read_only=True, data_only=True)
            try:
                names = list(wb.sheetnames)
                total = len(names)
                sel = (parse_pages(pages_spec, total) if explicit else
                       (sample_pages(total) if total > MAX_PAGES else list(range(1, total + 1))))
                parts = []
                for n in sel:
                    ws = wb[names[n - 1]]
                    rows = ["\t".join("" if c is None else str(c) for c in row)
                            for row in ws.iter_rows(values_only=True)]
                    parts.append(f"── 工作表: {ws.title} ──\n" + "\n".join(rows))
                text, note = _sample_text("\n\n".join(parts), limit)
                return _result(True, kind, "text", text=text, pages=sel,
                               meta=_with_total(meta, total),
                               note=(note + " " if note else "") + f"共 {total} 个工作表（按表查看）")
            finally:
                wb.close()
    except ImportError as e:
        return _result(False, kind, m, meta=meta,
                       error=f"缺少解析库: {e}（docx→python-docx / pptx→python-pptx / xlsx→openpyxl）")
    except Exception as e:
        return _result(False, kind, m, meta=meta, error=f"解析失败: {e}")
    return _result(False, kind, m, meta=meta, error=f"不支持的类型: {kind}")


def describe_image(img, prompt: str = "", vision_fn: Optional[Callable] = None) -> str:
    """★ 1.9.0: 直接描述一张**内存里的图片**（没有文件路径的场景，例如剪贴板粘贴）。

    与 view_file 走的是同一套视觉后端与 STRICT_PROMPT，保证"看到的内容"
    在两条路径上一致。
    """
    fn = vision_fn or _default_vision
    try:
        return fn(img, prompt or STRICT_PROMPT)
    except Exception as e:
        _log.warning(f"内存图片描述失败: {e}")
        return f"[视觉描述失败: {e}]"


def view_file(path: str, method: str = "auto", pages: str = "", max_chars: int = 0,
              prompt: str = "", vision_fn: Optional[Callable] = None,
              stt_fn: Optional[Callable] = None) -> dict:
    """统一文件查看入口（详见模块 docstring）。

    参数：
      path       文件路径（相对路径按项目根解析）
      method     auto | multimodal | vision | stt | text   查看方式
      pages      ""（默认/抽样）| "3" | "3-5" | "1,4,7"      查看位置
      max_chars  文本上限（默认 TEXT_FULL_CHARS，超出自动抽样头尾）
      prompt     自定义视觉提示词（默认用 STRICT_PROMPT：禁思考、强制如实描述）
      vision_fn / stt_fn  可注入（自测用）
    """
    vision_fn = vision_fn or _default_vision
    stt_fn = stt_fn or _default_stt
    if not path:
        return _result(False, "unknown", "auto", error="path 不能为空")

    p = Path(str(path).strip().strip('"').strip("'"))
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    try:
        p = p.resolve()
    except Exception:
        pass
    if not p.exists():
        return _result(False, "unknown", "auto", error=f"文件不存在: {path}")
    if p.is_dir():
        return _result(False, "dir", "auto", error=f"这是一个目录，不是文件: {p}")

    size = p.stat().st_size
    kind = detect_kind(p)
    meta = {"path": str(p), "name": p.name, "ext": p.suffix.lower(), "kind": kind,
            "size": size, "size_str": _fmt_size(size),
            "is_small": size <= SMALL_BYTES, "small_threshold": _fmt_size(SMALL_BYTES)}
    m = (method or "auto").strip().lower()
    if m not in METHODS:
        m = "auto"
    limit = int(max_chars) if max_chars else TEXT_FULL_CHARS

    # ── 图片：小文件整份直读 ──
    if kind == "image":
        if m == "text":
            return _result(False, kind, "text", meta=meta,
                           error='图片没有文字原文；看内容请用 method="multimodal"')
        try:
            img = _open_image(p)
        except Exception as e:
            return _result(False, kind, m, meta=meta, error=f"图片打不开: {e}")
        text = vision_fn(img, prompt or STRICT_PROMPT)
        note = ("整份直读（视觉如实描述，禁思考）" if meta["is_small"]
                else f"较大图片（{meta['size_str']}）仍整份直读；如需缩小可先压缩")
        return _result(True, kind, "multimodal" if m == "auto" else m,
                       text=text, pages=[1], meta=meta, note=note)

    # ── 音频：转写 ──
    if kind == "audio":
        if m in ("multimodal", "vision"):
            return _result(False, kind, m, meta=meta,
                           error='音频没有画面；请用 method="stt" 或 text')
        return _result(True, kind, "stt", text=stt_fn(str(p)), meta=meta,
                       note="音频转写（STT）")

    # ── 视频：元信息 + 音轨转写 ──
    if kind == "video":
        info = _ffprobe(str(p))
        if m in ("stt", "auto"):
            return _result(True, kind, "stt", text=f"{info}\n\n[音轨转写]\n{stt_fn(str(p))}",
                           meta=meta, note="视频：元信息 + 音轨转写（逐帧视觉暂未实现）")
        return _result(True, kind, m, text=info, meta=meta,
                       note='视频只回了元信息；要听音轨内容用 method="stt"')

    # ── 文本类：原文（超长自动抽样头尾）──
    if kind == "text":
        try:
            raw = p.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return _result(False, kind, "text", meta=meta, error=f"读取失败: {e}")
        text, note = _sample_text(raw, limit)
        return _result(True, kind, "text", text=text, meta=meta,
                       note=note or f"原文直读（{len(raw)} 字）")

    if kind == "pdf":
        return _view_pdf(p, m, pages, limit, prompt, vision_fn, meta)
    if kind in ("docx", "pptx", "xlsx"):
        return _view_office(p, kind, m, pages, limit, meta)

    # ── 未知二进制：元信息 + 头部预览 ──
    try:
        with open(p, "rb") as f:
            head = f.read(256)
        hex_part = head[:64].hex(" ")
        printable = "".join(chr(b) if 32 <= b < 127 else "." for b in head[:128])
        return _result(True, "binary", "text",
                       text=(f"[二进制文件] {p.name}\n大小: {meta['size_str']}\n"
                             f"文件头(hex): {hex_part}\n可打印字符: {printable}"),
                       meta=meta, note="二进制文件只能给文件头预览")
    except Exception as e:
        return _result(False, "binary", "text", meta=meta, error=f"读取失败: {e}")


def view_file_json(path: str, method: str = "auto", pages: str = "",
                   max_chars: int = 0, prompt: str = "") -> str:
    """工具层用：返回 JSON 字符串"""
    return json.dumps(view_file(path, method=method, pages=pages,
                                max_chars=max_chars, prompt=prompt),
                      ensure_ascii=False, indent=2)

