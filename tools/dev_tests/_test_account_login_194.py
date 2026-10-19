# -*- coding: utf-8 -*-
"""★ 1.9.4：**一键开通 B站 / P站** 守卫（**静态**，因为沙箱里跑不了）。

## 用户的要求
  "参考项目 bili_pixiv_toolkit 可以做到开通 P站 和 B站 的功能。
   如果用户要开通这两个功能，就**引导用户登陆一次**就可以了。"

## ★ 为什么这个守卫是**静态**的
  本模块最终会起 Playwright 浏览器（子进程 + 管道）。而 DSH 沙箱**不允许创建管道**
  （`PermissionError: [WinError 5]`，实测过），所以**在沙箱里跑不了真流程**。
  ⇒ 守卫只钉"结构对不对、关键判据在不在"，**真流程必须真机验证**。
    这一点必须写清楚，**不能假装测过了**。

## 钉什么
  A. 工具包**已原样并入** `tools/_vendor/`（第三方代码，不该改它的业务逻辑）；
  B. ★★ 产出的东西**搬到项目该在的位置**：
       B站 → `data/cookies/bilibili.json`（`bilibili_tool` 读这里；
              **B站凭据是文件，不是 .env 键**）
       P站 → `.env` 的 `PIXIV_REFRESH_TOKEN`
  C. ★★ **改写工具包的输出目录** —— 别让它把浏览器 profile / 下载物吐进
     `tools/_vendor/`（那里是第三方代码区，写脏了很乱）；
  D. ★★ B站成功后**必须校验 SESSDATA**（`bilibili_tool` 只认这一个字段）
     —— 没登录成功就不能报"已开通"；
  E. ★★ 依赖缺失或流程出错时，**给可操作的安装命令**（`sys.executable -m pip install ...`），
     且**装进本项目自己的解释器**（参考项目用的是它那台机器的 `py`，混用会 import 不到）；
  F. ★ **不抛异常**（界面要能拿到 `(ok, msg)`），且 `deps()` 单项失败不能带崩。
"""
import os
import re
import sys
from pathlib import Path

os.environ.setdefault("PRTS_NO_COMFY_START", "1")
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))


print("== A. 工具包已原样并入 _vendor ==")
_v = ROOT / "tools" / "_vendor" / "bili_pixiv_toolkit.py"
check("★★第三方工具包在 `tools/_vendor/bili_pixiv_toolkit.py`", _v.is_file(),
      f"{_v.stat().st_size} 字节" if _v.is_file() else "缺失")
_vs = _v.read_text(encoding="utf-8") if _v.is_file() else ""
check("★它里面那两个关键登录函数还在（我们只调这两个）",
      "def bili_login(" in _vs and "def pixiv_login(" in _vs)

_s = (ROOT / "src" / "core" / "account_login.py").read_text(encoding="utf-8")

print("\n== B. 产物搬到项目该在的位置 ==")
check("★★B站 cookie 落到 `data/cookies/bilibili.json`"
      "（bilibili_tool 从这儿读；**B站凭据是文件，不是 .env 键**）",
      'COOKIE_DST = COOKIE_DIR / "bilibili.json"' in _s)
check("★★P站写 `.env` 的 `PIXIV_REFRESH_TOKEN`（pixiv_tool 从这儿读）",
      '_set_env("PIXIV_REFRESH_TOKEN"' in _s)

print("\n== C. 改写工具包的输出目录（别写进 _vendor）==")
for _const in ("BILI_PROFILE", "BILI_COOKIE_FILE", "BILI_STATE_FILE"):
    check(f"★改写 `{_const}` 指向 `data/account_login`",
          f"mod.{_const} = WORK" in _s)

print("\n== D. B站必须校验 SESSDATA 才能报成功 ==")
check("★★校验 `SESSDATA`（bilibili_tool 只认这一个字段）",
      'data.get("SESSDATA")' in _s)
