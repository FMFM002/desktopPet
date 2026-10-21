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
import logging
import sys
from pathlib import Path

_log = logging.getLogger("AccountLogin")

from src.utils.runtime import frozen_feature_hint, is_frozen   # noqa: E402

# ★★ 1.9.4（用户 2026-10-19）：**等你登录的上限，5 分钟 → 8 分 20 秒**。
#   原来两条路各是 300 秒；用户实测"扫码/输密码 + 代理慢"时 5 分钟不够
#   （尤其 P站还要过一遍人机验证）。按用户要求**再宽 200 秒**。
#   ★ 只放宽"等人登录"这一段 —— 页面加载、跳转等待都没动，失败报错不会更慢太多。
LOGIN_WAIT_S = 500.0

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
    """给人看的安装命令（**装进桌宠自己的解释器**）。

    ★★ 1.9.4 exe：冻结环境里 `sys.executable` 是 **exe 自己**，`-m pip` 不可能成立
       —— 所以这里不能再拼命令，否则界面会显示一条**照着做会再开一个桌宠**的假命令。
    """
    if is_frozen():
        return ("⚠ exe 里没有 pip。正常安装的 exe **自带**了这些依赖"
                "（含 playwright 的 Edge 驱动）；\n"
                "     只有**精简版 exe** 才会缺，请换完整版或改用便携 zip 版。")
    return (f'"{sys.executable}" -m pip install '
            f'playwright requests pixivpy3 gppt')


def feature_available() -> tuple[bool, str]:
    """「一键开通 B站/P站」能不能用；不能用时给一句**可操作**的话。

    ★★ 1.9.4：判据从"**是不是 exe**"改成"**依赖到底在不在**"。
      以前 exe 里把 playwright 排除了，所以在 exe 里**永远**不可用；
      现在完整版 exe 已经带上 playwright + 驱动 → exe 里也能一键开通。
      只有"精简版 exe / 没装依赖"才降级（降级也要说清怎么补救，不许崩）。
    """
    try:
        has_pw = importlib.util.find_spec("playwright") is not None
    except Exception:                                # noqa: BLE001
        has_pw = False
    if has_pw:
        return True, ""
    if is_frozen():
        # 精简版 exe（没带 playwright）：用统一口径说清"exe 里没有 + 怎么补救"
        return False, (frozen_feature_hint("一键开通 B站/P站")
                       + "\n     ★ 或换**完整版 exe**（多约 107MB，已自带 Edge 驱动）。")
    return False, ("❌ 还没装 playwright（浏览器驱动）。\n"
                   "     在项目目录里跑这条就行：\n     " + install_hint())


