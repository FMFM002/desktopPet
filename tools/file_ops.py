"""
文件操作工具集 — 读/写/编辑/搜索/列表文件
素材来源: 提取终端工具部分/file_ops.py (codex-rs 移植)
"""
import os
import re
import difflib
import fnmatch
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# ═══════════════════════════════════════════════════════════════════
# 安全沙箱：用户电脑全盘只读，仅 data/桌宠私有data 允许读写
#   - 读（read/search/list）：任意路径均允许（只读 = 可读取不可写入）
#   - 写（write/edit）：仅允许写入 WRITABLE_ROOT，其余一律拒绝
# ═══════════════════════════════════════════════════════════════════
WRITABLE_ROOT = PROJECT_ROOT / "data" / "桌宠私有data"
_PROTECTED_FILES = {".env", "config.json", "main.py", "requirements.txt", "cookies.txt"}


def _normalize(p: str) -> Path:
    return Path(os.path.normpath(os.path.abspath(p)))


def _is_within_writable(p: str) -> bool:
    """判断路径是否位于沙箱可写目录（data/桌宠私有data）内"""
    try:
        np = _normalize(p)
    except Exception:
        return False
    try:
        np.resolve().relative_to(WRITABLE_ROOT.resolve())
        return True
    except Exception:
        pass
    return np == WRITABLE_ROOT


def _check_write_protection(full_path: str, raw_path: str = "") -> str | None:
    """检查写保护。返回 None 表示允许，返回字符串表示被拒绝的原因。"""
    # 关键配置文件始终禁止修改
    if os.path.basename(full_path) in _PROTECTED_FILES:
        return f"[拒绝] 禁止修改关键配置文件: {os.path.basename(full_path)}"
    # 沙箱：仅 WRITABLE_ROOT 内允许写入
    if not _is_within_writable(full_path):
        # ★★ 1.9.3 第 25 轮（真机教训）：**没给 path 时不要再报"只读模式"**。
        #   现场（logs/prts_20261004_025634.log）：
        #     `file_ops({'action': 'write', 'content': '...'})`  ← 忘了 path
        #     → 路径解析成 "." → 落到项目根外面 → 回了一句
        #     "[拒绝] 沙箱限制：当前为只读模式，仅允许写入…不允许写入: ."
        #   模型看到"只读模式"完全猜不到真正的原因是**自己漏了 path**，
        #   白烧一轮（那次已经是第 20 轮，直接撞上预算）。
        if not (raw_path or "").strip():
            return ("[拒绝] write 缺少 `path`：你只给了 content。\n"
                    "请补上 path，例如 "
                    "\"data/桌宠私有data/xxx/build.py\"（只能写在「桌宠私有data」目录下）。")
        # ★ 1.6.0: 跨盘符/项目外路径曾让 relpath 抛异常，直接把原始异常抛给 LLM 很难懂，
        #   这里统一成可读的沙箱说明
        try:
            rel = os.path.relpath(full_path, str(PROJECT_ROOT))
        except Exception:
            rel = full_path
        # ★★ 1.9.3 第 27 轮：**别再写"当前为只读模式"** —— 那句话把模型带沟里了。
        #   真机现场（logs/prts_20261004_034150.log）：模型想写
        #   `临时文件\_scratch_dev\probe_nahida.py`，收到"当前为只读模式"后，
        #   在思考里得出错误结论：「file_ops is restricted to write in 桌宠私有data
        #   (read-only mode, only allows writing to that dir)」，接着开始怀疑
        #   "file_ops 是不是根本不能写"，白烧好几轮。
        #   事实是"**只有**桌宠私有data 能写"，不是"哪都不能写"。改成说人话。
        return (
            f"[拒绝] 只能写在「桌宠私有data」目录里，别的地方（包括项目根、临时文件、源码目录）"
            f"一律不行。\n"
            f"  可写目录: {WRITABLE_ROOT}\n"
            f"  你给的路径: {rel}\n"
            f"  改写成: data/桌宠私有data/<你的文件名>（例如 桌宠私有data/结果.txt）再试。"
        )
    return None


