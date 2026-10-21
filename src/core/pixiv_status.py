# -*- coding: utf-8 -*-
"""★★ 1.9.4：**pixiv 现在到底是什么状态**（维护中？还是单纯连不上？）。

## 为什么需要这个文件（真机 2026-10-21）
用户拿到的报错是 `HTTP 405: <html>…<h1>405 Not Allowed</h1>…`，我一开始判成
"代理出口被 Cloudflare 拦了"，让他换节点 —— **结论错了**。
他一开浏览器，pixiv 首页就是官方维护页：

    Under maintenance
    pixiv is currently under maintenance. The maintenance is scheduled to end
    in the early hours of October 22, 2026 (JST).

实测（本机，同一个代理）也印证了：

| 请求 | 结果 |
|---|---|
| `GET https://www.pixiv.net/`（浏览器 UA） | **503** + 页面里 `Under maintenance` |
| `GET https://oauth.secure.pixiv.net/auth/token`（浏览器 UA） | **503** + 同样的维护页 |
| `POST …/auth/token`（App UA） | **405 Not Allowed**（维护期间边缘直接挡 POST） |

也就是说：**405 / 403 / 503 都可能只是"维护中"的不同表现**，
跟令牌、跟代理节点**没有关系**。没有这个探测，就会把用户往"换节点/重登"上带。

## 怎么用
    from src.core.pixiv_status import check
    st = check()
    if st["under_maintenance"]:
        ...  # 直接告诉用户"pixiv 在维护"，别让他折腾网络

★ 探测是**只读 GET**，8 秒超时，结果缓存 60 秒（避免失败路径上反复拖慢）。
★ 只有**出错时**才调用它 —— 正常请求不会多这一次网络往返。
"""
from __future__ import annotations

import time

_URL = "https://www.pixiv.net/"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/131.0 Safari/537.36")

_CACHE_TTL = 60.0
_cache: dict = {"t": 0.0, "v": None}

# 维护页的三种写法（2026-10-21 抓到的英文页为准，日文/中文兜底）
_MAINTENANCE_MARKS = ("under maintenance", "メンテナンス", "维护", "維護")


def looks_like_maintenance(status, body: str) -> bool:
    """这些特征是否像 pixiv 的维护页。★ 单独抽出来是为了**能离线单测**。"""
    text = (body or "").lower()
    hit = any(m in text for m in _MAINTENANCE_MARKS)
    if not hit:
        return False
    # 维护页一定是"站点整体不可用"的状态；正常 200 页面里出现这些词不算
    return status is None or int(status) >= 400 or "scheduled to end" in text


def check(timeout: float = 8.0, use_cache: bool = True) -> dict:
    """返回 `{under_maintenance: bool, status: int|None, detail: str}`。**不抛异常**。"""
    now = time.time()
    if use_cache and _cache["v"] is not None and (now - _cache["t"]) < _CACHE_TTL:
        return _cache["v"]

    v = {"under_maintenance": False, "status": None, "detail": "未探测"}
    try:
        import requests                                    # noqa: PLC0415

        r = requests.get(_URL, headers={"User-Agent": _UA,
                                        "Accept-Language": "zh-CN"},
                         timeout=timeout, allow_redirects=True)
        body = r.text or ""
        under = looks_like_maintenance(r.status_code, body)
        v = {
            "under_maintenance": bool(under),
            "status": r.status_code,
            "detail": ("pixiv 正在维护（页面标题：Under maintenance）" if under
                       else f"pixiv 首页 HTTP {r.status_code}（不是维护页）"),
        }
    except Exception as e:                                 # noqa: BLE001
        v = {"under_maintenance": False, "status": None,
             "detail": f"探测失败：{type(e).__name__}"}
    _cache.update(t=now, v=v)
    return v


def maintenance_hint() -> str:
    """一句给用户看的话（只在 `check()["under_maintenance"]` 为真时用）。"""
    return ("⚠ 现在 **pixiv 官方的服务器在维护**（网站首页就是 Under maintenance），\n"
            "     这跟你这边没关系：**不是令牌失效，也不是代理的问题**。\n"
            "     官方公告说维护结束后会自行恢复（他们会在 X 上发进度）。\n"
            "     等它恢复后再点「刷新状态」就行 —— 现在换节点、重登都没用。")
