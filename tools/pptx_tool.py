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
  · **不拼 shell**：纯 Python API，天然躲开沙箱对 `&&`/`2>&1` 的误判。
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
空行忽略；`## 子标题` 视为该页的小标题行。"""

TOOL_PARAMETERS = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["create", "edit", "info"],
                   "description": "create=新建；edit=在已有 pptx 后追加页；info=看已有 pptx 页数与文字"},
        "outline": {"type": "string", "description": "大纲文本（见描述里的格式）"},
        "path": {"type": "string", "description": "已有 pptx 路径（edit/info 用）"},
        "title": {"type": "string", "description": "首页标题（没写 `# ` 时用）"},
        "subtitle": {"type": "string", "description": "首页副标题（可选）"},
        "out_dir": {"type": "string", "description": "输出目录，默认 data/桌宠私有data/pptx"},
        "filename": {"type": "string", "description": "输出文件名（不带路径；默认按标题取名）"},
        "assets_dir": {"type": "string", "description": "素材目录（插图在这里找）"},
        "theme": {"type": "string", "enum": ["deep_blue", "warm", "mono"],
                  "description": "配色，默认 deep_blue"},
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

    # 图片（等比裁剪后放右侧）
    if imgs:
        box_ratio = 4.6 / 4.6
        y = 1.55
        for name in imgs[:2]:
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
            tmp = DEFAULT_OUT / "_tmp" / f"{p.stem}_crop.jpg"
            cropped = _crop_to(p, tmp, box_ratio, img)
            if cropped is None:
                skipped.append(f"{p.name}(裁剪失败)")
                continue
            slide.shapes.add_picture(str(cropped), Inches(8.25), Inches(y),
                                     width=Inches(4.6), height=Inches(4.6))
            y += 4.7
            if y > 6.2:
                break

    # 页码
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
    t = title or slides[0]["title"] or "演示文稿"
    _add_page(prs, t, ([subtitle] if subtitle else []), [], theme, adir, 0, skipped)
    # 内容页
    body = slides[1:] if (slides and slides[0]["title"] == t and not slides[0]["bullets"]) else slides
    if not body:
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


def execute(action: str = "create", outline: str = "", path: str = "",
            title: str = "", subtitle: str = "", out_dir: str = "",
            filename: str = "", assets_dir: str = "", theme: str = "deep_blue",
            **kwargs) -> str:
    """工具入口。返回 JSON 字符串（含 text 字段）。"""
    a = (action or "create").strip().lower()
    try:
        if a in ("create", "建", "新建", "做"):
            return _do_create(outline, title, subtitle, out_dir, filename, assets_dir, theme)
        if a in ("edit", "改", "追加", "加页"):
            return _do_edit(path, outline, assets_dir, theme)
        if a in ("info", "看", "查看"):
            return _do_info(path)
        return _err(f"不认识 action「{action}」。可用：create / edit / info")
    except ImportError as e:
        return _err(f"缺少依赖（python-pptx / Pillow）：{e}")
    except Exception as e:
        return _err(f"{type(e).__name__}: {e}")
