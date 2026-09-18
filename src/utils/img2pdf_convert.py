"""
图片转 PDF 工具
将下载的图片列表合成为单个 PDF 文件，使用 Pillow 实现
"""
import logging
from pathlib import Path
from typing import List, Union

_log = logging.getLogger("Utils.Img2PDF")

# 单张图片最大尺寸限制（像素），超出的会等比缩放
MAX_DIMENSION = 4096


async def images_to_pdf(
    image_paths: List[Union[str, Path]],
    pdf_path: Union[str, Path],
    quality: int = 85,
) -> Path:
    """
    将多张图片合成为 PDF。

    Args:
        image_paths: 已排序的图片路径列表
        pdf_path: 输出的 PDF 文件路径
        quality: JPEG 压缩质量 (1-100)，仅对 JPEG 编码有效

    Returns:
        生成的 PDF 文件 Path
    """
    from PIL import Image

    pdf_path = Path(pdf_path)
    pdf_path.parent.mkdir(parents=True, exist_ok=True)

    if not image_paths:
        raise ValueError("图片列表为空，无法生成 PDF")

    images: list[Image.Image] = []

    for img_path in image_paths:
        img_path = Path(img_path)
        if not img_path.exists():
            _log.warning(f"图片不存在，跳过: {img_path.name}")
            continue
        try:
            img = Image.open(img_path)
            # PDF 需要 RGB 模式
            if img.mode in ("RGBA", "LA", "P", "PA"):
                # 有透明通道 → 白色背景
                background = Image.new("RGB", img.size, (255, 255, 255))
                if img.mode == "P":
                    img = img.convert("RGBA")
                background.paste(img, mask=img.split()[-1] if img.mode in ("RGBA", "LA") else None)
                img = background
            elif img.mode not in ("RGB", "L", "CMYK"):
                img = img.convert("RGB")

            # 超大图等比缩放
            w, h = img.size
            scale = MAX_DIMENSION / max(w, h)
            if scale < 1.0:
                new_size = (int(w * scale), int(h * scale))
                img = img.resize(new_size, Image.LANCZOS)

            images.append(img)
        except Exception as e:
            _log.warning(f"打开图片失败，跳过 {img_path.name}: {e}")

    if not images:
        raise RuntimeError("没有可用的图片生成 PDF")

    try:
        first = images[0]
        rest = images[1:]
        first.save(
            pdf_path,
            save_all=True,
            append_images=rest,
            resolution=100.0,
            quality=quality,
        )
        _log.info(f"PDF 生成成功: {pdf_path.name} (共 {len(images)} 页, {pdf_path.stat().st_size / 1024:.0f} KB)")
    except Exception:
        # 部分 Pillow 版本不支持 append_images，逐页追加
        first.save(pdf_path, resolution=100.0, quality=quality)
        for img in rest:
            img.save(pdf_path, append=True, save_all=True, resolution=100.0, quality=quality)
        _log.info(f"PDF 生成成功(逐页模式): {pdf_path.name} (共 {len(images)} 页, {pdf_path.stat().st_size / 1024:.0f} KB)")

    # 关闭所有图片
    for img in images:
        img.close()

    return pdf_path