def _bili_login_browser(timeout: float = LOGIN_WAIT_S, on_line=None) -> tuple:
    """★★ 自己实现 B站登录：**轮询 cookie**，不再用工具包的 `bili_login()`。

    ## 为什么不能直接用工具包的 `bili_login()`（真机踩到的坑）
      它里面是：
          print("请在弹出的 Edge 里登录 B站，登录完成后**回终端按回车**...")
          input()          # ← ★★★
      那是**给命令行用的**。而桌宠是 GUI 进程，**没有终端 stdin** ——
      `input()` 永远等不到回车 → 工作线程**永久阻塞** →
      设置页按钮永远停在"正在登录…"、状态永远"未开通"、也**不落任何日志**
      （用户 2026-10-18 截图："我确实登陆了，但是刷新状态仍然显示正在登陆"）。

    ## 现在的做法
      打开 Edge（持久化 profile）→ 每 2 秒读一次 context 的 cookie →
      **一旦出现 `SESSDATA` 就立刻导出并返回** → 关窗口。
      整个过程有 `timeout` 上限（默认 `LOGIN_WAIT_S` = 8 分 20 秒），**绝不会挂死**。

    ## 顺带处理 412 风控
      用户截图里 B站 返回了 `错误号 412 / 触发安全风控策略`。
      加 `--disable-blink-features=AutomationControlled` 去掉最明显的自动化特征
      （`navigator.webdriver`）；若页面确实是 412，就把这件事**明确报出来**，
      而不是让他对着一个空白页干等。

    返回 `(cookie dict 或 None, 给人看的话)`。
    """
    import time
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None, ("❌ 没装 playwright。\n"
                      f"     在项目目录里跑：\n     {install_hint()}")

    WORK.mkdir(parents=True, exist_ok=True)
    _profile = WORK / "edge_bili_profile"
    _say(on_line, "正在打开浏览器…")
    try:
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(
                user_data_dir=str(_profile),
                channel="msedge",
                headless=False,
                viewport={"width": 1280, "height": 900},
                locale="zh-CN",
                args=["--disable-blink-features=AutomationControlled"],
            )
            try:
                page = ctx.new_page()
                try:
                    page.goto("https://www.bilibili.com",
                              wait_until="domcontentloaded", timeout=60000)
                except Exception as e:
                    _log.warning(f"打开 B站首页有问题（继续等登录）: {e}")

                _say(on_line, "请在浏览器窗口里**登录 B站**；登录成功后我会自动完成，"
                              "不需要你按任何键。")
                _deadline = time.time() + max(30.0, timeout)
                _saw_412 = False
                while time.time() < _deadline:
                    try:
                        _cookies = {
                            c["name"]: c["value"]
                            for c in ctx.cookies("https://www.bilibili.com")
                            if "bilibili.com" in c["domain"]
                        }
                    except Exception:
                        _cookies = {}
                    if _cookies.get("SESSDATA"):
                        (WORK / "bili_cookies.json").write_text(
                            json.dumps(_cookies, ensure_ascii=False, indent=2),
                            encoding="utf-8")
                        try:
                            ctx.storage_state(path=str(WORK / "bili_state.json"))
                        except Exception:
                            pass
                        _say(on_line, "已检测到登录成功，正在保存凭据…")
                        return _cookies, "ok"
                    # 412 检测（只在第一次报，别刷屏）
                    if not _saw_412:
                        try:
                            _txt = page.content()[:4000]
                            if "412" in _txt and ("风控" in _txt or "security control" in _txt):
                                _saw_412 = True
                                _say(on_line, "⚠ B站把这个浏览器判成自动化了（错误 412 风控）。"
                                              "如果页面白屏，可以在窗口里手动点一下"
                                              "「刷新」或重新打开 bilibili.com 再登录。")
                        except Exception:
                            pass
                    time.sleep(2.0)
                return None, ("❌ 等了一会儿还是没检测到登录（{:.0f} 秒）。\n"
                              "     常见原因：\n"
                              "       · 还没真的登进去（要点「登录」并完成扫码/密码）；\n"
                              "       · B站 412 风控把自动化浏览器拦了 —— 在窗口里"
                              "手动刷新一次 bilibili.com 再登录。").format(timeout)
            finally:
                try:
                    ctx.close()
                except Exception:
                    pass
    except Exception as e:
        return None, f"❌ 启动浏览器失败: {type(e).__name__}: {e}"