check("★★没拿到/没 SESSDATA 时返回**失败**并有可操作的话",
      "说明**还没真的登录**" in _s or "还没真的登录" in _s)

print("\n== E. 依赖缺失给可操作的安装命令 ==")
check("★★install_hint 用 **sys.executable**（本项目解释器），"
      "不是参考项目那台机器的 `py`（混用会 import 不到）",
      "sys.executable" in _s and "pip install" in _s)
check("★四个依赖都点名了", all(d in _s for d in
                              ("playwright", "requests", "pixivpy3", "gppt")))

print("\n== F. 不抛异常 / 探测不自崩 ==")
check("★★两个登录函数都用 try 包住流程（界面要能拿到 (ok, msg)）",
      _s.count("except Exception as e:") >= 4)
check("★★`deps()` 每一项**单独 try** —— 探测函数不能把整个界面带崩"
      "（沙箱里 `find_spec` 都可能抛 PermissionError）",
      "except Exception:" in _s.split("def deps()")[1].split("def missing_deps")[0])
check("★没有任何 `raise` 直接往上抛（只允许 FileNotFoundError 在 _toolkit 里"
      "由调用方 try 住）",
      "raise " not in _s.split("def login_bilibili")[0].split("def _toolkit")[0])

print("\n== ★★ G. 防泄露：一键开通产生的凭据**绝不能**进发布包 ==")
# ★ 这是我这轮差点漏掉的：新建的 `data/account_login/` 会装
#   **整个 Edge 浏览器 profile + 真实 cookie**，而装配器原来**没排除它**。
_br = (ROOT / "tools" / "build_release.py").read_text(encoding="utf-8")
check("★★★装配器排除了 `data/account_login`"
      "（里面有浏览器 profile + 真实登录凭据，绝不能随包发）",
      '"data/account_login"' in _br)
import importlib.util as _u                                  # noqa: E402
import pathlib as _pl                                        # noqa: E402
_sp = _u.spec_from_file_location("_br", ROOT / "tools" / "build_release.py")
_br_mod = _u.module_from_spec(_sp)
_sp.loader.exec_module(_br_mod)
for _rel in ("data/account_login/edge_bili_profile/Default/Cookies",
             "data/account_login/bili_cookies.json",
             "data/cookies/bilibili.json"):
    _skip, _why = _br_mod._should_skip(_pl.Path(_rel), False)
    check(f"★★实测 `{_rel}` **被排除**", _skip, _why[:20])

print("\n== ★★ H. 根目录的说明文档必须进包（否则用户拿到包却看不到说明）==")
# ★ 收尾时发现的缺口：装配器的 INCLUDE 只认几个固定文件名，
#   所以 `发布说明` / 凭据指南 / 施工说明书 **都没被打进 zip** —— 等于没写。
_br_src = (ROOT / "tools" / "build_release.py").read_text(encoding="utf-8")
for _doc in ("发布说明_1.9.4.txt",
             "B站与P站凭据获取指南_1.9.4.md",
             "施工说明书_ComfyUI集成_1.9.4.txt",
             "评估_画图工具自部署_1.9.4.txt"):
    check(f"★★装配器 INCLUDE 里有 `{_doc}`", f'"{_doc}"' in _br_src)

print("\n== ★ 诚实声明：真流程必须在真机上验 ==")
print("  ⚠ 本守卫**只验结构**。真正弹浏览器登录这件事，"
      "沙箱里起不了子进程/管道（WinError 5），**必须真机试**。")

print("\n== I. ★★★ B站登录**不能再用工具包的 bili_login()**（真机挂死过）==")
# 用户 2026-10-18 真机：登录了但状态一直"正在登录…"、没有 cookie、没有日志。
# 根因：工具包的 `_run_browser(wait_login=True)` 里是
#       print("...回终端按回车...") + `input()`
#   那是给命令行用的；桌宠是 GUI，**没有终端 stdin** → `input()` 永远等不到
#   → 工作线程永久阻塞。
scan = _s   # account_login.py 的源码
check("★★★**没有调用**工具包的 `bili_login()`（它内部 `input()` 会挂死）",
      ".bili_login(" not in scan)
