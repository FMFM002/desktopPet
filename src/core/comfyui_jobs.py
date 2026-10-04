#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
★ 1.9.1: ComfyUI 画图的**后台任务队列**（用户要求"和下载一样，提交为后台任务"）

【用户原话（2026-09-18 01:4x 批注）】
  "画图是成功了，但是一路上全是bug和报错，直接画了好多。和下载一样，
   提交为后台任务，不影响继续聊天，画完了后，立刻发送，
   上下文插入系统提示：刚刚发送了xxxx图片。"

【为什么要单独一个模块（而不是写在 tools/comfyui_tool.py 里）】
  ToolManager 对 LAZY 工具是"空闲就卸载、下次重新 import"，模块级变量活不过一次卸载：
  工具把任务塞进模块 A，主窗口 import 到的是模块 B，永远读不到结果。
  这是 `src/core/meme_bus.py`（表情包信箱）踩过的同一个坑，见那里的长注释。
  → 队列、worker 线程、回调都放 `src/core/`：不属于任何工具，进程启动到退出都是同一个对象。

【为什么要串行（一次只跑一张）】
  · ComfyUI 画完**不卸载模型**（实测占着 5.79GB 显存 + ~13.7GB 私有提交），
    并发提交只会互相抢显存/提交额度，反而更容易触发"资源耗尽"；
  · 单张 512×512 / 20 步在本机约 18~32 秒，排队完全是可接受的体验。
  → 所以 worker 是**单线程**，任务严格先进先出。

【回调（主窗口在启动时注册，工具被卸载后由 post_load_hook 重新注册）】
  · set_file_ready_callback(cb)  cb(label, path, size)  → 发图到聊天窗口 + 落库
  · set_context_note_callback(cb) cb(text)              → 往上下文插一条提示
  ★ 回调都可能在 **worker 线程**里被调用，主窗口侧必须走 Signal 回主线程
    （样板见 main_window 里 jmcomic 的 `_jm_callback`）。
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
from pathlib import Path

_log = logging.getLogger("ComfyUIJobs")

# ── 跨模块状态（工具卸载不影响）──
_lock = threading.Lock()
_queue: "queue.Queue" = queue.Queue()
_jobs: dict = {}          # prompt_id -> 任务信息
_history: list = []       # 已完成/失败的任务（最多留 50 条，给 status 看）
_worker: threading.Thread | None = None
_file_ready_cb = None     # (label, path, size)
_context_note_cb = None   # (text)
_current: dict = {}       # 正在跑的那个

POLL_INTERVAL = 2.0       # 秒，查一次 ComfyUI 历史
MAX_HISTORY = 50
_seq = 0                  # job_id 自增序号（锁内取，保证唯一）


def _next_job_id() -> str:
    global _seq
    with _lock:
        _seq += 1
        return f"draw-{_seq}-{int(time.time() * 1000) % 100000}"


# ══════════════════ 回调注册 ══════════════════

def set_file_ready_callback(cb):
    """注册"出图了 → 发给聊天窗口"的回调（label, path, size）"""
    global _file_ready_cb
    _file_ready_cb = cb
    _log.debug(f"[ComfyUI] 出图回调已注册: {bool(cb)}")


def set_context_note_callback(cb):
    """注册"往上下文插一条系统提示"的回调（text）"""
    global _context_note_cb
    _context_note_cb = cb
    _log.debug(f"[ComfyUI] 上下文提示回调已注册: {bool(cb)}")


def _call(fn, *args):
    if not callable(fn):
        return
    try:
        fn(*args)
    except Exception as e:
        _log.warning(f"[ComfyUI] 回调执行失败: {e}")


# ══════════════════ 提交 ══════════════════