def login_bilibili(on_line=None) -> tuple:
    """★ 引导用户**登录一次 B站**，然后把 cookie 搬到 `data/cookies/bilibili.json`。

    返回 `(成功?, 给人看的话)`。**不抛异常**（界面要能拿到结果）。
    """
    ok, why = feature_available()
    if not ok:
        return False, why
    miss = [m for m in missing_deps() if m in ("playwright", "requests")]
    if miss:
        return False, ("❌ 还没装依赖：" + "、".join(miss) + "\n"
                       "     在项目目录里跑这条就行：\n     " + install_hint())
    try:
        _toolkit()          # 只为确认工具包在（顺便把目录准备好）
    except Exception as e:
        return False, f"❌ 加载工具包失败: {type(e).__name__}: {e}"

    _say(on_line, "正在打开浏览器，请在窗口里**登录 B站**（只需这一次）。")
    # ★ 用自己实现的轮询版，**不用**工具包的 `bili_login()`（它里面 `input()` 会挂死）
    cookies, why = _bili_login_browser(on_line=on_line)
    if not cookies:
        return False, why
    data = cookies
    # ★ 和 bilibili_tool 的判据一致：**它只主动检查 SESSDATA**
    if not data.get("SESSDATA"):
        return False, ("❌ cookie 里没有 SESSDATA —— 说明**还没真的登录**。\n"
                       "     请再点一次，登录成功后**先看到自己的头像**再关窗口。")
    try:
        COOKIE_DIR.mkdir(parents=True, exist_ok=True)
        # ★★★ 1.9.4 真机修复（2026-10-18）：**格式必须和 bilibili_tool 对齐**。
        #   `bilibili_tool._parse_json()` 的读法是：
        #       items = data.get("cookies", []) if isinstance(data, dict) else data
        #       for c in items: c.get("name") / c.get("value")
        #   —— 它要的是 **`[{"name","value"}, ...]`**（或 `{"cookies":[...]}`）。
        #   而我之前写的是**扁平字典** `{"SESSDATA": "...", ...}` →
        #   `data.get("cookies", [])` 直接是 `[]` → **工具读到 0 条 cookie**
        #   → 下载仍然只有 480P。用户真机实测：文件 26 条、`_get_cookie_string()`
        #   返回长度 **0**。这就是"登录成功了但还是低画质"的真因。
        COOKIE_DST.write_text(
            json.dumps([{"name": k, "value": v} for k, v in data.items()],
                       ensure_ascii=False, indent=2),
            encoding="utf-8")
    except Exception as e:
        return False, f"❌ 写入 cookie 到项目失败: {type(e).__name__}: {e}"
    _say(on_line, f"✅ B站已开通（cookie 已写入 {COOKIE_DST.relative_to(ROOT)}）")
    return True, (f"✅ B站已开通！拿到了 {len(data)} 个 cookie（含 SESSDATA）。\n"
                  f"     文件位置：{COOKIE_DST.relative_to(ROOT)}\n"
                  f"     ★ 如果桌宠已经在运行，**重启一次**它才会读到（工具会缓存 cookie）。")


def _verify_pixiv_token(token: str) -> tuple:
    """用**一次** Pixiv API 调用验证令牌到底能不能用。

    ★★ 1.9.4（用户 2026-10-18）：他说"不要读项目文件夹以外的文件，
      别人电脑的情况不一样。**没有就是没有**"。
      ⇒ 所以**不再去读** `~/.config/gppt/...` 那种开发者本机的文件；
        改成**用它给的令牌真调一次 API** —— 这才是"这令牌到底能不能用"的
        唯一可信判据，而且**只依赖我们自己的凭据**。

    返回 `(可用?, 说明)`。网络问题与令牌失效要区分开。
    """
    try:
        from pixivpy3 import AppPixivAPI
    except ImportError:
        return None, "没装 pixivpy3，跳过验证"
    api = AppPixivAPI()
    try:
        api.auth(refresh_token=token)
    except Exception as e:
        _m = str(e)
        # ★★ 1.9.4 真机（2026-10-21）：405/403/503 **很可能只是 pixiv 在维护**。
        #   实测：维护期间浏览器 UA 访问首页/令牌端点都是 503 + "Under maintenance"，
        #   而 App UA 的 POST 会被边缘直接挡成 **405**。先探测一次，别让用户白折腾。
        if any(k in _m for k in ("405", "503")) or "cloudflare" in _m.lower():
            try:
                from src.core.pixiv_status import check as _px_check, maintenance_hint
                if _px_check().get("under_maintenance"):
                    _log.warning("[Pixiv] 令牌验证失败，且探测到 pixiv 正在维护：%s", _m[:160])
                    return None, maintenance_hint()
            except Exception:                              # noqa: BLE001
                pass
            _log.warning("[Pixiv] 令牌验证被拦：%s", _m[:200])
            return None, ("pixiv 的 API 被拦了（HTTP 405/503）。\n"
                          "     先看 pixiv 官网是不是维护页；不是再换代理节点。")
        # 401/403 之类 = 令牌无效；但 403 有时也是维护，先探一下（结果缓存 60 秒）
        if any(k in _m for k in ("401", "403", "invalid_grant", "Unauthorized")):
            if "403" in _m:
                try:
                    from src.core.pixiv_status import check as _px_check, maintenance_hint
                    if _px_check().get("under_maintenance"):
                        return None, maintenance_hint()
                except Exception:                          # noqa: BLE001
                    pass
            return False, f"令牌无效（{_m[:80]}）"
        _log.warning("[Pixiv] 令牌验证请求失败：%s", _m[:200])
        return None, f"没能验证（网络/代理问题）：{_m[:80]}"
    try:
        api.illust_ranking(mode="day")
        return True, "令牌可用"
    except Exception as e:
        _m = str(e)
        if any(k in _m for k in ("401", "403", "invalid_grant")):
            return False, f"令牌无效（{_m[:80]}）"
        return None, f"令牌已保存，但验证时网络/代理有问题：{_m[:80]}"