# ═══════════════════════════════════════════════════════════════════
# 底层操作函数
# ═══════════════════════════════════════════════════════════════════

def _resolve_path(path: str) -> str:
    """解析路径，相对路径基于项目根目录"""
    if os.path.isabs(path):
        return path
    return os.path.normpath(os.path.join(str(PROJECT_ROOT), path))


def _make_diff(old: str, new: str, path: str) -> str:
    """生成unified diff"""
    old_lines = old.splitlines(keepends=True)
    new_lines = new.splitlines(keepends=True)
    diff_lines = list(difflib.unified_diff(
        old_lines, new_lines,
        fromfile=f"a/{path}", tofile=f"b/{path}",
        lineterm=""
    ))
    return "".join(line + "\n" for line in diff_lines) if diff_lines else "(无变化)"


# ═══════════════════════════════════════════════════════════════════
# 1. file_read — 读取文件
# ═══════════════════════════════════════════════════════════════════

TOOL_READ = "file_read"
TOOL_READ_DESC = ("读取文件内容，支持指定行号范围。返回带行号的内容。"
                  "支持任意类型文件：纯文本/代码直接读取；PDF/Word(.docx)/PPT(.pptx)/Excel(.xlsx) "
                  "自动抽取文本；图片返回尺寸等元信息。")
TOOL_READ_PARAMS = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "文件路径（相对或绝对）"},
        "offset": {"type": "integer", "description": "起始行号（从1开始，可选）"},
        "limit": {"type": "integer", "description": "读取行数（可选，默认全部）"},
    },
    "required": ["path"],
}

# ★ 1.8.0: 文档抽取引擎支持的扩展名（缺库时给出安装提示而不是乱码）
_DOC_EXTRACTORS = {".pdf", ".docx", ".pptx", ".xlsx"}
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".tiff"}


def _extract_document_text(full_path: str, ext: str) -> str:
    """抽取 Office/PDF 文档的文本内容（按扩展名选择引擎）"""
    if ext == ".pdf":
        import fitz  # PyMuPDF
        doc = fitz.open(full_path)
        try:
            return "\n".join(page.get_text() for page in doc)
        finally:
            doc.close()
    if ext == ".docx":
        from docx import Document
        doc = Document(full_path)
        return "\n".join(p.text for p in doc.paragraphs)
    if ext == ".pptx":
        from pptx import Presentation
        prs = Presentation(full_path)
        slides = []
        for slide in prs.slides:
            slides.append("\n".join(s.text for s in slide.shapes if hasattr(s, "text")))
        return "\n---\n".join(slides)
    if ext == ".xlsx":
        from openpyxl import load_workbook
        wb = load_workbook(full_path, read_only=True, data_only=True)
        try:
            parts = []
            for ws in wb.worksheets:
                rows = ["\t".join("" if c is None else str(c) for c in row)
                        for row in ws.iter_rows(values_only=True)]
                parts.append(f"[工作表: {ws.title}]\n" + "\n".join(rows))
            return "\n\n".join(parts)
        finally:
            wb.close()
    raise ValueError(f"不支持的文档类型: {ext}")


def _image_meta(path_str: str, full_path: str) -> str:
    """图片文件：返回尺寸等元信息（图片内容本身应通过视觉识别/多模态获取）"""
    size = os.path.getsize(full_path)
    try:
        from PIL import Image
        with Image.open(full_path) as im:
            dims = f"{im.width}x{im.height}"
            fmt = im.format or "未知"
    except Exception:
        dims, fmt = "未知", "未知"
    return (f"[图片文件] {path_str}\n尺寸: {dims} 格式: {fmt} 大小: {size/1024:.1f}KB\n"
            f"提示: 图片的视觉内容请走多模态直传或视觉识别，这里只能拿到元信息。")


