# -*- coding: utf-8 -*-
"""★★ 1.9.4：**发布前自检**（只读，不改任何东西）。

## 为什么需要它
1.9.4 做了 19 次提交，验证散落在：回归留档、五个守卫套件、真机启动演练、
以及若干"**只有人能做**"的确认项（双击 exe、编译安装程序）。
发布前要一条条回想"我还差什么"太容易漏。

**跑这一条命令，就知道现在离发布还差什么。**

## 用法
    python tools/preflight.py            # 人类可读
    python tools/preflight.py --json     # 给脚本/CI 用

## 设计原则
  · **只读**：不构建、不写入、不动用户的配置；
  · **如实**：把"未验证项"和"必须人工确认项"分开列，**不把没验过的说成验过了**；
  · 每条都给出**下一步的确切命令或动作**。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.core.models import status_all, total_size_hint      # noqa: E402
from src.utils.proc import no_window_kwargs as _nw           # noqa: E402
from src.version import APP_ID, __version__                  # noqa: E402

OK, WARN, TODO = "OK", "WARN", "TODO"


def _run(cmd: list, timeout: int = 120) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", cwd=str(ROOT), timeout=timeout,
                           # ★ 1.9.4：别弹控制台黑窗口（回归会反复跑这里，用户被闪过）
                           **_nw())
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except Exception as e:
        return 1, f"{type(e).__name__}: {e}"


def collect() -> dict:
    items: list[dict] = []

    def add(status: str, name: str, detail: str = "", action: str = ""):
        items.append({"status": status, "name": name, "detail": detail,
                      "action": action})

    # ── 1. 版本一致性 ──
    rc, out = _run([sys.executable, str(ROOT / "tools" / "dev_tests"
                                       / "_test_version_193.py")])
    add(OK if rc == 0 else WARN, "版本信息一致（README 标题 == src/version.py，"
        "全仓无漂移的 vX.Y.Z）", f"当前 {__version__}",
        "" if rc == 0 else "python tools/dev_tests/_test_version_193.py")

    # ── 2. 个人信息不随包 ──
    rc2, out2 = _run([sys.executable, str(ROOT / "tools" / "dev_tests"
                                         / "_test_release_pack_194.py")], timeout=300)
    add(OK if rc2 == 0 else WARN,
        "发布装配器**不含**个人信息（密钥/DB/日志/私有目录）",
        "已由守卫验证（含反向守卫）" if rc2 == 0 else (out2.strip()[-120:] or ""),
        "" if rc2 == 0 else "python tools/dev_tests/_test_release_pack_194.py")

    # ── 3. 独立运行（缺模型不崩）──
    rc3, _ = _run([sys.executable, str(ROOT / "tools" / "dev_tests"
                                      / "_test_standalone_194.py")])
    add(OK if rc3 == 0 else WARN, "独立运行：可选依赖缺失时**优雅降级**（不崩）",
        "已由守卫验证" if rc3 == 0 else "",
        "" if rc3 == 0 else "python tools/dev_tests/_test_standalone_194.py")

    # ── 4. 模型状态（不随包，按需）──
    rows = status_all()
    miss = [r["key"] for r in rows if not r["present"]]
    legacy = [r["key"] for r in rows if r["suggest_migrate"]]
    add(OK if not miss else WARN,
        f"模型状态（{len(rows)} 个，合计 {total_size_hint()}）｜**不随包发布**",
        (f"缺失 {miss}" if miss else "全部就位")
        + (f"；还在旧位置（建议迁移）: {legacy}" if legacy else ""),
        "python tools/model_manager.py migrate --dry-run" if legacy else "")

    # ── 5. 三种发布形态的产物 ──
    zipA = ROOT / "dist" / f"{APP_ID}-{__version__}-portable.zip"
    dirB = ROOT / "dist" / f"{APP_ID}-{__version__}"
    exeB = dirB / f"{APP_ID}-{__version__}.exe"
    iss = ROOT / "installer" / "PRTS.iss"
    add(OK if zipA.is_file() else TODO, "A 便携 zip 已产出",
        f"{zipA.name}（{zipA.stat().st_size/1024/1024:.1f} MB）" if zipA.is_file() else "还没构建",
        f"python tools/build_release.py --zip")
    add(OK if exeB.is_file() else TODO, "B exe 已产出",
        f"{exeB.name}" if exeB.is_file() else "还没构建",
        "python -m PyInstaller PRTS.spec --noconfirm")
    add(OK if iss.is_file() else TODO, "C 安装脚本已就绪",
        f"{iss.name}" if iss.is_file() else "缺 installer/PRTS.iss",
        "" if iss.is_file() else "需先有 installer/PRTS.iss")

    # ── 6. ★★ 只有人能做的确认项（**不许当成已验证**）──
    add(TODO, "【人工】A 的真机启动演练",
        "解压 zip 到**另一个路径**、双击「启动.bat」→ 应弹出配置引导；"
        "填真 Key 后应能正常聊天。"
        "（开发机上已验证过'解压→启动→弹出引导'这一步，但**没有真正填 Key 聊过天**）",
        "解压 dist/*-portable.zip 到别处，双击 启动.bat")
    add(TODO, "【人工】B 的 exe 双击",
        "**未验证**：沙箱里启动 GUI 进程会挂住（试过两次），所以 B 从没被真跑过。"
        "若双击无反应，把 PRTS.spec 的 console=False 改成 True 重打包即可看到报错",
        f"双击 {exeB.name if exeB.is_file() else '（先构建 B）'}")
    add(TODO, "【人工】C 的安装程序编译",
        "**未验证**：开发机没装 Inno Setup，.iss 只做过静态校验",
        r'& "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer\PRTS.iss')

    # ── 7. 地雷提醒（与代码无关）──
    add(WARN, "环境地雷：site-packages 里若有 GBK 的 .pth，"
        "Python 会全线启动失败",
        "发布/安装说明里要提醒；本机已把 users.pth 改名为 .bak 禁用",
        "python -c \"print(1)\" 若报错就去查 Lib/site-packages/*.pth")

    return {
        "version": __version__,
        "counts": {s: sum(1 for i in items if i["status"] == s)
                   for s in (OK, WARN, TODO)},
        "items": items,
    }


def main() -> int:
    rep = collect()
    if "--json" in sys.argv:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        return 0

    icon = {OK: "✅", WARN: "⚠️ ", TODO: "☐ "}
    print("=" * 74)
    print(f"  铃兰桌宠 · 发布前自检   版本 {rep['version']}")
    print("=" * 74)
    for it in rep["items"]:
        print(f"\n{icon[it['status']]} {it['name']}")
        if it["detail"]:
            print(f"     {it['detail']}")
        if it["action"] and it["status"] != OK:
            print(f"     → {it['action']}")
    c = rep["counts"]
    print("\n" + "=" * 74)
    print(f"  已就绪 {c[OK]} ｜ 提醒 {c[WARN]} ｜ **待办 {c[TODO]}**")
    if c[TODO]:
        print("  ★ 上面每一个 ☐ 都是**发布前必须落实**的（多数只有人能做）")
    else:
        print("  ★ 待办清单为空 —— 但发布前仍建议按 交接块 走一遍真机流程")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
