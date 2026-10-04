# -*- coding: utf-8 -*-
"""★★ 1.9.4：**新用户流程 · 端到端验证**（在**真装配出来的发布产物**上跑）。

## 为什么单独一条
前面两条守卫分别验证了"检测核心"和"向导界面"，但都是**在工作区里**跑的。
而用户真正会拿到的是 `tools/build_release.py` 装配出来的**那个目录**。
两者可能不一样 —— 例如产物里有没有 `config.default.json`、有没有漏掉 `.env`、
目录结构是不是被排除表切歪了。**只有在新用户看到的那份东西上跑一遍，才算数。**

## 钉子（模拟"一台全新机器"）
  A. 产物里**该有的都在**：`main.py` / `requirements.txt` / `config.default.json` /
     `src/version.py` / `.env.example` / 角色目录；
  B. 产物里**没有 `config.json`**（那是用户的东西）→ 所以第一次跑必须靠出厂配置生成；
  C. 把 config/env 都指向产物目录 → **确实被判定为"首次运行"**，且**只差 1 项必填**；
  D. 在那份产物上构造 AppConfig → **真的会生成 config.json**，且内容=出厂值；
  E. 生成之后 `is_first_run()` 仍为真（因为还没有 Key）——★ 这一步最能说明问题：
     **光生成配置不够，必须有 Key 才不算"首次"**；
  F. 模拟用户填了 Key → `is_first_run()` 变假、`should_show()` 也变假（不再打扰）；
  G. 产物的**版本号**与 `src/version.py` 一致（发布物不能带着旧版本号出去）。
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PRTS_NO_COMFY_START", "1")
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))


OUT = ROOT / "临时文件" / "_scratch_dev" / "tmp_newuser_194"
if OUT.exists():
    shutil.rmtree(OUT, ignore_errors=True)

print("== 0. 先装配出一份发布产物 ==")
_r = subprocess.run([sys.executable, str(ROOT / "tools" / "build_release.py"),
                     "--out", str(OUT)],
                    capture_output=True, text=True, encoding="utf-8",
                    errors="replace", cwd=str(ROOT))
check("★装配成功", OUT.is_dir(),
      ((_r.stdout or "") + (_r.stderr or "")).strip().splitlines()[-1:][0]
      if ((_r.stdout or "") + (_r.stderr or "")) else "")

print("\n== A. 产物里该有的都在 ==")
for _n, _p in (("main.py", "main.py"), ("requirements.txt", "requirements.txt"),
               ("config.default.json", "config.default.json"),
               ("版本源", "src/version.py"), ("环境模板", ".env.example")):
    check(f"★产物含 {_n}", (OUT / _p).exists(), _p)
check("★产物含角色目录", (OUT / "data" / "桌宠形象").is_dir())

print("\n== B. 产物里**没有**用户的 config.json（所以必须靠出厂配置）==")
check("★★产物里没有 config.json（那是用户的东西，不该随包发）",
      not (OUT / "config.json").exists())
check("★★产物里没有 .env（密钥绝不随包发）", not (OUT / ".env").exists())

print("\n== G. 产物带着正确版本号（不能是旧版）==")
from src.version import __version__          # noqa: E402
_v = (OUT / "src" / "version.py").read_text(encoding="utf-8")
check(f"★产物 src/version.py 里是 {__version__}", f'"{__version__}"' in _v)

print("\n== C~F. 在产物上模拟一台全新机器 ==")
# ★ 关键：把"读配置"和"读 .env"都指到**产物目录**，而不是工作区
sys.path.insert(0, str(OUT))
from src.core import first_run as fr          # noqa: E402

_env_in_out = OUT / ".env"                    # 产物里本来没有，模拟"用户还没填 key"
fr.use_env_file_for_tests(_env_in_out)
try:
    _rep = fr.detect(base=OUT)
    check("★★在产物上判定为『首次运行』", fr.is_first_run())
    check("★★★首次运行**只差 1 项必填**（是 LLM Key，问多了会把人吓跑）",
          [f.key for f in _rep.must_fill] == ["llm_key"],
          str([f.label for f in _rep.must_fill]))
    check("★引导会弹（should_show 为真）",
          __import__("src.ui.first_run_wizard", fromlist=["x"]).should_show())

    # D. 构造 AppConfig → 应从产物的 config.default.json 生成 config.json
    from src.utils.config import AppConfig
    _cfg = AppConfig(config_path=OUT / "config.json")
    check("★★在产物上首启**真的会生成 config.json**", (OUT / "config.json").exists())
    if (OUT / "config.json").exists():
        _j = json.loads((OUT / "config.json").read_text(encoding="utf-8"))
        check("★生成的是**出厂值**（人设空、无个人头像、位置 [100,100]）",
              _j["pet"]["personality_prompt"] == ""
              and _j["ui"]["user_avatar_path"] == ""
              and _j["ui"]["pet_position"] == [100, 100],
              f"{_j['pet']['personality_prompt']!r} / {_j['ui']['user_avatar_path']!r}")
        check("★出厂配置里**没有密钥字段**",
              "api_key" not in (_j.get("llm") or {})
              and "api_key" not in (_j.get("vision") or {}))

    # E. ★ 光生成配置还不够 —— 没有 Key 仍然算"首次"
    check("★★生成了 config.json 但**还没有 Key** → 仍判定为首次运行"
          "（配置存在 ≠ 能用）", fr.is_first_run())

    # F. 模拟用户填了 Key
    _env_in_out.write_text("DEEPSEEK_API_KEY=sk-模拟用户刚填的\n", encoding="utf-8")
    check("★★填了 Key 之后**不再算首次**（以后不再打扰）", not fr.is_first_run())
    check("★填了之后 should_show() 也为假",
          not __import__("src.ui.first_run_wizard",
                         fromlist=["x"]).should_show())
    _rep2 = fr.detect(base=OUT)
    check("★填了之后报告里**没有必填项**", not _rep2.must_fill)
finally:
    fr.use_env_file_for_tests(None)
    # ★ 顺手把 sys.path 恢复干净，别把产物目录留给后面的套件
    try:
        sys.path.remove(str(OUT))
    except ValueError:
        pass
    shutil.rmtree(OUT, ignore_errors=True)

print(f"\n==== 结果: {len(PASS)} PASS / {len(FAIL)} FAIL ====")
for f in FAIL:
    print("  FAIL:", f)
sys.exit(1 if FAIL else 0)