def submit_draw(prompt: str, raw: bool = False, width: int = 512, height: int = 512,
                steps: int = 35, seed=None, negative: str = "", lora: str = "",
                timeout: int = 600) -> dict:
    """把一次画图丢进后台队列 → **立刻返回**，不阻塞对话。

    返回 {"success": True, "queued": True, "job_id": ..., "position": N, "message": ...}
    用户的 LLM 会看到"已经在后台画了"，于是可以继续聊别的；
    画完之后由 worker 直接发图 + 往上下文插提示。
    """
    from src.core import comfyui_client as cc

    prompt = (prompt or "").strip()
    if not prompt:
        return {"success": False, "error": "prompt 是空的。请把你规范化重写后的 XML 提示词放进 prompt。"}

    # ★ 1.9.3 第 25 轮：入队即视为"真的用过了" → 撤销"刚启动还没用过"的宽限
    global _draw_seen_since_start
    with _lock:
        _draw_seen_since_start = True

    # ★ 1.9.1 修正：**只查一次** alive()，后面复用这个结果。
    #   原来在"检查能不能启动"和"拼返回话术"两处各查一次，
    #   两次之间状态可能变（后台 worker 正好把 ComfyUI 拉起来了），
    #   结果话术自相矛盾（自测里真踩到）。
    _alive = cc.alive()
    if not _alive:
        # ★ 1.9.1（用户批注："comfyUI的启动…都写入tool自动完成"）：
        #   不再要求用户自己去双击 bat。只要启动脚本在，就**由后台 worker 负责拉起**。
        #   ★ 这里**不能同步等启动**（要几十秒），否则又把聊天卡住了 ——
        #     所以只检查"能不能启动"，真正的启动放在 _run_one 里做。
        if not cc.launcher():
            return {"success": False,
                    "error": (f"ComfyUI 没在跑，而且我找不到它的启动脚本"
                              f"（在 {cc.comfy_root()} 下找 run_nvidia_gpu.bat）。"
                              f"请检查 .env 里的 COMFYUI_PATH。"),
                    "hint": "需要用户操作"}
        _log.info("[ComfyUI] 当前没在跑 → 交给后台 worker 先启动它")

    # ★ 提交前先查资源（显存 + 提交量）。这一步在**主线程**做，
    #   用户在对话里能立刻拿到"资源不够"的答复，而不是等后台悄悄失败。
    ok, note, detail = cc.guard()
    if not ok:
        return {"success": False, "error": detail, "resource_blocked": True}

    job = {
        # ★ job_id 必须**唯一**：第一版用 `draw-{毫秒}`，结果用户快速连提两张
        #   （或模型一轮里调两次）会撞 ID → `_jobs[job_id]` 被覆盖、
        #   队列里同 id 出现两次 → **有一张凭空消失**（自测里真丢了一张）。
        #   现在 = 进程内自增序号 + 毫秒，锁内取号，绝不可能重复。
        "job_id": _next_job_id(),
        "prompt": prompt, "raw": bool(raw),
        "width": max(64, min(2048, int(width or 512))),
        "height": max(64, min(2048, int(height or 512))),
        "steps": max(1, min(100, int(steps or 35))),
        "seed": seed, "negative": negative or "", "lora": lora or "",
        "timeout": max(30, min(1800, int(timeout or 600))),
        "state": "queued", "created": time.time(),
        "prompt_id": "", "paths": [], "error": "",
    }
    with _lock:
        _jobs[job["job_id"]] = job
        # ★ 位次 = "还在排队等着的" + 1（不是 qsize()+1）。
        #   qsize() 和 put() 之间有竞态：worker 可能刚好把上一张取走，
        #   于是两张都报"第 1 位"（真机自测踩到）。锁内数一遍才准。
        #   语义也更对：前面那张已经在画了，你就是队首。
        pos = 1 + sum(1 for j in _jobs.values()
                      if j.get("state") == "queued" and j is not job)
    _queue.put(job["job_id"])
    _ensure_worker()
    _log.info(f"[ComfyUI] 任务已入队 {job['job_id']}（第 {pos} 位）"
              f" {job['width']}x{job['height']} steps={job['steps']}")

    msg = (f"已经在后台开始画了（第 {pos} 位，"
           f"{job['width']}×{job['height']} / {job['steps']} 步）。"
           f"你可以继续聊别的，画好我会立刻把图发给你。")
    if not _alive:
        # 告诉模型/用户会发生什么，免得它以为卡住了又重复调用
        msg = ("ComfyUI 当前没在跑，我已经在后台把它启动起来并排队画图了"
               "（第一次启动会慢一点，可能要一两分钟）。"
               "你可以继续聊别的，画好我会立刻把图发给你。")
    if note:
        msg += "\n" + note
    return {"success": True, "action": "draw", "queued": True,
            "job_id": job["job_id"], "position": pos,
            "width": job["width"], "height": job["height"], "steps": job["steps"],
            "text": msg, "message": msg}