def _binary_sniff(path_str: str, full_path: str) -> str:
    """未知二进制：元信息 + 文件头预览（十六进制 + 可打印字符）"""
    size = os.path.getsize(full_path)
    with open(full_path, "rb") as f:
        head = f.read(256)
    hex_part = head[:64].hex(" ")
    printable = "".join(chr(b) if 32 <= b < 127 else "." for b in head[:128])
    return (f"[二进制文件] {path_str}\n大小: {size/1024:.1f}KB\n"
            f"文件头(hex): {hex_part}\n"
            f"可打印字符: {printable}\n"
            f"提示: 这是二进制文件，以上是头部预览。")


def file_read(path: str, offset: int = 0, limit: int = 0) -> str:
    full_path = _resolve_path(path)

    if not os.path.exists(full_path):
        return f"[未找到] {path}: 文件不存在"
    if not os.path.isfile(full_path):
        return f"[错误] {path}: 不是文件"
    if os.path.getsize(full_path) > 5 * 1024 * 1024:
        return f"[错误] {path}: 文件过大 (>5MB)，请用更小的offset/limit读取"

    ext = os.path.splitext(full_path)[1].lower()

    # ★ 1.8.0: 图片 → 元信息
    if ext in _IMAGE_EXTS:
        return _image_meta(path, full_path)

    # ★ 1.8.0: Office/PDF → 文本抽取
    if ext in _DOC_EXTRACTORS:
        try:
            content = _extract_document_text(full_path, ext)
        except ImportError as e:
            return (f"[缺少依赖] 读取 {ext} 需要 {e.name}（pip install "
                    f"{'PyMuPDF' if ext == '.pdf' else 'python-docx' if ext == '.docx' else 'python-pptx' if ext == '.pptx' else 'openpyxl'}）")
        except Exception as e:
            return f"[错误] 读取 {path}: {e}"
        lines = content.splitlines()
        total = len(lines)
        start = max(0, offset - 1) if offset > 0 else 0
        end = min(total, start + limit) if limit > 0 else total
        result = [f"     {i+1} {line}" for i, line in enumerate(lines[start:end], start=start)]
        header = f"[文档抽取成功] {path} (共 {total} 行"
        if start > 0 or end < total:
            header += f"，当前 {start+1}-{end} 行"
        header += ")\n"
        return header + "\n".join(result) if result else header + "(该范围无内容)"

    # 纯文本 / 未知扩展名：先嗅探是否二进制
    try:
        with open(full_path, "rb") as f:
            head = f.read(4096)
        if b"\x00" in head:
            return _binary_sniff(path, full_path)
    except Exception:
        pass

    try:
        with open(full_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except Exception as e:
        return f"[错误] 读取 {path}: {e}"

    total = len(lines)
    start = max(0, offset - 1) if offset > 0 else 0
    end = min(total, start + limit) if limit > 0 else total

    # 添加行号
    result_lines = [f"[{path} (行 {start+1}-{end} / 共{total}行)]"]
    for i in range(start, end):
        result_lines.append(f"{i+1:4d}| {lines[i].rstrip()}")

    if end < total:
        result_lines.append(f"... (还有 {total - end} 行未显示)")

    return "\n".join(result_lines)


# ═══════════════════════════════════════════════════════════════════
# 2. file_write — 写入文件
# ═══════════════════════════════════════════════════════════════════

TOOL_WRITE = "file_write"
TOOL_WRITE_DESC = "创建或覆盖文件。⚠ 会实际修改文件系统。"
TOOL_WRITE_PARAMS = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "文件路径"},
        "content": {"type": "string", "description": "要写入的内容"},
    },
    "required": ["path", "content"],
}


