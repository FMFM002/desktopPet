# -*- coding: utf-8 -*-
"""★★ 1.9.4：**出厂预设审计** —— "所有预设都要是随包内容，不要虚空预设。检测一遍。"

（用户原话，2026-10-06。）

## 为什么值得单独一条（已经抓到真 bug）
出版本前出厂配置写着 `tts.provider=genie, tts.character=feibi`，
而 GENIE 那套模型 **1855MB 根本不随包**（发布包故意不带模型）——
于是**设置界面显示 genie/feibi，实际日志却在跑 edge-tts**。
用户的原话就是"**设置里面写的不一样**"、"**虚空预设**"。

这属于**最坑的一类 bug**：界面上写得一本正经，实际走的是另一条路。
所以必须让**机器**逐条盯着：**出厂配置里凡是"指向某个随包文件/目录"的预设，
那个东西必须真的在包里。**

## 分类（本套件把每条预设判成三种）
  ✅ 随包        —— 指向的东西确实在包里
  ℹ️ 依赖外部服务 —— 需要用户自己装/联网（**不算虚空**，但要说明白）
  ❌ 虚空        —— 指向本地内容，可那东西**不在包里**（就是这次的 bug）

## 反面（故意不判失败）但要在输出里点名的
  模型类（whisper/easyocr/GENIE）**故意不随包** —— 它们是"可选能力"。
  只要**预设不假装自己已经能用**就行。所以判据是：
  **`tts.provider` 这类"引擎选择"不许指向没随包的引擎**；
  模型的**路径字段**为空是**正常**的（空 = 还没配，用户去引导里弄）。
"""
import json
import os
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


_cfg = json.loads((ROOT / "config.default.json").read_text(encoding="utf-8"))


def g(path, default=None):
    cur = _cfg
    for seg in path.split("."):
        if not isinstance(cur, dict) or seg not in cur:
            return default
        cur = cur[seg]
    return cur


print("== ① 角色形象：pet.character 指向的形象必须在包里 ==")
_ch = g("pet.character", "")
_chdir = ROOT / "data" / "桌宠形象" / str(_ch)
check("★★出厂角色指向的目录**真实存在**（不是虚空形象）",
      bool(_ch) and _chdir.is_dir(), f"pet.character={_ch!r} → {_chdir.name}")
check("★而且那个形象有 settings.json（不是空壳目录）",
      (_chdir / "settings.json").is_file())
_shipped = sorted(p.name for p in (ROOT / "data" / "桌宠形象").iterdir()
                  if p.is_dir()) if (ROOT / "data" / "桌宠形象").is_dir() else []
print(f"      包里实际有的形象: {_shipped}")

print("\n== ② 用户头像：预设必须真的在包里（这条踩过）==")
_uav = g("ui.user_avatar_path", "")
check("★★出厂头像指向的文件**真的在**（用户拍板『头像作为出厂预设』）",
      bool(_uav) and (ROOT / str(_uav)).is_file(), str(_uav))

print("\n== ③ ★★ TTS 引擎预设：不许指向**没随包**的引擎（本次的真 bug）==")
_prov = str(g("tts.provider", "")).lower()
_genie_shipped = (ROOT / "src" / "genie_tts" / "assets" / "genie").is_dir()
check("★★出厂 TTS 预设是**开箱即用**的那个："
      "要么 provider=edge（线上合成，不依赖模型），"
      "要么 provider=genie **且 GENIE 模型确实随包**",
      _prov == "edge" or (_prov == "genie" and _genie_shipped),
      f"tts.provider={_prov!r} / GENIE 模型随包={_genie_shipped}")
check("★（说明，不断言）本机 src/genie_tts/assets/genie 是否存在 —— "
      "开发机上**有**是正常的，发布包里被排除",
      True, f"GENIE 模型在本机={'有' if _genie_shipped else '没有'}"
      f"（发布包一定不带，由 _test_release_pack_194 盯着）")
print("      ⇒ 这条正是用户说的『设置里面写的不一样』："
      "界面写 genie/feibi，日志跑 edge-tts")

print("\n== ④ 模型路径字段：空是**正常**的（用户去引导里配），"
      "但**不许指向不存在的东西** ==")
for _p in ("tts.genie_data_dir", "tts.genie_character_dir",
           "stt.whisper_cli_path", "stt.whisper_model_path"):
    _v = str(g(_p, "") or "")
    if not _v:
        check(f"★{_p} 为空 → 正常（还没配，交给引导/设置）", True, "(空)")
    else:
        check(f"★★{_p} 非空 → 那个路径必须真的存在（不能虚空指路）",
              (ROOT / _v).exists() or Path(_v).exists(), _v)