def _ensure_worker():
    global _worker
    with _lock:
        if _worker is not None and _worker.is_alive():
            return
        _worker = threading.Thread(target=_run_loop, daemon=True,
                                   name="comfyui-draw-worker")
        _worker.start()
        _log.info("[ComfyUI] 后台画图 worker 已启动（单线程串行）")


# ══════════════════ ★ 空闲自动释放（E2）══════════════════
# 背景（2026-09-18 那次"浏览器被关掉"的事故）：ComfyUI 画完**不卸载模型**，
# 实测会一直占着 ~13.7GB 的**私有提交**（虚拟内存），而本机物理内存只有 15.73GB、
# 提交上限 33GB。画完图之后用户随便再开点别的就容易撞上限，
# Windows 就会关掉最大的前台程序（那次关的就是浏览器）。
#
# ★ 1.9.1 改版（用户 12:57 的批注）：
#   我第一版自己做了一个"画完空闲 5 分钟就 /free"的计时器。用户否了：
#     "走其他tool一样的卸载规则。comfyUI和模型同加载同卸载，不用分开。"
#   → **那套计时器整个删掉了**，改成挂在 ToolManager 的**卸载后钩子**上
#     （`on_tool_unloaded`）：LAZY 工具空闲卸载（IDLE_UNLOAD_TIMEOUT = 30 秒）
#     时，把 ComfyUI 一起关掉。ComfyUI 一关，模型和提交额度自然全还回去 ——
#     "同加载同卸载"，而且**只有一套生命周期规则**，不再多一个要记的时间常数。
#     （首次要用时由 `cc.ensure_running()` 自己把 ComfyUI 拉起来。）


# ══════════════════ worker ══════════════════

def _run_loop():
    while True:
        try:
            job_id = _queue.get()
        except Exception:
            continue
        try:
            _run_one(job_id)
        except Exception as e:
            _log.exception(f"[ComfyUI] 任务 {job_id} 异常: {e}")
            _finish(job_id, ok=False, error=f"后台任务异常: {e}")
        finally:
            try:
                _queue.task_done()
            except Exception:
                pass


def _run_one(job_id: str):
    from src.core import comfyui_client as cc

    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return
        job["state"] = "running"
        _current.clear()
        _current.update({"job_id": job_id, "started": time.time()})

    _log.info(f"[ComfyUI] 开始画 {job_id}")
    t0 = time.time()

    # ★ 1.9.1（用户："comfyUI的启动…都写入tool自动完成"）：
    #   先确保 ComfyUI 在跑 —— 没跑就由这里把它拉起来。
    #   放在 worker 里而不是工具调用里，是因为启动要几十秒，
    #   放前面会把聊天卡住（那就又回到"同步阻塞"的老毛病了）。
    if not cc.alive():
        with _lock:
            _current["phase"] = "starting"
        _log.info("[ComfyUI] 没在跑 → 后台启动中…")
        st = cc.ensure_running(wait_seconds=180)
        if not st.get("ok"):
            _finish(job_id, ok=False,
                    error=f"ComfyUI 启动失败：{st.get('text', '')}")
            return
        with _lock:
            _current["phase"] = "submitting"
        _log.info(f"[ComfyUI] 已就绪，共花 {time.time() - t0:.0f} 秒（含启动）")

    res = cc.submit(job["prompt"], negative=job["negative"],
                    width=job["width"], height=job["height"],
                    steps=job["steps"], seed=job["seed"], lora=job["lora"])
    if not res.get("ok"):
        _finish(job_id, ok=False, error=res.get("error", "提交失败"))
        return

    pid = res["prompt_id"]
    with _lock:
        job["prompt_id"] = pid
        job["seed"] = res.get("seed")
        _current["prompt_id"] = pid

    deadline = time.time() + job["timeout"]
    while time.time() < deadline:
        time.sleep(POLL_INTERVAL)
        st = cc.poll(pid)
        if st.get("state") == "running":
            continue
        if st.get("state") == "error":
            _finish(job_id, ok=False, error=st.get("detail", "出图失败"))
            return
        if st.get("state") == "done":
            _finish(job_id, ok=True, paths=st.get("paths") or [],
                    elapsed=time.time() - t0)
            return
    _finish(job_id, ok=False,
            error=f"等图超时（{job['timeout']} 秒还没画完）。可能模型太大、或队列前面还有任务。")