def file_write(path: str, content: str) -> str:
    full_path = _resolve_path(path)

    # 自保护: 禁止写入项目源码（★ 把原始 path 也传进去：漏给 path 时要报"漏了 path"，
    #   而不是含糊的"只读模式"—— 见 _check_write_protection 的说明）
    err = _check_write_protection(full_path, path)
    if err:
        return err

    try:
        os.makedirs(os.path.dirname(full_path) or ".", exist_ok=True)
        old = ""
        if os.path.exists(full_path):
            with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                old = f.read()

        with open(full_path, "w", encoding="utf-8") as f:
            f.write(content)

        diff = _make_diff(old, content, path) if old else ""
        size = len(content.encode("utf-8"))
        return f"✅ 已写入 {path} ({size} 字节)" + (f"\n\nDiff:\n{diff}" if diff else "")

    except Exception as e:
        return f"[错误] 写入 {path}: {e}"


# ═══════════════════════════════════════════════════════════════════
# 3. file_edit — 精确编辑文件
# ═══════════════════════════════════════════════════════════════════

TOOL_EDIT = "file_edit"
TOOL_EDIT_DESC = (
    "精确查找替换编辑文件。old_string必须在文件中唯一匹配（否则会报错）。"
    "⚠ 会实际修改文件系统。"
)
TOOL_EDIT_PARAMS = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "文件路径"},
        "old_string": {"type": "string", "description": "要替换的文本（必须完整匹配，包括空格）"},
        "new_string": {"type": "string", "description": "替换后的文本"},
    },
    "required": ["path", "old_string", "new_string"],
}


def file_edit(path: str, old_string: str, new_string: str) -> str:
    full_path = _resolve_path(path)

    if not os.path.exists(full_path):
        return f"[错误] {path}: 文件不存在"

    # 自保护: 禁止编辑项目源码（同 file_write，带原始 path）
    err = _check_write_protection(full_path, path)
    if err:
        return err

    try:
        with open(full_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except Exception as e:
        return f"[错误] 读取 {path}: {e}"

    count = content.count(old_string)
    if count == 0:
        return f"[错误] old_string在文件中未找到。请确保文本完全匹配（包括空格、换行）。"
    if count > 1:
        # 显示上下文帮助定位
        lines = content.split("\n")
        contexts = []
        for i, line in enumerate(lines):
            if old_string.split("\n")[0].strip() in line.strip():
                ctx_start = max(0, i - 2)
                ctx_end = min(len(lines), i + 3)
                contexts.append(
                    f"  出现位置 {len(contexts)+1} (行{i+1}):\n" +
                    "\n".join(f"  {j+1:4d}| {lines[j]}" for j in range(ctx_start, ctx_end))
                )
                if len(contexts) >= 5:
                    break
        ctx_info = "\n\n".join(contexts) if contexts else ""
        return (
            f"[错误] old_string在文件中出现了{count}次，必须唯一匹配。"
            f"请添加更多上下文使old_string唯一。\n"
            f"提示: 可以在file_read中查看更多上下文。{ctx_info}"
        )

    new_content = content.replace(old_string, new_string, 1)
    try:
        with open(full_path, "w", encoding="utf-8") as f:
            f.write(new_content)
    except Exception as e:
        return f"[错误] 写入 {path}: {e}"

    diff = _make_diff(content, new_content, path)
    return f"✅ 已编辑 {path}\n\nDiff:\n{diff}"


# ═══════════════════════════════════════════════════════════════════
# 4. search_files — 代码搜索
# ═══════════════════════════════════════════════════════════════════

TOOL_SEARCH = "search_files"
TOOL_SEARCH_DESC = "在项目中搜索文件内容，支持正则表达式。返回匹配的文件、行号和内容。"
TOOL_SEARCH_PARAMS = {
    "type": "object",
    "properties": {
        "pattern": {"type": "string", "description": "搜索模式（支持正则表达式）"},
        "path": {"type": "string", "description": "搜索路径（可选，默认项目根目录）"},
        "file_types": {"type": "string", "description": "过滤文件类型，逗号分隔如 '.py,.json,.md'（可选）"},
        "case_sensitive": {"type": "boolean", "description": "是否区分大小写（默认否）"},
    },
    "required": ["pattern"],
}

EXCLUDE_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", "target",
                "logs", ".codebuddy", "-p"}
