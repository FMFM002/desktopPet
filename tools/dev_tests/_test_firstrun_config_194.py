# -*- coding: utf-8 -*-
"""★ 1.9.4 阶段3：**出厂配置守卫** —— 发布包里不许夹带个人信息，且首启能自建。

## 为什么要有这一条
`config.json` 里有**个人信息**（用户自己写的 1100 字人设、个人头像路径、
自定义界面配色）而且它**被 git 跟踪** —— 直接发布等于把用户的东西发给所有人。

同时：出厂版 `config.default.json` 必须**与代码默认值一致**，
否则"程序没读文件时用什么"和"出厂文件写什么"会变成两套真理，
新人拿到手的行为说不清。

## 本套件钉什么
  A. `config.default.json` 存在、是合法 JSON、键集与代码默认一致；
  B. **不含个人信息**：人设空、头像路径空、窗口位置出厂值、
     **绝不出现 api_key**（密钥只走 .env）；
  C. 与代码默认值**不漂移**（配置项增减后忘了重跑生成器 → 报红）；
  D. **首启能自建**：只有出厂文件时，构造 AppConfig 会生成 config.json 且值正确；
  E. **缺出厂文件也不崩、不乱造文件**（优雅降级）。
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from _fixtures_193 import no_window_kwargs   # noqa: E402

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))


def leaf_keys(o, pre=""):
    if isinstance(o, dict):
        for k, v in o.items():
            yield from leaf_keys(v, f"{pre}.{k}" if pre else k)
    else:
        yield pre


DEFAULT = ROOT / "config.default.json"

print("== A. 出厂配置存在且结构正确 ==")
check("★config.default.json 存在", DEFAULT.exists())
if DEFAULT.exists():
    raw = DEFAULT.read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
    except Exception as e:
        data = None
        check("★是合法 JSON", False, str(e))
    else:
        check("★是合法 JSON", True)

print("\n== B. 不含个人信息（这是发布红线）==")
if DEFAULT.exists():
    _d = json.loads(DEFAULT.read_text(encoding="utf-8"))

    def get(path, default=None):
        cur = _d
        for seg in path.split("."):
            if not isinstance(cur, dict) or seg not in cur:
                return default
            cur = cur[seg]
        return cur

    check("★★出厂人设为空（不夹带用户写的性格提示词）",
          get("pet.personality_prompt") == "",
          repr(get("pet.personality_prompt"))[:60])
    # ★★ 1.9.4 真机修复后**改了口径**：用户 2026-10-04 拍板"头像作为出厂预设"，
    #   所以 `user_avatar_path` **应当**指向随包的预设头像，而不是空串。
    #   （旧断言要求"必须为空" —— 那正是"用户头像显示成 deepseek 的脸"的成因：
    #     空 → 回落 → 而回落目标是**形象**头像。）
    _uav = get("ui.user_avatar_path")
    check("★★出厂头像指向**随包的预设**（不是空串，空串会让用户头像变成形象的脸）",
          bool(_uav), repr(_uav))
    check("★而且那个预设文件真的在（不许指向不存在的路径）",
          bool(_uav) and (ROOT / _uav).exists(), str(_uav))
    check("★预设放在 data/avatars 下（不是指向某个角色目录里的图）",
          str(_uav).replace("\\", "/").startswith("data/avatars/"), str(_uav))
    check("★窗口位置为出厂值 [100, 100]", get("ui.pet_position") == [100, 100],
          str(get("ui.pet_position")))
    check("★★绝不出现 llm.api_key（密钥只走 .env）",
          "api_key" not in (_d.get("llm") or {}))
    check("★★绝不出现 vision.api_key", "api_key" not in (_d.get("vision") or {}))
    # 全文兜底：别把密钥/凭据写进任何字段。
    # ★ 判据要精确 —— 我第一版用 `"token" in k`，结果把 `llm.max_tokens` 和
    #   `pipeline.bulk_max_tokens` 当成密钥报了出来（它们是**数值上限**，不是凭据）。
    #   用 `_token$`（单数结尾）区分：`refresh_token` 是凭据，`max_tokens` 不是。
    import re as _re
    _SECRET_RE = _re.compile(r"api[_-]?key|secret|password|passwd|credential|"
                             r"cookie|refresh_token|access_token|(^|_)token$",
                             _re.I)
    _leak = [k for k in leaf_keys(_d) if _SECRET_RE.search(k)]
    check("★★全文没有任何密钥/凭据字段（api_key / secret / *_token …）",
          not _leak, str(_leak))
    # 项目级结构要保留，否则新用户没工具可用
    check("★保留了 enabled_tools（不然新用户一个工具都没有）",
          len(get("enabled_tools", []) or []) > 0, str(len(get("enabled_tools", []) or [])))

print("\n== C. 出厂配置与代码默认值不漂移 ==")
_r = subprocess.run([sys.executable, str(ROOT / "tools" / "make_default_config.py"), "--check"],
                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                    cwd=str(ROOT), **no_window_kwargs())
check("★★config.default.json 与代码当前默认值一致"
      "（配置项有增减却没重跑生成器 → 这里报红）",
      _r.returncode == 0, (_r.stdout or _r.stderr or "").strip().splitlines()[-1:][0]
      if (_r.stdout or _r.stderr) else "")

print("\n== D/E. 首启自建 与 优雅降级 ==")
from src.utils.config import AppConfig  # noqa: E402

_base = ROOT / "临时文件" / "_scratch_dev" / "tmp_guard_firstrun_194"

# D. 有出厂文件、没有 config.json → 应生成
if _base.exists():
    shutil.rmtree(_base, ignore_errors=True)
_base.mkdir(parents=True)
shutil.copyfile(DEFAULT, _base / "config.default.json")
_cfg = AppConfig(config_path=_base / "config.json")
check("★★只有出厂文件时，首启会生成 config.json", (_base / "config.json").exists())
if (_base / "config.json").exists():
    _j = json.loads((_base / "config.json").read_text(encoding="utf-8"))
    check("★生成的 config.json 用的是出厂值（人设空、头像指向随包预设）",
          _j["pet"]["personality_prompt"] == ""
          and _j["ui"]["user_avatar_path"] == _uav)
    check("★内存里的配置也确实按出厂值生效", _cfg.pet.personality_prompt == "")

# E. 连出厂文件都没有 → 不崩、也不乱造文件
_d2 = ROOT / "临时文件" / "_scratch_dev" / "tmp_guard_nofile_194"
if _d2.exists():
    shutil.rmtree(_d2, ignore_errors=True)
_d2.mkdir(parents=True)
try:
    _c2 = AppConfig(config_path=_d2 / "config.json")
    check("★★没有出厂文件时也不崩（用代码默认值继续跑）", True)
    check("★没有出厂文件时不乱造 config.json", not (_d2 / "config.json").exists())
except Exception as e:
    check("★★没有出厂文件时也不崩（用代码默认值继续跑）", False,
          f"{type(e).__name__}: {e}")

print(f"\n==== 结果: {len(PASS)} PASS / {len(FAIL)} FAIL ====")
for f in FAIL:
    print("  FAIL:", f)
sys.exit(1 if FAIL else 0)
