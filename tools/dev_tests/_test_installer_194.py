# -*- coding: utf-8 -*-
"""★ 1.9.4 阶段5(C)：**安装程序脚本守卫**（发布形态 C）。

## 背景（如实说）
用户定的顺序是 A → B → C。C 用 Inno Setup。但**本机实测没有安装 Inno Setup**
（三个常见路径都查过），所以 `.iss` **没有被真正编译过** ——
本套件只能做**静态校验**（把该有的结构钉住），不能证明它能编译成功。
这个事实本身也要被断言出来，免得日后误以为"C 已经验证过"。

## 钉什么
  A. `installer/PRTS.iss` 存在且是 UTF-8；
  B. ★★ **含"询问是否一起装模型"的那一项**（用户明确要求的：
     "是否下载这些包，在安装程序里面询问，后面一起装了"）——
     既要有 `[Tasks]` 里的复选框，也要有 `[Run]` 里真正去装的动作；
  C. ★ 那个动作指向的 `tools/model_manager.py install` **确实存在且可用**
     （否则安装程序勾了也没用 —— 这是"界面骗人"最容易发生的地方）；
  D. 版本号与 `src/version.py` 一致、可执行文件名与 B 的产物名一致
     （否则装完了没有 exe）；
  E. 打包源目录指向 B 的产物（`dist/PRTS-DesktopPet-<版本>`）；
  F. 安全默认：模型那个复选框**默认不勾**（2.4GB，不该默认让用户等）；
     装到用户目录（`PrivilegesRequired=lowest`）而不是要求管理员。
"""
import os
import re
import subprocess
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PRTS_NO_COMFY_START", "1")
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.version import APP_ID, __version__          # noqa: E402

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))


ISS = ROOT / "installer" / "PRTS.iss"

print("== A. 安装脚本存在 ==")
check("★installer/PRTS.iss 存在", ISS.is_file())
if not ISS.is_file():
    print(f"\n==== 结果: {len(PASS)} PASS / {len(FAIL)} FAIL ====")
    sys.exit(1)

_txt = ISS.read_text(encoding="utf-8")

print("\n== B. ★★ 含『询问是否一起装模型』（用户明确要求）==")
check("★★[Tasks] 里有 models 复选框",
      bool(re.search(r'Name:\s*"models"', _txt)))
check("★复选框文案说明了体积与需要联网（用户好判断）",
      "2.4 GB" in _txt or "2.4GB" in _txt)
check("★文案里说明了它是可选的", "可选" in _txt or "同时下载" in _txt)
check("★★[Run] 里真的有『去装模型』的动作（不能只有界面没有实现）",
      "model_manager.py" in _txt and "install" in _txt)
check("★那个动作只在勾选时执行（Tasks: models）",
      bool(re.search(r'Tasks:\s*models', _txt)))

print("\n== C. ★ 动作指向的东西确实存在且可用（否则界面骗人）==")
_mm = ROOT / "tools" / "model_manager.py"
check("★tools/model_manager.py 存在", _mm.is_file())
if _mm.is_file():
    _r = subprocess.run([sys.executable, str(_mm), "status", "--json"],
                        capture_output=True, text=True, encoding="utf-8",
                        errors="replace", cwd=str(ROOT))
    check("★★`model_manager.py status --json` 真能跑（安装程序要读它）",
          _r.returncode == 0 and '"models"' in (_r.stdout or ""),
          ((_r.stdout or "")[:40] + "…") if _r.stdout else (_r.stderr or "")[:60])
    check("★它支持 install 子命令（[Run] 调的就是这个）",
          "install" in _mm.read_text(encoding="utf-8"))

print("\n== D. 版本与可执行文件名必须对得上 ==")
check(f"★.iss 里的版本是 {__version__}",
      f'#define AppVer "{__version__}"' in _txt)
check("★★.iss 里的 exe 名与 B 的产物名一致（否则装完没有 exe）",
      f"{APP_ID}-{__version__}.exe" in _txt,
      f"期望 {APP_ID}-{__version__}.exe")
check("★打包源目录指向 B 的产物 dist/<APP_ID>-<版本>",
      f"dist\\{APP_ID}-{__version__}" in _txt or f"dist/{APP_ID}-{__version__}" in _txt)

print("\n== E. 安全默认 ==")
check("★模型复选框**默认不勾**（2.4GB 不该默认让用户等）",
      "unchecked" in _txt)
check("★装到用户目录、不要求管理员（PrivilegesRequired=lowest）",
      "PrivilegesRequired=lowest" in _txt)

print("\n== F. ★ 如实标注『本机没编译过』==")
_has_inno = any(Path(p).exists() for p in (
    r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
    r"C:\Program Files\Inno Setup 6\ISCC.exe",
))
check("★★脚本自己写明了『本机没有 Inno Setup，未被编译验证』"
      "（免得日后误以为 C 已验证）",
      "没有被编译验证" in _txt or "没有装 Inno Setup" in _txt)
check("★（现状记录）本机是否装了 Inno Setup —— 没装是已知情况，不算失败",
      True, "已安装" if _has_inno else "未安装（所以 .iss 只做了静态校验）")

print(f"\n==== 结果: {len(PASS)} PASS / {len(FAIL)} FAIL ====")
for f in FAIL:
    print("  FAIL:", f)
sys.exit(1 if FAIL else 0)
