#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
★ 1.9.1：ComfyUI 画图 —— 工具 / 引擎 / **后台任务队列** 的回归

分三层：
  · **结构层**（默认就跑）—— 工具元数据、精简提示词规范、工作流图、显存与提交量判定
  · **队列层**（默认就跑）—— 把 ComfyUI 打桩，验证"提交立刻返回 / 后台真的跑 /
    出图回调 / 上下文提示 / 失败也要有交代 / 串行不并发"
  · **真机层**（`PRTS_TEST_COMFYUI=1` 才跑）—— 真连 8188 出一张 512×512

为什么要打桩测队列：ComfyUI 不一定开着，而"draw 是不是真的秒回、回调是不是真的发出去了"
是这次改动的**核心行为**，不能只靠"结构存在"糊过去。
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

PASS = FAIL = 0


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}   {detail}")


print("== ★1.9.1 ComfyUI 画图（工具 / 引擎 / 后台队列）==")
from src.core import comfyui_client as C  # noqa: E402
from src.core import comfyui_jobs as J    # noqa: E402
import tools.comfyui_tool as T            # noqa: E402

# ══════════════ 1. 元数据（ToolManager 靠这三个模块级常量扫）══════════════
print("\n-- 1. 工具元数据 --")
check("★TOOL_NAME 是 comfyui", T.TOOL_NAME == "comfyui")
_p = T.TOOL_PARAMETERS
check("★参数 schema 非空（不是 PARAMETERS/TOOL_PARAMETERS 那个坑）",
      bool(_p.get("properties")), str(list(_p.keys())))
check("★required 只有 action", _p.get("required") == ["action"], str(_p.get("required")))
check("★七个 action 都在 enum 里（含 free / start / stop）",
      set(_p["properties"]["action"]["enum"])
      == {"draw", "status", "queue", "models", "free", "start", "stop"},
      str(_p["properties"]["action"]["enum"]))
check("★描述里说明了 ComfyUI 的启动/关闭也归工具管（不用用户动手）",
      "start" in T.TOOL_DESCRIPTION and "stop" in T.TOOL_DESCRIPTION)
check("★draw 必填参数 prompt 有描述", "prompt" in _p["properties"])
check("★raw 开关在（用户说『原样传』时用）", "raw" in _p["properties"])

# ══════════════ 2. 精简版提示词规范（用户："xml标准.txt 要简化"）══════════════
print("\n-- 2. 精简提示词规范（token 闸门）--")
_spec = T._XML_SPEC
check("★★精简版规范不超过 1400 字符（原文 8220，压了 6 倍多）",
      len(_spec) <= 1400, f"现在 {len(_spec)} 字符")
check("★★整个 tool 描述不超过 1800 字符（每次请求都进 API，是真花钱的）",
      len(T.TOOL_DESCRIPTION) <= 1800, f"现在 {len(T.TOOL_DESCRIPTION)} 字符")
for _k in ("<character_1>", "<appearance>", "<clothing>", "<expression>",
           "<pose>", "general_tags", "<composition>", "<style>", "<background>", "<quality>"):
    check(f"★规范里有 {_k}", _k in _spec)
check("★规范里写了 solo 比 1girl 更能保证单人", "solo" in _spec and "only_one_person" in _spec)
check("★规范里写了 appearance 别超过 15 个标签", "15" in _spec)
check("★规范里写了『用户说原样传就 raw=true』", "raw=true" in _spec)
# ★★ 这次改动的关键说法：必须告诉模型 draw 是后台任务、别等别重复调
check("★★描述里写明了 draw 是**后台任务、立刻返回**（否则模型还会傻等）",
      "后台任务" in T.TOOL_DESCRIPTION and "立刻返回" in T.TOOL_DESCRIPTION)
check("★★描述里明确叫模型**不要说『稍等』、不要重复调用等结果**",
      "重复调用" in T.TOOL_DESCRIPTION and "不要" in T.TOOL_DESCRIPTION)
check("★★描述里说明了画完会自动发图 + 告诉它『刚刚发送了 xxx 图片』",
      "刚刚发送了" in T.TOOL_DESCRIPTION)

