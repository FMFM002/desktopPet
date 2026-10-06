# -*- coding: utf-8 -*-
"""★★ 1.9.4：**本地语言模型的一键获取**（用户拍板 Q4：**只做这个**）。

## 用户的决定（2026-10-06，用提问工具选的）
  Q4 范围 → **② 只自动拉模型**：
      "没装 Ollama 就引导你去官网装（可一键打开下载页），装好后我自动
       `ollama pull` 推荐模型。**不替你装软件**，风险低、体验也够顺。"
  选哪个模型 → **按硬件自动选**：
      "拉之前会把型号和体积告诉你，你确认了再拉。"

## 为什么先做这个（而不是 ComfyUI）
  它是"自部署"最好的**试点**：来源稳定（Ollama 官方 registry）、体积小、
  成功率高。把这套"**推荐 → 确认 → 拉取（看得见进度）→ 失败可重试**"的架子
  验一遍，将来套到 20GB 的 ComfyUI 上就只是换清单
  （见 `评估_画图工具自部署_1.9.4.txt`）。

## 边界（明确不做的）
  · **不下载、不静默安装 Ollama 本体**（那是"替用户装软件"，用户选了不做）；
  · 不设开机自启、不改注册表、不改 PATH；
  · 探测不到就如实说"没装"，并给出**去哪装**。
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass

from src.core import services as _svc

# Ollama 官方下载页（"引导用户去装"时给他）
OLLAMA_DOWNLOAD_URL = "https://ollama.com/download"


@dataclass
class Pick:
    """给用户看的推荐结果 —— **拉之前先给他看清楚**。"""
    model: str
    tier: str
    size_hint: str
    why: str


# 常见模型的**大概**体积（GB，量化版）。★ 只用于"拉之前告诉你多大"，
# 拉完的真实体积以 `ollama list` 为准 —— **宁可说个大概，也不编精确值**。
_APPROX_GB = {
    "qwen2.5:3b": 1.9,
    "qwen2.5:7b": 4.7,
    "qwen2.5:14b": 9.0,
    "qwen2.5:32b": 20.0,
    "llama3.1:8b": 4.7,
}


def recommend(hw=None) -> Pick:
    """按硬件推荐一个本地模型（用户要求"按硬件自动选"）。

    复用 `hardware.advise_local_llm()` —— **不另立一套判据**，
    否则两处标准迟早不一致。
    """
    from src.core import hardware as _hw
    if hw is None:
        hw = _hw.detect()
    adv = _hw.advise_local_llm(hw)
    # 从他的 picks 里取第一个"能直接 pull 的名字"（形如 xxx:tag）
    model = ""
    for p in adv.picks:
        m = re.search(r"([a-zA-Z0-9._\-]+:[a-zA-Z0-9._\-]+)", p)
        if m:
            model = m.group(1)
            break
    if not model:
        model = "qwen2.5:3b" if adv.tier != "none" else ""
    _gb = _APPROX_GB.get(model, 0.0)
    size = f"约 {_gb:.1f} GB" if _gb else "体积未知"
    return Pick(model=model, tier=adv.tier,
                size_hint=size, why=adv.why + f"｜{adv.title}")


def list_installed() -> tuple:
    """已经拉下来的模型。返回 `(列表, 说明)`；Ollama 不在就返回空列表。"""
    if not _svc.port_open(_svc.OLLAMA_HOST, _svc.OLLAMA_PORT):
        return [], "Ollama 没在跑（先启动它再试）"
    try:
        r = subprocess.run(["ollama", "list"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=20,
                           **_nw())
        names = []
        for ln in (r.stdout or "").splitlines()[1:]:      # 跳过表头
            parts = ln.split()
            if parts:
                names.append(parts[0])
        return names, f"已装 {len(names)} 个"
    except FileNotFoundError:
        return [], "没找到 ollama 命令"
    except Exception as e:
        return [], f"读取失败: {type(e).__name__}: {e}"


def has_model(model: str) -> bool:
    """★ 别重复下载（用户反复强调"**不要重复下载**"）。"""
    names, _ = list_installed()
    base = model.split(":")[0]
    return any(n == model or n.split(":")[0] == base for n in names)


def _nw() -> dict:
    """Windows 上别弹黑窗口（用户反馈过）。"""
    import sys
    return {"creationflags": 0x08000000} if sys.platform == "win32" else {}


def pull(model: str, on_line=None, timeout: float = 3600.0) -> tuple:
    """拉取模型。`on_line(str)` 会**逐行**收到输出（给界面显示进度）。

    ★ 返回 `(成功?, 说明)`；**不抛异常**（界面要能拿到结果）。
    ★ 先 `ollama list` 查一遍：**已经有了就直接返回成功**，不重复下
      （用户："**不要重复下载**"）。
    """
    if not _svc.port_open(_svc.OLLAMA_HOST, _svc.OLLAMA_PORT):
        return False, ("Ollama 没在运行。\n"
                       f"     请先去 {OLLAMA_DOWNLOAD_URL} 装好它并启动，再回来拉取。")
    if has_model(model):
        return True, f"✅ 已经有 {model} 了，跳过（不重复下载）"
    try:
        proc = subprocess.Popen(
            ["ollama", "pull", model],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            encoding="utf-8", errors="replace", bufsize=1, **_nw())
    except FileNotFoundError:
        return False, (f"没找到 ollama 命令。\n"
                       f"     请先去 {OLLAMA_DOWNLOAD_URL} 装一下。")
    except Exception as e:
        return False, f"启动拉取失败: {type(e).__name__}: {e}"

    try:
        for line in proc.stdout:                 # 逐行 → 界面（看得见在动）
            if on_line and line.strip():
                on_line(line.rstrip())
        proc.wait(timeout=timeout)
    except Exception as e:
        try:
            proc.kill()
        except Exception:
            pass
        return False, f"拉取中断: {type(e).__name__}: {e}"
    if proc.returncode == 0:
        return True, f"✅ {model} 拉取完成"
    return False, f"❌ 拉取失败（返回码 {proc.returncode}）—— 上面的输出里有原因"


def write_back(model: str) -> tuple:
    """拉成功后把它写进配置（`vision.ollama_model`），这样桌宠就会真的用它。

    ★ 用**现成的配置槽**，不新增键。
    """
    try:
        from src.utils.config import get_config
        cfg = get_config()
        cfg.vision.ollama_model = model
        cfg.save()
        return True, f"已写入配置: vision.ollama_model = {model}"
    except Exception as e:
        return False, f"写配置失败: {type(e).__name__}: {e}"