check("★★改成自己实现的轮询版 `_bili_login_browser`", "_bili_login_browser" in scan)
check("★★轮询判据是 **cookie 里出现 SESSDATA**（和 bilibili_tool 的判据一致）",
      'get("SESSDATA")' in scan)
check("★★有**硬超时**（`_deadline`）—— 保证按钮一定能恢复，绝不永久卡住",
      "_deadline" in scan and "timeout" in scan)
check("★去掉最明显的自动化特征（`AutomationControlled`），减少 412 风控",
      "AutomationControlled" in scan)
check("★★会检测并说明 **412 风控**（用户截图里就是 412）",
      '"412"' in scan or "412 " in scan)
check("★超时时给的是**可操作**的话（而不是干等）",
      "没检测到登录" in scan)

print("\n== J. ★★★ cookie 文件格式必须和 bilibili_tool 对齐（真机踩过）==")
# 真机证据（2026-10-18）：登录成功、文件里 26 条 cookie、含 SESSDATA，
# 但 `bilibili_tool._get_cookie_string()` 返回长度 **0** → 下载仍 480P。
# 根因：`_parse_json()` 要的是 **`[{"name","value"}, ...]`**（或 `{"cookies":[...]}`），
#       而我写的是**扁平字典** `{"SESSDATA": "..."}` →
#       `data.get("cookies", [])` 直接得 `[]` → 0 条。
# 修完实测：cookie 串 0 → **1874**，画质 **480P → 1080P（quality=80）**。
check("★★★`account_login` 写的是**列表**格式（`[{'name':…,'value':…}]`），"
      "不是扁平字典 —— 否则工具读到 0 条",
      '[{"name": k, "value": v}' in _s or "[{'name': k, 'value': v}" in _s)
check("★★`status()` 能认两种格式（新的列表 + 老的扁平），"
      "否则升级后会把已开通误报成未开通",
      "isinstance(_d, list)" in _s and "isinstance(_d, dict)" in _s)

print("\n== K. ★★ 低画质提示必须区分「没登录」和「要大会员」==")
_s_bi = (ROOT / "tools" / "bilibili_tool.py").read_text(encoding="utf-8")
# 加了 cookie 之后还写"因为没登录"就是说假话（实测登录后拿到 1080P，4K 才需要大会员）。
check("★★有 cookie 时提示改成「已是普通账号上限 / 再往上要大会员」",
      "已经是普通账号的上限" in _s_bi or "普通账号上限" in _s_bi)
check("★★没 cookie 时才说「因为没登录」", "因为**没登录**" in _s_bi)
check("★★两条路径（DASH / durl）都做了区分"
      "（用共同子串「普通账号」计数 —— 两处措辞略有不同，别用整句匹配）",
      _s_bi.count("普通账号") >= 2)

# ★★★ 1.9.4（用户 2026-10-18 **明确否决**，这段做法整套撤掉）：
#   这里原本钉的是"读 gppt 家目录里的令牌、识别是不是沿用了旧令牌"那套。
#   用户原话："**不要读项目文件夹以外的文件**，别人电脑的情况不一样。
#             **没有就是没有**。"
#   ⇒ 那套做法连同断言一起删掉（**不留放行名单**）；
#     改成 `_verify_pixiv_token()`：**用一次 API 调用**验证令牌能不能用。
#     判据只看"我们自己的凭据能否使用"，不去看别人的机器上有什么。
check("★★`_existing_gppt_token` 那套已**彻底移除**（用户否决：不读项目外文件）",
      "_existing_gppt_token" not in _s)
check("★★★`login_pixiv` 里**不再有**「沿用旧令牌」的分支",
      "沿用它" not in _s and "本来就有" not in _s)
check("★★★**不再走**工具包那个会静默回退的 `pixiv_login()`",
      ".pixiv_login(" not in _s)
