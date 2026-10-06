# -*- coding: utf-8 -*-
"""★★ 1.9.4：**电脑配置检测 + 按配置推荐**（用户 2026-10-06 要求）。

用户原话："根据用户电脑配置来推荐，另外推荐**本地语言模型**。**电脑配置直接检测**。"

## 为什么需要它
引导里要问"要不要用本地画图 / 本地语言模型"，而**能不能用、该推荐哪一档，
完全取决于这台机器**（有没有独显、显存多大、内存多少、磁盘还剩多少）。
让用户自己判断"我该装哪个"是不现实的（"把用户当傻逼对待"——用户原话）。

## 设计原则
  · **只读**：不装东西、不改配置、不启动服务；`detect()` 可以在任何地方安全调用。
  · **零新依赖**：只用 psutil（已在 requirements）+ 标准库；GPU 靠 `nvidia-smi`，
    没有就当没有独显（**不猜、不编**）。
  · **如实**：探测不到就写"未知"，**不假装知道**（推荐逻辑要能容忍未知）。

## 检测什么
    CPU  逻辑核数 / 物理核数
    内存 总量与可用
    磁盘 项目所在盘的剩余空间（装 ComfyUI + 模型要 10~20GB）
    GPU  型号与显存（NVIDIA 走 nvidia-smi；其它厂商暂时探测不到 → 标"未知"）
"""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from src.utils.proc import no_window_kwargs as _nw

ROOT = Path(__file__).resolve().parents[2]


@dataclass
class Hardware:
    cpu_logical: int = 0
    cpu_physical: int = 0
    ram_total_gb: float = 0.0
    ram_avail_gb: float = 0.0
    disk_free_gb: float = 0.0
    gpu_name: str = ""
    vram_gb: float = 0.0
    gpu_vendor: str = "unknown"      # nvidia | amd | intel | unknown
    notes: list = field(default_factory=list)

    @property
    def has_gpu(self) -> bool:
        return bool(self.gpu_name) and self.vram_gb > 0


def _nvidia_smi() -> tuple:
    """用 nvidia-smi 问显卡（它随 NVIDIA 驱动一起装，最可靠且零依赖）。

    ★ 拿不到就返回空 —— **不猜**。用 `capture_output` 在这里是安全的：
      这是用户机器上的正常调用；沙箱里失败也只是返回空，不影响其它逻辑。
    """
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=6, **_nw())
        if r.returncode != 0 or not (r.stdout or "").strip():
            return "", 0.0
        first = r.stdout.strip().splitlines()[0]
        parts = [x.strip() for x in first.split(",")]
        name = parts[0] if parts else ""
        vram = 0.0
        if len(parts) > 1:
            try:
                vram = round(float(parts[1]) / 1024, 1)      # MB → GB
            except Exception:
                vram = 0.0
        return name, vram
    except Exception:
        return "", 0.0


def detect(base: Path | None = None) -> Hardware:
    """跑一遍硬件检测。**只读、不装东西、不启动服务。**"""
    hw = Hardware()
    try:
        import psutil
        hw.cpu_logical = psutil.cpu_count(logical=True) or 0
        hw.cpu_physical = psutil.cpu_count(logical=False) or 0
        vm = psutil.virtual_memory()
        hw.ram_total_gb = round(vm.total / 1024 ** 3, 1)
        hw.ram_avail_gb = round(vm.available / 1024 ** 3, 1)
    except Exception as e:
        hw.notes.append(f"psutil 不可用（CPU/内存探测不到）: {type(e).__name__}")
        hw.cpu_logical = os.cpu_count() or 0

    try:
        hw.disk_free_gb = round(
            shutil.disk_usage(str(base or ROOT)).free / 1024 ** 3, 1)
    except Exception as e:
        hw.notes.append(f"磁盘探测失败: {type(e).__name__}")

    name, vram = _nvidia_smi()
    if name:
        hw.gpu_name, hw.vram_gb, hw.gpu_vendor = name, vram, "nvidia"
    else:
        hw.notes.append("没检测到 NVIDIA 显卡（nvidia-smi 不可用）——"
                        "如果你有独显，可能是驱动没装；本工具暂时只认 NVIDIA")
    return hw


# ══════════════════════════════════════════════════════════════
# 按配置推荐（用户："根据用户电脑配置来推荐"）
# ══════════════════════════════════════════════════════════════
@dataclass
class Advice:
    tier: str            # none | low | mid | high
    title: str
    why: str
    picks: list = field(default_factory=list)


