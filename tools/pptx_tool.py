# -*- coding: utf-8 -*-
"""★ 1.9.3 第 16 轮：ppt 工具 —— 给大纲 + 素材目录，**一次出一份能用的 PPT**。

为什么要有它（真机教训，见 `PPT制作过程分析_1.9.3.md`）：
  以前模型做 PPT 得靠 `file_ops write` 手写脚本 + `shell_exec` 跑脚本，
  真机一次跑出 **37 次工具调用、9 轮、17 次都在搜集素材**，最后还撞上"空回复放弃"。
  缺的不是库（python-pptx 本来就装好了），而是**一次到位的能力 + 规范**。

本工具把 office-pptx 那套规范吃进来（见 TOOL_DESCRIPTION），并做到：
  · **素材校验**：插图片前逐张试解码，坏图跳过并如实报告（真机踩过"坏图"暗雷）；
  · **等比裁剪**：按目标框比例中心裁剪（不拉伸变形）；
  · **中文排版默认值**：字体、字号、行距、页码给好默认值，不靠模型现调；
  · **不拼 shell**：纯 Python API，天然躲开沙箱对 `&&`/`2>&1` 的误判；
  · ★ 1.9.3 第 21 轮：**render 渲染核对** —— 调本机 PowerPoint 把每页导出 PNG，
    让"看一眼成品"成为可能（以前生成完无法核对错版/溢出/图糊）。
"""
import json
import os
from pathlib import Path

TOOL_NAME = "pptx"
TOOL_DESCRIPTION = """在本机生成或增补 PowerPoint（.pptx）。**做 PPT 优先用它，不要自己写脚本**。

规范（照 office-pptx 那套，已内置默认值，你只需给大纲与素材）：
· 16:9（13.333×7.5 英寸）；显式设置版式、字号、图表数据；用本地图片，不用网图。
· 中文默认字体（微软雅黑，含东亚字体回退）；标题 30pt、正文 18pt 起，正文过长会自动缩号。
· 每页都有页码；末页不空。
· 插图片前**逐张校验可解码**，坏图会被跳过并在返回里列出（不会把坏图塞进 PPT）。
· 图片按目标框比例**中心裁剪**（等比，不拉伸变形）。
· 改现有 PPT 用 action=edit：**保留原有页**，只在后面追加，不重建（重建会丢动画/SmartArt）。

大纲格式（一行一条）：
  `# 标题`        → 开一页（无 # 时用 title 参数开首页）
  `- 要点`        → 该页的一个要点
  `@图片文件名`    → 该页右侧插图（在 assets_dir 里找；也可给绝对路径）
空行忽略；`## 子标题` 视为该页的小标题行。

· **想核对排版就用 action=render**：调本机 PowerPoint 把每页导出成 PNG（返回图片
  绝对路径），再用你的看图能力检查文字溢出/图片变形/字体缺失。没装 Office 的机器
  会如实返回失败（按"无法渲染核对"处理，别硬试）。"""

TOOL_PARAMETERS = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["create", "edit", "info", "render"],
                   "description": "create=新建；edit=在已有 pptx 后追加页；info=看已有 pptx 页数与文字；render=调本机 PowerPoint 把每页导出 PNG（用于核对排版）"},
        "outline": {"type": "string", "description": "大纲文本（见描述里的格式）"},
        "path": {"type": "string", "description": "已有 pptx 路径（edit/info 用）"},
        "title": {"type": "string", "description": "首页标题（没写 `# ` 时用）"},
        "subtitle": {"type": "string", "description": "首页副标题（可选）"},
        "out_dir": {"type": "string", "description": "输出目录，默认 data/桌宠私有data/pptx"},
        "filename": {"type": "string", "description": "输出文件名（不带路径；默认按标题取名）"},
        "assets_dir": {"type": "string", "description": "素材目录（插图在这里找）"},
        "theme": {"type": "string", "enum": ["deep_blue", "warm", "mono"],
                  "description": "配色，默认 deep_blue"},
        "pages": {"type": "string",
                  "description": "render 用：要导出哪几页，如 \"3\" / \"3-5\" / \"1,4,7\"；不填=全部"},
        "dpi": {"type": "integer",
                "description": "render 用：导出分辨率，默认 144（越高越清楚也越慢）"},
    },
    "required": [],
}

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = ROOT / "data" / "桌宠私有data" / "pptx"