def _finish(job_id: str, ok: bool, paths: list = None, error: str = "",
            elapsed: float = 0.0):
    """任务收尾：发图给聊天窗口 + 往上下文插一句系统提示"""
    from src.core import comfyui_client as cc

    with _lock:
        job = _jobs.get(job_id) or {}
        job["state"] = "done" if ok else "failed"
        job["paths"] = [str(p) for p in (paths or [])]
        job["error"] = error or ""
        job["elapsed"] = round(elapsed, 1)
        job["finished"] = time.time()
        _history.append(dict(job))
        del _history[:-MAX_HISTORY]
        # ★ 从活跃表里摘掉（历史里有记录），否则 _jobs 会随使用无限增长
        _jobs.pop(job_id, None)
        _current.clear()

    if not ok:
        _log.warning(f"[ComfyUI] 任务 {job_id} 失败: {error[:200]}")
        _call(_context_note_cb, f"（刚才让你画的那张图没画出来：{error.splitlines()[0][:120]}）")
        return

    files = [Path(p) for p in job["paths"]]
    if not files:
        _call(_context_note_cb, "（刚才让你画的那张图没产出文件）")
        return

    f = files[0]
    try:
        size = f.stat().st_size
    except Exception:
        size = 0
    label = (f"🎨 画好了 · {f.name}  "
             f"({job.get('width')}×{job.get('height')}, {job.get('steps')}步"
             + (f", {elapsed:.0f}s" if elapsed else "") + ")")
    _log.info(f"[ComfyUI] 任务 {job_id} 完成 → {f}")

    # ① 发到聊天窗口（主窗口侧负责显示 + 落库）
    _call(_file_ready_cb, label, str(f), cc.fmt_size(size))

    # ② ★ 往**上下文**插一条系统提示（用户明确要求的那句）
    #    说明：这里用 assistant 口吻写，因为它是"你（桌宠）刚做了这件事"的记录，
    #    和 1.9.0 附件消息（`_persist_attachment`）走的是同一套语义。
    _call(_context_note_cb, f"刚刚发送了 {f.name} 图片（{label}）")

    # ★ 1.9.1: 这里原来会挂"空闲 5 分钟自动释放"的计时器；按用户要求改成
    #   走 ToolManager 的卸载钩子（见 on_tool_unloaded），所以不再自己排表。


# ══════════════════ 查询 ══════════════════

def queue_status() -> dict:
    """给工具的 status action 用：队列里有几个、正在画哪个"""
    with _lock:
        pending = [j for j in _jobs.values() if j.get("state") == "queued"]
        running = dict(_current) if _current else {}
    return {"queued": len(pending), "running": running,
            "queued_jobs": [{"job_id": j["job_id"], "prompt": j["prompt"][:60],
                             "size": f"{j['width']}x{j['height']}",
                             "steps": j["steps"]} for j in pending[:10]]}


def recent(n: int = 5) -> list:
    with _lock:
        return [{"job_id": j.get("job_id"), "state": j.get("state"),
                 "paths": j.get("paths") or [], "error": (j.get("error") or "")[:200],
                 "elapsed": j.get("elapsed")} for j in _history[-n:]]