def _tier(hw: Hardware, need_disk: float) -> str:
    """按显存分档。**未知也不崩** —— 未知一律按最低档走（保守）。"""
    if hw.disk_free_gb and hw.disk_free_gb < need_disk:
        return "none"
    if not hw.has_gpu:
        return "none"
    if hw.vram_gb >= 12:
        return "high"
    if hw.vram_gb >= 8:
        return "mid"
    if hw.vram_gb >= 4:
        return "low"
    return "none"


def advise_drawing(hw: Hardware) -> Advice:
    """画图（ComfyUI + 模型）建议。"""
    t = _tier(hw, need_disk=25)
    if t == "none":
        if not hw.has_gpu:
            return Advice("none", "不建议在这台机器上本地画图",
                          f"没检测到可用的 NVIDIA 显卡。本地画图基本靠显卡，"
                          f"硬上会非常慢。（内存 {hw.ram_total_gb}GB／"
                          f"磁盘余 {hw.disk_free_gb}GB）",
                          ["可以只用「云端画图」（把提示词交给在线服务）",
                           "或者你自己装好 ComfyUI 后，我来接管调用"])
        return Advice("none", "磁盘不太够",
                      f"本地画图要预留约 25GB（ComfyUI 本体 + 模型），"
                      f"你现在只剩 {hw.disk_free_gb}GB。", [])
    table = {
        "low": ("入门", "显存 4~8GB：能跑，但分辨率与批量要压着来",
                ["NewBie（现用预设，轻）— 512~768 分辨率"]),
        "mid": ("够用", "显存 8~12GB：主流模型都能跑，速度可接受",
                ["NewBie（现用预设）— 768~1024 分辨率",
                 "想更精细可再上 SDXL 类模型"]),
        "high": ("舒服", "显存 ≥12GB：可以上更大的模型与更高分辨率",
                 ["NewBie（现用预设）— 1024+ 分辨率",
                  "也可考虑 SDXL / Flux 类（更大更慢）"]),
    }
    title, why, picks = table[t]
    return Advice(t, title, f"{why}。显卡 {hw.gpu_name}（{hw.vram_gb}GB 显存）／"
                            f"内存 {hw.ram_total_gb}GB／磁盘余 {hw.disk_free_gb}GB",
                   picks)


def advise_local_llm(hw: Hardware) -> Advice:
    """本地语言模型建议（用户明确要求"另外推荐本地语言模型"）。

    ★ 依据是**内存 + 显存**（本地 LLM 要么吃显存，要么吃内存）。
      档位用"参数量"表述，并给出 Ollama 的常见拉取名。
    """
    vram, ram = hw.vram_gb, hw.ram_total_gb
    if vram >= 12 or ram >= 32:
        return Advice("high", "可以跑 14B~32B 级别的本地模型",
                      f"显存 {vram}GB／内存 {ram}GB",
                      ["qwen2.5:14b（中文好，14B）", "更大：qwen2.5:32b（很吃资源）"])
    if vram >= 8 or ram >= 16:
        return Advice("mid", "适合 7B~8B 级别",
                      f"显存 {vram}GB／内存 {ram}GB",
                      ["qwen2.5:7b（中文好、体积适中）", "llama3.1:8b"])
    if ram >= 8:
        return Advice("low", "只能跑 3B 以下的小模型（质量一般）",
                      f"显存 {vram}GB／内存 {ram}GB",
                      ["qwen2.5:3b", "建议：还是用云端 API 更划算"])
    return Advice("none", "不建议本地跑语言模型",
                  f"显存 {vram}GB／内存 {ram}GB —— 太勉强了",
                  ["直接用云端 API（就是引导里填的那个 Key）"])


def summary_line(hw: Hardware) -> str:
    """一行摘要，给日志/界面用。"""
    gpu = f"{hw.gpu_name}（{hw.vram_gb}GB）" if hw.has_gpu else "无独显/未知"
    return (f"CPU {hw.cpu_physical}核/{hw.cpu_logical}线程 ｜ "
            f"内存 {hw.ram_total_gb}GB（可用 {hw.ram_avail_gb}GB）｜ "
            f"显卡 {gpu} ｜ 磁盘余 {hw.disk_free_gb}GB")
