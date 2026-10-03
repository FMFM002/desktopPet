#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
本地画图工具（comfyui）—— ★ 1.9.1

【这一版最大的变化：draw 改成**后台任务**】
  用户 2026-09-18 01:4x 的原话：
    "画图是成功了，但是一路上全是bug和报错，直接画了好多。和下载一样，
     提交为后台任务，不影响继续聊天，画完了后，立刻发送，
     上下文插入系统提示：刚刚发送了xxxx图片。"
  → `action=draw` 现在**只负责入队就返回**（1 秒内），不再同步等到出图；
    真正的出图、发图、往上下文插提示都由 `src/core/comfyui_jobs.py`
    的单线程 worker 完成。所以画图期间可以继续聊天，也不会再因为
    长时间阻塞把工具调用链拖出各种超时/重试。

【代码为什么分三块】
  · `src/core/comfyui_client.py` —— 引擎（HTTP/工作流/显存与提交量检查/取图/释放）
  · `src/core/comfyui_jobs.py`   —— 队列 + 后台 worker + 回调
  · 本文件                       —— 工具元数据 + 参数解析 + 转发（薄壳）
  分块的原因：ToolManager 对 LAZY 工具"空闲就卸载"，工具模块里的队列/线程/回调
  **活不过一次卸载**（表情包信箱 `meme_bus` 踩过同一个坑），所以状态必须放 `src/core/`。

【五个 action】
  draw   —— 入队一张图（**立刻返回**，附录制好的 XML 提示词要求）
  status —— ComfyUI 在不在、显存、**提交余量**、队列里几个、正在画哪个
  queue  —— ComfyUI 自己的队列（正在跑 / 排队）
  models —— 可用的底模 / CLIP / VAE / LoRA
  free   —— 释放 ComfyUI 的模型与显存（可逆，下次画图自动重载）
  start  —— ★ 新增：把 ComfyUI 启动起来（画图时也会自动启动，这个用于手动/排查）
  stop   —— ★ 新增：关闭 ComfyUI（也会在工具空闲卸载时自动做）

【★ 1.9.1: ComfyUI 的整个生命周期都归这个工具管】
  用户批注："comfyUI的启动，加载模型，卸载模型，关闭comfyUI都写入tool自动完成。"
  以及："走其他tool一样的卸载规则。comfyUI和模型同加载同卸载，不用分开。"
  → 用完不用管：
      · 要画图但 ComfyUI 没跑 → **后台 worker 自己把它拉起来**（不用用户双击 bat）；
      · 模型由 ComfyUI 按需自己载入/卸载，我们不用插手；
      · **工具空闲卸载（和其他 LAZY 工具同一条规则，30 秒）→ 自动关掉 ComfyUI**，
        模型和提交额度一起还回去。见 `src/core/comfyui_jobs.on_tool_unloaded`。
"""
from __future__ import annotations

import json
import logging

from src.core import comfyui_client as _cc
from src.core import comfyui_jobs as _jobs

_log = logging.getLogger("Tool.ComfyUI")

TOOL_NAME = "comfyui"

# ★ 精简版提示词规范：由 xml标准.txt（265 行 / 9.8KB）压缩而来，
#   宁可少说也不能把 9.8KB 塞进每次请求的 tool 描述里（那要烧掉大量 token）。
#   回归里有一条"整份描述不许超过 1800 字符"的闸门盯着它别再膨胀。
_XML_SPEC = """★ 调用 draw 之前，你必须先把用户的话**规范化重写**成下面的 XML（NewBie 底层
吃 Danbooru 标签，英文小写 + 下划线），只把重写结果放进 prompt：

<character_1>
<n>角色名或 main</n><gender>1girl|1boy</gender><count>1</count><exclusive>true</exclusive>
<appearance>发色_hair, 发型, 瞳色_eyes, 其他特征(ahoge/blush/fang…)</appearance>
<clothing>服装标签，逗号分隔</clothing>
<expression>表情标签</expression>
<pose>姿势标签</pose>
<position>center</position>
</character_1>
<general_tags>
<composition>solo, only_one_person, no_other_people, no_extra_limbs, fully_visible</composition>
<style>anime_style, japanese_anime, clean_lines, cel_shading, vibrant_colors</style>
<background>具体背景标签</background>
<atmosphere>bright, soft_lighting</atmosphere>
<quality>highres, absurdres, masterpiece, best_quality</quality>
</general_tags>

