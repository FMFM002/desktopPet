# -*- coding: utf-8 -*-
"""★★ 1.9.4：**模型集中管理 · 唯一注册表与路径解析**。

## 为什么要集中
1.9.4 之前模型**散落在四个地方**（真机取证）：

    src/genie_tts/assets/genie   1855 MB   TTS（GENIE）★ 藏在**源码目录**里
    data/whisper                  533 MB   STT（whisper.cpp）
    data/models/easyocr           100 MB   OCR
    data/tools                     11 MB   aria2c 二进制

用户的要求：**"这些模型散落在文件各个地方，希望集中管理。和 aria2 一样作为工具。"**

## 统一布局（本文件是唯一的"真理"）
    data/models/genie/      TTS 模型（GENIE 的 GenieData + CharacterModels）
    data/models/whisper/    STT 模型（ggml-*.bin + whisper-cli）
    data/models/easyocr/    OCR 模型
    data/models/            ← 以后新模型一律放这里，不许再散

> aria2c 是**可执行文件**不是模型，仍留在 `data/tools/`（由 `tools/fetch_aria2.py` 管）。
> 它俩的区别是"**模型** vs **二进制工具**"，所以不硬塞到一起。

## ★ 迁移期间的安全设计（为什么不是直接搬文件）
`resolve()` **优先新位置，找不到就回落旧位置** —— 这样：
  · 还没搬的机器**照常能用**（不会因为这次改动而开不了 TTS）；
  · 想搬的时候，跑 `python tools/model_manager.py migrate` 一键搬（**它才动文件**）；
  · 搬完自动走新位置，旧位置消失也无所谓。
**本模块自己永远不搬文件**，只回答"东西在哪"。

## 用法
    from src.core.models import resolve, status_all, MODELS
    p = resolve("genie")          # -> Path 或 None
    for row in status_all(): ...  # 给引导/安装程序用
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# 统一根目录：**以后所有模型都放这儿**
MODELS_ROOT = ROOT / "data" / "models"


@dataclass(frozen=True)
class ModelSpec:
    """一个模型资产的注册项。"""
    key: str                 # 稳定标识（配置/CLI 用它）
    label: str               # 给人看的名字
    rel: str                 # **规范位置**（相对项目根）
    legacy: tuple            # 旧位置（相对项目根）；迁移期回落用
    size_hint: str           # 大概多大（给安装程序提示）
    feature: str             # 没有它会少哪个功能
    marker: str = ""         # 判定"已就位"的标志文件/子目录（空 = 目录非空即可）
    fetchable: bool = False  # 是否支持自动下载（False = 需要用户手动放）


# ══════════════════════════════════════════════════════════════
# 注册表 —— **唯一真理**。新增模型只改这里。
# ══════════════════════════════════════════════════════════════
MODELS: dict[str, ModelSpec] = {
    "genie": ModelSpec(
        key="genie", label="语音合成模型（GENIE / GPT-SoVITS）",
        rel="data/models/genie",
        legacy=("src/genie_tts/assets/genie", "data/genie"),
        size_hint="约 1855 MB", feature="TTS 语音合成",
        marker="GenieData", fetchable=False,
    ),
    "whisper": ModelSpec(
        key="whisper", label="语音识别模型（whisper.cpp）",
        rel="data/models/whisper",
        legacy=("data/whisper",),
        size_hint="约 465 MB", feature="STT 语音识别",
        marker="", fetchable=False,
    ),
    "easyocr": ModelSpec(
        key="easyocr", label="OCR 模型（EasyOCR）",
        rel="data/models/easyocr",
        legacy=("data/models/easyocr",),
        size_hint="约 100 MB", feature="本地文字识别",
        marker="", fetchable=True,
    ),
}


def _has_content(p: Path, marker: str = "") -> bool:
    try:
        if not p.is_dir():
            return False
        if marker:
            return (p / marker).exists()
        return any(p.iterdir())
    except Exception:
        return False


def resolve_dir(key: str, base: Path | None = None) -> tuple[Path | None, str]:
    """找出这个模型**现在**在哪。

    返回 `(路径 或 None, 位置标记)`；标记取值：
      `"canonical"` 已在规范位置 / `"legacy"` 还在旧位置（建议迁移）/ `"missing"` 都没有。
    ★ 优先规范位置 —— 这样迁移是**渐进**的，不必一次搬完。

    ★ `base` 给**测试**用：让引导能"在一台什么都没有的机器上"跑
      （`first_run.detect(base=...)` 就干这个）。
      我第一版这里写死了 `ROOT`，于是 `detect(base=空目录)` **照样报"已就位"** ——
      "模拟新机器"的断言全部失效（是反向验证把它抓出来的）。
    """
    spec = MODELS.get(key)
    if spec is None:
        return None, "missing"
    _root = Path(base) if base is not None else ROOT
    canon = _root / spec.rel
    if _has_content(canon, spec.marker):
        return canon, "canonical"
    for leg in spec.legacy:
        lp = _root / leg
        if lp == canon:
            continue
        if _has_content(lp, spec.marker):
            return lp, "legacy"
    return None, "missing"


def resolve(key: str, base: Path | None = None) -> Path | None:
    """只要路径（找不到返回 None）。给业务代码用。"""
    return resolve_dir(key, base)[0]


def status_all() -> list[dict]:
    """给**引导**与**安装程序**用的状态表。

    每行：key / label / 在不在 / 在哪 / 大小提示 / 少哪个功能 / 能不能自动下。
    """
    out = []
    for key, spec in MODELS.items():
        p, where = resolve_dir(key)
        size = -1
        if p is not None:
            try:
                size = sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
            except Exception:
                size = -1
        out.append({
            "key": key, "label": spec.label,
            "present": p is not None, "where": where,
            "path": str(p) if p else "",
            "canonical_rel": spec.rel,
            "search_paths": [spec.rel, *spec.legacy],
            "size_hint": spec.size_hint,
            "size_bytes": size,
            "feature": spec.feature,
            "fetchable": spec.fetchable,
            "suggest_migrate": where == "legacy",
        })
    return out


def total_size_hint() -> str:
    """全部模型都要的大概体积（安装程序里问"要不要一起装"时显示）。"""
    return "约 2.4 GB（TTS 1855MB + STT 465MB + OCR 100MB）"


def ensure_models_root() -> Path:
    """把统一根目录建起来（先有地方放，才好往里搬）。"""
    MODELS_ROOT.mkdir(parents=True, exist_ok=True)
    return MODELS_ROOT


# ══════════════════════════════════════════════════════════════
# ★★ 1.9.4：**"我本地已经有这些文件了"** —— 扫他给的目录、校验、写进配置
#
#   用户要求（原话）："下载之前希望先询问用户有没有所需文件，**要求填写路径**，
#   **扫描路径（深度为1）**，确认是正确的模型就把**路径写到配置里面**。"
#   目的很实际：GENIE 那套 1855MB，谁本地已经有一份就不该再下一次。
# ══════════════════════════════════════════════════════════════
# 每个模型"长什么样"的判据（在**单个目录**层面判断，见 _looks_like）
_SHAPE_DOC = {
    "genie": "含 GenieData/ 或 CharacterModels/ 子目录（GENIE 的模型布局）",
    "whisper": "含 ggml-*.bin 或 whisper-cli.exe",
    "easyocr": "含 *.pth 模型文件",
}


def _looks_like(key: str, d: Path) -> bool:
    """这个目录**本身**像不像某个模型的根（不往下递归）。"""
    try:
        if not d.is_dir():
            return False
        if key == "genie":
            return (d / "GenieData").is_dir() or (d / "CharacterModels").is_dir()
        if key == "whisper":
            return (bool(list(d.glob("ggml-*.bin")))
                    or (d / "whisper-cli.exe").is_file()
                    or (d / "whisper-cli").is_file())
        if key == "easyocr":
            return bool(list(d.glob("*.pth")))
    except Exception:
        pass
    return False


def probe_dir(key: str, path: str, depth: int | None = None) -> tuple:
    """★ 扫用户给的目录，确认是不是我们要的模型。

    ★★ 1.9.4 真机修复（用户 2026-10-06）：
       原话："**模型检测，改成当前路径，搜索全部文件，不再限制深度。**"
       我原来写的是 `depth=1`（目录本身 + 一级子目录），因为想着"用户习惯不同"，
       但真机上仍然找不到 —— 用户的模型散在更深的层级里（尤其画图模型，
       **一般是散装在 ComfyUI 里面**的）。
       ⇒ 默认 `depth=None` = **不限深度**，把整棵树走一遍，
         找**第一个**满足特征判据的目录。
       （性能没问题：只在用户点"扫描"时跑，而且模型目录里文件不多。）

    返回 `(是否可用, 可用路径 或 None, 给人看的说明)`。
    """
    spec = MODELS.get(key)
    if spec is None:
        return False, None, f"未知模型: {key}"
    try:
        root = Path(path).expanduser()
    except Exception:
        return False, None, "路径无法解析"
    if not root.exists():
        return False, None, f"路径不存在: {root}"
    if not root.is_file() and not root.is_dir():
        return False, None, f"不是文件也不是目录: {root}"

    # ★ 不限深度：把 root 及其所有子目录都试一遍（root 自己最先试）
    tried = [root]
    if root.is_dir() and depth != 0:
        try:
            for _cur, _dirs, _files in os.walk(root):
                _dirs.sort()                       # 稳定顺序，便于复现
                for _d in _dirs:
                    tried.append(Path(_cur) / _d)
                if depth is not None and depth >= 1:
                    # 兼容仍想限制深度的调用方
                    _rel = len((Path(_cur) / "x").relative_to(root).parts) - 1
                    if _rel >= depth:
                        _dirs[:] = []
        except Exception:
            pass

    for cand in tried:
        if _looks_like(key, cand):
            _rel = "（就是它）"
            try:
                if cand != root:
                    _rel = f"（在子目录 {cand.relative_to(root)}）"
            except Exception:
                _rel = f"（在子目录 {cand.name}）"
            return True, cand, f"✅ 找到了 {spec.label} 的模型文件{_rel}"
    return False, None, (
        f"❌ 把 {root} 及其**所有子目录**都翻了一遍，"
        f"没找到 {spec.label} 的特征：\n"
        f"     {_SHAPE_DOC.get(key, '（未定义判据）')}\n"
        f"     如果你确认文件在，请直接把**模型所在的那一层**路径填进来。")


def bind_path(key: str, found) -> tuple:
    """把**已经找到的模型路径写进配置**（用现成的配置槽，**不新增键**）。

    返回 `(成功?, 给人看的结果)`。
      · genie   → `tts.genie_data_dir` / `tts.genie_character_dir`
      · whisper → `stt.whisper_cli_path` / `stt.whisper_model_path`
      · easyocr → **没有配置槽**（`ocr_engine._MODEL_DIR` 把目录写死在
                  `data/models/easyocr`）→ 改为**把它复制过去**（约 100MB），
                  这样"把路径写进配置"的意图仍然落实了。
    """
    spec = MODELS.get(key)
    if spec is None:
        return False, f"未知模型: {key}"
    found = Path(found)
    try:
        from src.utils.config import get_config
        cfg = get_config()
    except Exception as e:
        return False, f"读不到配置: {e}"

    try:
        if key == "genie":
            gd, cm = found / "GenieData", found / "CharacterModels"
            done = []
            if gd.is_dir():
                cfg.tts.genie_data_dir = str(gd)
                done.append(f"genie_data_dir = {gd}")
            if cm.is_dir():
                cfg.tts.genie_character_dir = str(cm)
                done.append(f"genie_character_dir = {cm}")
            if not done:
                return False, "这个目录里既没有 GenieData 也没有 CharacterModels"
            cfg.save()
            return True, "已写入配置：\n     " + "\n     ".join(done)

        if key == "whisper":
            done = []
            cli = next((p for p in (found / "whisper-cli.exe", found / "whisper-cli")
                        if p.is_file()), None)
            bins = sorted(found.glob("ggml-*.bin"))
            if cli:
                cfg.stt.whisper_cli_path = str(cli)
                done.append(f"whisper_cli_path = {cli.name}")
            if bins:
                cfg.stt.whisper_model_path = str(bins[0])
                done.append(f"whisper_model_path = {bins[0].name}")
            if not done:
                return False, "这个目录里没有 whisper-cli / ggml-*.bin"
            cfg.save()
            return True, "已写入配置：\n     " + "\n     ".join(done)

        if key == "easyocr":
            import shutil
            dst = ensure_models_root() / "easyocr"
            dst.mkdir(parents=True, exist_ok=True)
            n = 0
            for p in found.glob("*.pth"):
                shutil.copy2(p, dst / p.name)
                n += 1
            if not n:
                return False, "没找到 *.pth"
            return True, (f"已把 {n} 个模型文件复制到 {dst}"
                          f"（easyocr 的目录是写死的，没有配置项）")

    except Exception as e:
        return False, f"写入失败: {type(e).__name__}: {e}"
    return False, "未处理的模型"