check("★★改成直接调 gppt，失败就报**真实错误**（不静默回退）",
      "GetPixivToken" in _s)

print("\n== M. ★★★ 发布版硬要求：不读项目外的文件、界面不露 markdown / 绝对路径 ==")
# 用户 2026-10-18："这是发布版，不可以这样写。还有，**不要读项目文件夹以外的文件**，
#                 别人电脑的情况不一样。**没有就是没有**。"
import re as _re                                          # noqa: E402
_code = "\n".join(ln for ln in _s.splitlines()
                  if not ln.strip().startswith("#"))
check("★★★**不读用户家目录**（`Path.home()` 已移除 —— 别人电脑上没有那些文件）",
      "Path.home()" not in _code)
check("★★★不再去翻 gppt 的本机令牌目录（`_existing_gppt_token` 已删）",
      "_existing_gppt_token" not in _code)
check("★★返回给界面的文案里**没有绝对路径**（不泄露用户名/目录结构）",
      not _re.search(r"[A-Za-z]:\\\\Users", _code)
      and not _re.search(r"[A-Za-z]:\\\\D", _code))
check("★★改成**用一次 API 验证令牌**（`_verify_pixiv_token`）——"
      "这才是「这令牌能不能用」的唯一可信判据",
      "_verify_pixiv_token" in _code and "auth(refresh_token=" in _code)
check("★★令牌验证失败时**不算开通成功**（不能把无效令牌报成已开通）",
      "不算开通成功" in _code)

_ui = (ROOT / "src" / "ui" / "settings_window.py").read_text(encoding="utf-8")
check("★★★设置页会**剥掉 markdown 的 `**`**（QLabel 不认它，会原样显示星号）",
      "def _plain(" in _ui and '.replace("**", "")' in _ui)
check("★★`_lbl` / `_hint` 都过 `_plain`（不只是某一个标签）",
      "_plain(text)" in _ui and _ui.count("_plain(text)") >= 2)

_cli = (ROOT / "tools" / "account_login.py").read_text(encoding="utf-8")
check("★命令行输出也剥掉 `**`（终端里同样不该露）",
      'replace("**", "")' in _cli)

print("\n== N. ★★★ gppt 5.x 换了 API（真机：'没装 gppt' 是误报）==")
# 用户 2026-10-18 截图：P站页报"❌ 没装 gppt"，可命令行 `deps` 明明四个都 ✅。
# 根因：`from gppt import GetPixivToken` 是 **gppt 0.9.x** 的老 API；
#       他装的是 **gppt 5.1.0**，`GetPixivToken` **已不存在**
#       （实测 ImportError: cannot import name 'GetPixivToken'），
#       而我把它当成"没装"来报 → **误导**。
# 5.x 的正确入口是 `gppt.login(headless=False)`（官方文档：v4
# `GetPixivToken().login()` 的 **direct replacement**，且**不读写磁盘**）。
check("★★★支持 **gppt 5.x** 的 `gppt.login(...)`",
      'hasattr(_gppt, "login")' in _s and "_gppt.login(" in _s)
check("★★保留 **gppt 0.9.x** 的 `GetPixivToken` 回退（别把老用户弄坏）",
      'hasattr(_gppt, "GetPixivToken")' in _s)
check("★★能从 **Token 对象**取 `refresh_token`（5.x 返回的是对象，不是 dict）",
      'getattr(tok_obj, "refresh_token"' in _s)
check("★★★**区分**「没装」与「装了但没这个接口」（不能都报成没装）",
      "这个版本没有可用的登录接口" in _s)
check("★★用 `login` 而不是 `get_token` —— 后者会**读写磁盘**上的 gppt profile，"
      "违反用户「不读项目外文件」的要求",
      "_gppt.get_token(" not in _s)
check("★报「没装」时附带**底层错误**（便于日后判断到底是哪种）",
      "底层错误" in _s)

