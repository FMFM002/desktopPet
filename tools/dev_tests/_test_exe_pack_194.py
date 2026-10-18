# -*- coding: utf-8 -*-
"""★★ 1.9.4 阶段5(B)：**exe 打包守卫**（发布形态 B）。

## 这套件为什么存在
上一阶段（1.9.4 A 形态）把"exe 从没被真正运行过"当成**未完成项**留给了下一阶段。
本套件把"B 能成立"所需的那些**容易漏、漏了就坏、但平时看不出来**的点全部钉住：

  A. `PRTS.spec` 的 DATAS 里必须有 **`.env.preset`**（出厂预设密钥）、4 份文档；
  B. HIDDEN 里必须有 **`src.core.account_login`**（运行时 import，静态分析扫不到）；
  C. `tools/*.py` 与 `tools/_vendor` 必须**作为数据**进包
     —— 工具是 `spec_from_file_location()` **按文件路径**加载的，静态分析看不见；
     同理它们必须在 HIDDEN 里（否则**工具自己的依赖**不会被分析进包）；
  D. `contents_directory="."` —— 否则用户数据会写进 `_internal/` 而不是 exe 旁边；
  E. COLLECT 目录名**不能**等于 A 的装配目录（`--noconfirm` 会先删同名目录）；
  F. 冻结环境的代码路径必须**有降级**（不是崩、也不是假报"没装"）：
     · `account_login` → 明确说"exe 版不支持，请用 zip 版"；
     · 首次向导的模型下载 → **在本进程内**跑，不再 `[sys.executable, "*.py"]`
       （冻结后 `sys.executable` 是 exe 自己，那样等于**再开一个桌宠**）；
  G. `main.py --selftest` 存在且退出码有约定（0=全过 / 3=必检项没过）；
  H. `.iss` 的源目录指向 **B** 的产物（不是 A）。

★ 本套件**只做静态校验**（不跑 400MB 的 exe）。真正的"能不能起来"由
  `<exe> --selftest` 的 `logs/selftest_*.txt` 证明，`tools/preflight.py` 会读它。
"""
import ast
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


SPEC = ROOT / "PRTS.spec"
MAIN = ROOT / "main.py"
ISS = ROOT / "installer" / "PRTS.iss"

print("== A. PRTS.spec 本身 ==")
check("★PRTS.spec 存在", SPEC.is_file())
spec_text = SPEC.read_text(encoding="utf-8") if SPEC.is_file() else ""
try:
    ast.parse(spec_text)
    _spec_ok = True
except SyntaxError as e:                             # noqa: F841
    _spec_ok = False
check("★spec 是合法 Python（语法错误会让打包直接失败）", _spec_ok)

print("\n== A2. ★★ 缺口 1：出厂预设密钥必须随包 ==")
check("★★DATAS 里有 `.env.preset`（漏了天气/搜索/终末地全废）",
      '".env.preset"' in spec_text or "'.env.preset'" in spec_text)
check("★DATAS 里仍有 `.env.example`（给用户看要填什么）",
      '".env.example"' in spec_text)
for _doc in ("发布说明_1.9.4.txt", "B站与P站凭据获取指南_1.9.4.md",
             "施工说明书_ComfyUI集成_1.9.4.txt", "评估_画图工具自部署_1.9.4.txt"):
    check(f"★文档随包: {_doc}", _doc in spec_text)
check("★requirements.txt / config.default.json 随包",
      "requirements.txt" in spec_text and "config.default.json" in spec_text)

print("\n== B. ★★ 缺口 2：运行时 import 的模块必须登记 ==")
check("★★HIDDEN 里有 `src.core.account_login`（设置页运行时 import）",
      "src.core.account_login" in spec_text)
check("★HIDDEN 里有 model_manager 相关（tools.* 自动收集）",
      'f"tools.{p.stem}"' in spec_text or "tools.model_manager" in spec_text)

print("\n== C. ★★ tools 是按文件路径加载的，必须当数据带上 ==")
_tool_py = sorted((ROOT / "tools").glob("*.py"))
check(f"★仓库里 tools/*.py 有 {len(_tool_py)} 个", len(_tool_py) > 10)
check("★★spec 把 tools/*.py 收集进 DATAS（否则 exe 里一个工具都没有）",
      'TOOL_SRC' in spec_text and '"tools"' in spec_text)
check("★★spec 把 tools/_vendor 带上（account_login 按路径 exec_module）",
      '"_vendor"' in spec_text)
check("★tools/_vendor 目录确实存在", (ROOT / "tools" / "_vendor").is_dir())

print("\n== D. ★★ 用户数据要落在 exe 旁边（不是 _internal/）==")
check('★★contents_directory="."（PyInstaller legacy 布局，_MEIPASS == exe 目录）',
      re.search(r'contents_directory\s*=\s*["\']\.["\']', spec_text) is not None)
check("★没有再把 console 写死成 False（可用 PRTS_SPEC_CONSOLE=1 排查启动失败）",
      "PRTS_SPEC_CONSOLE" in spec_text)
check("★★EXCLUDES **不能排 setuptools**（否则 pyi_rth_pkgres 要的 jaraco 缺失 → "
      "窗口版弹模态错误框、进程卡死）",
      '"setuptools"' not in spec_text.split("EXCLUDES")[-1],
      "真机踩过：ModuleNotFoundError: No module named 'jaraco'")

