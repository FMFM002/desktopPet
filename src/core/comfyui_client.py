#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
★ 1.9.1: ComfyUI 画图**引擎**（纯逻辑，不含工具元数据）

【为什么从 tools/comfyui_tool.py 搬到这里】
  用户要求"画图像下载一样提交为后台任务"。后台任务有两条硬约束：

  ① **任务状态必须活得比工具久**。ToolManager 对 LAZY 工具是"空闲就卸载、
     下次重新 import"的（`sys.modules` 里那个模块对象会被扔掉），
     所以写在 `tools/comfyui_tool.py` 里的队列/线程/回调**活不过一次卸载**。
     这正是表情包信箱（`src/core/meme_bus.py`）踩过的坑 ——
     工具把结果塞进模块 A，主窗口 import 到的是模块 B，永远读不到。
     → 引擎和任务队列放 `src/core/`，它们不属于任何工具，从进程启动活到退出。

  ② **后台线程要能干活，不能依赖工具模块还在**。worker 线程直接用本模块的
     `submit()/poll()`，跟 ToolManager 加载/卸载工具完全解耦。

【职责】
  · 配置/HTTP（`COMFYUI_HOST` / `COMFYUI_PORT`）
  · 显存与**提交量**检查（`vram_guard`）—— 见下面「关于提交量」那段
  · 工作流拼装（照用户库里的 `ONLY-NB-QQBot.json`，参数对过 `/object_info`）
  · 提交任务 / 查一次状态 / 取回图片 / 释放模型

【关于提交量（2026-09-18 事故后加的）】
  实测数据（512×512 / 8 步，本机 RTX 5060 Laptop 8GB + 15.73GB 内存）：
      指标                  空闲      画图峰值        画完
      常驻物理内存 RSS      0.68 GB   3.77 GB(+3.08)  3.76 GB
      **私有提交(虚拟内存)** 4.60 GB  **14.11 GB**    **13.74 GB（不释放）**
  → 模型确实靠显存跑，**常驻内存只涨约 3GB**；
    但 ComfyUI 会预提交一大批宿主内存（CUDA host staging / pinned buffer，
    因为 7.96GB 显存装不下 UNET 6.49 + gemma3 7.23，要走宿主内存卸载通道）。
    而 Windows 事件 2004（资源耗尽）判的正是**提交额度**（物理内存 + 页面文件）。
  → 所以本模块的检查**同时看显存和提交余量**，并提供 `free()` 释放。
