# -*- coding: utf-8 -*-
"""★ 1.9.4 阶段4：**首次配置引导 · 检测核心守卫**。

## 为什么要有这一条
目标④是"实现首次配置引导并测试新用户流程"。引导里最容易出错的两处：
  ① **把已经配好的老用户又拦一遍**（探路实测：`.env` 文件里有 key，但
     `os.getenv()` 读不到 —— 因为 `.env` 要先被 `load_dotenv()` 加载。只查
     `os.getenv` 就会误判成"新用户"）；
  ② **把"必须手填"和"可选能力"混为一谈** —— 新用户只该被问 1 件事（LLM Key），
     其它都该自动发现或"想用才问"；问多了就是把人吓跑。

## 本套件钉什么
  A. 全新机器（没有 .env / 没有 key）→ **只报 1 项必填（LLM Key）**；
  B. 已配好 key（写进 .env 文件，**但环境变量里没有**）→ **判定为"不是首次"**；
  C. 检测**不写任何东西**（只探测不落盘）；
  D. 报告结构可用（summary / must_fill / auto_found / optional）；
  E. 运行目录能自建；
  F. ★ **反向守卫**：给一个假的 .env（含 key）也必须被认出来 ——
     证明"查文件"这条路真的生效，而不是碰巧读到了环境变量。
"""
import os
import shutil
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PRTS_NO_COMFY_START", "1")
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.core import first_run as fr  # noqa: E402

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))


_TMP = ROOT / "临时文件" / "_scratch_dev" / "tmp_firstrun_guard_194"
if _TMP.exists():
    shutil.rmtree(_TMP, ignore_errors=True)
_TMP.mkdir(parents=True)

print("== A. 全新机器：只该有 1 项必填（LLM Key）==")
# 造一台"干净"的机器：空目录 + 空 .env
_fresh = _TMP / "fresh"
_fresh.mkdir()
(_fresh / ".env").write_text("# 空的\n", encoding="utf-8")
_env = _fresh / ".env"

fr.use_env_file_for_tests(_env)
try:
    check("★★全新机器（.env 无 key）→ has_llm_key() 为假", not fr.has_llm_key())
    check("★★全新机器 → is_first_run() 为真", fr.is_first_run())

    _r = fr.detect(base=_fresh)
    check("★★★全新机器**只有 1 项必须手填**（问多了会把新用户吓跑）",
          len(_r.must_fill) == 1,
          f"实际 {len(_r.must_fill)} 项: {[f.label for f in _r.must_fill]}")
    check("★那 1 项就是 LLM API Key",
          bool(_r.must_fill) and _r.must_fill[0].key == "llm_key",
          _r.must_fill[0].label if _r.must_fill else "")
    check("★必填项给了**可操作**的指引（去哪申请 / 写到哪）",
          bool(_r.must_fill) and len(_r.must_fill[0].hint) > 8,
          _r.must_fill[0].hint if _r.must_fill else "")
    check("★每一条都写清了「我是怎么知道的」（可核对）",
          all(f.how for f in _r.findings),
          str([f.key for f in _r.findings if not f.how]))
    check("★报告有 summary 且数字自洽",
          _r.summary().count("项") == 3, _r.summary())

    # ── C. 检测不写东西 ──
    _before = sorted(p.name for p in _fresh.iterdir())
    fr.detect(base=_fresh)
    fr.has_llm_key()
    check("★★检测过程**不写任何文件**（只探测不落盘）",
          sorted(p.name for p in _fresh.iterdir()) == _before,
          str(sorted(p.name for p in _fresh.iterdir())))

    # ── E. 运行目录自建 ──
    _made = fr.ensure_runtime_dirs(base=_fresh)
    check("★运行时目录能自建（首次运行不报错）",
          (_fresh / "data" / "DBs").is_dir() and (_fresh / "logs").is_dir(),
          f"新建 {len(_made)} 个")

    print("\n== B/F. 已配好 key 的用户不许被当成新用户（★ 探路踩过的坑）==")
    # 关键：key **只写在 .env 文件里**，环境变量里没有
    os.environ.pop("DEEPSEEK_API_KEY", None)
    _configured = _TMP / "configured"
    _configured.mkdir()
    (_configured / ".env").write_text("DEEPSEEK_API_KEY=sk-这是一把假钥匙用于测试\n",
                                      encoding="utf-8")
    fr.use_env_file_for_tests(_configured / ".env")
    check("★★★key 只在 .env 文件里（环境变量没有）也算「已配置」"
          " —— 只查 os.getenv 会把老用户又拦一遍",
          fr.has_llm_key())
    check("★★这类用户 **is_first_run() 为假**（不弹引导）", not fr.is_first_run())
    _r2 = fr.detect(base=_configured)
    check("★★他们的报告里**没有必填项**", not _r2.must_fill,
          str([f.label for f in _r2.must_fill]))
    check("★LLM Key 那条被标成「已配置」",
          any(f.key == "llm_key" and f.ok for f in _r2.findings))

    print("\n== D. 可选能力是「想用才问」，不是必填 ==")
    check("★可选能力都带 askable 标记（UI 据此渲染勾选框）",
          all(f.askable for f in _r2.optional) if _r2.optional else True,
          f"{len(_r2.optional)} 项")
    check("★★可选能力**绝不**出现在 must_fill 里",
          not {f.key for f in _r2.optional} & {f.key for f in _r2.must_fill})

    print("\n== G. 反向守卫：判据本身有效（不是碰巧读到真环境）==")
    # 把 override 设成一个**不存在**的文件 → 必须判定为"没有 key"
    fr.use_env_file_for_tests(_TMP / "根本不存在的文件.env")
    check("★★override 指向不存在的文件时，判定为「没有 key」"
          "（证明它真的在读那个文件，而不是碰巧读到真环境）",
          not fr.has_llm_key())

    fr.use_env_file_for_tests(_configured / ".env")
    check("★override 指回假的已配置 .env → 又变回「有 key」（可双向切换）",
          fr.has_llm_key())

    print("\n== H. read_env_file 的解析正确性 ==")
    _p = _TMP / "parse.env"
    _p.write_text('# 注释\n\nA=1\nB = "带引号"\nC=\nD=有=等号\n  E=缩进\n',
                  encoding="utf-8")
    _d = fr.read_env_file(_p)
    check("★跳过注释与空行", "A" in _d and len(_d) == 5, str(list(_d)))
    check("★去引号 / 去空白", _d.get("B") == "带引号" and _d.get("E") == "缩进",
          f"B={_d.get('B')!r} E={_d.get('E')!r}")
    check("★值里有等号也能正确切分", _d.get("D") == "有=等号", repr(_d.get("D")))
    check("★空值解析为空串", _d.get("C") == "", repr(_d.get("C")))