# ══════════════ 3. 工作流图（照 ONLY-NB-QQBot.json）══════════════
print("\n-- 3. 工作流 --")
wf = C.workflow("P", "N", 512, 512, 20, 42, "NBfym.safetensors")
check("★API 格式（class_type + inputs）",
      all("class_type" in v and "inputs" in v for v in wf.values()))
check("★节点齐：UNETLoader→RescaleCFG→NewBieLoraLoader→KSampler→VAEDecode→SaveImage",
      {wf["2"]["class_type"], wf["4"]["class_type"], wf["19"]["class_type"],
       wf["3"]["class_type"], wf["9"]["class_type"], wf["10"]["class_type"]}
      == {"UNETLoader", "RescaleCFG", "NewBieLoraLoader", "KSampler",
          "VAEDecode", "SaveImage"})
check("★双 CLIP：gemma3-4b-it + jina-clip-v2，type=newbie",
      wf["7"]["inputs"]["clip_name1"] == "gemma3-4b-it.safetensors"
      and wf["7"]["inputs"]["clip_name2"] == "jina-clip-v2.safetensors"
      and wf["7"]["inputs"]["type"] == "newbie")
check("★采样参数照参考工作流：dpmpp_2m / sgm_uniform / cfg7 / denoise1",
      wf["3"]["inputs"]["sampler_name"] == "dpmpp_2m"
      and wf["3"]["inputs"]["scheduler"] == "sgm_uniform"
      and wf["3"]["inputs"]["cfg"] == 7.0 and wf["3"]["inputs"]["denoise"] == 1.0)
check("★RescaleCFG=0.7（NewBie 必须的）", wf["4"]["inputs"]["multiplier"] == 0.7)
check("★参数真的传进去了", wf["8"]["inputs"]["width"] == 512
      and wf["8"]["inputs"]["height"] == 512
      and wf["3"]["inputs"]["steps"] == 20 and wf["3"]["inputs"]["seed"] == 42)
check("★prompt 进正向、negative 进负向",
      wf["5"]["inputs"]["text"] == "P" and wf["6"]["inputs"]["text"] == "N")
_bad = [f"{nid}.{k}→{v[0]}" for nid, node in wf.items() for k, v in node["inputs"].items()
        if isinstance(v, list) and len(v) == 2 and isinstance(v[0], str) and v[0] not in wf]
check("★所有连线都指向存在的节点（没悬空引用）", _bad == [], str(_bad))

# ══════════════ 4. 产物目录（用户："放桌宠私有文件夹"）══════════════
print("\n-- 4. 产物目录 --")
_outd = C.output_dir()
check("★★出图目录在 data/桌宠私有data/comfyui 下",
      "桌宠私有data" in str(_outd) and _outd.name == "comfyui", str(_outd))
check("★不再用 data/downloads/comfyui", "downloads" not in str(_outd).replace("\\", "/"))
check("★目录会被自动创建", _outd.exists() and _outd.is_dir())

# ══════════════ 5. 显存 + 提交量判定（打桩，不看真显卡）══════════════
print("\n-- 5. 资源判定（显存 + 提交量）--")
_o_vram, _o_top, _o_root, _o_hm = C.vram, C.gpu_top, C.comfy_root, C.host_memory
GB = 1024 ** 3
_REAL_ROOT = str(C.comfy_root() or "")
# ★ 桩必须保留**真路径**：required_vram_bytes 要靠 comfy_root() 找模型文件，
#   桩成空路径会让它 resident=0 → 判定直接放行，测出来的都是假 PASS（踩过）
_RESIDENT = C.required_vram_bytes()["resident"]


def _stub(vram, top, hm, root=None):
    C.vram = lambda: vram
    C.gpu_top = lambda n=15: list(top)
    C.host_memory = lambda: hm
    C.comfy_root = lambda: Path(_REAL_ROOT if root is None else root)


_ENOUGH = {"ram_total": 16 * GB, "ram_avail": 8 * GB,
           "commit_total": 33 * GB, "commit_free": 20 * GB}