def wait_all(timeout: float = 600.0) -> bool:
    """等队列跑空（给自测/收尾用，生产代码不用调）

    ★ 别用 `_queue.empty() and not _current` 判断 —— worker 在
      `_queue.get()` 和 `_current.update()` 之间有一个极短的窗口，
      这时两边看着都是空的，会**提前返回 True**（自测里真踩到了：
      两张图只跑了一张就报"跑空"）。改成看任务状态表，锁内一次性判断。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        with _lock:
            busy = any(j.get("state") in ("queued", "running") for j in _jobs.values())
        if not busy:
            return True
        time.sleep(0.1)
    return False


# ══════════════════ ★ 1.9.1: 工具卸载 → 收掉 ComfyUI（用户要求）══════════════════
# 用户原话：
#   "comfyUI的启动，加载模型，卸载模型，关闭comfyUI都写入tool自动完成。"
#   "走其他tool一样的卸载规则。comfyUI和模型同加载同卸载，不用分开。"
# → 所以**不再自己搞一套空闲计时器**（原来那个 5 分钟的 _arm_idle_free 已废弃），
#   改成挂在 ToolManager 的**卸载后钩子**上：
#   工具空闲 30 秒被卸载（IDLE_UNLOAD_TIMEOUT）→ 这里把 ComfyUI 也关掉。
#   ComfyUI 一关，模型和那一大坨提交额度自然全还回去了 —— 同加载同卸载。

# ★★ 1.9.3 第 25 轮：**"刚启动就被卸载关掉"的护栏**。
#   真机现场（logs/prts_20261004_025634.log）：
#     03:03:39 模型调 `comfyui start` → 03:04:09 就绪（耗时 30 秒）
#     03:04:12 工具因空闲被卸载 → `on_tool_unloaded` **把刚拉起来的 ComfyUI 关了**
#     之后模型以为 ComfyUI 还开着，转去下载素材，**一张图都没画成**
#   两个原因：
#     ① 那次调用耗时正好吃满空闲阈值（已在 tool_manager 里修：调用结束也重新盖章）；
#     ② 就算盖对了章，模型**紧接着去干别的**，30 秒一到照样关 —— 而"启动"本身
#        是没有产出的动作，一关等于白干。
#   所以补一条：**显式 `start` 起、且此后一张图都还没画过**时，给一段宽限期不关。
_explicit_start_ts: float = 0.0     # 最近一次显式 start 的时间
_draw_seen_since_start: bool = False
EXPLICIT_START_GRACE = 600.0        # 10 分钟：够模型回头来画，又不会无限占着显存


def note_explicit_start():
    """`comfyui` 工具的 `action=start` 成功后调这里（说明"是特意拉起来的"）。"""
    global _explicit_start_ts, _draw_seen_since_start
    with _lock:
        _explicit_start_ts = time.time()
        _draw_seen_since_start = False


def on_tool_unloaded():
    """LAZY 工具被空闲卸载时调用（主窗口通过 register_post_unload_hook 注册）。

    做的事：① 取消内部空闲释放计时器（不再需要，统一走这里）
            ② 让 ComfyUI 释放模型 ③ 关掉 ComfyUI 进程
    安全前提：**队列必须已经跑空**。如果还有画图任务在跑/排队，
    就跳过这次卸载（等它们做完，下一次空闲超时再来收），
    绝不能把正在画图的 ComfyUI 关掉。
    """
    from src.core import comfyui_client as cc

    with _lock:
        busy = any(j.get("state") in ("queued", "running") for j in _jobs.values())
    if busy:
        _log.info("[ComfyUI] 工具被卸载，但后台还有画图任务 → 这次不关，等它画完")
        return
    # ★ 第 25 轮：刚被显式启动、还没画过任何图 → 别关，给模型留出调用 draw 的时间
    #   （否则"start 成功"这个动作本身永远拿不到产出，等于白拉一次 30 秒）
    global _explicit_start_ts, _draw_seen_since_start
    with _lock:
        _just_started = (bool(_explicit_start_ts) and not _draw_seen_since_start
                         and (time.time() - _explicit_start_ts) < EXPLICIT_START_GRACE)
    if _just_started:
        _log.info(f"[ComfyUI] 刚被显式启动（{int(time.time() - _explicit_start_ts)} 秒前）、"
                  f"还没画过图 → 这次卸载先不关，等模型来画")
        return
    if not cc.alive():
        _log.debug("[ComfyUI] 工具被卸载，ComfyUI 本来就没跑，无需处理")
        return
    _log.info("[ComfyUI] 工具空闲卸载 → 释放模型并关闭 ComfyUI（同加载同卸载）")
    try:
        cc.free_models()
    except Exception as e:
        _log.debug(f"[ComfyUI] 卸载时释放模型失败: {e}")
    try:
        cc.stop()
    except Exception as e:
        _log.warning(f"[ComfyUI] 卸载时关闭失败: {e}")
    with _lock:
        _explicit_start_ts = 0.0        # 关掉了就清账