def _wait_for_redirect_tolerant(page, timeout_ms: int) -> None:
    """等"登录完成"—— **不再依赖 networkidle**（1.9.4 真机第二轮修复）。

    ## 真机现象（用户 2026-10-20 00:2x）
        P站点了「一键开通」→ Edge 打开了 → **登录成功了** →
        但界面一直转圈，随后浏览器自己关掉，报「等太久没有结果（超时）」；
        再点一次、同样的操作**又成功了**。日志里能看到 Edge 标题从
        「登录 | pixiv」变成「Online community for artists | pixiv」——
        说明**登录本身早就完成了**，是我们这边没识别出来。

    ## 根因：gppt 原来的等待条件太苛刻
        gppt/browser.py 的 `_wait_for_redirect()`：
            page.wait_for_url(pixiv://..., wait_until="networkidle", timeout=...)
        ① **`networkidle`**：pixiv 首页会一直挂着长连接/轮询/广告请求，
           可能**永远到不了 networkidle** → 明明登录完了还在等；
        ② **`wait_for_url` 要求 URL 变成 `pixiv://`**：那是自定义协议，
           Edge 没有注册处理器 → 要么被拦、要么整页跳到 pixiv 首页
           （用户日志里就是后一种）→ URL 永远不匹配 → 一直等到超时。

    ## 现在的判据（只看"人到底登进去了没"）
        每 250ms 看一次 `page.url`：
          · 变成 `pixiv://…`（或带 `code=`）→ 直接返回；
          · **离开登录页**（URL 里不再有 login）→ 也返回（说明跳走了）；
        剩下"code 到底抓没抓到"交给 gppt 自己判断 —— 它抓不到会抛
        `Did not capture an authorization code`，比"等超时"精确得多。

    ★ 这样最坏情况仍然是 timeout_ms（`LOGIN_WAIT_S` = 500 秒），
      但**正常登录会在跳转的那一瞬间返回**，不会再"一直转圈"。
    """
    import time as _t                                      # noqa: PLC0415

    deadline = _t.time() + max(5.0, timeout_ms / 1000.0)
    while _t.time() < deadline:
        try:
            url = page.url or ""
        except Exception:                                  # noqa: BLE001
            return                                         # 页面被关了：交给上层报错
        if url.startswith("pixiv://") or "code=" in url:
            return
        if "login" not in url.lower():
            return                                         # 跳走了 = 登录动作完成
        try:
            page.wait_for_timeout(250)
        except Exception:                                  # noqa: BLE001
            return