try:
    check("★常驻量只算 UNET+LoRA（文本编码器过路，不算）",
          0 < _RESIDENT < 7 * GB, f"resident={_RESIDENT / GB:.2f}GB")

    # (a) 都充足 → 放行、不啰嗦
    _stub({"name": "F", "total": 8 * GB, "free": int(7 * GB)},
          [("other.exe", int(0.5 * GB))], _ENOUGH)
    check("★资源充足 → 放行、不啰嗦", C.guard() == (True, "", ""))

    # (b) ★ 真机回归：ComfyUI 自己占着显存（画完不卸载），free 很小但那是它自己的
    _stub({"name": "F", "total": 8 * GB, "free": int(0.97 * GB)},
          [(_REAL_ROOT + r"\python_embeded\python.exe", int(5.79 * GB)),
           ("msedge.exe", int(0.1 * GB))], _ENOUGH)
    check("★★ComfyUI 自己占着显存时**不能**判显存不足（否则第二张就画不出来）",
          C.guard() == (True, "", ""))

    # (c) 显存够、但**提交量不够** → ★ 这正是 2026-09-18 那次事故的形态
    _stub({"name": "F", "total": 8 * GB, "free": int(7 * GB)},
          [("other.exe", int(0.3 * GB))],
          {"ram_total": 16 * GB, "ram_avail": int(1.5 * GB),
           "commit_total": 33 * GB, "commit_free": int(3 * GB)})
    ok, note, detail = C.guard()
    check("★★提交量不够也要拒绝（事故就是这个形态：显存够、提交爆）",
          ok is False and "提交" in detail, detail[:80])
    check("★拒绝时说明了要多少、还剩多少、让人关程序",
          "14GB" in detail and "关掉" in detail)

    # (d) 提交量偏紧但能画 → 放行 + 提醒
    _stub({"name": "F", "total": 8 * GB, "free": int(7 * GB)},
          [("other.exe", int(0.3 * GB))],
          {"ram_total": 16 * GB, "ram_avail": int(3 * GB),
           "commit_total": 33 * GB, "commit_free": int(8 * GB)})
    ok, note, detail = C.guard()
    check("★提交量偏紧 → 放行但提醒（不拦）",
          ok is True and "提交量偏紧" in note and detail == "", f"note={note[:40]!r}")

    # (e) 别人把显存吃光 → 拒绝 + 列前 5 名
    _stub({"name": "F", "total": 8 * GB, "free": int(1.0 * GB)},
          [("game.exe", int(4.5 * GB)), ("chrome.exe", int(1.2 * GB))], _ENOUGH)
    ok, note, detail = C.guard()
    check("★别的程序吃光显存 → 拒绝并列前几名",
          ok is False and "game.exe" in detail and "chrome.exe" in detail)

    # (f) 拿不到信息 → 放行（不误伤）
    _stub({}, [], {})
    check("★拿不到显存/内存信息时放行（不误伤）", C.guard() == (True, "", ""))

    # (g) ★ 没配 COMFYUI_PATH 不能把别人的显存算成自己的（"." 能匹配任何带点的进程名）
    C.comfy_root = lambda: Path("")
    own, others = C._split_gpu_usage([("game.exe", 5 * GB), ("x\\y.exe", GB)])
    check("★★没配 COMFYUI_PATH 时不会误把别人的显存算成自己的",
          own == 0 and len(others) == 2, f"own={own}")
    C.comfy_root = lambda: Path(_REAL_ROOT)
    own, others = C._split_gpu_usage([(_REAL_ROOT + r"\python.exe", 5 * GB), ("game.exe", GB)])
    check("★配了 COMFYUI_PATH 时能认出 ComfyUI 自己那一份", own == 5 * GB and len(others) == 1)
finally:
    C.vram, C.gpu_top, C.comfy_root, C.host_memory = _o_vram, _o_top, _o_root, _o_hm

# ══════════════ 6. ★★ 后台队列（打桩 ComfyUI，但 worker 是真线程）══════════════
print("\n-- 6. 后台队列（核心行为）--")
_o = dict(alive=C.alive, guard=C.guard, submit=C.submit, poll=C.poll,
          interval=J.POLL_INTERVAL)
_files, _notes, _submitted, _polls = [], [], [], {}
_FAKE = C.output_dir() / "_test_fake_draw.png"
_FAKE.write_bytes(b"\x89PNG" + b"0" * 2048)


def _fake_submit(prompt, negative="", width=512, height=512, steps=35,
                 seed=None, lora=""):
    _submitted.append({"prompt": prompt, "w": width, "h": height, "steps": steps})
    return {"ok": True, "prompt_id": f"pid{len(_submitted)}", "seed": 7,
            "width": width, "height": height, "steps": steps}