BINARY_EXTS = {".exe", ".dll", ".so", ".o", ".pyc", ".pyd",
               ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".bmp",
               ".zip", ".tar", ".gz", ".7z", ".rar",
               ".mp3", ".wav", ".ogg", ".mp4", ".avi",
               ".ttf", ".otf", ".woff", ".woff2"}


def search_files(pattern: str, path: str = "", file_types: str = "",
                 case_sensitive: bool = False) -> str:
    """搜索文件内容"""
    search_root = _resolve_path(path) if path else str(PROJECT_ROOT)

    flags = 0 if case_sensitive else re.IGNORECASE
    try:
        regex = re.compile(pattern, flags)
    except re.error as e:
        return f"[错误] 无效的正则表达式 '{pattern}': {e}"

    type_list = [t.strip() for t in file_types.split(",") if t.strip()] if file_types else []
    matches = []
    files_searched = 0
    MAX_MATCHES = 100

    for root, dirs, files in os.walk(search_root):
        # 排除目录
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS and not d.startswith(".")]

        for fname in files:
            ext = os.path.splitext(fname)[1].lower()
            if ext in BINARY_EXTS:
                continue
            if type_list and ext not in type_list:
                continue

            full_path = os.path.join(root, fname)
            rel_path = os.path.relpath(full_path, search_root)
            files_searched += 1

            try:
                with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                    for lno, line in enumerate(f, 1):
                        if regex.search(line):
                            matches.append(f"{rel_path}:{lno}: {line.rstrip()[:200]}")
                            if len(matches) >= MAX_MATCHES:
                                break
            except (OSError, UnicodeDecodeError):
                continue

            if len(matches) >= MAX_MATCHES:
                break
        if len(matches) >= MAX_MATCHES:
            break

    lines = [f"搜索 '{pattern}' — 找到 {len(matches)} 个匹配 (搜索了{files_searched}个文件)"]
    if matches:
        lines.append("")
        lines.extend(matches[:50])
        if len(matches) > 50:
            lines.append(f"... 还有 {len(matches) - 50} 个匹配未显示")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════
# 5. list_dir — 列出目录
# ═══════════════════════════════════════════════════════════════════

TOOL_LIST = "list_dir"
TOOL_LIST_DESC = "列出目录中的文件和子目录。"
TOOL_LIST_PARAMS = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "目录路径（可选，默认项目根目录）"},
        "pattern": {"type": "string", "description": "文件名过滤模式，如 '*.py'（可选）"},
    },
    "required": [],
}


def list_dir(path: str = "", pattern: str = "*") -> str:
    full_path = _resolve_path(path) if path else str(PROJECT_ROOT)

    if not os.path.isdir(full_path):
        return f"[错误] {path}: 不是目录"

    try:
        entries = os.listdir(full_path)
    except PermissionError:
        return f"[错误] {path}: 权限不足"

    files, dirs = [], []
    for entry in sorted(entries):
        if entry.startswith("."):
            continue
        if pattern != "*" and not fnmatch.fnmatch(entry, pattern):
            continue
        full_entry = os.path.join(full_path, entry)
        if os.path.isdir(full_entry):
            dirs.append(f"📁 {entry}/")
        else:
            size = os.path.getsize(full_entry)
            size_str = f"{size:,}B" if size < 1024 else f"{size/1024:.1f}KB"
            files.append(f"📄 {entry} ({size_str})")

    lines = [f"[{path or '.'}] — {len(dirs)} 目录, {len(files)} 文件"]
    if dirs:
        lines.append("\n--- 目录 ---")
        lines.extend(dirs)
    if files:
        lines.append("\n--- 文件 ---")
        lines.extend(files)
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════
# 文件发送 & 下载文件列表 — 供 LLM 发送文件到聊天窗口
# ═══════════════════════════════════════════════════════════════════

