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

print(f"\n==== 结果: {len(PASS)} PASS / {len(FAIL)} FAIL ====")
for f in FAIL:
    print("  FAIL:", f)
sys.exit(1 if FAIL else 0)