def _wait_for_form_better(page) -> None:
    """等 pixiv 的登录表单出现；**把三种失败分开**（1.9.4 真机第三轮）。

    ## 为什么不能直接用 gppt 原来的 `_wait_for_form`
        它把**所有** playwright 错误都包成同一句
            "Login form did not appear. Please check connectivity for ..."
        于是下面两种完全不同的情况在界面上长得一模一样：
          · pixiv **522 / Cloudflare 挡了** → 登录页根本没加载（用户截图就是这种）；
          · 用户**手快把窗口关了** → 也是同一句，被误解成"网络问题"。

    ## 现在
      · 错误里含 "closed" → **原样抛出**（上层会翻译成"你把浏览器关掉了"）；
      · 其它 → 先往日志里记下当时的 `url` 与 `title`（这就是诊断 522 的铁证），
        再抛 gppt 原本那句（保持错误类型不变，别让上层逻辑失配）。
    """
    import gppt.browser as _gb                              # noqa: PLC0415
    from playwright.sync_api import Error as _PWError       # noqa: PLC0415

    try:
        page.wait_for_selector(_gb.USERNAME_SELECTOR, timeout=_gb.FORM_TIMEOUT_MS)
    except _PWError as exc:
        if "closed" in str(exc).lower():
            raise                                           # 用户关窗口：交给上层说清楚
        try:
            _log.warning("[Pixiv] 登录页没出现：url=%s ，title=%s",
                         page.url, page.title())
        except Exception:                                   # noqa: BLE001
            pass
        raise _gb.LoginError(
            "Login form did not appear. Please check connectivity for "
            f"{_gb.LOGIN_URL}") from exc


def _login_gppt_with_system_edge(headless: bool = False):
    """★ 1.9.4 真机修复（2026-10-19）：让 **gppt 5.x 用系统自带的 Edge** 登录。

    ## 真机现象（用户截图，P站失败 / B站成功）
        Pixiv 登录失败（gppt5:login）: Error: BrowserType.launch:
        Executable doesn't exist at
        ...\\playwright\\driver\\package\\.local-browsers\\chromium-1223\\chrome-win64\\chrome.exe
        Looks like Playwright was just installed or updated.
        Please run the following command to download new browsers: playwright install
    而且**闪了一下终端黑窗**、浏览器**根本没出现**。开了 VPN 也一样（跟网络无关）。

    ## 根因
    gppt 5.x 的 `fetch_authorization()` 里有这两行（`gppt/browser.py`）：
        install([pw.chromium], with_deps=True)      # ← 去下 ~150MB 的 Chromium
        browser = pw.chromium.launch(headless=headless, args=BROWSER_ARGS)
    也就是说它**默认用 playwright 自带的 Chromium**，而不是系统浏览器。
    我们的 exe 只带了驱动（node.exe），**没有也不该带** Chromium（+150MB 起），
    于是：它先去"安装"（在 exe 里表现为**闪一个终端黑窗**，而且装不成）→
    launch 找不到 chrome.exe → 报上面那条。

    ## 做法（最小、且不改第三方代码）
      ① `gppt.browser.install` 换成 no-op —— 别再去下 Chromium（也顺手消掉黑窗）；
      ② 给 `BrowserType.launch` 补一个 `channel="msedge"` —— 用**系统 Edge**，
         和 B站那条路（`_bili_login_browser`）用的是同一个浏览器；
      ③ 把 gppt 的 `MANUAL_TIMEOUT_MS`（等人登录）改成 `LOGIN_WAIT_S` ——
         它写死 300 秒；
      ④ **换掉它的 `_wait_for_redirect`** —— 原来靠 networkidle + URL 匹配，
         pixiv 首页永远不 idle，登录完成了也会等成超时（见上面那个函数的注释）；
      ⑤ 新开的页面把**导航超时**放宽到 120 秒 —— 走 VPN 时 pixiv 首次加载很慢，
         playwright 默认 30 秒会把还在下载的页面判死。
      ★ 五处只在这一个调用期间生效，`finally` 里还原，绝不影响别的功能。
    """
    import gppt.browser as _gb                              # noqa: PLC0415
    from playwright.sync_api import BrowserContext as _BC   # noqa: PLC0415
    from playwright.sync_api import BrowserType as _BT      # noqa: PLC0415

    _orig_install = _gb.install
    _orig_launch = _BT.launch
    _orig_manual_ms = getattr(_gb, "MANUAL_TIMEOUT_MS", None)
    _orig_wait_redirect = getattr(_gb, "_wait_for_redirect", None)
    _orig_wait_form = getattr(_gb, "_wait_for_form", None)
    _orig_new_page = _BC.new_page

    def _no_install(*_a, **_k):
        # gppt 想装它自带的 Chromium —— 我们不用它，直接用系统 Edge
        return None

    def _launch_edge(self, **kwargs):
        kwargs.setdefault("channel", "msedge")
        return _orig_launch(self, **kwargs)

    def _new_page_slow_ok(self, **kwargs):
        pg = _orig_new_page(self, **kwargs)
        try:
            # 走 VPN 时 pixiv 首次加载可能远超 playwright 默认的 30 秒
            pg.set_default_navigation_timeout(120_000)
        except Exception:                                   # noqa: BLE001
            pass
        return pg

    try:
        _gb.install = _no_install
        _BT.launch = _launch_edge
        _BC.new_page = _new_page_slow_ok
        if _orig_manual_ms is not None:
            _gb.MANUAL_TIMEOUT_MS = int(LOGIN_WAIT_S * 1000)
        if _orig_wait_redirect is not None:
            _gb._wait_for_redirect = _wait_for_redirect_tolerant
        if _orig_wait_form is not None:
            _gb._wait_for_form = _wait_for_form_better
        return _gppt_login(headless)
    finally:
        _gb.install = _orig_install
        _BT.launch = _orig_launch
        _BC.new_page = _orig_new_page
        if _orig_manual_ms is not None:
            _gb.MANUAL_TIMEOUT_MS = _orig_manual_ms
        if _orig_wait_redirect is not None:
            _gb._wait_for_redirect = _orig_wait_redirect
        if _orig_wait_form is not None:
            _gb._wait_for_form = _orig_wait_form