print("\n== ⑤ 视觉：模式与模型要说清是「远端/本地服务」还是「随包文件」==")
_vm = str(g("vision.mode", ""))
_vo = str(g("vision.ollama_model", ""))
check("★vision.mode 是已知取值（ocr/ollama/api 之一）",
      _vm in ("ocr", "ollama", "api"), _vm)
check("★ollama 的模型名只是**名字**（模型在用户的 Ollama 里，不是随包文件）"
      "—— 这类**不算虚空预设**，但界面上要写明白",
      bool(_vo), _vo)

print("\n== ⑥ 记忆/表情等「小预设」：指向的标签要让程序认得 ==")
_tags = g("pet.meme_tags_prefer", []) or []
check("★meme 标签预设非空且是字符串列表",
      isinstance(_tags, list) and all(isinstance(t, str) for t in _tags), str(_tags))

print("\n== ⑦ ★ 反向守卫：这套判据**真的能抓到虚空预设** ==")
# ★ 用**合成的假配置**来验判据本身，而不是拿当前真配置做对照 ——
#   我第一版就是拿真配置凑的，结果因为"本机 GENIE 模型存在"而算错（假红一次）。
#   判据（与上面③同一条）：provider=edge 一定合格；
#                          provider=genie 只在**发布包真的带 GENIE 模型**时才合格。


def _engine_ok(provider: str, models_in_package: bool) -> bool:
    return provider == "edge" or (provider == "genie" and models_in_package)


check("★★判据有效：`provider=genie` + **发布包不带模型** → 判它不合格"
      "（这正是本次那个真 bug 的形状）",
      not _engine_ok("genie", False))
check("★`provider=genie` + 发布包**确实带了**模型 → 合格（别误伤）",
      _engine_ok("genie", True))
check("★`provider=edge` → 一定合格（它不依赖任何随包模型）",
      _engine_ok("edge", False))
check("★当前出厂配置在这条判据下**合格**",
      _engine_ok(_prov, _genie_shipped))

print("\n== ⑧ ★★ 角色 settings.json 里的'名字型预设'："
      "头像 / 图标 / 启动 GIF / 待机 GIF 名字都必须能落到真文件 ==")
# 用户 2026-10-06："扩，做到全部自然最好。" —— 这就是"全部"的另一半：
# 出厂配置之外，每个角色的 settings.json 里还有一堆**用名字指路**的预设。
_chars = sorted(p for p in (ROOT / "data" / "桌宠形象").iterdir() if p.is_dir())
check("★有角色目录可审", bool(_chars), f"{len(_chars)} 个")
_bad_entries = []
for _c in _chars:
    _sp = _c / "settings.json"
    if not _sp.is_file():
        _bad_entries.append(f"{_c.name}: 没有 settings.json")
        continue
    _d = json.loads(_sp.read_text(encoding="utf-8-sig"))
    # ① avatar / icon：setting 里写的是**文件名**，必须真的在角色目录里
    for _fld in ("avatar", "icon"):
        _v = (_d.get(_fld) or "").strip()
        if _v and not (_c / _v).exists():
            _bad_entries.append(f"{_c.name}.{_fld} = {_v!r}（文件不存在）")
    # ② gif.startup / gif.idle：写的是**不带扩展名的名字**，要能落到 GIF/<名>.gif
    _gif = _d.get("gif") or {}
    _names = []
    if (_gif.get("startup") or "").strip():
        _names.append(("gif.startup", _gif["startup"].strip()))
    for _n in (_gif.get("idle") or []):
        _names.append(("gif.idle", str(_n).strip()))
    for _where, _n in _names:
        _cand = _c / "GIF" / f"{_n}.gif"
        if not _cand.exists():
            _bad_entries.append(f"{_c.name}.{_where} = {_n!r}（GIF 不存在）")

check("★★每个角色的 头像/图标/启动GIF/待机GIF **名字都能落到真文件**"
      "（虚空的会列在下面）",
      not _bad_entries,
      ("；".join(_bad_entries[:6]) + (" …" if len(_bad_entries) > 6 else "")))
for _b in _bad_entries:
    print(f"      ❌ {_b}")

print(f"\n==== 结果: {len(PASS)} PASS / {len(FAIL)} FAIL ====")
for f in FAIL:
    print("  FAIL:", f)
sys.exit(1 if FAIL else 0)