finally:
    fr.use_env_file_for_tests(None)          # ★ 一定要恢复，别影响别的套件
    shutil.rmtree(_TMP, ignore_errors=True)

print("\n== I. 恢复后，真实环境的行为不受影响 ==")
check("★use_env_file_for_tests(None) 之后，本机真实判定仍为「已配置」"
      "（这台机器确实有 key）", fr.has_llm_key())

print("\n== J. 向导要用的三个动作（测它们的**输入校验分支**，不写盘、不联网）==")
# ★ 这些是引导的"写入/验证"入口。这里**只测安全分支**：
#   空 Key 必须被拒（绝不能在测试里把假 Key 写进真实 .env）。
# ★ 注意：上面那个 try/finally 已经把 _TMP 删了，所以这里**自己造文件**，
#   不能再引用 _TMP 下的东西 —— 我第一版就是这么写的，于是这条假红了一次。
_J = ROOT / "临时文件" / "_scratch_dev" / "tmp_firstrun_guard_J"
if _J.exists():
    shutil.rmtree(_J, ignore_errors=True)
_J.mkdir(parents=True)
(_J / ".env").write_text("DEEPSEEK_API_KEY=sk-假钥匙\n", encoding="utf-8")

try:
    fr.use_env_file_for_tests(_J / "不存在.env")     # 模拟"没有 key"
    _ok, _msg = fr.save_llm_key("")
    check("★★save_llm_key('') 必须拒绝，且**不写盘**（保护真实 .env）",
          _ok is False and "空" in _msg, _msg)

    _ok2, _msg2 = fr.verify_llm_key("")
    check("★verify_llm_key('') 没有 Key 时直接返回失败（**不去发网络请求**）",
          _ok2 is False and "没有 Key" in _msg2, _msg2)

    _r3 = fr.detect(base=_J)
    check("★FirstRunReport.needs_setup：缺 Key 时为真（UI 据此决定要不要弹引导）",
          _r3.needs_setup is True)

    fr.use_env_file_for_tests(_J / ".env")
    _r4 = fr.detect(base=_J)
    check("★已经配好时 needs_setup 为假（老用户不弹引导）", _r4.needs_setup is False)
finally:
    fr.use_env_file_for_tests(None)
    shutil.rmtree(_J, ignore_errors=True)

print(f"\n==== 结果: {len(PASS)} PASS / {len(FAIL)} FAIL ====")
for f in FAIL:
    print("  FAIL:", f)
sys.exit(1 if FAIL else 0)
