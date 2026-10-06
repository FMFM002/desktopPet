# -*- coding: utf-8 -*-
"""★★ 1.9.4：**本地服务探测**（Ollama / ComfyUI）—— 用户 2026-10-06 要求。

用户原话：
  "检测时，**ollama 可以直接启动服务看看有没有 ollama**；
   **comfyUI 不能自动检测注册表**，path 的话就让用户**手动启动，再点击检测**。"

## 两者为什么处理方式不同（照用户的判断来）
  · **Ollama**：装完就是一个能拉起来的本地服务，命令行 `ollama serve` 即可。
    → 所以探测时可以**替他试着启动一次**，起来了就说明装好了。
    但我们**不能**假设 `ollama` 在 PATH 里（用户可能装在别处）→ 先探测端口，
    再尝试启动；启动失败也**只是"没起来"，不是错误**。
  · **ComfyUI**：是一坨绿色目录（portable），**没有注册表、没有固定服务名**，
    我们**不可能**替他找到并启动 → 只能"给路径 + 让他自己启动 + 再点检测"。

## 设计原则
  · **只探测、不改用户的系统**：不写注册表、不设开机自启、不装东西。
  · 试启动 Ollama 时**只在本进程里起一个子进程**，且**不阻塞**（给几秒就返回）。
  · 探测不到就是"没起来"，**如实说**，不编。
"""
from __future__ import annotations

import socket
import subprocess
import time

OLLAMA_HOST = "127.0.0.1"
OLLAMA_PORT = 11434
COMFYUI_HOST = "127.0.0.1"
COMFYUI_PORT = 8188


def port_open(host: str = "127.0.0.1", port: int = 0,
              timeout: float = 0.6) -> bool:
    """某个端口有没有人在听。**这是所有本地服务探测的基础。**"""
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except Exception:
        return False


def probe_ollama(host: str = OLLAMA_HOST, port: int = OLLAMA_PORT,
                 try_start: bool = False, wait: float = 6.0) -> tuple:
    """探测 Ollama。返回 `(在跑?, 给人看的说明)`。

    `try_start=True` 时：端口没开就**替用户试一次 `ollama serve`**（用户要求），
    然后等最多 `wait` 秒看它起没起来。
      ★ 用 `DETACHED_PROCESS` 起，**不占住我们的进程**；起不来也**不算错误**
        （可能没装、可能装在别处 —— 如实说就行）。
    """
    if port_open(host, port):
        return True, f"✅ Ollama 正在运行（{host}:{port}）"
    if not try_start:
        return False, f"❌ {host}:{port} 没有服务在跑"
    try:
        import sys
        kwargs = {}
        if sys.platform == "win32":
            # ★ 让它独立于我们运行，别把桌宠拖住
            kwargs["creationflags"] = (0x00000008       # DETACHED_PROCESS
                                       | 0x00000200)    # CREATE_NEW_PROCESS_GROUP
        subprocess.Popen(["ollama", "serve"], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                         **kwargs)
    except FileNotFoundError:
        return False, ("❌ 没找到 ollama 命令（可能没装，或没加进 PATH）。\n"
                       "     去 https://ollama.com/download 装一下就好。")
    except Exception as e:
        return False, f"❌ 试着启动 Ollama 失败：{type(e).__name__}: {e}"

    _deadline = time.time() + max(1.0, wait)
    while time.time() < _deadline:
        if port_open(host, port):
            return True, f"✅ 刚把 Ollama 启动起来了（{host}:{port}）"
        time.sleep(0.5)
    return False, (f"❌ 试着启动了 Ollama，但 {wait:.0f} 秒内 {host}:{port} 还没起来。\n"
                   f"     可以打开一个命令行手动跑 `ollama serve` 看看它报什么。")


def probe_comfyui(host: str = COMFYUI_HOST, port: int = COMFYUI_PORT) -> tuple:
    """探测 ComfyUI 服务。返回 `(在跑?, 给人看的说明)`。

    ★ **不替他启动**（用户说得对：ComfyUI 没有注册表可查、位置也不固定）。
      所以这里只做一件事：**看那个端口有没有人** —— 用户自己启动好再点检测。
    """
    if port_open(host, port):
        return True, f"✅ ComfyUI 正在运行（{host}:{port}）"
    return False, (f"❌ {host}:{port} 没有服务在跑。\n"
                   f"     请**先自己启动 ComfyUI**（双击它的 `run_nvidia_gpu.bat`），\n"
                   f"     等它自己的窗口跑起来之后，再点这个「检测」。")


def probe_by_key(key: str, **kw) -> tuple:
    """按能力 key 探测（给向导用）。未知 key 返回"不需要探测"。"""
    if key == "ollama":
        return probe_ollama(try_start=kw.get("try_start", False))
    if key == "comfyui":
        return probe_comfyui()
    return False, ""          # 其它能力不需要本地服务探测