print("\n== O. ★★★ 真机：P站登录失败 —— gppt 默认去下它自带的 Chromium ==")
# 用户 2026-10-19 截图：B站成功（26 个 cookie），P站报
#   "Executable doesn't exist at ...\playwright\driver\package\.local-browsers\
#    chromium-1223\chrome-win64\chrome.exe"，还**闪了一下终端黑窗**、浏览器没出现。
# 根因：gppt 5.x 的 fetch_authorization() 里写死了
#   install([pw.chromium], with_deps=True) + pw.chromium.launch(...)
#   —— 它要的是 **playwright 自带 Chromium**；我们 exe 只带驱动，没有也不该有 Chromium。
check("★★★存在 `_login_gppt_with_system_edge`（强制用系统 Edge）",
      "def _login_gppt_with_system_edge" in _s)
check("★★把 gppt 的 `install(...)` 换成 no-op"
      "（不再下 Chromium，也就不再闪那个黑窗）",
      "_gb.install = _no_install" in _s and "def _no_install" in _s)
check('★★给 `BrowserType.launch` 补 `channel="msedge"`（和 B站用同一个浏览器）',
      'kwargs.setdefault("channel", "msedge")' in _s)
check("★★patch 只在这一次调用期间生效，finally 里还原",
      "_BT.launch = _orig_launch" in _s and "_gb.install = _orig_install" in _s)
check("★★报错必须翻成**人话**（原始那段 Playwright 英文长文对用户没用）",
      "def _humanize_pixiv_error" in _s and "找不到浏览器" in _s)
check("★★pixiv 失败分支走 `_humanize_pixiv_error(e)`，不再整段抛原始异常",
      "_humanize_pixiv_error(e)" in _s)
check("★「找不到浏览器」的说明里给了**下一步**（确认 Edge / 用 zip 版）",
      "Microsoft Edge" in _s and "便携 zip 版" in _s)

# —— 行为自证：patch 真的挂上去了、也真的还原了 ——
sys.path.insert(0, str(ROOT))
import src.core.account_login as _AL                    # noqa: E402
import gppt.browser as _gb                              # noqa: E402
from playwright.sync_api import BrowserType as _BT      # noqa: E402
from playwright.sync_api import BrowserContext as _BC   # noqa: E402

print("\n== P. ★★ 等登录的时间：5 分钟 → 8 分 20 秒（用户 2026-10-19 要求 +200s）==")
check("★★存在统一常量 `LOGIN_WAIT_S = 500`（= 300 + 200）",
      re.search(r"^LOGIN_WAIT_S\s*=\s*500\.0", _s, re.M) is not None,
      "期望 LOGIN_WAIT_S = 500.0")
check("★★B站登录默认用这个常量（不是写死 300）",
      "def _bili_login_browser(timeout: float = LOGIN_WAIT_S" in _s)
check("★★P站（gppt）的 `MANUAL_TIMEOUT_MS` 也要被放宽到同一个值",
      "_gb.MANUAL_TIMEOUT_MS = int(LOGIN_WAIT_S * 1000)" in _s)
check("★★放宽只在这一个调用期间，结束后还原（别污染全局）",
      "_gb.MANUAL_TIMEOUT_MS = _orig_manual_ms" in _s)

print("\n== Q. ★★★ 真机：登录成功却「一直转圈到超时」==")
# 用户 2026-10-20 00:2x：Edge 打开了、登录成功了、页面都跳到 pixiv 首页了，
# 界面却一直转圈，随后浏览器自己关掉 + 报「超时」；再点一次又成功。
# 根因：gppt 的 _wait_for_redirect 用 page.wait_for_url(pixiv://…, wait_until="networkidle")
#   ① pixiv 首页永远到不了 networkidle；② pixiv:// 是自定义协议，Edge 不会真的跳过去。
check("★★★存在 `_wait_for_redirect_tolerant`（不再等 networkidle）",
      "def _wait_for_redirect_tolerant" in _s)
check("★★新判据是**轮询 URL**（250ms 一次），而不是靠 networkidle 事件",
      "while _t.time() < deadline" in _s and "page.wait_for_timeout(250)" in _s)
