# -*- coding: utf-8 -*-
"""★★ 1.9.4：**"桌宠自述"守卫** —— 它必须真的能被桌宠读到，而且内容不许飘。

## 这份文档是给谁看的
用户 2026-10-20："现在写一份 md，这是给桌宠看的，桌宠在有必要时，
需要知道自己系统的运行逻辑。"
所以 `桌宠自述.md` 不是给人看的说明，而是**模型的知识来源**：
用户问"你怎么工作的 / 这个功能为什么不行"时，桌宠要用 `file_view` 读它。

## 钉什么
  A. 文件存在、是 UTF-8、有关键章节（运行形态 / 数据在哪 / 工具 / 模型 / 日志 / 边界）；
  B. **系统提示词里指了路**（否则模型不知道该去读它 → 只能编）；
     而且两条分支（自定义人设 / 默认模板）都要带；
  C. **两个发布形态都带上它**（exe 的 DATAS、便携 zip 的 INCLUDE）；
  D. ★ 文档里的关键事实必须和代码**对得上**（最怕"写了但已经改了"）：
     登录等待 500 秒、18 个工具、`--selftest`、"数据落在 exe 旁边"。
"""
import os
import re
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PRTS_NO_COMFY_START", "1")
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.version import __version__                  # noqa: E402

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))


DOC = ROOT / "桌宠自述.md"
print("== A. 文档本身 ==")
check("★★桌宠自述.md 存在", DOC.is_file())
_text = DOC.read_text(encoding="utf-8", errors="replace") if DOC.is_file() else ""
check("★能按 UTF-8 读出来（中文别乱码）", "桌宠" in _text and "运行" in _text)
for _sec in ("我跑在哪", "数据都放在哪", "我的工具", "模型", "出问题了怎么自查", "我的边界"):
    check(f"★含关键小节：{_sec}", _sec in _text)
check("★开头就写明「这是给桌宠自己看的」", "给自己" in _text or "给我自己" in _text)

print("\n== B. 系统提示词必须**指路** ==")
_llm = (ROOT / "src" / "core" / "llm_client.py").read_text(encoding="utf-8")
check("★★有 `_self_manual_section()`", "def _self_manual_section" in _llm)
check("★★提示词里点名了 `桌宠自述.md`（否则模型不知道去读）",
      "`桌宠自述.md`" in _llm or "桌宠自述.md" in _llm)
check("★★要求「先用 file_view 读它，再回答」", "file_view" in _llm and "再回答" in _llm)
check("★★立了规矩「不要编」", "不要编" in _llm)
# 两条分支：自定义人设 & 默认模板，都必须追加这一段
_i_calls = [m.start() for m in re.finditer(r"prompt \+= self\._self_manual_section\(\)", _llm)]
check("★★自定义人设分支、默认模板分支**都**追加了这一段", len(_i_calls) >= 2,
      f"出现 {len(_i_calls)} 次")

print("\n== C. 两种发布形态都要带上它 ==")
_spec = (ROOT / "PRTS.spec").read_text(encoding="utf-8")
_br = (ROOT / "tools" / "build_release.py").read_text(encoding="utf-8")
check("★★exe 的 DATAS 里有 `桌宠自述.md`", '"桌宠自述.md"' in _spec)
check("★★便携 zip 的 INCLUDE 里有 `桌宠自述.md`", '"桌宠自述.md"' in _br)

print("\n== D. ★ 文档里的事实必须和代码对得上（防飘）==")
# 登录等待：文档写 500 秒，代码里也必须是 500
check("★文档写「等用户登录 500 秒」",
      "500 秒" in _text or "500s" in _text)
_ac = (ROOT / "src" / "core" / "account_login.py").read_text(encoding="utf-8")
check("★★代码里确实是 LOGIN_WAIT_S = 500", "LOGIN_WAIT_S = 500.0" in _ac)
# 工具数量：文档写 18，代码扫出来的也该是 18（按 tools/*.py 里能注册的算）
check("★文档写「18 个工具」", "18 个" in _text)
# 自检
check("★★文档写了 exe 的 `--selftest` 与退出码约定", "--selftest" in _text and "3 = 有必检项" in _text)
check("★main.py 真的支持 --selftest", '"--selftest" in sys.argv'
      in (ROOT / "main.py").read_text(encoding="utf-8"))
# 数据落点
check("★★文档写「数据落在 exe 旁边」", "exe 旁边" in _text)
check("★spec 里确实是 contents_directory=\".\"",
      re.search(r'contents_directory\s*=\s*["\']\.["\']', _spec) is not None)
# 模型大小（防止过时）
for _mb in ("1855", "465", "100"):
    check(f"★文档写了模型体积 {_mb}MB（与 model_manager 口径一致）", _mb in _text)
# 日志路径
check("★文档写了日志位置 `logs/prts_`", "logs/prts_" in _text)
check("★文档写了隐私红线 data/DBs / cookies",
      "data/DBs" in _text and "cookies" in _text)

print("\n== E. ★ 不让它变成「资料夹里的死文件」 ==")
# 版本号：文档头注明当前版本，方便日后核对（不强制，但要有）
check("★文档标了版本号", __version__ in _text)
# 不能写死绝对路径（换台机器就错）
check("★★文档里没有写死的 Windows 绝对路径",
      not re.search(r"[A-Za-z]:\\\\?Users|D:\\\\", _text))
check("★文档不包含任何真实密钥的模样（32 位 hex / tvly- / ef_）",
      not re.search(r"\btvly-[A-Za-z0-9]{8,}|\bef_[0-9a-f]{16,}|\b[0-9a-f]{32}\b", _text))

print(f"\n==== 结果: {len(PASS)} PASS / {len(FAIL)} FAIL ====")
for f in FAIL:
    print("  FAIL:", f)
sys.exit(1 if FAIL else 0)
