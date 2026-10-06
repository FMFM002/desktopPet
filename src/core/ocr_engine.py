"""
本地 OCR 引擎模块
- 支持 Tesseract (pytesseract) 和 EasyOCR
- 自动检测可用引擎，未安装时提供安装引导
- 图像预处理提升识别准确率
"""
import logging
import subprocess
import sys
import time
import hashlib
from pathlib import Path
from PIL import Image
from src.utils.proc import no_window_kwargs as _nw

_log = logging.getLogger("OCR")

# ★ 1.9.0: 模型也放项目里（你的要求：尽量别把东西放到项目外面，方便以后打包）。
#   easyocr 默认把模型下到用户目录 ~/.EasyOCR/model，装到别的机器/打包后就丢了；
#   这里固定指到 data/models/easyocr，第一次用时下到这里，之后离线可用。
_MODEL_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "models" / "easyocr"
try:
    _MODEL_DIR.mkdir(parents=True, exist_ok=True)
except Exception:
    pass

# 检测 Tesseract 系统级依赖
_tesseract_available = False
try:
    result = subprocess.run(["tesseract", "--version"], capture_output=True, text=True, timeout=5, **_nw())
    if result.returncode == 0:
        _tesseract_available = True
except (FileNotFoundError, subprocess.TimeoutExpired):
    pass

# 检测 Python 库
_easyocr_available = False
_pytesseract_available = False

try:
    import pytesseract
    _pytesseract_available = True
except ImportError:
    pass

try:
    import easyocr
    _easyocr_available = True
except ImportError:
    pass

# ★ 1.9.2: easyocr Reader 空闲多久后释放（用户要求"一样的30s后卸载"，
#   与 src/core/tool_manager.py 的 IDLE_UNLOAD_TIMEOUT = 30.0 保持一致）
READER_IDLE_TIMEOUT = 30.0

# ★ 1.9.2: OCR 结果缓存上限。内存调研发现它原先无上限、且 clear() 无人调用 ——
#   key 是 64×64 灰度 md5，value 是整页识别文本，长会话会一直涨。
#   64 条足够覆盖"同一张截图反复识别"这个真实用途。
MAX_OCR_CACHE = 64


def check_ocr_availability() -> dict:
    """检测所有 OCR 引擎的可用性"""
    return {
        "tesseract_system": _tesseract_available,
        "pytesseract": _pytesseract_available,
        "easyocr": _easyocr_available,
    }


def get_available_ocr_engines() -> list[str]:
    """获取可用的 OCR 引擎列表"""
    engines = []
    if _tesseract_available and _pytesseract_available:
        engines.append("tesseract")
    if _easyocr_available:
        engines.append("easyocr")
    return engines