check("★★判据是「URL 变成 pixiv:// 或**离开登录页**」",
      'url.startswith("pixiv://")' in _s and '"login" not in url.lower()' in _s)
check("★★导航超时放宽到 120 秒（VPN 下 pixiv 首次加载经常 >30 秒）",
      "set_default_navigation_timeout(120_000)" in _s)
check("★★替换 gppt 的等待函数，且调用结束还原",
      "_gb._wait_for_redirect = _wait_for_redirect_tolerant" in _s
      and "_gb._wait_for_redirect = _orig_wait_redirect" in _s)
check("★★失败时把**原始异常 + 耗时**写进日志（下次不用猜是哪一个超时）",
      "[Pixiv] 登录失败" in _s and "用时 %.1f 秒" in _s and "exc_info=True" in _s)

_seen = {}
_orig_login = _AL._gppt_login
_orig_install_name = getattr(_gb.install, "__name__", "?")
_orig_launch_name = getattr(_BT.launch, "__name__", "?")
_orig_manual = getattr(_gb, "MANUAL_TIMEOUT_MS", None)
_orig_redirect = getattr(_gb, "_wait_for_redirect", None)
_orig_new_page = _BC.new_page


def _fake_gppt_login(headless):
    _seen["install"] = getattr(_gb.install, "__name__", "?")
    _seen["launch"] = getattr(_BT.launch, "__name__", "?")
    _seen["headless"] = headless
    # ★ 调用期间 gppt 看到的是不是 500 秒
    _seen["manual_ms"] = getattr(_gb, "MANUAL_TIMEOUT_MS", None)
    # ★ 等待函数是不是被换成了"宽容版"
    _seen["redirect"] = getattr(_gb, "_wait_for_redirect", None) is _AL._wait_for_redirect_tolerant
    return "FAKE_TOKEN"


_AL._gppt_login = _fake_gppt_login
try:
    _tok = _AL._login_gppt_with_system_edge(headless=False)
finally:
    _AL._gppt_login = _orig_login

check("★★调用期间 gppt 的 install 确实变成了 no-op",
      _seen.get("install") == "_no_install", str(_seen.get("install")))
check("★★调用期间 BrowserType.launch 确实换成了 Edge 版",
      _seen.get("launch") == "_launch_edge", str(_seen.get("launch")))
check("★headless=False 原样传下去（P站要人手动登录，不能开无头）",
      _seen.get("headless") is False)
check("★★调用期间 gppt 的等人登录上限 = 500 秒（原来 300）",
      _seen.get("manual_ms") == 500_000, str(_seen.get("manual_ms")))
check("★★调用期间等待函数确实换成了**宽容版**", _seen.get("redirect") is True)
check("★★调用结束后**五处**都还原（install / launch / 超时 / 等待 / new_page）",
      getattr(_gb.install, "__name__", "?") == _orig_install_name
      and getattr(_BT.launch, "__name__", "?") == _orig_launch_name
      and getattr(_gb, "MANUAL_TIMEOUT_MS", None) == _orig_manual
      and getattr(_gb, "_wait_for_redirect", None) is _orig_redirect
      and _BC.new_page is _orig_new_page,
      f"install={getattr(_gb.install, '__name__', '?')} "
      f"launch={getattr(_BT.launch, '__name__', '?')} "
      f"manual={getattr(_gb, 'MANUAL_TIMEOUT_MS', None)} "
      f"redirect_restored={getattr(_gb, '_wait_for_redirect', None) is _orig_redirect} "
      f"new_page_restored={_BC.new_page is _orig_new_page}")
check("★假的登录结果被原样返回（说明包装层没吞掉返回值）", _tok == "FAKE_TOKEN")

print(f"\n==== 结果: {len(PASS)} PASS / {len(FAIL)} FAIL ====")
for f in FAIL:
    print("  FAIL:", f)
sys.exit(1 if FAIL else 0)