# 主题：背景、标题色、正文色、强调色
THEMES = {
    "deep_blue": {"bg": None, "title": (0x1F, 0x3A, 0x5F), "body": (0x33, 0x33, 0x33),
                  "accent": (0x2E, 0x86, 0xDE)},
    "warm": {"bg": None, "title": (0x7A, 0x3E, 0x1D), "body": (0x3A, 0x2E, 0x28),
             "accent": (0xE6, 0x8A, 0x2E)},
    "mono": {"bg": None, "title": (0x22, 0x22, 0x22), "body": (0x33, 0x33, 0x33),
             "accent": (0x66, 0x66, 0x66)},
}
FONT_CN = "微软雅黑"
SLIDE_W_IN, SLIDE_H_IN = 13.333, 7.5


def _err(msg: str) -> str:
    return json.dumps({"success": False, "action": "error", "text": msg}, ensure_ascii=False)


def _rgb(t):
    from pptx.dml.color import RGBColor
    return RGBColor(t[0], t[1], t[2])


def _set_ea_font(run, name: str = FONT_CN):
    """同时设置西文与**东亚**字体（python-pptx 不直接暴露 ea，需要改 XML）。"""
    try:
        from pptx.oxml.ns import qn
        rPr = run.font._rPr
        for tag in ("a:ea", "a:cs"):
            el = rPr.find(qn(tag))
            if el is None:
                el = rPr.makeelement(qn(tag), {})
                rPr.append(el)
            el.set("typeface", name)
    except Exception:
        pass


def _style_run(run, size, color, bold=False):
    from pptx.util import Pt
    run.font.size = Pt(size)
    run.font.bold = bold
    run.font.name = FONT_CN
    run.font.color.rgb = _rgb(color)
    _set_ea_font(run, FONT_CN)


def _load_image(path: Path):
    """★ 素材校验：能解码才算数（坏图会被跳过，不塞进 PPT）。"""
    try:
        from PIL import Image
        with Image.open(path) as im:
            im.verify()                      # 校验完整性
        with Image.open(path) as im:
            return im.convert("RGB")
    except Exception:
        return None


def _crop_to(path: Path, out: Path, ratio: float, img):
    """按目标框比例**中心裁剪**（等比，不拉伸）。"""
    try:
        w, h = img.size
        if w <= 0 or h <= 0:
            return None
        cur = w / h
        if cur > ratio:                      # 太宽 → 裁两边
            nw = int(h * ratio)
            box = ((w - nw) // 2, 0, (w - nw) // 2 + nw, h)
        else:                                # 太高 → 裁上下
            nh = int(w / ratio)
            box = (0, (h - nh) // 2, w, (h - nh) // 2 + nh)
        out.parent.mkdir(parents=True, exist_ok=True)
        img.crop(box).save(out, "JPEG", quality=88)
        return out
    except Exception:
        return None


def _parse_outline(outline: str):
    """解析大纲 → [(标题, [要点], [图片])]"""
    slides = []
    for raw in (outline or "").splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        s = line.strip()
        if s.startswith("#"):
            slides.append({"title": s.lstrip("#").strip(), "bullets": [], "imgs": []})
        elif s.startswith("-") or s.startswith("·"):
            if not slides:
                slides.append({"title": "", "bullets": [], "imgs": []})
            slides[-1]["bullets"].append(s.lstrip("-·").strip())
        elif s.startswith("@") or (("." in s) and s.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".bmp"))):
            if not slides:
                slides.append({"title": "", "bullets": [], "imgs": []})
            slides[-1]["imgs"].append(s.lstrip("@").strip())
        else:
            if not slides:
                slides.append({"title": s, "bullets": [], "imgs": []})
            else:
                slides[-1]["bullets"].append(s)
    return slides