要点：① «solo» 比 «1girl» 更能保证单人，一定写；② 多人时加 <character_2> 并各自写
<position>；③ 不要的特征写成 no_xxx 放进对应块；④ <appearance> 别超过 15 个标签；
⑤ 属性不明显时用 (tag:1.2) 加权重。用户只说"画个XX"时，就按常识把这五块补全，
不要反问。用户明确说"原样传 / 别改"时，改用 raw=true 直接传原话。"""

TOOL_DESCRIPTION = (
    "在本机 ComfyUI 上画图（NewBie 二次元模型）。\n"
    "action:\n"
    "  draw   —— 画一张图。prompt 必须是**你先规范化重写过的 XML**（见下）。\n"
    "            ★ 它是**后台任务**：调用后**立刻返回**，你继续正常聊天即可；\n"
    "              画好之后系统会自动把图发给用户，并告诉你『刚刚发送了 xxx 图片』。\n"
    "              所以**不要**说『稍等 / 马上就好』，也不要重复调用等结果。\n"
    "  status —— ComfyUI 在不在、显存、提交余量、后台队列里几个、正在画哪个。\n"
    "  queue  —— ComfyUI 自己的队列（正在跑 / 排队）。\n"
    "  models —— 列出可用的底模 / CLIP / VAE / LoRA 文件名。\n"
    "  free   —— 释放 ComfyUI 占着的模型与显存（画完不用时调，把内存还给系统）。\n"
    "  start  —— 启动 ComfyUI（画图时也会自动启动，这个用于手动/排查）。\n"
    "  stop   —— 关闭 ComfyUI（工具空闲时也会自动关）。\n\n"
    + _XML_SPEC +
    "\n\n其它参数：width/height（默认 512×512，你测试也用这个，快）、"
    "steps（默认 35）、seed（不传就随机）、negative（不传用内置那套）、"
    "lora（默认 NBfym.safetensors）、raw（用户说原样传时设 true）、timeout（秒）。"
)

TOOL_PARAMETERS = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["draw", "status", "queue", "models", "free", "start", "stop"],
            "description": ("draw=后台画图 / status=服务·显存·提交量·队列 / "
                            "queue=ComfyUI 队列 / models=可用模型 / "
                            "free=释放模型与显存 / start=启动 ComfyUI / stop=关闭 ComfyUI"),
        },
        "prompt": {
            "type": "string",
            "description": ("draw 必填。**你规范化重写后的 XML 提示词**"
                            "（除非 raw=true，那时才放用户原话）"),
        },
        "raw": {
            "type": "boolean",
            "description": "用户明确说「直接传 / 不要修正 / 原样传」时设 true，prompt 原样送进去",
        },
        "width": {"type": "integer", "description": "宽（像素），默认 512"},
        "height": {"type": "integer", "description": "高（像素），默认 512"},
        "steps": {"type": "integer", "description": "采样步数，默认 35；越大越慢越细"},
        "seed": {"type": "integer", "description": "随机种子；不传就随机，想复现同一张就传上次的"},
        "negative": {"type": "string", "description": "负面提示词；不传用内置的那套"},
        "lora": {"type": "string", "description": "LoRA 文件名，默认 NBfym.safetensors"},
        "timeout": {"type": "integer", "description": "后台等图的最长秒数，默认 600"},
    },
    "required": ["action"],
}


# ══════════════════ 给主窗口注册回调 ══════════════════
# ★ 主窗口通过 `register_post_load_hook("comfyui", ...)` 重新注册，
#   因为 LAZY 工具被卸载后再加载时模块级变量会重置（见文件头说明）。

def set_file_ready_callback(cb):
    """出图了 → (label, path, size)。主窗口据此发图 + 落库。"""
    _jobs.set_file_ready_callback(cb)


def set_context_note_callback(cb):
    """出图了 → (text)。主窗口据此往上下文插一条系统提示。"""
    _jobs.set_context_note_callback(cb)


# ══════════════════ 入口 ══════════════════

def _err(msg: str, **extra) -> str:
    d = {"success": False, "error": msg}
    d.update(extra)
    return json.dumps(d, ensure_ascii=False)


def _do_status() -> str:
    st = _cc.status_text()
    if not st.get("ok"):
        return _err(st["text"], need_start=True)
    qs = _jobs.queue_status()
    txt = st["text"] + f"\n后台队列：排队 {qs['queued']} 张"
    txt += (f"，正在画 {qs['running'].get('prompt_id') or '（正在提交）'}"
            if qs["running"] else "，当前空闲")
    recent = _jobs.recent(3)
    if recent:
        txt += "\n最近任务：" + "；".join(
            f"{r['job_id']} → {r['state']}"
            + (f"({len(r['paths'])}张)" if r["paths"] else "")
            + (f" {r['error'][:40]}" if r["error"] else "")
            for r in recent)
    return json.dumps({"success": True, "action": "status", "url": _cc.base_url(),
                       "vram": st.get("vram"), "host": st.get("host"),
                       "comfy_running": st.get("running"),
                       "comfy_pending": st.get("pending"),
                       "top_vram": st.get("top_vram"), "jobs": qs,
                       "recent": recent, "text": txt}, ensure_ascii=False)


def _do_queue() -> str:
    st = _cc.queue_text()
    if not st.get("ok"):
        return _err(st["text"])
    qs = _jobs.queue_status()
    txt = (st["text"] + f"\n（本工具的后台队列：排队 {qs['queued']} 张"
           + ("，正在画" if qs["running"] else "，空闲") + "）")
    return json.dumps({"success": True, "action": "queue",
                       "running": st.get("running"), "pending": st.get("pending"),
                       "jobs": qs, "text": txt}, ensure_ascii=False)


def _do_models() -> str:
    st = _cc.models_text()
    if not st.get("ok"):
        return _err(st["text"])
    return json.dumps({"success": True, "action": "models", "models": st.get("models"),
                       "text": st["text"]}, ensure_ascii=False)


def _do_free() -> str:
    """★ 释放 ComfyUI 的模型与显存（可逆）"""
    if not _cc.alive():
        return _err(f"ComfyUI 没在跑（{_cc.base_url()} 连不上）")
    ok = _cc.free_models()
    hm = _cc.host_memory()
    txt = ("已让 ComfyUI 释放模型与显存（下次画图会自动重新加载，只慢约 30 秒）。"
           if ok else "释放请求失败了，看日志。")
    if hm:
        txt += (f"\n现在提交量：上限 {_cc.fmt_gb(hm['commit_total'])} / "
                f"还能提交 {_cc.fmt_gb(hm['commit_free'])}；"
                f"内存空闲 {_cc.fmt_gb(hm['ram_avail'])}")
    return json.dumps({"success": ok, "action": "free", "host": hm, "text": txt},
                      ensure_ascii=False)


def _do_start() -> str:
    """★ 1.9.1: 启动 ComfyUI（没跑就拉起来，已经在跑就什么都不做）"""
    st = _cc.start(wait_seconds=180)
    if st.get("ok"):
        # ★ 1.9.3 第 25 轮：告诉 jobs 层"这是特意拉起来的" ——
        #   免得它因为工具空闲卸载、在模型还没来得及 draw 时就把服务关掉
        #   （真机 03:04:09 启动 → 03:04:12 就被关了，一张图没画成）。
        try:
            from src.core import comfyui_jobs as _jobs
            _jobs.note_explicit_start()
        except Exception:
            pass
        return json.dumps({
            "success": True, "action": "start", "already": bool(st.get("already")),
            "text": (st.get("text", "") +
                     "\n★ 已就绪，**尽快接着调 `draw`**（一次可入队多张，不阻塞）。"
                     "注意：这个工具空闲约 30 秒会被卸载，ComfyUI 会跟着关掉 —— "
                     "所以别「先启动、再去搜半小时素材、最后才画」。"),
        }, ensure_ascii=False)
    return json.dumps({"success": False, "action": "start",
                       "already": bool(st.get("already")),
                       "text": st.get("text", "")}, ensure_ascii=False)


def _do_stop() -> str:
    """★ 1.9.1: 关闭 ComfyUI（只关这个安装目录下的进程）"""
    st = _cc.stop()
    return json.dumps({"success": bool(st.get("ok")), "action": "stop",
                       "killed": st.get("killed", 0),
                       "text": st.get("text", "")}, ensure_ascii=False)


def execute(action: str = "status", prompt: str = "", raw: bool = False,
            width: int = 512, height: int = 512, steps: int = 35,
            seed=None, negative: str = "", lora: str = "",
            timeout: int = 600, **kwargs) -> str:
    """工具入口（同步；但 draw 只入队、不等待出图）"""
    a = (action or "").strip().lower()
    if a in ("", "status", "状态"):
        return _do_status()
    if a in ("queue", "队列"):
        return _do_queue()
    if a in ("models", "模型"):
        return _do_models()
    if a in ("free", "释放"):
        return _do_free()
    if a in ("start", "启动"):
        return _do_start()
    if a in ("stop", "关闭", "shutdown"):
        return _do_stop()
    if a in ("draw", "画", "画图", "generate"):
        r = _jobs.submit_draw(prompt, raw=raw, width=width, height=height,
                              steps=steps, seed=seed, negative=negative,
                              lora=lora, timeout=timeout)
        return json.dumps(r, ensure_ascii=False)
    return _err(f"不认识的 action「{action}」。可用：draw / status / queue / models / free / start / stop")


if __name__ == "__main__":       # 手动排查用
    import sys
    logging.basicConfig(level=logging.INFO)
    kw = dict(x.split("=", 1) for x in sys.argv[1:] if "=" in x)
    if "raw" in kw:
        kw["raw"] = kw["raw"].lower() in ("1", "true", "yes")
    print(execute(**kw))
