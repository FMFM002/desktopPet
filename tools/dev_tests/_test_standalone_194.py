# -*- coding: utf-8 -*-
"""★ 1.9.4 阶段2：**独立运行守卫** —— 可选能力必须是"软依赖"，不许把应用拖垮。

## 背景（真机取证）
这台机器上 `jmcomic` 与 `genie_tts` 都是**可编辑安装（editable）**，指向**仓库外**目录：

    __editable__.jmcomic-2.7.0.pth   -> ...\\JMComic-Crawler-Python-master\\...\\src
    __editable__.genie_tts-2.0.2.pth -> ...\\桌宠\\参考项目\\Genie-TTS-master\\...\\src

新用户克隆本项目时**不会有**这两个目录。所以必须满足：
  · 缺包**不能让 import 期就崩**；
  · 缺包**不能让工具整体注册失败**（该工具仍应出现，只是调用时报"缺依赖 + 怎么装"）；
  · **TTS 不能依赖那个外部包** —— GENIE 用的是项目内副本 `src/genie_tts/*`
    （`tts_engine.py` 里是 `from src.genie_tts.Internal import ...`）。

## 本套件钉什么
  A. `tools/jmcomic_tool.py` 的 `import jmcomic` **在 try 里**（AST 口径，不看注释）；
  B. 它的失败信息**可操作**（含"pip install"之类的指引），不是干巴巴一句错误；
  C. `tts_engine` 的 GENIE 资源指向**项目内**（`src/genie_tts/...`），
     而不是仓库外路径；
  D. 全仓没有任何**顶层硬 import** 只存在于"同级目录可编辑安装"里的包
     （按 AST 查 `import X` / `from X import`，且不在 try 内）。

★ 判据一律走 **AST**，不用文本搜索 —— 本项目已 3 次栽在
  "字符串搜索被自己的注释/docstring 骗到"（第 41/57/58 轮）。
"""
import ast
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))


# ── 只存在于"同级目录可编辑安装"里的包（新用户不会有）──
SIBLING_ONLY = {"jmcomic", "genie_tts"}


def _imports_outside_try(path: Path):
    """返回该文件里**不在 try 内**的顶层 import 名集合（AST 口径）。"""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        return set()
    guarded = set()          # 落在 try 里的 import 行号
    for n in ast.walk(tree):
        if isinstance(n, ast.Try):
            for sub in ast.walk(n):
                if isinstance(sub, (ast.Import, ast.ImportFrom)):
                    guarded.add(id(sub))
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.Import, ast.ImportFrom)) and id(n) not in guarded:
            if isinstance(n, ast.Import):
                for a in n.names:
                    out.add(a.name.split(".")[0])
            elif n.level == 0 and n.module:
                out.add(n.module.split(".")[0])
    return out


print("== A. jmcomic 必须是软依赖（import 在 try 里）==")
_jt = ROOT / "tools" / "jmcomic_tool.py"
_src = _jt.read_text(encoding="utf-8")
_tree = ast.parse(_src)
_in_try = False
for _n in ast.walk(_tree):
    if isinstance(_n, ast.Try):
        for _sub in ast.walk(_n):
            if isinstance(_sub, ast.Import):
                if any(a.name.split(".")[0] == "jmcomic" for a in _sub.names):
                    _in_try = True
check("★tools/jmcomic_tool.py 的 `import jmcomic` 在 try 块里（缺包不会在 import 期崩）",
      _in_try)

print("\n== B. 缺依赖时的报错要**可操作**（告诉用户怎么装）==")
_probe = None
try:
    import tools.jmcomic_tool as _jm_tool
    _probe = _jm_tool
except Exception as _e:
    print(f"    （导入 jmcomic_tool 失败: {_e}）")

if _probe is not None:
    # 把 jmcomic 屏蔽掉，看失败文案
    class _Blk:
        def find_spec(self, name, path=None, target=None):
            if name.split(".")[0] == "jmcomic":
                raise ModuleNotFoundError("blocked")
            return None

    sys.meta_path.insert(0, _Blk())
    try:
        for _k in [k for k in list(sys.modules) if k.split(".")[0] == "jmcomic"]:
            del sys.modules[_k]
        _probe._jm = None
        _out = str(_probe.execute(action="search", keyword="x"))
    except Exception as _e:
        _out = f"<抛异常 {type(_e).__name__}: {_e}>"
    finally:
        sys.meta_path = [x for x in sys.meta_path if not isinstance(x, _Blk)]

    _ok = ("pip install" in _out) or ("未加载" in _out) or ("未安装" in _out)
    check("★缺 jmcomic 时返回一句**带安装指引**的话（不是崩、也不是空）",
          _ok, _out.replace(chr(10), " ")[:110])

print("\n== C. GENIE 资源必须在**项目内**（不依赖外部 editable 包）==")
_te = (ROOT / "src" / "core" / "tts_engine.py").read_text(encoding="utf-8")
_t2 = ast.parse(_te)
_genie_res_ok = False
_uses_inproject = False
for _n in ast.walk(_t2):
    if isinstance(_n, ast.Assign):
        _tgt = ast.unparse(_n.targets[0])
        if "GENIE_RES_PRIMARY" in _tgt:
            _val = ast.unparse(_n.value)
            _genie_res_ok = "src" in _val and "genie_tts" in _val
        if "GENIE_ROOT" in _tgt:
            _rv = ast.unparse(_n.value)
            # 必须是"本文件所在位置往上推"，不是写死的绝对路径
            _uses_inproject = "__file__" in _rv
check("★GENIE 主资源指向项目内 `src/genie_tts/...`", _genie_res_ok)
check("★`_GENIE_ROOT` 由 `__file__` 推出（换机器/换路径都成立，不是写死的绝对路径）",
      _uses_inproject)

print("\n== D. 全仓不许有'同级目录包'的顶层硬 import ==")
_bad = []
for _p in (ROOT / "src").rglob("*.py"):
    _hits = _imports_outside_try(_p) & SIBLING_ONLY
    if _hits:
        _bad.append(f"{_p.relative_to(ROOT).as_posix()}: {sorted(_hits)}")
for _p in (ROOT / "tools").rglob("*.py"):
    if "_vendor" in _p.parts:
        continue
    _hits = _imports_outside_try(_p) & SIBLING_ONLY
    if _hits:
        _bad.append(f"{_p.relative_to(ROOT).as_posix()}: {sorted(_hits)}")
check("★★src/ 与 tools/（除 _vendor）里没有对 jmcomic/genie_tts 的硬 import",
      not _bad, "; ".join(_bad))

print("\n== E. 反向守卫：确认本套件的判据本身有效 ==")
# 构造一个"故意硬 import"的临时文件，确认 AST 判据抓得到（否则这道闸可能是空的）
_tmp = ROOT / "_test_tmp_standalone_probe.py"
_tmp.write_text("import jmcomic\n", encoding="utf-8")
try:
    _caught = bool(_imports_outside_try(_tmp) & SIBLING_ONLY)
finally:
    _tmp.unlink(missing_ok=True)
check("★判据有效：一个'裸 import jmcomic'能被抓出来（证明这道闸不是摆设）", _caught)

print(f"\n==== 结果: {len(PASS)} PASS / {len(FAIL)} FAIL ====")
for f in FAIL:
    print("  FAIL:", f)
sys.exit(1 if FAIL else 0)