def _add_page(prs, title: str, bullets, imgs, theme, assets_dir: Path, page_no: int,
              skipped: list):
    from pptx.util import Inches, Pt
    slide = prs.slides.add_slide(prs.slide_layouts[6])       # 6 = 空白版式
    has_img = bool(imgs)
    text_w = 7.4 if has_img else 12.1

    # 标题
    if title:
        tb = slide.shapes.add_textbox(Inches(0.6), Inches(0.42), Inches(12.1), Inches(0.9))
        tf = tb.text_frame
        tf.word_wrap = True
        _style_run(tf.paragraphs[0].add_run(), 30, theme["title"], bold=True)
        tf.paragraphs[0].runs[0].text = title

    # 要点（按条数自动缩号，避免溢出）
    if bullets:
        n = len(bullets)
        size = 18 if n <= 6 else (16 if n <= 9 else 14)
        tb = slide.shapes.add_textbox(Inches(0.75), Inches(1.55), Inches(text_w), Inches(5.3))
        tf = tb.text_frame
        tf.word_wrap = True
        for i, b in enumerate(bullets):
            p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
            p.space_after = Pt(6)
            run = p.add_run()
            run.text = ("• " if not b.startswith(("•", "-")) else "") + b
            _style_run(run, size, theme["body"])

    # 图片（等比裁剪后放右侧；**两张并排**）
    # ★★ 1.9.3 第 20 轮：修一个静默丢弃 bug —— 旧代码 ``y += 4.7; if y > 6.2: break``
    #   在第一张图之后立刻成立，于是**第二张及以后的图根本不处理**（也不进 skipped，
    #   所以"坏图跳过"看起来永远没生效）。现在：1 张=右侧大图；2 张=左右并排；
    #   超出上限的**记账**（未排版），不再无声无息地丢。
    if imgs:
        names = list(imgs)
        if len(names) == 1:
            slots = [(8.25, 1.55, 4.6, 4.6)]
        else:
            slots = [(7.15, 2.0, 3.5, 3.5), (10.85, 2.0, 3.5, 3.5)]
        placed = 0
        for name in names:
            if placed >= len(slots):
                skipped.append(f"{Path(name).name}(超出每页图片上限，未排版)")
                continue
            p = Path(name)
            if not p.is_absolute():
                p = assets_dir / name if assets_dir else p
            if not p.exists():
                skipped.append(f"{name}(找不到)")
                continue
            img = _load_image(p)
            if img is None:
                skipped.append(f"{p.name}(坏图/无法解码)")
                continue
            bx, by, bw, bh = slots[placed]
            tmp = DEFAULT_OUT / "_tmp" / f"{p.stem}_crop.jpg"
            cropped = _crop_to(p, tmp, bw / bh, img)
            if cropped is None:
                skipped.append(f"{p.name}(裁剪失败)")
                continue
            slide.shapes.add_picture(str(cropped), Inches(bx), Inches(by),
                                     width=Inches(bw), height=Inches(bh))
            placed += 1

    # 页码（★ 1.9.3 第 21 轮：page_no 为 0/None 表示**这是封面，不打页码** ——
    #   以前封面硬打了一个"0"，用户看图一眼就看见）
    if page_no:
        tb = slide.shapes.add_textbox(Inches(12.35), Inches(6.95), Inches(0.7), Inches(0.35))
        r = tb.text_frame.paragraphs[0].add_run()
        r.text = str(page_no)
        _style_run(r, 11, theme["accent"])


def _do_create(outline: str, title: str, subtitle: str, out_dir, filename: str,
               assets_dir: str, theme_name: str) -> str:
    from pptx import Presentation
    from pptx.util import Inches
    theme = THEMES.get(theme_name or "deep_blue", THEMES["deep_blue"])
    slides = _parse_outline(outline)
    if not slides:
        return _err("大纲是空的：至少给一行 `# 标题` 或几个 `- 要点`。")
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(SLIDE_W_IN), Inches(SLIDE_H_IN)
    skipped = []
    adir = Path(assets_dir) if assets_dir else None

    # 首页
    # ★★ 1.9.3 第 21 轮（render 一上手就抓到的 bug）：**首页会被画两遍**。
    #   旧代码：封面用 `slides[0]["title"]`，而 body 只在"第一页没有要点"时才吃掉它 ——
    #   于是 `# 标题` + `- 要点` 这种最常见的大纲会生成
    #   「封面(只有标题) + 内容页(同一标题+要点)」→ **标题在片子里出现两次**，
    #   而且封面还带个页码 0。现在：只要封面取自大纲第一页，就**把它的要点一并吃进封面**，
    #   并且绝不作为内容页重画。
    first = slides[0]
    take_first = bool(first["title"]) and first["title"] == (title or first["title"])
    if take_first:
        cover_bullets = ([subtitle] if subtitle else []) + list(first["bullets"])
        _add_page(prs, first["title"], cover_bullets, first["imgs"], theme, adir, 0, skipped)
        body = slides[1:]
    else:
        t = title or first["title"] or "演示文稿"
        _add_page(prs, t, ([subtitle] if subtitle else []), [], theme, adir, 0, skipped)
        body = slides
    for i, s in enumerate(body, start=1):
        _add_page(prs, s["title"], s["bullets"], s["imgs"], theme, adir, i, skipped)

    od = Path(out_dir) if out_dir else DEFAULT_OUT
    od.mkdir(parents=True, exist_ok=True)
    fn = filename or (t.strip().replace("/", "_").replace("\\", "_")[:40] or "deck")
    if not fn.lower().endswith(".pptx"):
        fn += ".pptx"
    path = od / fn
    prs.save(str(path))
    return json.dumps({
        "success": True, "action": "create", "path": str(path),
        "slides": len(prs.slides._sldIdLst), "skipped_images": skipped,
        "text": (f"PPT 已生成：{path}（共 {len(body) + 1} 页）"
                 + (f"；跳过 {len(skipped)} 张素材：{'、'.join(skipped[:3])}" if skipped else "")),
    }, ensure_ascii=False)