def _fake_poll(pid):
    n = _polls.get(pid, 0)
    _polls[pid] = n + 1
    if n < 1:
        return {"state": "running"}     # 第一次说还在跑
    return {"state": "done", "paths": [_FAKE]}


try:
    C.alive = lambda: True
    C.guard = lambda check_commit=True: (True, "", "")
    C.submit = _fake_submit
    C.poll = _fake_poll
    J.POLL_INTERVAL = 0.05
    J.set_file_ready_callback(lambda l, p, s: _files.append((l, p, s)))
    J.set_context_note_callback(lambda t: _notes.append(t))

    t0 = time.time()
    r1 = J.submit_draw("PROMPT-A", width=512, height=512, steps=8)
    _dt = time.time() - t0
    check("★★draw 立刻返回（不阻塞对话）", _dt < 1.0 and r1.get("success") and r1.get("queued"),
          f"耗时 {_dt:.2f}s / {str(r1)[:80]}")
    check("★返回里告诉用户/模型『在后台画了、可以继续聊』",
          "后台" in r1.get("message", "") and "继续聊" in r1.get("message", ""),
          r1.get("message", "")[:80])
    check("★返回带 job_id 和排队位次",
          bool(r1.get("job_id")) and r1.get("position") == 1)

    r2 = J.submit_draw("PROMPT-B", width=768, height=768, steps=12)
    check("★第二张也秒回（队列里已经有人了也不阻塞）", r2.get("success") is True,
          str(r2)[:80])

    ok = J.wait_all(timeout=20)
    check("★队列被 worker 跑空", ok, str(J.queue_status()))
    check("★★两张都被真的提交给了 ComfyUI（worker 在干活）",
          len(_submitted) == 2, str(_submitted))
    check("★参数原样透传（尺寸/步数没被吞）",
          _submitted[0]["w"] == 512 and _submitted[0]["steps"] == 8
          and _submitted[1]["w"] == 768 and _submitted[1]["steps"] == 12)
    check("★串行：每张各自都轮询过（不是并发提交）", len(_polls) == 2, str(_polls))

    check("★★出图后**发图回调**被调用（主窗口据此发到聊天窗口）",
          len(_files) == 2 and "🎨 画好了" in _files[0][0]
          and _files[0][1].endswith("_test_fake_draw.png"), str(_files[:1]))
    check("★回调带上了文件大小字符串", len(_files[0][2]) > 0, str(_files[0][2]))
    check("★★出图后**上下文提示**被调用，且就是用户要的那句话",
          len(_notes) == 2 and "刚刚发送了" in _notes[0]
          and "_test_fake_draw.png" in _notes[0], str(_notes[:1]))

    # ── 6b. ★ 排队位次（必须确定性，不能靠 sleep 赌）──
    #   做法：让 worker **明确卡在 submit 里**（gate 不放行），
    #   于是它状态是 running、后面进来的都老老实实排队。
    #   第一版用 `qsize()+1`：worker 一取走，两张都报"第 1 位"（竞态，已修代码）；
    #   第二版拿 poll 卡 worker：poll 是在 submit 之后才换的，worker 可能已经在旧 poll 里 → 还是赌。
    import threading as _th
    _notes.clear(); _files.clear(); _submitted.clear(); _polls.clear()
    with J._lock:                      # 清掉上一阶段的状态（测试里可以碰私有）
        J._jobs.clear()
        J._history.clear()
    _gate = _th.Event()

    def _gated_submit(prompt, negative="", width=512, height=512, steps=35,
                      seed=None, lora=""):
        _submitted.append({"prompt": prompt, "w": width, "h": height, "steps": steps})
        _gate.wait(8)                  # ← worker 卡在这里
        return {"ok": True, "prompt_id": f"pid{len(_submitted)}", "seed": 7,
                "width": width, "height": height, "steps": steps}

    C.submit = _gated_submit
    rX = J.submit_draw("POS-X", steps=8)
    for _ in range(200):               # 等 worker 真的开始跑 X
        if J.queue_status()["running"]:
            break
        time.sleep(0.01)
    check("（前置）worker 已开始跑第一张", bool(J.queue_status()["running"]))
    rY = J.submit_draw("POS-Y", steps=8)
    rZ = J.submit_draw("POS-Z", steps=8)
    check("★排队位次按『前面还有几张在等』算（依次 1、2）",
          rY.get("position") == 1 and rZ.get("position") == 2,
          f"x={rX.get('position')} y={rY.get('position')} z={rZ.get('position')}")
    _gate.set()
    check("★放行后队列能跑空", J.wait_all(timeout=20), str(J.queue_status()))
    check("★三张都跑到了（串行执行没漏）", len(_submitted) == 3, str(_submitted))

    # ── 6c. ★ 工具空闲卸载 → 关掉 ComfyUI（用户 12:57 的批注）──
    #   用户原话："走其他tool一样的卸载规则。comfyUI和模型同加载同卸载，不用分开。"
    #   第一版我自己搞了个"画完空闲 5 分钟 /free"的计时器，被否了 ——
    #   现在挂在 ToolManager 的**卸载后钩子**上，只有一套生命周期规则。
    check("★★自己那套'空闲 5 分钟'计时器已经删干净（不留两套规则）",
          not hasattr(J, "IDLE_FREE_MINUTES") and not hasattr(J, "_arm_idle_free")
          and not hasattr(J, "_idle_timer"))
    from src.core.tool_manager import get_tool_manager as _gtm_t  # noqa: E402
    check("★ToolManager 提供了 register_post_unload_hook（与 post_load 对称）",
          hasattr(_gtm_t(), "register_post_unload_hook")
          and hasattr(_gtm_t(), "_run_post_unload_hooks"))

    _life = []
    _o_free, _o_stop, _o_alive = C.free_models, C.stop, C.alive
    C.alive = lambda: True
    C.free_models = lambda: (_life.append("free"), True)[1]
    C.stop = lambda **k: (_life.append("stop"), {"ok": True, "killed": 1, "text": "x"})[1]
    C.submit = _fake_submit
    try:
        # (a) 队列空闲 → 释放 + 关闭
        J.on_tool_unloaded()
        for _ in range(60):
            if "stop" in _life:
                break
            time.sleep(0.05)
        check("★★工具卸载且队列空闲 → 既释放模型又关掉 ComfyUI",
              _life == ["free", "stop"], str(_life))

        # (b) ★ 队列里还有任务 → 绝不关（不能把正在画图的 ComfyUI 杀掉）
        _life.clear()
        _gate2 = __import__("threading").Event()

        def _slow_submit(prompt, negative="", width=512, height=512, steps=35,
                         seed=None, lora=""):
            _gate2.wait(8)
            return {"ok": True, "prompt_id": "busy-pid", "seed": 1,
                    "width": width, "height": height, "steps": steps}

        C.submit = _slow_submit
        J.submit_draw("BUSY-KEEP", steps=8)
        for _ in range(200):
            if J.queue_status()["running"]:
                break
            time.sleep(0.01)
        J.on_tool_unloaded()
        time.sleep(0.5)
        check("★★还有画图任务在跑时 → 卸载钩子**不动** ComfyUI",
              _life == [], str(_life))
        _gate2.set()
        J.wait_all(timeout=20)
    finally:
        C.free_models, C.stop, C.alive = _o_free, _o_stop, _o_alive

    # ── 6d. ★ "启动也归工具管"（用户："comfyUI的启动…都写入tool自动完成"）──
    _o_alive2, _o_ensure, _o_launcher = C.alive, C.ensure_running, C.launcher
    _started = []
    try:
        C.alive = lambda: False                      # 假装 ComfyUI 没开
        _launcher_path = C.launcher()                # 拿真实启动脚本路径
        C.launcher = lambda: _launcher_path
        J._jobs.clear(); J._history.clear()

        def _ensure(wait_seconds=120):
            _started.append(wait_seconds)
            C.alive = lambda: True                   # 启动完就"活了"
            return {"ok": True, "already": False, "text": "已启动"}
        C.ensure_running = _ensure
        C.guard = lambda check_commit=True: (True, "", "")
        C.submit = _fake_submit
        C.poll = _fake_poll
        r = J.submit_draw("AUTOSTART", steps=8)
        check("★ComfyUI 没在跑时，draw **不再拒绝**（不再叫用户去双击 bat）",
              r.get("success") is True, str(r)[:100])
        check("★返回话术说明了'会先把它启动起来、第一次慢一点'",
              "启动" in r.get("message", ""), r.get("message", "")[:90])
        J.wait_all(timeout=20)
        check("★★worker 真的去启动了 ComfyUI（ensure_running 被调用）",
              len(_started) == 1, str(_started))

        # 启动脚本都找不到 → 才该拒绝
        C.alive = lambda: False
        C.launcher = lambda: None          # ★ 必须返回 None；Path("") 是 Path(".")
        rr = J.submit_draw("NOSCRIPT", steps=8)
        check("★连启动脚本都找不到时才拒绝，并指向 COMFYUI_PATH",
              rr.get("success") is False and "COMFYUI_PATH" in rr.get("error", ""),
              str(rr)[:100])
        check("★★launcher() 找不到时返回 None（返回 Path() 会因为 Path('.') 存在而误判）",
              "return None" in (ROOT / "src" / "core" / "comfyui_client.py")
              .read_text(encoding="utf-8").split("def launcher()")[1].split("def ")[0])
    finally:
        C.alive, C.ensure_running, C.launcher = _o_alive2, _o_ensure, _o_launcher
        C.submit = _fake_submit
        C.poll = _fake_poll
        C.guard = lambda check_commit=True: (True, "", "")

    # ── 6e. ★ /free 返回空 body 不能算失败（用户真机报的误报）──
    _o_post2 = C._post
    _seen_empty = []
    try:
        def _fake_post(path, payload, timeout=30.0, allow_empty=False):
            _seen_empty.append((path, allow_empty))
            return {}
        C._post = _fake_post
        check("★★free_models 走 allow_empty（/free 是 200+空 body，不能解析 JSON）",
              C.free_models() is True
              and _seen_empty and _seen_empty[-1][0] == "/free"
              and _seen_empty[-1][1] is True, str(_seen_empty))
    finally:
        C._post = _o_post2

    # 失败路径
    _notes.clear(); _files.clear(); _submitted.clear()
    C.poll = lambda pid: {"state": "error", "detail": "ComfyUI 报错了：模拟 OOM"}
    J.submit_draw("PROMPT-FAIL", steps=8)
    J.wait_all(timeout=20)
    check("★失败时不发图", _files == [])
    check("★失败也要往上下文交代一句（免得模型以为还在画）",
          len(_notes) == 1 and "没画出来" in _notes[0], str(_notes[:1]))

    # 空 prompt / ComfyUI 没开
    check("★空 prompt 直接拒绝", J.submit_draw("").get("success") is False)
    C.alive = lambda: False
    C.launcher = lambda: _launcher_path
    check("★ComfyUI 没开但启动脚本在 → **排队并在后台启动**（不再叫用户去双击 bat）",
          J.submit_draw("WILL-AUTOSTART").get("success") is True)
    C.alive = lambda: True
    J.wait_all(timeout=30)

    # 资源不够时提交就被拦（不排进队列）
    C.guard = lambda check_commit=True: (False, "", "显存不足：模拟")
    _before = J.queue_status()["queued"]
    rr = J.submit_draw("PROMPT-NOVRAM")
    check("★★资源不足时**在提交处**就拒绝（不让它排进队列悄悄失败）",
          rr.get("success") is False and rr.get("resource_blocked") is True
          and J.queue_status()["queued"] == _before, str(rr)[:90])