def send_file_to_chat(path_str: str) -> str:
    """发送文件到聊天窗口（生成文件卡片）。
    返回的 JSON 中包含 action:"send_file" 标记，UI 层检测到后自动弹出文件卡片。
    """
    import json
    if not path_str:
        return json.dumps({"success": False, "action": "send_file", "message": "未提供文件路径"})
    p = Path(path_str)
    if not p.exists():
        # 尝试在常见目录中搜索
        for search_base in [
            PROJECT_ROOT / "data" / "downloads",
            PROJECT_ROOT / "downloads",
            WRITABLE_ROOT,                           # ★ 1.5.0: 桌宠私有data（沙箱可写区，已含 office/knowledge_graph 子目录）
        ]:
            candidates = list(search_base.rglob(p.name))
            if candidates:
                p = candidates[0]
                break
        else:
            return json.dumps({
                "success": False, "action": "send_file",
                "message": f"文件不存在: {path_str}。可用 list_downloads 查看下载目录中的文件。"
            })
    size = p.stat().st_size
    if size >= 1024 * 1024:
        size_str = f"{size / 1024 / 1024:.1f} MB"
    elif size >= 1024:
        size_str = f"{size / 1024:.1f} KB"
    else:
        size_str = f"{size} B"
    return json.dumps({
        "success": True, "action": "send_file",
        "file_path": str(p), "file_size": size_str,
        "file_name": p.name,
        "message": f"文件已发送: {p.name} ({size_str})"
    })


def list_downloaded_files(pattern: str = "*") -> str:
    """列出下载目录中可用的文件，供 LLM 了解已下载的内容"""
    import json
    download_dirs = [
        PROJECT_ROOT / "data" / "downloads",
        PROJECT_ROOT / "downloads",
        WRITABLE_ROOT,                           # ★ 1.5.0: 桌宠私有data（沙箱可写区）
    ]
    results = []
    for d in download_dirs:
        if not d.exists():
            continue
        for f in sorted(d.rglob(pattern)):
            if f.is_file():
                size = f.stat().st_size
                size_str = f"{size / 1024:.0f}KB" if size < 1024 * 1024 else f"{size / 1024 / 1024:.1f}MB"
                results.append({
                    "name": f.name,
                    "path": str(f),
                    "size": size_str,
                    "type": f.suffix.lower(),
                })
    if not results:
        return json.dumps({"success": True, "files": [], "message": "下载目录中暂无文件"})
    return json.dumps({"success": True, "files": results, "count": len(results)})


# ═══════════════════════════════════════════════════════════════════
# 工具分发器 — 兼容 ToolManager 的单文件多工具模式
# 通过 action 参数分发到不同操作
# ═══════════════════════════════════════════════════════════════════