"""
from __future__ import annotations

import json
import logging
import os
import random
import subprocess
import time
from pathlib import Path

import requests
from dotenv import load_dotenv
from src.utils.proc import no_window_kwargs as _nw

_log = logging.getLogger("ComfyUIClient")

BASE_DIR = Path(__file__).resolve().parent.parent.parent
load_dotenv(BASE_DIR / ".env")

# 参考工作流（用户 ComfyUI 里的 ONLY-NB-QQBot.json）自带的负面提示词，原样搬来
DEFAULT_NEGATIVE = (
    "<danbooru_tags>low_score_rate, worst quality, low quality, bad quality, lowres, "
    "low res, pixelated, blurry, blurred, compression artifacts, jpeg artifacts, "
    "bad anatomy, worst hands, deformed hands, deformed fingers, deformed feet, "
    "deformed toes, extra limbs, extra arms, extra legs, extra fingers, extra digits, "
    "extra digit, fused fingers, missing limbs, missing arms, missing fingers, "
    "missing toes, wrong hands, ugly hands, ugly fingers, twisted hands, "
    "flexible deformity, conjoined, disembodied, text, watermark, signature, logo, "
    "ugly, worst, very displeasing, displeasing, error, doesnotexist, unfinished, "
    "poorly drawn face, poorly drawn hands, poorly drawn feet, artistic error, "
    "bad proportions, bad perspective, out of frame, ai-generated, ai-assisted, "
    "stable diffusion, overly saturated, overly vivid, cross-eye, expressionless, "
    "scan, sketch, monochrome, simple background, abstract, sequence, lineup, "
    "2koma, 4koma, microsoft paint \\(medium\\), artifacts, adversarial noise, "
    "has bad revision, resized, image sample,low_aesthetic</danbooru_tags>\n"
    "embedding:badhandv4,negative_hand-neg,negative_hand,bhands,bhands-neg"
)

# 默认模型（= ONLY-NB-QQBot.json 里那套）
DEFAULT_UNET = "newbie01.safetensors"
DEFAULT_CLIP1 = "gemma3-4b-it.safetensors"
DEFAULT_CLIP2 = "jina-clip-v2.safetensors"
DEFAULT_VAE = "ae.safetensors"
DEFAULT_LORA = "NBfym.safetensors"


# ══════════════════ 配置 / HTTP ══════════════════

def base_url() -> str:
    host = (os.getenv("COMFYUI_HOST", "") or "127.0.0.1").strip()
    port = (os.getenv("COMFYUI_PORT", "") or "8188").strip()
    return f"http://{host}:{port}"


def comfy_root() -> Path:
    return Path((os.getenv("COMFYUI_PATH", "") or "").strip())


def _get(path: str, timeout: float = 15.0):
    r = requests.get(base_url() + path, timeout=timeout)
    r.raise_for_status()
    return r.json()


def _post(path: str, payload: dict, timeout: float = 30.0, allow_empty: bool = False):
    """POST 一个 JSON 出去，返回解析后的 dict。

    ★ 1.9.1 修正（用户真机实测报的误报）：`/free` 返回的是
      **HTTP 200 + 空 body**（Content-Length: 0）。
      原来的实现一律 `r.json()` → 空 body 直接 JSONDecodeError →
      被外层 except 吞掉 → **返回 False**，于是"工具嘴上说失败了两次"，
      可实际上模型**已经卸干净了**（用户实测：python 显存 5.67G→0.20G、
      显卡空闲 1.10G→6.80G、内存空闲 2.4G→5.24G、commit 余量 22.74G）。
      所以：这类允许空 body 的接口，只要 HTTP 2xx 就算成功。
    """
    r = requests.post(base_url() + path, json=payload, timeout=timeout)
    r.raise_for_status()
    if not (r.content or b"").strip():
        return {}
    try:
        return r.json()
    except ValueError:
        return {} if allow_empty else {}


def alive() -> bool:
    try:
        _get("/system_stats", timeout=6)
        return True
    except Exception:
        return False


def free_models() -> bool:
    """★ 释放 ComfyUI 的模型与显存（可逆：下次画图自动重载，只慢约 30 秒）。

    主要目的是**把那一大坨提交额度还回去** —— 实测画完不释放时
    ComfyUI 会一直占着 ~13.7GB 私有提交，而本机物理内存只有 15.73GB。

    ★ 1.9.1: 走 `allow_empty=True`。`/free` 返回 **200 + 空 body**，
      老实现解析 JSON 抛异常 → 误报失败（用户看到"嘴上说失败了两次"，
      实际早就卸干净了）。这个接口没有响应体，**HTTP 2xx 就是成功**。
    """
    try:
        _post("/free", {"unload_models": True, "free_memory": True},
              timeout=30, allow_empty=True)
        _log.info("[ComfyUI] 已请求释放模型与显存")
        return True
    except Exception as e:
        _log.warning(f"[ComfyUI] 释放失败: {e}")
        return False


# ══════════════════ ★ 1.9.1: 进程生命周期（启动 / 关闭）══════════════════
# 用户原话："comfyUI的启动，加载模型，卸载模型，关闭comfyUI都写入tool自动完成。"
# 以及："走其他tool一样的卸载规则。comfyUI和模型同加载同卸载，不用分开。"
# → 所以工具负责：要用时**自己把 ComfyUI 拉起来**；工具空闲卸载时**把它关掉**
#   （见 src/core/comfyui_jobs.py 的 on_tool_unloaded 与主窗口注册的 post_unload 钩子）。
#   "加载/卸载模型"由 ComfyUI 自己按需做（画图时自动载，/free 时卸），不用我们插手。

def launcher():
    """ComfyUI 的启动脚本（用户给的 COMFYUI_PATH 下的 run_nvidia_gpu.bat）

    ★ 找不到时返回 **None**，不要返回 `Path()` ——
      `Path("")` 会变成 `Path(".")`，而 **`Path(".").exists()` 是 True**，
      于是"能不能启动"这个判断永远为真：工具会以为自己能拉起 ComfyUI，
      实际什么都没发生（自测抓到：连启动脚本都找不到时还返回了"已排队"）。
    """
    root = comfy_root()
    for name in ("run_nvidia_gpu.bat", "run_cpu.bat", "run.bat"):
        p = root / name
        if p.exists():
            return p
    return None


def _comfy_pids() -> list:
    """找出属于这个 ComfyUI 安装目录的进程（按 exe 路径认，别误杀别人）"""
    root = str(comfy_root() or "").lower().replace("/", "\\")
    if root in ("", ".", "\\"):
        return []
    out = []
    try:
        import psutil
        for p in psutil.process_iter(["pid", "name", "exe"]):
            try:
                exe = (p.info.get("exe") or "").lower().replace("/", "\\")
                if exe and root in exe:
                    out.append(p.info["pid"])
            except Exception:
                continue
    except Exception as e:
        _log.debug(f"[ComfyUI] 枚举进程失败: {e}")
    return out


def start(wait_seconds: int = 120) -> dict:
    """启动 ComfyUI 并等它就绪 → {"ok": bool, "text": str, "already": bool}

    已经在跑就直接返回（幂等）。启动方式：用 `cmd /c start` **脱离父进程**拉起
    `run_nvidia_gpu.bat` —— 不能直接 Popen 等它，那样桌宠一退出 ComfyUI 就跟着死，
    而且 bat 会一直占住管道。
    """
    # ★★ 1.9.3（第 3 轮）：**路径无关的硬闸** —— 回归/自测绝不允许启动用户的 ComfyUI。
    #   真凶：`_test_comfyui_191.py` 第 384 行把 ensure_running 还原成真函数、
    #   第 491 行又调 execute(action="draw") → comfyui_jobs 的 worker 走到
    #   ensure_running() → 机器上没跑就真的 Popen 拉起 run_nvidia_gpu.bat。
    #   只在测试里打桩不保险（还原一处就漏一处），所以在**启动的唯一出口**上加闸；
    #   `_sweep190.ps1` 会给所有测试进程设 PRTS_NO_COMFY_START=1。
    if os.environ.get("PRTS_NO_COMFY_START") == "1":
        _log.warning("[ComfyUI] 被 PRTS_NO_COMFY_START=1 拦住，**拒绝启动**"
                     "（回归/自测环境，避免动用户的 ComfyUI）")
        return {"ok": False, "already": False,
                "text": "已按环境变量 PRTS_NO_COMFY_START=1 禁用自动启动（回归环境）。"}
    if alive():
        return {"ok": True, "already": True, "text": "ComfyUI 已经在跑了。"}
    bat = launcher()
    if not bat:
        return {"ok": False,
                "text": (f"找不到 ComfyUI 启动脚本（在 {comfy_root()} 下找 "
                         f"run_nvidia_gpu.bat / run_cpu.bat）。"
                         f"请检查 .env 里的 COMFYUI_PATH。")}
    try:
        # ★ CREATE_NEW_CONSOLE + 不捕获输出：让 ComfyUI 有自己的窗口、不占我们的管道
        subprocess.Popen(
            ["cmd", "/c", "start", "", str(bat)],
            cwd=str(bat.parent),
            creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
        )
        _log.info(f"[ComfyUI] 已拉起启动脚本: {bat}")
    except Exception as e:
        return {"ok": False, "text": f"启动失败: {e}"}

    deadline = time.time() + max(10, int(wait_seconds))
    while time.time() < deadline:
        time.sleep(1.5)
        if alive():
            _log.info(f"[ComfyUI] 已就绪（{int(time.time() - (deadline - wait_seconds))} 秒）")
            return {"ok": True, "already": False,
                    "text": "ComfyUI 已启动并就绪。"}
    return {"ok": False,
            "text": (f"启动了但 {wait_seconds} 秒内没就绪（{base_url()} 连不上）。"
                     f"请看它自己那个窗口里的报错。")}


def stop(wait_seconds: int = 15) -> dict:
    """关闭 ComfyUI（只关**这个安装目录**下的进程，绝不误杀别人的 python）

    → {"ok": bool, "text": str, "killed": n}
    """
    pids = _comfy_pids()
    if not pids:
        return {"ok": True, "killed": 0, "text": "ComfyUI 没在跑。"}
    import psutil
    killed = []
    for pid in pids:
        try:
            p = psutil.Process(pid)
            p.terminate()          # 先礼后兵：给它机会自己退出
            killed.append(pid)
        except Exception as e:
            _log.debug(f"[ComfyUI] 结束 pid {pid} 失败: {e}")
    deadline = time.time() + max(2, int(wait_seconds))
    while time.time() < deadline:
        time.sleep(0.5)
        if not _comfy_pids():
            break
    else:
        # 还没死透 → 强杀
        for pid in _comfy_pids():
            try:
                psutil.Process(pid).kill()
            except Exception:
                pass
    _log.info(f"[ComfyUI] 已关闭（结束 {len(killed)} 个进程: {killed}）")
    return {"ok": True, "killed": len(killed),
            "text": f"已关闭 ComfyUI（结束 {len(killed)} 个进程）。"}


def ensure_running(wait_seconds: int = 120) -> dict:
    """要用之前保证它在跑：没跑就自己拉起来。"""
    if alive():
        return {"ok": True, "already": True, "text": ""}
    return start(wait_seconds=wait_seconds)


# ══════════════════ 内存 / 显存 ══════════════════

def vram() -> dict:
    """ComfyUI 自己报的显存（比从外面猜准）"""
    try:
        j = _get("/system_stats", timeout=8)
        for d in (j.get("devices") or []):
            return {"name": d.get("name", "?"),
                    "total": int(d.get("vram_total", 0) or 0),
                    "free": int(d.get("vram_free", 0) or 0)}
    except Exception as e:
        _log.debug(f"[ComfyUI] 取显存失败: {e}")
    return {}


def host_memory() -> dict:
    """宿主内存 + **提交量**（Windows 事件 2004 判的是后者）。

    `commit_limit` = 物理内存 + 页面文件；`commit_free` 是还能提交多少。
    这才是"会不会把系统压爆"的正确指标。
    """
    try:
        import psutil
        vm = psutil.virtual_memory()
        sm = psutil.swap_memory()
        commit_total = vm.total + sm.total
        commit_used = sm.used + (vm.total - vm.available)
        return {"ram_total": vm.total, "ram_avail": vm.available,
                "commit_total": commit_total,
                "commit_free": max(0, commit_total - commit_used)}
    except Exception as e:
        _log.debug(f"[ComfyUI] 取内存失败: {e}")
        return {}


def fmt_gb(b) -> str:
    try:
        return f"{int(b) / (1024 ** 3):.2f} GB"
    except Exception:
        return "?"


def proc_name(pid: int) -> str:
    try:
        import psutil
        p = psutil.Process(pid)
        n = p.name()
        try:
            n += "  (" + (p.exe() or "") + ")"
        except Exception:
            pass
        return n
    except Exception:
        return f"pid {pid}"


def gpu_top(n: int = 5) -> list:
    """显存占用前 n 名：[(名字, 字节), ...]

    数据源是 Windows 性能计数器 `\\GPU Process Memory(*)\\Local Usage`。
    **不能用 nvidia-smi** —— 本机 WDDM 模式下它给每进程一律返回 [N/A]（实测过）。
    """
    ps = (
        "$ErrorActionPreference='SilentlyContinue';"
        r"Get-Counter '\GPU Process Memory(*)\Local Usage' |"
        "Select-Object -ExpandProperty CounterSamples |"
        "Where-Object {$_.CookedValue -gt 0} |"
        "ForEach-Object { '{0}|{1}' -f $_.InstanceName, [int64]$_.CookedValue }"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=30,
            encoding="utf-8", errors="replace", **_nw())
        raw = out.stdout or ""
    except Exception as e:
        _log.debug(f"[ComfyUI] 取显存占用失败: {e}")
        return []

    per_pid = {}
    for line in raw.splitlines():
        line = line.strip()
        if "|" not in line or not line.startswith("pid_"):
            continue
        inst, _, val = line.rpartition("|")
        try:
            pid = int(inst.split("_")[1])
            per_pid[pid] = per_pid.get(pid, 0) + int(val)
        except Exception:
            continue
    top = sorted(per_pid.items(), key=lambda kv: -kv[1])[:n]
    return [(proc_name(pid), b) for pid, b in top]


def _model_path(kind: str, name: str) -> Path:
    """按 ComfyUI 的模型目录找文件（unet 有 diffusion_models / unet 两个位置）"""
    root = comfy_root() / "ComfyUI" / "models"
    cands = {
        "unet": ["diffusion_models", "unet"],
        "clip": ["clip", "text_encoders"],
        "vae": ["vae"],
        "lora": ["loras"],
    }.get(kind, [])
    for sub in cands:
        p = root / sub / name
        if p.exists():
            return p
    return Path()


def required_vram_bytes() -> dict:
    """估算常驻量：**只看 UNET + LoRA**，别把所有权重加起来。

    ★ 为什么（实测得出，不是猜的）：
      · gemma3-4b-it 有 7.23GB，比本机 7.96GB 显存还接近上限，
        但 ComfyUI 编完提示词就把它换出（必要时丢内存）→ **画得出来**。
        第一版把"最大的文件"当成需要显存，于是每次画都误报"显存偏紧"。
      · 真正全程常驻 GPU 的是 **UNET**（采样器一直在用）＋LoRA。
    """
    sizes, resident = {}, 0
    for kind, name in (("unet", DEFAULT_UNET), ("clip", DEFAULT_CLIP1),
                       ("clip", DEFAULT_CLIP2), ("vae", DEFAULT_VAE),
                       ("lora", DEFAULT_LORA)):
        p = _model_path(kind, name)
        if p and p.exists():
            n = p.stat().st_size
            sizes[f"{kind}:{name}"] = n
            if kind in ("unet", "lora"):
                resident += n
    return {"sizes": sizes, "resident": resident}


def _split_gpu_usage(top_all: list) -> tuple:
    """把进程显存占用分成 (ComfyUI 自己的, 别人的)

    ★ 为什么必须分：ComfyUI **画完不卸载模型**（实测跑完一张后它的 python.exe
      一直占着 5.79GB 显存）。如果拿 `vram_free` 直接和模型大小比，
      第二次画时 free 只剩 0.99GB → 会被误判"显存不足"，
      而实际上那些显存正是 ComfyUI 自己拿着、随时可以复用。
      所以判定要用 **空闲 + ComfyUI 自己占的** = "ComfyUI 能动的量"。
    """
    root = str(comfy_root() or "").lower().replace("/", "\\")
    # ★ 坑：没配 COMFYUI_PATH 时 `Path("")` 的 str() 是 "."，
    #   而 "." 能匹配上**任何带点的进程名**（"chrome.exe" 也含点）→
    #   会把全世界的显存都算成"ComfyUI 自己的"，永远不报显存不足。
    if root in ("", ".", "\\", "none"):
        root = ""
    own, others = 0, []
    for name, b in (top_all or []):
        n = (name or "").lower().replace("/", "\\")
        if root and root in n:
            own += b
        else:
            others.append((name, b))
    return own, others


def guard(check_commit: bool = True):
    """画图前的资源检查 → (能不能画, 提醒语, 拒绝详情)

    只拦"**绝对会爆**"（用户原话："判断绝对会爆就返回显存不足"）：
      · 显存：连常驻权重的 0.6 倍都不到才拒绝
      · ★ 提交量（1.9.1 新增）：空闲提交低于 6GB 就拒绝 ——
        实测画图峰值要 ~14GB 提交（相对空载 +9.5GB），
        而事故就是这么发生的（ComfyUI 13.7 + llava 6.4 + whisper 3.8 > 上限 33GB）。
        这一条比显存那条更重要：本机显存从没真正 OOM 过，爆的一直是提交额度。
    """
    v = vram()
    est = required_vram_bytes()
    resident = est.get("resident", 0)
    notes, blocks = [], []

    if v and resident:
        free, total = v.get("free", 0), v.get("total", 0)
        top_all = gpu_top(15)
        if top_all:
            own, others = _split_gpu_usage(top_all)
            avail = free + own
            head = (f"显卡 {v.get('name', '?')}：总 {fmt_gb(total)}，"
                    f"空闲 {fmt_gb(free)}"
                    + (f"，其中 ComfyUI 自己占着 {fmt_gb(own)}（画完不卸载，正常）"
                       if own else "")
                    + f"；这次要常驻约 {fmt_gb(resident)}")
            if avail < resident * 0.6:
                lines = ("；".join(f"{n} {fmt_gb(b)}" for n, b in others[:5])
                         or "（没有别的程序占显存）")
                blocks.append(f"显存不足。\n{head}\n"
                              f"ComfyUI 能动用的显存只有 {fmt_gb(avail)}，"
                              f"连模型常驻需要的 {fmt_gb(resident)} 都远远不到。\n"
                              f"当前显存占用前 5 名（ComfyUI 之外）：{lines}\n"
                              f"→ 关掉上面这些程序再试。")
            elif avail < resident:
                notes.append(f"显存偏紧（{head}），能画但可能慢。")

    if check_commit:
        hm = host_memory()
        if hm:
            cf = hm.get("commit_free", 0)
            if cf < 6 * 1024 ** 3:
                top = gpu_top(5)
                lines = ("；".join(f"{n} {fmt_gb(b)}" for n, b in top)
                         or "（拿不到每进程占用）")
                blocks.append(
                    f"系统虚拟内存（提交量）不够，画不了。\n"
                    f"提交上限 {fmt_gb(hm.get('commit_total', 0))}，"
                    f"现在只剩 {fmt_gb(cf)} 可用。\n"
                    f"画一张图峰值要 ~14GB 提交（ComfyUI 要把权重经由宿主内存装卸）。\n"
                    f"当前占用较高的进程：{lines}\n"
                    f"→ 关掉一些程序；或者先让我 free 掉 ComfyUI 已经占着的那部分"
                    f"（action=free）。")
            elif cf < 10 * 1024 ** 3:
                notes.append(f"提交量偏紧（只剩 {fmt_gb(cf)}），能画但别同时开别的吃内存的东西。")

    if blocks:
        _log.warning("[ComfyUI] 资源不足，已拒绝: " + " | ".join(b[:80] for b in blocks))
        return False, "", "\n\n".join(blocks)
    return True, ("（提醒：" + "；".join(notes) + "）") if notes else "", ""


# ══════════════════ 工作流 ══════════════════

def workflow(prompt: str, negative: str, width: int, height: int,
             steps: int, seed: int, lora: str, lora_strength: float = 1.0) -> dict:
    """照着 ONLY-NB-QQBot.json 搭的 API 格式工作流（节点号沿用原文件的）"""
    return {
        "2": {"class_type": "UNETLoader",
              "inputs": {"unet_name": DEFAULT_UNET, "weight_dtype": "default"}},
        "4": {"class_type": "RescaleCFG",
              "inputs": {"model": ["2", 0], "multiplier": 0.7}},
        "7": {"class_type": "DualCLIPLoader",
              "inputs": {"clip_name1": DEFAULT_CLIP1, "clip_name2": DEFAULT_CLIP2,
                         "type": "newbie", "device": "default"}},
        "19": {"class_type": "NewBieLoraLoader",
               "inputs": {"model": ["4", 0], "clip": ["7", 0],
                          "lora_name": lora, "strength": lora_strength,
                          "enabled": True}},
        "5": {"class_type": "CLIPTextEncode",
              "inputs": {"clip": ["19", 1], "text": prompt}},
        "6": {"class_type": "CLIPTextEncode",
              "inputs": {"clip": ["19", 1], "text": negative}},
        "8": {"class_type": "EmptyLatentImage",
              "inputs": {"width": width, "height": height, "batch_size": 1}},
        "3": {"class_type": "KSampler",
              "inputs": {"model": ["19", 0], "positive": ["5", 0],
                         "negative": ["6", 0], "latent_image": ["8", 0],
                         "seed": seed, "steps": steps, "cfg": 7.0,
                         "sampler_name": "dpmpp_2m", "scheduler": "sgm_uniform",
                         "denoise": 1.0}},
        "17": {"class_type": "VAELoader", "inputs": {"vae_name": DEFAULT_VAE}},
        "9": {"class_type": "VAEDecode",
              "inputs": {"samples": ["3", 0], "vae": ["17", 0]}},
        "10": {"class_type": "SaveImage",
               "inputs": {"images": ["9", 0], "filename_prefix": "prts"}},
    }


def output_dir() -> Path:
    """★ 1.9.1（用户 01:4x 的批注）：产物落到**桌宠私有文件夹**里。

    用户原话："保存路径到data的桌宠私有文件夹下面（因为都是桌宠产物），
    不要把图片留在项目外，我懒得清理。"
    → `data/桌宠私有data/comfyui/`（和 endfield / office / popcorn 放一起）。
    """
    d = BASE_DIR / "data" / "桌宠私有data" / "comfyui"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ══════════════════ 提交 / 轮询 / 取图 ══════════════════

def submit(prompt: str, negative: str = "", width: int = 512, height: int = 512,
           steps: int = 35, seed=None, lora: str = "") -> dict:
    """提交一次画图任务 → {"ok": bool, "prompt_id": str, "seed": int, "error": str}"""
    width = max(64, min(2048, int(width or 512)))
    height = max(64, min(2048, int(height or 512)))
    steps = max(1, min(100, int(steps or 35)))
    # ★ 1.9.1 修复（真机 2026-09-18 日志实锤）：LLM 会把 seed=-1 当"随机"
    #   传进来（-1 惯例=随机），而 ComfyUI KSampler 校验 seed≥0 →
    #   "Value -1 smaller than min of 0" 整个任务被拒。
    #   净化：非法 / 负数 / 0（LLM 常把 0 当默认值填，沿用旧行为）→ 随机。
    try:
        _s = int(seed)
    except (TypeError, ValueError):
        _s = 0
    seed = _s if _s > 0 else random.randint(0, 2 ** 31 - 1)
    lora = (lora or DEFAULT_LORA).strip()
    wf = workflow(prompt, negative or DEFAULT_NEGATIVE, width, height, steps, seed, lora)
    try:
        res = _post("/prompt", {"prompt": wf, "client_id": "prts_tool"})
    except requests.HTTPError as e:
        # ★ 1.9.1：校验失败（400）时 body 里才有真原因（哪个节点哪个参数），
        #   只甩 "400 Client Error" 用户/LLM 都看不出该改什么。
        try:
            body = (e.response.text or "")[:300]
        except Exception:
            body = ""
        return {"ok": False, "error": f"提交任务失败: {e}" + (f" | {body}" if body else "")}
    except Exception as e:
        return {"ok": False, "error": f"提交任务失败: {e}"}
    pid = res.get("prompt_id")
    if not pid:
        return {"ok": False,
                "error": f"ComfyUI 没返回 prompt_id: {json.dumps(res, ensure_ascii=False)[:300]}"}
    _log.info(f"[ComfyUI] 已提交 {pid}  {width}x{height} steps={steps} seed={seed}")
    return {"ok": True, "prompt_id": pid, "seed": seed,
            "width": width, "height": height, "steps": steps}


def poll(prompt_id: str) -> dict:
    """查一次任务状态 → {"state": "running"|"done"|"error", ...}

    只查一次就返回 —— **不要在这里 sleep 轮询**，那是同步阻塞的老写法。
    由后台 worker（comfyui_jobs）负责按间隔反复调用。
    """
    try:
        hist = _get(f"/history/{prompt_id}", timeout=20)
    except Exception as e:
        return {"state": "running", "note": f"查询失败(下次再试): {e}"}
    entry = (hist or {}).get(prompt_id)
    if entry is None:
        return {"state": "running"}
    status = entry.get("status") or {}
    if status.get("status_str") == "error" or status.get("completed") is False:
        msgs = []
        for m in (status.get("messages") or []):
            try:
                msgs.append(f"{m[0]}: {json.dumps(m[1], ensure_ascii=False)[:200]}")
            except Exception:
                pass
        return {"state": "error", "detail": "ComfyUI 报错了：\n" + "\n".join(msgs[:5])}
    try:
        paths = fetch_images(hist, prompt_id)
    except Exception as e:
        return {"state": "error", "detail": f"出图了但下载失败: {e}"}
    if not paths:
        return {"state": "error",
                "detail": "ComfyUI 跑完了但没有产出图片（工作流里是不是没有 SaveImage？）"}
    return {"state": "done", "paths": paths}


def fetch_images(history: dict, prompt_id: str) -> list:
    """把这次跑出来的图从 ComfyUI 拉下来存到桌宠私有目录"""
    saved = []
    entry = (history or {}).get(prompt_id) or {}
    for _nid, out in (entry.get("outputs") or {}).items():
        for im in (out.get("images") or []):
            fn = im.get("filename")
            if not fn:
                continue
            params = {"filename": fn, "subfolder": im.get("subfolder", ""),
                      "type": im.get("type", "output")}
            r = requests.get(base_url() + "/view", params=params, timeout=120)
            r.raise_for_status()
            dst = output_dir() / f"{int(time.time())}_{fn}"
            dst.write_bytes(r.content)
            saved.append(dst)
            _log.info(f"[ComfyUI] 出图: {dst} ({len(r.content)} 字节)")
    return saved


def fmt_size(n: int) -> str:
    n = int(n or 0)
    if n >= 1024 * 1024:
        return f"{n / (1024 * 1024):.2f} MB"
    if n >= 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n} B"


# ══════════════════ 只读查询（给工具的三个 action）══════════════════

def status_text() -> dict:
    if not alive():
        return {"ok": False,
                "text": (f"ComfyUI 没在跑（{base_url()} 连不上）。"
                         f"请让用户先双击 run_nvidia_gpu.bat 把它启动起来。")}
    v = vram()
    hm = host_memory()
    try:
        q = _get("/queue", timeout=10)
        running = len(q.get("queue_running") or [])
        pending = len(q.get("queue_pending") or [])
    except Exception:
        running = pending = -1
    top = gpu_top(5)
    top_s = "；".join(f"{n} {fmt_gb(b)}" for n, b in top) if top else "（拿不到）"
    txt = (f"ComfyUI 在线（{base_url()}）\n"
           f"显卡：{v.get('name', '?')}  总 {fmt_gb(v.get('total', 0))} / "
           f"空闲 {fmt_gb(v.get('free', 0))}\n")
    if hm:
        txt += (f"内存：总 {fmt_gb(hm['ram_total'])} / 空闲 {fmt_gb(hm['ram_avail'])}\n"
                f"★提交量：上限 {fmt_gb(hm['commit_total'])} / "
                f"**还能提交 {fmt_gb(hm['commit_free'])}**"
                f"（画一张峰值要约 14GB；这个比「内存空闲」更该盯）\n")
    txt += (f"队列：正在跑 {running}，排队 {pending}\n"
            f"显存占用前 5 名：{top_s}")
    return {"ok": True, "vram": v, "host": hm, "running": running, "pending": pending,
            "top_vram": [{"name": n, "bytes": b} for n, b in top], "text": txt}


def queue_text() -> dict:
    if not alive():
        return {"ok": False, "text": f"ComfyUI 没在跑（{base_url()} 连不上）"}
    try:
        q = _get("/queue", timeout=10)
    except Exception as e:
        return {"ok": False, "text": f"取队列失败: {e}"}
    run = q.get("queue_running") or []
    pend = q.get("queue_pending") or []
    lines = [f"正在跑 {len(run)} 个，排队 {len(pend)} 个"]
    for it in (run + pend)[:10]:
        try:
            lines.append(f"  · {it[1]}  ({it[3].get('client_id', '')[:8]})")
        except Exception:
            pass
    return {"ok": True, "running": len(run), "pending": len(pend),
            "text": "\n".join(lines)}


def models_text() -> dict:
    if not alive():
        return {"ok": False, "text": f"ComfyUI 没在跑（{base_url()} 连不上）"}
    try:
        with requests.get(base_url() + "/object_info", timeout=60) as r:
            oi = r.json()
    except Exception as e:
        return {"ok": False, "text": f"取模型列表失败: {e}"}

    def enum_of(cls, key):
        try:
            v = oi[cls]["input"]["required"][key][0]
            return v if isinstance(v, list) else []
        except Exception:
            return []

    out = {"unet": enum_of("UNETLoader", "unet_name"),
           "clip": sorted(set(enum_of("DualCLIPLoader", "clip_name1"))),
           "vae": enum_of("VAELoader", "vae_name"),
           "lora": enum_of("NewBieLoraLoader", "lora_name")}
    txt = ("可用模型：\n"
           f"  底模: {', '.join(out['unet'])}\n"
           f"  CLIP: {', '.join(out['clip'])}\n"
           f"  VAE : {', '.join(out['vae'])}\n"
           f"  LoRA: {', '.join(out['lora'])}")
    return {"ok": True, "models": out, "text": txt}