finally:
    C.alive, C.guard, C.submit, C.poll = _o["alive"], _o["guard"], _o["submit"], _o["poll"]
    J.POLL_INTERVAL = _o["interval"]
    J.set_file_ready_callback(None)
    J.set_context_note_callback(None)
    try:
        _FAKE.unlink()
    except Exception:
        pass

# ══════════════ 7. 错误分支 / ToolManager 扫描 ══════════════
print("\n-- 7. 错误分支 + ToolManager --")
check("★不认识的 action → 明确报错并列可用值",
      json.loads(T.execute(action="hack")).get("success") is False
      and "draw / status / queue / models / free / start / stop" in T.execute(action="hack"))
from src.core.tool_manager import get_tool_manager  # noqa: E402
_ti = {t.name: t for t in get_tool_manager().get_all_tools()}.get("comfyui")
check("★ToolManager 扫到了 comfyui", _ti is not None)
check("★★扫描到的参数非空且 required=[action]（别又是常量名写错）",
      bool((_ti.parameters or {}).get("properties"))
      and (_ti.parameters or {}).get("required") == ["action"])
check("★扫描到的描述就是带精简规范的那份", T._XML_SPEC in (_ti.description or ""))

# ══════════════ 8. ★ 主窗口接线 ══════════════
print("\n-- 8. 主窗口接线 --")
_mw = (ROOT / "src" / "ui" / "main_window.py").read_text(encoding="utf-8")
check("★★主窗口注册了 comfyui 的**两个**回调（发图 + 上下文提示）",
      "set_file_ready_callback(_cg_file_cb)" in _mw
      and "set_context_note_callback(_cg_note_cb)" in _mw)