print("\n== E. ★★ 输出目录不能与 A 形态撞名 ==")
_coll_m = re.search(r'COLLECT_NAME\s*=\s*f?"([^"\n]*)"', spec_text)
_coll = _coll_m.group(1) if _coll_m else ""
check("★COLLECT 目录名带 `-exe` 后缀", _coll.endswith("-exe"), _coll or "（没找到 COLLECT_NAME）")
check("★★COLLECT 目录名 ≠ A 的装配目录（--noconfirm 会先删同名目录）",
      _coll != f"{APP_ID}-{__version__}", f"A={APP_ID}-{__version__} / B={_coll}")
check("★spec 文件头写明了这条坑（免得下次又改回去）",
      "--noconfirm" in spec_text)

print("\n== F. ★★ 冻结环境的降级路径必须存在 ==")
_rt = ROOT / "src" / "utils" / "runtime.py"
check("★src/utils/runtime.py 存在（冻结判定的唯一来源）", _rt.is_file())
_rt_text = _rt.read_text(encoding="utf-8") if _rt.is_file() else ""
check("★runtime.is_frozen() 认 `sys.frozen`", "sys.frozen" in _rt_text or '"frozen"' in _rt_text)
check("★runtime 区分 _MEIPASS 与 exe 目录", "_MEIPASS" in _rt_text)
check("★runtime 提供 frozen_feature_hint（统一口径）", "frozen_feature_hint" in _rt_text)

_ac_text = (ROOT / "src" / "core" / "account_login.py").read_text(encoding="utf-8")
check("★★account_login 有 feature_available() 冻结判定",
      "def feature_available" in _ac_text)
check("★★冻结时「一键开通」返回**可操作**的说明（不是崩、不是假报没装）",
      "frozen_feature_hint" in _ac_text and "if is_frozen()" in _ac_text)

_sw_text = (ROOT / "src" / "ui" / "settings_window.py").read_text(encoding="utf-8")
check("★★设置页在 exe 里把「一键开通」**置灰并写明原因**",
      "exe 版不支持" in _sw_text and "_b.setEnabled(not _frozen)" in _sw_text)

_fw_text = (ROOT / "src" / "ui" / "first_run_wizard.py").read_text(encoding="utf-8")
check("★★首次向导的模型下载有 `_install_inprocess`（exe 里没有 python 可调）",
      "def _install_inprocess" in _fw_text)
check("★★向导用 has_python() 分流，而不是直接拼 [sys.executable, script]",
      "has_python()" in _fw_text)
check("★源码形态仍然走原来的子进程（行为不变）",
      "subprocess.Popen(" in _fw_text)

print("\n== G. ★★ --selftest：打完包后能自动化证明它真起来了 ==")
_main_text = MAIN.read_text(encoding="utf-8")
check("★★main.py 认 `--selftest`", '"--selftest" in sys.argv' in _main_text)
check("★自检里区分「必检」与「仅供参考」（首启没 LLM Key 不算失败）",
      "required=False" in _main_text)
check("★自检会写 logs/selftest_*.txt", "selftest_" in _main_text and "logs" in _main_text)
check("★★自检退出码：0=全过 / 3=必检项没过", "app.exit(0 if ok else 3)" in _main_text)
check("★自检时**跳过**首次配置引导（否则模态框会卡住自检）",
      "SELFTEST: 跳过首次配置引导" in _main_text)
check("★自检覆盖「形象起来了没」（子进程 or 直接渲染都算）",
      "_direct_widget" in _main_text)
check("★自检覆盖「工具注册了没」",
      "tools_registered" in _main_text)
check("★自检覆盖「.env.preset 合并进来了没」",
      "env_preset_merged" in _main_text)

print("\n== H. ★★ 安装程序(C) 指向 B 的产物 ==")
_iss_text = ISS.read_text(encoding="utf-8") if ISS.is_file() else ""
check("★★.iss 的 SrcDir 指向 `-exe` 目录（不能是 A）",
      f'dist\\{APP_ID}-{__version__}-exe' in _iss_text,
      f"期望 dist\\{APP_ID}-{__version__}-exe")
check("★.iss 不再引用 B 里没有的 启动.bat",
      "启动.bat" not in _iss_text)

print("\n== I. ★ 冻结判定的运行时自证（monkeypatch sys.frozen）==")
sys.path.insert(0, str(ROOT))
from src.utils import runtime as _R                   # noqa: E402
from src.core import account_login as _AL             # noqa: E402

_old = getattr(sys, "frozen", None)
try:
    sys.frozen = True                                 # type: ignore[attr-defined]
    sys._MEIPASS = str(ROOT / "_fake_meipass")        # type: ignore[attr-defined]
    check("★is_frozen() 认得出", _R.is_frozen() is True)
    check("★app_dir() 用 _MEIPASS（就是 exe 目录）",
          _R.app_dir() == (ROOT / "_fake_meipass"))
    check("★has_python() 在 exe 里是 False（不能再用 sys.executable 跑脚本）",
          _R.has_python() is False)
    _ok, _why = _AL.feature_available()
    check("★★一键开通在 exe 里**明确不可用**，且给出替代做法",
          _ok is False and "zip" in _why and "exe" in _why)
    _hint = _AL.install_hint()
    check("★★安装提示不再是 `exe -m pip install` 那种假命令",
          "pip install" not in _hint, _hint[:60])
finally:
    if _old is None:
        try:
            del sys.frozen                            # type: ignore[attr-defined]
        except AttributeError:
            pass
    else:
        sys.frozen = _old                             # type: ignore[attr-defined]
    try:
        del sys._MEIPASS                              # type: ignore[attr-defined]
    except AttributeError:
        pass

print(f"\n==== 结果: {len(PASS)} PASS / {len(FAIL)} FAIL ====")
for f in FAIL:
    print("  FAIL:", f)
sys.exit(1 if FAIL else 0)
