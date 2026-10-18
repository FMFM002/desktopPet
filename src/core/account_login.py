# -*- coding: utf-8 -*-
"""★★ 1.9.4：**一键开通 B站 / P站** —— 用户"登录一次"就好。

## 用户的要求（2026-10-18）
    "D:\\...\\参考项目\\导出cookies和token\\最终产物bili_pixiv_toolkit
     这格项目可以做到开通p站和B站的功能。**如果用户要开通这两个功能，
     就引导用户登陆一次就可以了。**"

## 那个工具包做了什么（我读过它的实现，机制如下）
  · **B站**：Playwright 用**系统 Edge** 起一个**持久化 profile**（`channel="msedge"`，
    所以**不需要** `playwright install`）→ 弹出浏览器让用户**登录一次** →
    把 `bilibili.com` 域的 cookie 导出成 JSON。
     关键函数：`bili_login()` = `_run_browser(headless=False, wait_login=True)`
  · **P站**：`gppt.GetPixivToken(headless=False).login()` → 走 OAuth 拿 `refresh_token`
     （本质是 playright 打开 pixiv 登录页，用户登录一次）。
     关键函数：`pixiv_login()` → 返回 token 字符串

## 本模块只做"接线"，**不复制它的业务逻辑**
  · 工具包已**原样并入** `tools/_vendor/bili_pixiv_toolkit.py`（第三方代码，**不改**）；
  · 这里把它产出的东西**搬到项目该在的位置**：
      B站 cookie → `data/cookies/bilibili.json`
        （`tools/bilibili_tool.py` 就从这里读；**B站凭据是文件，不是 .env 键**）
      P站 refresh_token → `.env` 的 `PIXIV_REFRESH_TOKEN`
        （`tools/pixiv_tool.py` 从这里读）
  · 并**改写它的输出目录**，别让它把 profile / 下载物吐在 `tools/_vendor/` 里。

## 依赖（缺了就**如实告诉用户**怎么装，不静默失败）
    playwright（用系统 Edge，无需 playwright install）
    requests、pixivpy3、gppt
  ★ 一律装进**本项目自己的解释器**（`sys.executable`）—— 参考项目 README 里用的是
    它那台机器的 `py`（3.14），而桌宠跑在 3.12 上，混用会 import 不到。
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VENDOR = ROOT / "tools" / "_vendor" / "bili_pixiv_toolkit.py"
WORK = ROOT / "data" / "account_login"        # profile + 临时产物
COOKIE_DIR = ROOT / "data" / "cookies"        # bilibili_tool 读这里
COOKIE_DST = COOKIE_DIR / "bilibili.json"

_toolet = None


def _toolkit():
    """加载**并按项目需要改写路径**的第三方模块（只加载一次）。"""
    global _toolet
    if _toolet is not None:
        return _toolet
    if not VENDOR.is_file():
        raise FileNotFoundError(f"缺少工具包: {VENDOR}")
    WORK.mkdir(parents=True, exist_ok=True)
    COOKIE_DIR.mkdir(parents=True, exist_ok=True)
    spec = importlib.util.spec_from_file_location("_bili_pixiv_toolkit", VENDOR)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # ★ 改写它的输出位置：别把浏览器 profile / 下载物吐进 tools/_vendor/
    #   （它是靠**模块级全局变量**取路径的，`_run_browser` 里读的是 `BILI_PROFILE`，
    #    所以在这里直接覆盖模块属性即可。）
    mod.BASE = WORK
    mod.BILI_COOKIE_FILE = WORK / "bili_cookies.json"
    mod.BILI_STATE_FILE = WORK / "bili_state.json"
    mod.BILI_PROFILE = WORK / "edge_bili_profile"
    mod.BILI_DOWNLOAD_DIR = WORK / "bili_downloads"
    mod.PIXIV_DOWNLOAD_DIR = WORK / "pixiv_downloads"
    _toolet = mod
    return mod


def deps() -> dict:
    """哪些依赖缺了。返回 `{名字: 在不在}`。

    ★ 每一项都**单独 try**：`find_spec` 会去 import 父包，某些环境（受限沙箱、
      半装的包）里会抛 `PermissionError` / `ImportError` 而不是干净返回 ——
      一个探测函数**绝不能把整个界面带崩**。
    """
    out = {}
    for name in ("playwright", "requests", "pixivpy3", "gppt"):
        try:
            out[name] = importlib.util.find_spec(name) is not None
        except Exception:
            out[name] = False
    return out


def missing_deps() -> list:
    return [k for k, v in deps().items() if not v]


def install_hint() -> str:
    """给人看的安装命令（**装进桌宠自己的解释器**）。"""
    return (f'"{sys.executable}" -m pip install '
            f'playwright requests pixivpy3 gppt')


def login_bilibili(on_line=None) -> tuple:
    """★ 引导用户**登录一次 B站**，然后把 cookie 搬到 `data/cookies/bilibili.json`。

    返回 `(成功?, 给人看的话)`。**不抛异常**（界面要能拿到结果）。
    """
    miss = [m for m in missing_deps() if m in ("playwright", "requests")]
    if miss:
        return False, ("❌ 还没装依赖：" + "、".join(miss) + "\n"
                       "     在项目目录里跑这条就行：\n     " + install_hint())
    try:
        mod = _toolkit()
    except Exception as e:
        return False, f"❌ 加载工具包失败: {type(e).__name__}: {e}"

    _say(on_line, "正在打开浏览器，请在窗口里**登录 B站**（只需这一次）。")
    try:
        # ★ 它会**阻塞**直到用户登录完/关掉窗口 —— 所以调用方必须放在后台线程里。
        r = mod.bili_login()
    except Exception as e:
        return False, f"❌ 登录流程出错: {type(e).__name__}: {e}"

    src = Path(mod.BILI_COOKIE_FILE)
    if not src.is_file():
        return False, ("❌ 没拿到 cookie（可能登录没完成，或窗口被提前关掉）。\n"
                       "     再点一次，等看到已登录的头像再关窗口。")
    try:
        data = json.loads(src.read_text(encoding="utf-8"))
    except Exception as e:
        return False, f"❌ cookie 文件读不出来: {type(e).__name__}: {e}"
    # ★ 和 bilibili_tool 的判据一致：**它只主动检查 SESSDATA**
    if not data.get("SESSDATA"):
        return False, ("❌ cookie 里没有 SESSDATA —— 说明**还没真的登录**。\n"
                       "     请再点一次，登录成功后**先看到自己的头像**再关窗口。")
    try:
        COOKIE_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, COOKIE_DST)
    except Exception as e:
        return False, f"❌ 复制 cookie 到项目失败: {type(e).__name__}: {e}"
    _say(on_line, f"✅ B站已开通（cookie 已写入 {COOKIE_DST.relative_to(ROOT)}）")
    return True, (f"✅ B站已开通！拿到了 {len(data)} 个 cookie（含 SESSDATA）。\n"
                  f"     文件位置：{COOKIE_DST.relative_to(ROOT)}\n"
                  f"     ★ 如果桌宠已经在运行，**重启一次**它才会读到（工具会缓存 cookie）。")


def login_pixiv(on_line=None) -> tuple:
    """★ 引导用户**登录一次 P站**，把 `refresh_token` 写进 `.env`。"""
    miss = [m for m in missing_deps() if m in ("gppt", "pixivpy3")]
    if miss:
        return False, ("❌ 还没装依赖：" + "、".join(miss) + "\n"
                       "     在项目目录里跑这条就行：\n     " + install_hint())
    try:
        mod = _toolkit()
    except Exception as e:
        return False, f"❌ 加载工具包失败: {type(e).__name__}: {e}"

    _say(on_line, "正在打开浏览器，请在窗口里**登录 Pixiv**（只需这一次）。")
    try:
        token = mod.pixiv_login()
    except Exception as e:
        return False, f"❌ 登录流程出错: {type(e).__name__}: {e}"
    if not token:
        return False, ("❌ 没拿到 refresh_token。\n"
                       "     常见原因：登录没完成 / 网络需要代理（P站国内要代理）。")
    try:
        _set_env("PIXIV_REFRESH_TOKEN", token)
    except Exception as e:
        return False, f"❌ 写 .env 失败: {type(e).__name__}: {e}"
    _say(on_line, "✅ P站已开通（refresh_token 已写入 .env）")
    return True, ("✅ P站已开通！refresh_token 已写进 .env（不进版本库）。\n"
                  "     如果桌宠已经在运行，**重启一次**它才会读到。")


def _say(on_line, text: str):
    """把进度说给界面听（有回调就走回调，没有就算了）。"""
    try:
        if on_line:
            on_line(text)
    except Exception:
        pass


def _set_env(key: str, value: str):
    """写/改 `.env` 里的一项（**不动别的内容**，也不打印密钥）。"""
    env = ROOT / ".env"
    lines = []
    if env.is_file():
        lines = env.read_text(encoding="utf-8").splitlines()
    out, done = [], False
    for ln in lines:
        if ln.strip().startswith(key + "="):
            out.append(f"{key}={value}")
            done = True
        else:
            out.append(ln)
    if not done:
        out.append(f"{key}={value}")
    env.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")


def status() -> dict:
    """两个功能各自开通了没（给设置界面显示）。"""
    bili_ok = False
    try:
        if COOKIE_DST.is_file():
            bili_ok = bool(json.loads(COOKIE_DST.read_text(encoding="utf-8"))
                           .get("SESSDATA"))
    except Exception:
        pass
    pixiv_ok = False
    try:
        from src.core.first_run import _env_val
        pixiv_ok = bool(_env_val("PIXIV_REFRESH_TOKEN"))
    except Exception:
        try:
            env = (ROOT / ".env").read_text(encoding="utf-8")
            pixiv_ok = "PIXIV_REFRESH_TOKEN=" in env
        except Exception:
            pass
    return {"bilibili": bili_ok, "pixiv": pixiv_ok,
            "missing_deps": missing_deps()}
