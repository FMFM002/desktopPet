# -*- coding: utf-8 -*-
"""★★ 1.9.4：**一键开通 B站 / P站** 的命令行入口。

用户（2026-10-18）：
    "参考项目 bili_pixiv_toolkit 可以做到开通 P站 和 B站 的功能。
     如果用户要开通这两个功能，就**引导用户登陆一次**就可以了。"

用法：
    python tools/account_login.py status      # 看两个功能开通了没、缺什么依赖
    python tools/account_login.py bilibili    # 弹出浏览器，登录一次 B站
    python tools/account_login.py pixiv       # 弹出浏览器，登录一次 P站
    python tools/account_login.py deps        # 只检查依赖 / 给安装命令

★ 这是 `src/core/account_login.py` 的**第一个调用方**（日后设置界面里的
  「一键开通」按钮也调同一套函数）。
★ 会**弹出真实浏览器**，请在自己的机器上跑；沙箱里起不了子进程。
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core import account_login as A                    # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(
        description="开通 B站 / P站（登录一次即可）")
    ap.add_argument("action", nargs="?", default="status",
                    choices=["status", "deps", "bilibili", "pixiv"])
    a = ap.parse_args()

    if a.action == "deps":
        d = A.deps()
        for k, v in d.items():
            print(f"  {'✅' if v else '❌'} {k}")
        miss = A.missing_deps()
        if miss:
            print(f"\n  缺 {len(miss)} 个，跑这条装上：\n     {A.install_hint()}")
            return 1
        print("\n  依赖齐了。")
        return 0

    if a.action == "status":
        s = A.status()
        print(f"  B站：{'✅ 已开通' if s['bilibili'] else '❌ 未开通'}")
        print(f"  P站：{'✅ 已开通' if s['pixiv'] else '❌ 未开通'}")
        if s["missing_deps"]:
            print(f"  缺依赖：{'、'.join(s['missing_deps'])}")
            print(f"     跑这条装上：{A.install_hint()}")
        return 0

    fn = A.login_bilibili if a.action == "bilibili" else A.login_pixiv
    print(f"  ── 开始开通 {a.action} ──")
    # ★ 1.9.4：终端里不显示 markdown 的 `**`（用户："这是发布版，不可以这样写"）
    ok, msg = fn(on_line=lambda t: print(f"  · {str(t).replace('**', '')}"))
    print(f"\n  {'✅ 成功' if ok else '❌ 失败'}")
    print("  " + str(msg).replace("**", "").replace("\n", "\n  "))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