def _gppt_login(headless: bool):
    """真正调 gppt 的登录（抽出来只是为了让上面的 monkeypatch 有 finally）。"""
    import gppt as _gppt
    return _gppt.login(headless=headless)


def _humanize_pixiv_error(err) -> str:
    """把 playwright / gppt 的英文长报错翻成**用户能照做**的话。

    ★ 用户反馈（2026-10-19）："有模糊不清的信息，有歧义的地方"。
      原始报错里有一整段
          Looks like Playwright was just installed or updated. ...
      但真正的结论只有一句 —— 所以这里按"看得懂 + 下一步做什么"重写。

    ★ 1.9.4 第三轮（用户 2026-10-20 手动关掉浏览器 / pixiv 522）补了三种：
      · **登录页根本没加载出来**（gppt 的 "Login form did not appear"）——
        真机截图里是 Cloudflare `522 Connection timed out`（app-api.pixiv.net 不可达），
        这跟"你没登录"完全不是一回事，必须分开说；
      · **用户自己把浏览器关了**（TargetClosedError / "has been closed"）；
      · **登录页出来了但没抓到 code**（"Did not capture an authorization code"）。
    """
    s = f"{type(err).__name__}: {err}"
    low = s.lower()
    if "executable doesn't exist" in low or "playwright install" in low:
        return ("❌ 找不到浏览器！\n"
                "     这个版本应该用**系统自带的 Edge**，但没能启动它。\n"
                "     请确认：① 机器上装了 Microsoft Edge；② 不是精简版 exe。\n"
                "     ★ 不想折腾的话：先用**便携 zip 版**开通 P站，"
                "再把 .env 拷到 exe 旁边。")
    if "login form did not appear" in low:
        return ("❌ pixiv 的**登录页根本没打开**（不是你没登录）。\n"
                "     常见原因：代理/VPN 没覆盖 `app-api.pixiv.net`，\n"
                "     或者 pixiv 那边回了 Cloudflare 522/超时（截图里那种页面）。\n"
                "     可以这样确认：在浏览器里打开\n"
                "       https://app-api.pixiv.net/web/v1/login\n"
                "     如果能正常出登录框，再回来点一次；打不开就先修代理节点。")
    if ("has been closed" in low or "targetclosed" in low
            or "browser closed" in low or "page closed" in low):
        return ("❌ 浏览器被关掉了，这次登录没完成。\n"
                "     要开通的话请再点一次「一键开通」，并且**别提前关窗口** ——\n"
                "     登录成功后页面会自己跳走，窗口会自己关。")
    if "did not capture an authorization code" in low:
        return ("❌ 登录页打开了，但**没抓到授权码**（可能没真正登进去）。\n"
                "     请在窗口里完整走完登录（看到 pixiv 首页/头像）再等它自己关窗口。\n"
                "     如果一直停在验证码或报错页，多半还是代理的问题。")
    if "failed to login" in low or "authorization code" in low:
        return ("❌ 登录没有完成（浏览器里可能没登进去，或被 pixiv 拦了）。\n"
                "     常见原因：代理没生效 / 页面停在验证码 / 窗口被提前关掉。\n"
                "     建议：先确认浏览器里能正常打开 pixiv.net，再点一次。")
    if "timeout" in low:
        return ("❌ 等太久没有结果（超时）。\n"
                "     常见原因：代理不通、pixiv 加载很慢，或者登录窗口被关掉了。")
    return s[:400]