check("★★注册了 post_load_hook（LAZY 工具卸载重载后回调不会丢）",
      '"comfyui"' in _mw and "register_post_load_hook" in _mw)
check("★★有 context_note 信号并接到了主线程处理",
      "context_note = Signal(str)" in _mw
      and "self._task_relay.context_note.connect(self._on_context_note)" in _mw)
check("★★_on_context_note 会把它写进 session.messages（不只是显示）",
      'self.session.messages.append({"role": "assistant", "content": text})' in _mw)
check("★★用 _turn_active 区分『本轮进行中』与『空闲』（避免插消息破坏 tool_calls 配对）",
      "self._turn_active = True" in _mw and "self._turn_active = False" in _mw
      and '_turn_active", False)' in _mw)
check("★_turn_active 在构造函数里初始化",
      "self._turn_active: bool = False" in _mw)
_sess = (ROOT / "src" / "core" / "session.py").read_text(encoding="utf-8")
check("★[思考] 日志仍在（上一轮加的，别被这轮改回去）", "[思考]" in _sess)

# ══════════════ 9. 真机层（可选）══════════════
if os.getenv("PRTS_TEST_COMFYUI") == "1":
    print("\n-- 9. 真机：连 8188 出一张 512×512（后台任务）--")
    _s = json.loads(T.execute(action="status"))
    check("★status 连上了", _s.get("success") is True, str(_s)[:120])
    _m = json.loads(T.execute(action="models"))
    check("★models 列到了 newbie01", "newbie01.safetensors" in
          (_m.get("models", {}).get("unet") or []))
    _got = []
    T.set_file_ready_callback(lambda l, p, s: _got.append((l, p, s)))
    _P = ("<character_1><n>main</n><gender>1girl</gender><count>1</count>"
          "<exclusive>true</exclusive><appearance>silver_hair, red_eyes, chibi</appearance>"
          "<clothing>white_dress</clothing><expression>smiling</expression>"
          "<pose>standing</pose><position>center</position></character_1>"
          "<general_tags><composition>solo, only_one_person, fully_visible</composition>"
          "<style>anime_style, clean_lines</style><background>simple_background</background>"
          "<quality>highres, masterpiece</quality></general_tags>")
    _t0 = time.time()
    _r = json.loads(T.execute(action="draw", prompt=_P, width=512, height=512,
                              steps=8, seed=1, timeout=420))
    _ret = time.time() - _t0
    check("★★真机：draw **秒回**（后台任务的核心）", _r.get("success") and _ret < 3.0,
          f"{_ret:.2f}s {str(_r)[:120]}")
    J.wait_all(timeout=600)
    check("★★真机：后台跑完并回调发图", len(_got) == 1, str(_got[:1]))
    if _got:
        check("★真机：出的是真文件", Path(_got[0][1]).exists()
              and Path(_got[0][1]).stat().st_size > 10000)
    _rec = J.recent(1)
    check("★真机：任务记录是 done", bool(_rec) and _rec[0]["state"] == "done", str(_rec))
    check("★真机结束后释放（把提交额度还回去）",
          json.loads(T.execute(action="free")).get("success") is True)
else:
    print("\n  （真机层跳过：设 PRTS_TEST_COMFYUI=1 才连 ComfyUI 真画）")

print(f"\n★ ComfyUI 回归：{PASS} PASS / {FAIL} FAIL")
sys.exit(1 if FAIL else 0)
