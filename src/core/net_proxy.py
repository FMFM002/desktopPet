"""
★ 1.9.2 任务 11：BT 与 P站/JM 的「VPN 冲突」解决方案（应用级分流）

用户批注：
    ###下载BT和P站JM工具因为VPN冲突，有没有便捷的方案。研究研究合适的方案

## 冲突到底是什么
· P站 / JM 在国内直连不通 → 需要走代理（本机 `.env` 里
  `PIXIV_PROXY=http://127.0.0.1:7897`，是 Clash / v2rayN 那类**本地代理端口**）。
· BT 需要**直连** peer → 不能走代理。这不是"配置麻烦"，是**协议层不支持**：
  · aria2 的代理选项（`--all-proxy` / `--http-proxy` / `--ftp-proxy`）按手册口径
    只作用于 **HTTP / HTTPS / FTP** 下载；
  · BitTorrent 的 peer 连接是任意 TCP/UDP 直连，aria2 **没有**对应的代理选项。
    社区从 2015 年就在求（aria2 issue #1401 "Proxy server for torrents"），
    至今没有实现。
  · 更糟的是：走代理出口时，你的公网 IP 变成机房 IP（本机实测是
    45.67.201.106 / 香港 Catixs Ltd），**tracker 会直接拒绝**
    （实测回 "Requested download is not authorized"），私有 tracker 更会封号。
  → 所以结论很硬：**BT 必须直连；P站/JM 才需要代理。二者不能共用同一条出口。**

## 于是"便捷方案"就是：别用系统级 VPN，改用**应用级分流**
本模块提供这条路的代码侧那一半：让**我们的工具自己**决定谁走代理、
并且**保证 aria2c 绝不继承系统代理**。

    ┌─ P站 / JM 工具 ──→ 走 PRTS_PROXY（本地代理端口）→ 能上
    └─ aria2c（BT）  ──→ 强制直连（清掉代理环境变量 + --all-proxy=）→ 能连 peer

这样**开着代理客户端也不用管 VPN 开关**：P站/JM 正常、BT 也正常。

## 还需要你在代理客户端里做的一件事（唯一一步，见 net_proxy.advice()）
在 Clash / Mihomo 之类的配置里加一条**规则分流**：让 aria2c.exe 与 python.exe
直连，只把 pixiv / pximg / jm 那几个域名指向 PROXY。
比"每次手动开关 VPN"省事得多，而且 BT 不再被机房里出口拖累。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

_log = logging.getLogger("NetProxy")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
_ENV_FILE = PROJECT_ROOT / ".env"
_dotenv_tried = False


def _ensure_dotenv():
    """★ 1.9.2：自己兜底读一次 .env。

    不依赖"别处已经 load_dotenv() 了" —— 之前裸脚本里读到的是空值，
    因为 .env 还没被加载（load 时机不确定）。这里按需补一次，代价极小。
    """
    global _dotenv_tried
    if _dotenv_tried:
        return
    _dotenv_tried = True
    if not _ENV_FILE.exists():
        return
    try:
        from dotenv import load_dotenv
        load_dotenv(_ENV_FILE, override=False)
        _log.debug("[代理] 已按需加载 .env")
    except Exception as e:
        _log.debug(f"[代理] 加载 .env 失败: {e}")

# 读代理地址时的环境变量优先级：
#   PRTS_PROXY（通用，本项目自己的）
#   PIXIV_PROXY（历史遗留，P站那套已经在用，兼容它）
_PROXY_KEYS = ("PRTS_PROXY", "PIXIV_PROXY")

# 会被 aria2 / requests 等自动读取的代理环境变量（大小写都要查）
_SYS_PROXY_KEYS = ("http_proxy", "https_proxy", "all_proxy", "ftp_proxy",
                   "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "FTP_PROXY")


def proxy_url() -> str:
    """当前配置的代理地址；没配返回空串。"""
    _ensure_dotenv()
    for k in _PROXY_KEYS:
        v = (os.getenv(k, "") or "").strip()
        if v:
            return v
    return ""


def proxies_dict() -> dict:
    """给 requests / jmcomic 用的 {"http":…, "https":…}；没配返回 {}。"""
    p = proxy_url()
    return {"http": p, "https": p} if p else {}


def system_proxy_env() -> dict:
    """系统里**已经存在**的代理环境变量（排查用；不含我们自己的配置）。"""
    out = {}
    for k in _SYS_PROXY_KEYS:
        v = (os.environ.get(k) or "").strip()
        if v:
            out[k] = v
    return out


def clean_env() -> dict:
    """给 aria2c 用的环境变量：把代理相关全部摘掉，强制直连。

    ★ 为什么必须做：aria2 会读 `http_proxy` / `all_proxy` 这类环境变量。
      如果宿主的代理客户端设了全局环境变量（很常见），aria2 就会把
      tracker 请求塞进代理 —— 而 BT 的 peer 连接又**不支持**代理 →
      结果是"tracker 能连、peer 一个都连不上"，非常难排查。
    """
    env = dict(os.environ)
    for k in list(env.keys()):
        if k.lower() in ("http_proxy", "https_proxy", "all_proxy", "ftp_proxy"):
            env.pop(k, None)
    return env


def mode() -> str:
    """当前处于哪种模式（给日志/状态查询用）。"""
    return "app-proxy" if proxy_url() else "direct"


def advice() -> str:
    """给用户的可操作建议（一条规则分流就够，不用开关 VPN）。"""
    p = proxy_url()
    lines = [
        "【BT 与 P站/JM 的冲突怎么解】",
        "· 原理：P站/JM 需要代理；而 BT 的 peer 连接**协议层不支持代理**",
        "  （aria2 的代理选项只管 HTTP/HTTPS/FTP；issue #1401 求了十年没做），",
        "  而且走代理出口是机房 IP，tracker 会直接拒绝 → BT 必须直连。",
        "· 做法（推荐，只用配一次）：在代理客户端（Clash/Mihomo 等）里做**规则分流**——",
        "      PROCESS-NAME,aria2c.exe,DIRECT",
        "      PROCESS-NAME,python.exe,DIRECT",
        "      DOMAIN-SUFFIX,pixiv.net,PROXY",
        "      DOMAIN-SUFFIX,pximg.net,PROXY",
        "      # jm 的镜像域名按你用的填，也指向 PROXY",
        "      MATCH,DIRECT",
        "  这样 BT 直连、P站/JM 走代理，**不用再手动开关 VPN**。",
        "· 本项目这边已经替你处理好：",
        f"    代理地址 = {p or '（未配置，请在 .env 里设 PRTS_PROXY 或 PIXIV_PROXY）'}",
        "    · P站 / JM 工具会用它（JM 以前是硬编码不走代理，1.9.2 改成可配）",
        "    · aria2c 启动时会被强制直连（清掉代理环境变量 + --all-proxy=）",
    ]
    sysp = system_proxy_env()
    if sysp:
        lines.append(f"· 注意：检测到系统级代理环境变量 {list(sysp.keys())} —— "
                     f"aria2c 这边我会屏蔽掉，不影响 BT。")
    return "\n".join(lines)
