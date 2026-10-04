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


def resolve_dir(key: str) -> tuple[Path | None, str]:
    """找出这个模型**现在**在哪。

    返回 `(路径 或 None, 位置标记)`；标记取值：
      `"canonical"` 已在规范位置 / `"legacy"` 还在旧位置（建议迁移）/ `"missing"` 都没有。
    ★ 优先规范位置 —— 这样迁移是**渐进**的，不必一次搬完。
    """
    spec = MODELS.get(key)
    if spec is None:
        return None, "missing"
    canon = ROOT / spec.rel
    if _has_content(canon, spec.marker):
        return canon, "canonical"
    for leg in spec.legacy:
        lp = ROOT / leg
        if lp == canon:
            continue
        if _has_content(lp, spec.marker):
            return lp, "legacy"
    return None, "missing"


def resolve(key: str) -> Path | None:
    """只要路径（找不到返回 None）。给业务代码用。"""
    return resolve_dir(key)[0]


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