class OCREngine:
    """本地 OCR 引擎封装"""

    def __init__(self, engine: str = "tesseract", lang: str = "chi_sim+eng"):
        """
        Args:
            engine: "tesseract" | "easyocr"（配置里选的）
            lang: tesseract 语言包
        """
        self.engine = engine
        self.requested_engine = engine   # ★ 1.9.0: 记住用户配的那个，便于日志/回显
        self.lang = lang
        self._ocr_cache: dict[str, str] = {}
        self._easyocr_reader = None
        # ★ 1.9.2: easyocr Reader 的"最后使用时间"（空闲卸载用；照 LAZY 工具的 30s 规则）
        self._reader_last_used = 0.0
        self._available = False
        self._check_engine()
        if not self._available:
            # ★ 1.9.0: 配置的引擎没装 → **自动改用装了的那个**，而不是直接罢工。
            #   真机教训：设置页选的是 easyocr，但机器上只有另一个引擎可用时，
            #   旧行为是每次都回一句"[OCR引擎 'xxx' 不可用]"，功能等于废掉；
            #   用户看到的现象就是"OCR 挂了"。这里换一个能用的，并在日志里说清楚。
            _alts = [e for e in get_available_ocr_engines() if e != engine]
            if _alts:
                self.engine = _alts[0]
                self._check_engine()
                _log.warning(
                    f"OCR 引擎「{engine}」不可用 → 自动改用「{self.engine}」"
                    f"（想用回 {engine} 请先安装它）"
                )
        if self._available:
            _log.info(f"OCR 引擎就绪: {self.engine} (语言: {lang})")
        else:
            _log.warning(f"OCR 引擎不可用: {engine}"
                         f"（本机一个可用引擎都没有: {get_available_ocr_engines() or '无'}）")

    def _check_engine(self):
        if self.engine == "tesseract":
            self._available = _tesseract_available and _pytesseract_available
        elif self.engine == "easyocr":
            self._available = _easyocr_available
        else:
            self._available = False

    @property
    def available(self) -> bool:
        return self._available

    def _image_hash_key(self, image: Image.Image) -> str:
        """计算图像缓存键"""
        arr = image.resize((64, 64), Image.LANCZOS)
        return hashlib.md5(arr.tobytes()).hexdigest()

    @staticmethod
    def preprocess_for_ocr(image: Image.Image) -> Image.Image:
        """OCR 图像预处理：灰度化 + 锐化"""
        gray = image.convert("L")
        from PIL import ImageFilter
        return gray.filter(ImageFilter.SHARPEN)

    def extract_text(self, image: Image.Image, use_cache: bool = True) -> str:
        """从图像中提取文字"""
        if not self._available:
            _log.warning(f"OCR 引擎 {self.engine} 不可用")
            return ""
        if self.engine == "easyocr":
            return self._ocr_easyocr(image)
        return self._ocr_tesseract(image, use_cache)

    def _ocr_tesseract(self, image: Image.Image, use_cache: bool = True) -> str:
        try:
            import pytesseract
            if use_cache:
                key = self._image_hash_key(image)
                if key in self._ocr_cache:
                    return self._ocr_cache[key]

            processed = self.preprocess_for_ocr(image)
            text = pytesseract.image_to_string(processed, lang=self.lang)

            if use_cache:
                # ★ 1.9.2（内存调研结论）：这个缓存原先是**无上限**的 dict，
                #   key 是 64×64 灰度图的 md5、value 是整页 OCR 文本，
                #   而且 clear() 全项目零调用点 → 长会话会一直涨。
                #   给个上限，超了就丢最旧的（dict 保序）。
                self._ocr_cache[key] = text
                while len(self._ocr_cache) > MAX_OCR_CACHE:
                    self._ocr_cache.pop(next(iter(self._ocr_cache)), None)

            return text.strip()
        except ImportError:
            return ""
        except Exception as e:
            _log.error(f"Tesseract OCR 失败: {e}")
            return ""

    def _ocr_easyocr(self, image: Image.Image) -> str:
        try:
            import easyocr
            import numpy as np
            if self._easyocr_reader is None:
                # ★ 1.9.0: 模型目录固定在项目内（见文件头的 _MODEL_DIR 说明）
                self._easyocr_reader = easyocr.Reader(
                    ["ch_sim", "en"], gpu=False,
                    model_storage_directory=str(_MODEL_DIR),
                    # ★ easyocr 默认还会往 ~/.EasyOCR/user_network 写东西（在项目外），
                    #   一起搬进项目里 —— 否则"整个项目拷走"在别的机器上会重新下载。
                    user_network_directory=str(_MODEL_DIR.parent / "easyocr_network"),
                    download_enabled=True,
                )
                _log.info(f"easyocr 模型目录: {_MODEL_DIR}")
            # ★ 1.9.2: 每次真正用到就刷新"最后使用时间"（空闲卸载据此判断）
            self._reader_last_used = time.time()
            results = self._easyocr_reader.readtext(np.array(image))
            return " ".join([item[1] for item in results])
        except ImportError:
            return ""
        except Exception as e:
            _log.error(f"EasyOCR 失败: {e}")
            return ""

    def release_idle_reader(self, timeout: float = READER_IDLE_TIMEOUT) -> bool:
        """★ 1.9.2（用户要求）：easyocr 的 Reader 空闲超时就释放，别永久常驻。

        用户原话：「这个直接一样的30s后卸载就行了」—— 就是照 LAZY 工具那套
        `IDLE_UNLOAD_TIMEOUT = 30.0` 的规则（见 src/core/tool_manager.py:25）。

        为什么值得做：内存调研实测 easyocr 这条链非常贵 ——
          import torch +510.6 MB、easyocr +135.8 MB、Reader(...) 再 +174.2 MB
          （18.2 → 794.0 MB，4.8s）。
        ★ 诚实说明能省多少：`Reader` 对象本身（含加载的模型权重、检测/识别网络）
          约 174 MB 能被真正释放；但 **`torch` 一旦 import 就留在 sys.modules 里**，
          那 510 MB 本进程内要不回来（除非把 OCR 挪进独立子进程 —— 那是更大的改法）。
          所以这里省的是"每次 OCR 之后不必一直养着 Reader"，不是那 776 MB 的全部。

        返回 True 表示这次真的释放了。
        """
        if self._easyocr_reader is None:
            return False
        idle = time.time() - (self._reader_last_used or 0.0)
        if idle < timeout:
            return False
        try:
            self._easyocr_reader = None
        except Exception as e:
            _log.debug(f"释放 easyocr Reader 失败: {e}")
            return False
        # ★ 主动催一次 GC：Reader 里有循环引用（模型 ↔ 网络层），
        #   光删引用不一定立刻回收那 174 MB。
        try:
            import gc
            gc.collect()
        except Exception:
            pass
        _log.info(f"[OCR] easyocr Reader 已空闲 {idle:.0f}s（>{timeout:.0f}s）"
                  f"→ 已释放（下次 OCR 会重新加载）")
        return True