def login_pixiv(on_line=None, force: bool = False) -> tuple:
    """★ 引导用户**登录一次 P站**，把 `refresh_token` 写进 `.env`。

    ★★ 1.9.4 两条硬要求（用户 2026-10-18）：
      ① "**不要读项目文件夹以外的文件**，别人电脑的情况不一样。**没有就是没有**"
         ⇒ 不再去翻 `~/.config/gppt/` 那种本机文件；
            不去猜"是不是沿用了旧令牌" —— **我们只管自己这一次的结果**。
      ② **界面文字不能出现绝对路径 / 不能露原始 markdown**
         ⇒ 本函数返回的文案是**纯文本**，不写任何本机绝对路径。
            （gppt 自己的令牌存哪，是它的事，我们不提也不读。）

    做法：真的调 gppt 登录 → 拿到令牌 → 写进 `.env` →
          **用一次 API 调用验证它到底能不能用**（这是唯一可信的判据）。
    """
    miss = [m for m in missing_deps() if m in ("gppt", "pixivpy3")]
    if miss and not is_frozen():
        return False, ("❌ 还没装依赖：" + "、".join(miss) + "\n"
                       "     在项目目录里跑这条就行：\n     " + install_hint())
    # ★ 冻结环境：先给"exe 不支持"的明确说法（而不是"没装 gppt"的假象）
    _ok, _why = feature_available()
    if not _ok:
        return False, _why

    _say(on_line, "正在打开浏览器，请在窗口里登录 Pixiv（只需这一次）。")
    # ★★★ 1.9.4 真机修复（2026-10-18）：**gppt 5.x 换了 API**。
    #   参考项目那套 `from gppt import GetPixivToken` 是 **gppt 0.9.x** 的写法；
    #   用户机器上装的是 **gppt 5.1.0**，`GetPixivToken` **已经不存在**了
    #   （实测：`ImportError: cannot import name 'GetPixivToken'`）。
    #   而我原来把那个 ImportError 当成"没装 gppt"来报 —— **误导用户**。
    #   ⇒ 5.x 的正确用法是 `gppt.login(headless=False)`，官方文档说它
    #     "is the **direct replacement** for the v4 `GetPixivToken().login()`"，
    #     而且**不读也不写磁盘**（正好符合用户"不读项目外文件"的要求）。
    #   ⇒ 老版本（0.9.x）保留为回退，两边都试。
    token = None
    tok_obj = None
    try:
        import gppt as _gppt
    except ImportError as e:
        return False, ("❌ 没装 gppt。\n"
                       f"     在项目目录里跑：{install_hint()}\n"
                       f"     （底层错误：{e}）")
    _api = None
    # ★★ 1.9.4：把"开始 / 用了多久 / 原始异常"写进日志。
    #   用户真机报"2 分钟就超时"，但界面只给了翻译后的中文、日志里什么都没有
    #   —— 没有原始异常名和耗时，根本没法判断是哪一个等待超的时。补上。
    import time as _time
    _t0 = _time.time()
    _log.info("[Pixiv] 开始登录（系统 Edge，等人上限 %.0f 秒）", LOGIN_WAIT_S)
    try:
        if hasattr(_gppt, "login"):                     # gppt 5.x
            _api = "gppt5:login"
            # ★ 真机修复（2026-10-19）：**必须用系统 Edge**，
            #   不能让 gppt 去下它自带的 Chromium（exe 里没有，见上面的长注释）。
            tok_obj = _login_gppt_with_system_edge(headless=False)
        elif hasattr(_gppt, "GetPixivToken"):           # gppt 0.9.x（老写法）
            _api = "gppt0:GetPixivToken"
            try:
                tok_obj = _gppt.GetPixivToken(headless=False).login()
            except TypeError:
                tok_obj = _gppt.GetPixivToken().login()
        else:
            return False, ("❌ 装的是 gppt，但**这个版本没有可用的登录接口**。\n"
                           f"     这个 gppt 版本导出的东西："
                           f"{[x for x in dir(_gppt) if not x.startswith('_')][:12]}\n"
                           "     请升级或降级到一个带 login / GetPixivToken 的版本：\n"
                           f"     {install_hint()}")
    except Exception as e:
        _log.warning("[Pixiv] 登录失败（%s），用时 %.1f 秒；原始异常：%s: %s",
                     _api, _time.time() - _t0, type(e).__name__, e, exc_info=True)
        return False, (f"❌ Pixiv 登录失败（{_api}）\n"
                       f"     {_humanize_pixiv_error(e)}")
    _log.info("[Pixiv] 登录返回，用时 %.1f 秒", _time.time() - _t0)

    # 从返回对象里取 refresh_token：5.x 是 Token 对象，老版是 dict / str
    if isinstance(tok_obj, dict):
        token = (tok_obj.get("refresh_token") or "").strip()
    elif isinstance(tok_obj, str):
        token = tok_obj.strip()
    else:
        token = (getattr(tok_obj, "refresh_token", "") or "").strip()
    if not token:
        return False, ("❌ 没拿到 refresh_token（登录可能没完成）。\n"
                       "     再点一次，在浏览器里完成登录再关窗口。")
    try:
        _set_env("PIXIV_REFRESH_TOKEN", token)
    except Exception as e:
        return False, f"❌ 写 .env 失败: {type(e).__name__}: {e}"

    # ★ 验证：真调一次 API，不靠猜
    _say(on_line, "令牌已保存，正在验证它能不能用…")
    ok, why = _verify_pixiv_token(token)
    if ok is True:
        _say(on_line, "✅ P站已开通（令牌验证可用）")
        return True, ("✅ P站已开通：令牌已写进 .env，并且验证可用。\n"
                      "     如果桌宠正在运行，重启一次它才会读到。")
    if ok is None:
        _say(on_line, "令牌已保存，但没能验证（网络/代理）")
        return True, ("⚠ 令牌已写进 .env，但没能验证它是否可用。\n"
                      f"     原因：{why}\n"
                      "     P站国内需要代理；确认代理开着之后，"
                      "再在设置里点一次「刷新状态」即可。")
    # ok is False → 令牌无效，不能说"已开通"
    return False, ("❌ 拿到的令牌验证不通过，所以不算开通成功。\n"
                   f"     原因：{why}\n"
                   "     请再点一次「一键开通」，在浏览器里完整登录一遍。")


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
            _d = json.loads(COOKIE_DST.read_text(encoding="utf-8"))
            # ★ 兼容两种格式：`[{"name","value"}]`（我们写的、工具认的）
            #   和老的扁平 `{"SESSDATA": "..."}`
            if isinstance(_d, dict):
                _flat = _d.get("cookies") if isinstance(_d.get("cookies"), list) else _d
                if isinstance(_flat, list):
                    _flat = {c.get("name"): c.get("value") for c in _flat
                             if isinstance(c, dict)}
                bili_ok = bool((_flat or {}).get("SESSDATA"))
            elif isinstance(_d, list):
                bili_ok = any(isinstance(c, dict) and c.get("name") == "SESSDATA"
                              and c.get("value") for c in _d)
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
