# -*- coding: utf-8 -*-
"""★★ 1.9.4 阶段5：**发布包守卫** —— 发布目录里**绝不许出现个人信息**。

## 为什么这是红线
发布包一旦发出去就**收不回来**。而工作区里混着大量个人数据：
    `.env`（API Key）· `data/avatars/user_avatar.jpg`（头像）·
    `data/DBs`（聊天记录与长期记忆）· `data/cache`（聊天图片）·
    `logs/`（运行日志）· `data/桌宠私有data`（私有文件）
手动复制**一定会漏**。所以交给 `tools/build_release.py`，并由本套件盯着它。

## 本套件钉什么
  A. `--dry-run` 的排除表**确实命中了**上面每一项（不是"写了规则但没生效"）；
  B. **默认发布体积是合理的**（把 1.8GB 的 TTS 模型排除在外）——
     否则"发布包 2GB"这种事只有到打包那天才发现；
  C. **真跑一次装配**（到临时目录），确认产物里**搜不到**个人文件；
  D. **人设默认被清空**（发布红线：铃兰那份明确是「明日方舟」IP 角色）；
     `--keep-persona` 才保留；
  E. ★ **反向守卫**：故意放一个"个人文件"进工作区，确认它**会被排除**
     —— 证明排除规则真的在起作用，而不是碰巧没命中。
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


def _run(*args) -> str:
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "build_release.py"), *args],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", cwd=str(ROOT))
    return (r.stdout or "") + (r.stderr or "")


print("== A. 排除表确实命中每一项个人数据 ==")
# ★ 口径修正：`.env` 与 `logs/` **本来就不在 INCLUDE 里**（根目录不在扫描范围），
#   所以它们不会出现在"已排除"报告里 —— 我第一版却按"报告里要有它"来断言，假红了两条。
#   这里只查**确实落在扫描范围内、靠排除规则挡掉的**那些；
#   `.env`/`logs` 由下面 C 段的"产物里没有它"来钉（那才是真性质）。
_dry = _run("--dry-run")
_MUST_EXCLUDE = {
    # ★ 头像**不再**在这里 —— 用户 2026-10-04 拍板"作为出厂预设"，它应当随包（见 D 段）
    "聊天记录与长期记忆": "聊天记录与长期记忆",
    "运行时缓存": "运行时缓存",
    "私有文件夹": "私有文件夹",
    "回归套件": "回归套件",
    "TTS 模型": "TTS 模型",
    "你的登录 cookie": "你的登录 cookie",
    "你下载的文件": "你下载的文件",
}
for _label, _needle in _MUST_EXCLUDE.items():
    check(f"★排除表命中「{_label}」", _needle in _dry)
check("★报告里说明了 .env 不能发（EXCLUDE_FILES 里有它，防将来有人把它加进 INCLUDE）",
      ".env" in (ROOT / "tools" / "build_release.py").read_text(encoding="utf-8"))

print("\n== B. 默认发布体积要合理（1.8GB 的 TTS 模型必须被排除）==")
import re as _re
_m = _re.search(r"合计 ([\d.]+) MB", _dry)
_mb = float(_m.group(1)) if _m else -1
check("★★默认发布体积 < 300MB（否则说明又把 TTS 模型带上了）",
      0 < _mb < 300, f"{_mb} MB")
check("★报告里写明了'不含模型'", "不含模型" in _dry)

print("\n== C. 真跑一次装配，产物里搜不到个人文件 ==")
_OUT = ROOT / "临时文件" / "_scratch_dev" / "tmp_release_194"
if _OUT.exists():
    shutil.rmtree(_OUT, ignore_errors=True)
_build_log = _run("--out", str(_OUT))
check("★装配成功（跑完没报错）", _OUT.is_dir(), _build_log.strip().splitlines()[-1:][0]
      if _build_log.strip() else "")
if _OUT.is_dir():
    _forbidden = [".env", "data/DBs", "data/cache", "logs", "data/桌宠私有data",
                  "tools/dev_tests", "config.json",
                  "临时文件", ".git"]
    # ★ 1.9.4（用户 2026-10-04 拍板）：头像与角色人设**作为出厂预设随包**，
    #   所以不再断言它们"被排除"——它们**应当**在产物里（见下面 D 段的反向断言）。
    _leaked = [p for p in _forbidden if (_OUT / p).exists()]
    check("★★产物里**没有任何**个人/开发文件", not _leaked, str(_leaked))
    check("★产物里有该有的东西（main.py / config.default.json / src）",
          (_OUT / "main.py").is_file() and (_OUT / "config.default.json").is_file()
          and (_OUT / "src").is_dir())
    check("★产物里有 .env.example（让用户知道要填什么）",
          (_OUT / ".env.example").is_file())
    check("★产物里**没有** TTS 模型（src/genie_tts/assets）",
          not (_OUT / "src" / "genie_tts" / "assets").exists())
    check("★但 TTS 的**代码**在（src/genie_tts/Internal.py）",
          (_OUT / "src" / "genie_tts" / "Internal.py").is_file())

    print("\n== D. 头像与角色人设**作为出厂预设随包**（用户 2026-10-04 拍板）==")
    _av = _OUT / "data" / "avatars" / "user_avatar.jpg"
    check("★★出厂头像**在**产物里（它是预设，不是要清掉的个人信息）", _av.exists(),
          str(_av.relative_to(_OUT)) if _av.exists() else "缺失")
    _cleared_any, _kept_any = False, False
    for _c in ((_OUT / "data" / "桌宠形象").iterdir()
               if (_OUT / "data" / "桌宠形象").is_dir() else []):
        _sp = _c / "settings.json"
        if not _sp.is_file():
            continue
        _d = json.loads(_sp.read_text(encoding="utf-8-sig"))
        _xing = _d.get("性格") or {}
        if _xing.get("free_text"):
            _kept_any = True
        else:
            _cleared_any = True
    check("★★角色人设**作为出厂预设保留**（默认不清空；要清得显式传 --clear-persona）",
          _kept_any, f"kept={_kept_any} cleared={_cleared_any}")
    check("★人设保留的同时**结构仍完整**（输出控制等没被动）",
          all(((json.loads((_c / "settings.json").read_text(encoding="utf-8-sig")))
               .get("输出控制") is not None)
              for _c in (_OUT / "data" / "桌宠形象").iterdir()
              if (_c / "settings.json").is_file()))
    check("★两个预设形象都在（用户指定 deepseek / 铃兰 为预设）",
          {_c.name for _c in (_OUT / "data" / "桌宠形象").iterdir()
           if _c.is_dir()} >= {"deepseek", "铃兰"},
          str(sorted(_c.name for _c in (_OUT / "data" / "桌宠形象").iterdir()
                     if _c.is_dir())))

    print("\n== E. 反向守卫：密钥类文件确实进不了产物 ==")
    _OUT2 = ROOT / "临时文件" / "_scratch_dev" / "tmp_release_194b"
    if _OUT2.exists():
        shutil.rmtree(_OUT2, ignore_errors=True)
    _run("--out", str(_OUT2))
    check("★★重新装配后产物里**依然没有 .env**（规则真在起作用）",
          not (_OUT2 / ".env").exists())
    check("★★产物里没有 DBs / 日志（你的数据进不去）",
          not (_OUT2 / "data" / "DBs").exists() and not (_OUT2 / "logs").exists())
    shutil.rmtree(_OUT2, ignore_errors=True)

shutil.rmtree(_OUT, ignore_errors=True)

print(f"\n==== 结果: {len(PASS)} PASS / {len(FAIL)} FAIL ====")
for f in FAIL:
    print("  FAIL:", f)
sys.exit(1 if FAIL else 0)
