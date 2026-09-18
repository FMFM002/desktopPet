"""Pixiv API library（★ PRTS 1.9.0 内置副本，见 pixivpy3_LICENSE）"""
from .aapi import AppPixivAPI
from .utils import PixivError
try:  # ★ PRTS 补丁：SNI 绕过是可选能力，缺依赖不影响主 API
    from .bapi import ByPassSniApi
except Exception:  # pragma: no cover
    ByPassSniApi = None  # type: ignore[assignment]
__all__ = ("AppPixivAPI", "ByPassSniApi", "PixivError")