# 主工具注册: 使用 file_ops 作为统一入口名
TOOL_NAME = "file_ops"
TOOL_DESCRIPTION = (
    "文件操作工具集。支持: 查看文件、读取原文、写入、编辑、搜索、列出目录、发送文件到聊天窗口。"
    "通过action参数选择操作类型。"
    "★ 要看文件内容用 view（推荐）：它会按文件类型和大小**自动选路** —— "
    "图片/扫描件直接走多模态**如实描述**（禁思考、不编造）；大文件自动抽样；"
    "可以指定 pages 只看某几页（如 \"3\" 或 \"3-5\"）。"
    "★ 要逐字原文用 read（文本/代码/PDF/Word/Excel/PPT 都能抽文本）。"
    "用户发来的文件或提到的本地文件路径，你都可以（也应该）去看，"
    "不要说「我看不到文件/我没有眼睛」——看文件就是你的眼睛。"
    "要把文件发给用户时用 send 操作。"
)
TOOL_PARAMETERS = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["view", "read", "write", "edit", "search", "list", "send",
                     "list_downloads"],
            "description": "操作类型: view(统一查看文件内容，推荐), read(读原文), write(写入), edit(编辑), search(搜索), list(列表), send(发送文件到聊天窗口), list_downloads(列出下载目录中的文件)",
        },
        "path": {"type": "string", "description": "文件/目录路径。[send] 需要发送的文件的完整路径"},
        "content": {"type": "string", "description": "[write] 写入内容"},
        "old_string": {"type": "string", "description": "[edit] 要替换的旧文本"},
        "new_string": {"type": "string", "description": "[edit] 替换后的新文本"},
        "offset": {"type": "integer", "description": "[read] 起始行号"},
        "limit": {"type": "integer", "description": "[read] 读取行数"},
        "method": {"type": "string",
                   "enum": ["auto", "multimodal", "vision", "stt", "text"],
                   "description": "[view] 查看方式: auto(自动选路，默认) / multimodal(多模态直读) / vision(视觉识别) / stt(语音转写) / text(本地看原文)"},
        "pages": {"type": "string",
                  "description": "[view] 查看位置: 空=默认(超过8页自动抽样) / \"3\"=第3页 / \"3-5\"=第3到5页 / \"1,4,7\"。PDF按页、PPT按张、Excel按工作表"},
        "max_chars": {"type": "integer", "description": "[view] 返回文本上限（默认 20000 字，超出自动抽样头尾）"},
        "prompt": {"type": "string", "description": "[view] 自定义视觉提示词（一般不用填，默认强制如实描述）"},
        "pattern": {"type": "string", "description": "[search/list/list_downloads] 搜索模式或文件过滤"},
        "file_types": {"type": "string", "description": "[search] 文件类型过滤"},
        "case_sensitive": {"type": "boolean", "description": "[search] 区分大小写"},
    },
    "required": ["action"],
}


def execute(action: str = "", **kwargs) -> str:
    """统一分发入口"""
    if not action:
        return (
            "[提示] file_ops 需要指定 action 参数。可用操作:\n"
            "  - view: 统一查看文件内容（推荐；自动按类型/大小选路，可指定 pages）\n"
            "  - read: 读取文件原文 (需要 path)\n"
            "  - write: 写入文件 (需要 path + content)\n"
            "  - edit: 编辑文件 (需要 path + old_string + new_string)\n"
            "  - search: 搜索代码 (需要 pattern)\n"
            "  - list: 列出目录内容\n"
            "  - send: 发送文件到聊天窗口 (需要 path)\n"
            "  - list_downloads: 列出已下载的文件\n"
            "请选择操作并提供相应参数。例如: action='view', path='a.pdf', pages='1-3'。"
        )
    if action == "view":
        from tools.file_view import view_file_json
        return view_file_json(
            path=kwargs.get("path", ""),
            method=kwargs.get("method", "auto") or "auto",
            pages=kwargs.get("pages", "") or "",
            max_chars=int(kwargs.get("max_chars") or 0),
            prompt=kwargs.get("prompt", "") or "",
        )
    if action == "read":
        return file_read(
            path=kwargs.get("path", ""),
            offset=kwargs.get("offset", 0),
            limit=kwargs.get("limit", 0),
        )
    elif action == "write":
        return file_write(
            path=kwargs.get("path", ""),
            content=kwargs.get("content", ""),
        )
    elif action == "edit":
        return file_edit(
            path=kwargs.get("path", ""),
            old_string=kwargs.get("old_string", ""),
            new_string=kwargs.get("new_string", ""),
        )
    elif action == "search":
        return search_files(
            pattern=kwargs.get("pattern", ""),
            path=kwargs.get("path", ""),
            file_types=kwargs.get("file_types", ""),
            case_sensitive=kwargs.get("case_sensitive", False),
        )
    elif action == "list":
        return list_dir(
            path=kwargs.get("path", ""),
            pattern=kwargs.get("pattern", "*"),
        )
    elif action == "send":
        return send_file_to_chat(kwargs.get("path", ""))
    elif action == "list_downloads":
        return list_downloaded_files(kwargs.get("pattern", "*"))
    else:
        return (f"[错误] 未知操作: {action}。支持: view, read, write, edit, search, "
                f"list, send, list_downloads")