def _do_edit(path: str, outline: str, assets_dir: str, theme_name: str) -> str:
    from pptx import Presentation
    p = Path(path)
    if not p.exists():
        return _err(f"找不到 PPT：{p}")
    theme = THEMES.get(theme_name or "deep_blue", THEMES["deep_blue"])
    slides = _parse_outline(outline)
    if not slides:
        return _err("outline 为空，没有可追加的内容。")
    prs = Presentation(str(p))                 # ★ 保留原有页，只往后追加
    skipped = []
    adir = Path(assets_dir) if assets_dir else None
    base = len(prs.slides._sldIdLst)
    for i, s in enumerate(slides, start=1):
        _add_page(prs, s["title"], s["bullets"], s["imgs"], theme, adir, base + i, skipped)
    prs.save(str(p))
    return json.dumps({"success": True, "action": "edit", "path": str(p),
                       "added": len(slides), "skipped_images": skipped,
                       "text": f"已在 {p.name} 后面追加 {len(slides)} 页（原有页保留）"},
                      ensure_ascii=False)


def _do_info(path: str) -> str:
    from pptx import Presentation
    p = Path(path)
    if not p.exists():
        return _err(f"找不到 PPT：{p}")
    prs = Presentation(str(p))
    lines = []
    for i, slide in enumerate(prs.slides, start=1):
        txt = []
        for sh in slide.shapes:
            if sh.has_text_frame and sh.text_frame.text.strip():
                txt.append(sh.text_frame.text.strip().replace("\n", " / "))
        lines.append(f"{i}. " + (" | ".join(txt)[:120] or "(无文字)"))
    return json.dumps({"success": True, "action": "info", "slides": len(prs.slides._sldIdLst),
                       "path": str(p), "outline": lines,
                       "text": f"{p.name} 共 {len(prs.slides._sldIdLst)} 页：\n" + "\n".join(lines)},
                      ensure_ascii=False)


def _parse_pages(spec, total: int):
    """把 "3" / "3-5" / "1,4,7" 解析成页码列表（1 起，越界丢弃）。空/无效 → 全部。"""
    if spec is None:
        return list(range(1, total + 1))
    s = str(spec).strip().replace("，", ",").replace("－", "-")
    if not s:
        return list(range(1, total + 1))
    want = set()
    for part in s.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            try:
                lo, hi = int(a), int(b)
            except Exception:
                continue
            if lo > hi:
                lo, hi = hi, lo
            want.update(range(lo, hi + 1))
        else:
            try:
                want.add(int(part))
            except Exception:
                continue
    got = [n for n in range(1, total + 1) if n in want]
    return got or list(range(1, total + 1))       # 全越界 → 退回全部，别返回空


def _powerpoint_running() -> bool:
    """本机是否**已经**开着 PowerPoint（决定我们能不能 Quit —— 不能关掉用户的窗口）。"""
    try:
        import psutil
        for pr in psutil.process_iter(["name"]):
            if (pr.info.get("name") or "").lower() == "powerpnt.exe":
                return True
    except Exception:
        pass
    return False


def _do_render(path: str, out_dir: str = "", pages: str = "", dpi: int = 144) -> str:
    """★ 1.9.3 第 21 轮：调本机 PowerPoint（COM）把每页导出成 PNG，供"看一眼成品"。

    为什么走 COM 而不是 LibreOffice（第 19 轮实测结论）：
      本机 `C:\\Program Files\\Microsoft Office\\root\\Office16\\POWERPNT.EXE` 在、
      `pywin32` 也在 → **零新依赖**；LibreOffice 要另带 ~400MB，用户明确说"用现成的"。
    """
    p = Path(path)
    if not p.exists():
        return _err(f"找不到 PPT：{p}")
    # ★★ 1.9.3 第 21 轮（冒烟的教训）：**PowerPoint COM 不吃相对路径** ——
    #   传 "data\\…\\x.pptx" 会报 `(-2147352567, '发生意外。', …, -2147024893)`（= 找不到路径）。
    #   所以进 COM 之前一律转成绝对路径（文件不存在时 resolve() 不会抛，strict=False）。
    try:
        p = p.resolve()
    except Exception:
        p = Path(os.path.abspath(str(p)))
    try:
        import pythoncom
        import win32com.client
    except ImportError as e:
        return _err(f"渲染核对需要 pywin32（win32com）：{e}")

    try:
        dpi = int(dpi or 144)
    except Exception:
        dpi = 144
    dpi = max(48, min(300, dpi))                  # 防手抖给 5000 把内存打爆

    od = Path(out_dir) if out_dir else (p.parent / f"{p.stem}_png")
    try:
        od = od.resolve()
    except Exception:
        od = Path(os.path.abspath(str(od)))
    od.mkdir(parents=True, exist_ok=True)

    already = _powerpoint_running()
    app = None
    pres = None
    images, failed = [], []
    opened = False
    pythoncom.CoInitialize()                      # ★ 非主线程必须自己初始化 COM
    try:
        try:
            app = win32com.client.Dispatch("PowerPoint.Application")
        except Exception as e:
            return _err(f"调不起 PowerPoint（本机可能没装 Office）：{e}")
        try:
            # ReadOnly=True, Untitled=False, WithWindow=False（不弹窗打扰用户）
            pres = app.Presentations.Open(str(p), True, False, False)
            opened = True
        except Exception:
            try:
                pres = app.Presentations.Open(str(p))     # 某些版本不吃 WithWindow=False
                opened = True
            except Exception as e:
                return _err(f"PowerPoint 打不开这个文件：{e}")

        total = int(pres.Slides.Count)
        idxs = _parse_pages(pages, total)
        w = int(round(SLIDE_W_IN * dpi))
        h = int(round(SLIDE_H_IN * dpi))
        for i in idxs:
            out_png = od / f"{p.stem}_p{i:02d}.png"
            try:
                if out_png.exists():
                    out_png.unlink()
                pres.Slides(i).Export(str(out_png), "PNG", w, h)   # ★ 逐页导出最稳
                if out_png.exists() and out_png.stat().st_size > 0:
                    images.append(str(out_png))
                else:
                    failed.append(f"第{i}页(导出为空)")
            except Exception as e:
                failed.append(f"第{i}页({type(e).__name__}: {e})")
    finally:
        # 只收自己打开的那一份；**用户原本开着的 PowerPoint 不能 Quit**。
        try:
            if opened and pres is not None:
                pres.Close()
        except Exception:
            pass
        try:
            if app is not None and not already:
                app.Quit()
        except Exception:
            pass
        try:
            pythoncom.CoUninitialize()
        except Exception:
            pass

    if not images:
        return _err("没能导出任何页面" + (f"（{'；'.join(failed[:3])}）" if failed else ""))
    msg = (f"已导出 {len(images)} 张 PNG（{dpi}dpi）到 {od}：\n"
           + "\n".join(images[:12])
           + ("\n…（更多见目录）" if len(images) > 12 else "")
           + (f"\n★ {len(failed)} 页导出失败：{'；'.join(failed[:3])}" if failed else "")
           + "\n用你的看图能力逐张检查：文字有没有溢出、图片有没有变形、字体有没有缺失。")
    return json.dumps({
        "success": True, "action": "render", "path": str(p),
        "out_dir": str(od), "dpi": dpi, "slides": len(images),
        "images": images, "failed": failed, "text": msg,
    }, ensure_ascii=False)


def execute(action: str = "create", outline: str = "", path: str = "",
            title: str = "", subtitle: str = "", out_dir: str = "",
            filename: str = "", assets_dir: str = "", theme: str = "deep_blue",
            pages: str = "", dpi: int = 144, **kwargs) -> str:
    """工具入口。返回 JSON 字符串（含 text 字段）。"""
    a = (action or "create").strip().lower()
    try:
        if a in ("create", "建", "新建", "做"):
            return _do_create(outline, title, subtitle, out_dir, filename, assets_dir, theme)
        if a in ("edit", "改", "追加", "加页"):
            return _do_edit(path, outline, assets_dir, theme)
        if a in ("info", "看", "查看"):
            return _do_info(path)
        if a in ("render", "渲染", "预览", "看图", "导出"):
            return _do_render(path, out_dir, pages, dpi)
        return _err(f"不认识 action「{action}」。可用：create / edit / info / render")
    except ImportError as e:
        return _err(f"缺少依赖（python-pptx / Pillow）：{e}")
    except Exception as e:
        return _err(f"{type(e).__name__}: {e}")
