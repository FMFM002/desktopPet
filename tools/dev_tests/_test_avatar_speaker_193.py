# -*- coding: utf-8 -*-
"""★ 1.9.3（2026-10-04）：**共享工作台头像按说话者取**回归。

背景：所有形象共用同一个话题/聊天历史之后，每条 assistant 消息都带 `speaker`
（这句话是哪个形象说的）。而聊天窗口原来**只有一张桌宠头像** —— 所有 assistant
消息一律套"当前形象"的脸，于是共享历史里"别人的话"被套上当前形象的头像
（用户真机报的"切换形象后头像渲染错误"）。

这套件钉住四件事：
  1. 「角色名 → 头像路径」真的走的是 pet_manager 的现成入口（不是自己扫目录）；
  2. 两个**不同** speaker 的 assistant 消息，渲染出的头像 URI **不相等**；
     speaker 为空（历史遗留）或就是当前形象 → 用当前桌宠头像（原行为不变）；
  3. speaker 头像 URI 的缓存与失效自洽（set_avatars → 整体清空）；
  4. 调用点真的把 speaker 传下去了（`_reload_chat_view` / 流式实时那条路，
     用 AST 静态检查，防止以后有人又"忘了传"）。

★ 头像 URI 一律用"两个不同角色的 URI 不相等"这种稳健断言 ——
  不去比对具体文件名（不同机器上角色目录可能不一样）。
★ 不碰真实 `data/桌宠形象/`：在项目根的专用临时目录里造两个角色目录，
  把 pet_manager 的 CHARACTERS_DIR 指过去（用完即删）。"""
import ast
import io
import json
import os
import re
import shutil
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("DEEPSEEK_API_KEY", "test")
BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE))

PASS, FAIL = [], []


def check(name, ok, extra=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'[PASS]' if ok else '[FAIL]'} {name}" + (f"  {extra}" if extra else ""))


# ══════════════════════════════════════════════════════════════
# 0. 准备两个"角色目录"（真实图片），让"角色名 → 头像路径"能解析
# ══════════════════════════════════════════════════════════════
print("== 0. 造两个临时角色目录（真实图片）==")
_TMP = BASE / "_test_tmp_avatar193"
shutil.rmtree(_TMP, ignore_errors=True)     # 上一轮跑挂留下的残留，先清掉
_TMP.mkdir(parents=True, exist_ok=True)
_A, _B = "甲角色", "乙角色"


def _png_bytes(rgba):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGBA", (64, 64), rgba).save(buf, format="PNG")
    return buf.getvalue()


for _name, _color in ((_A, (255, 0, 0, 255)), (_B, (0, 0, 255, 255))):
    _d = _TMP / _name
    _d.mkdir()
    (_d / "头像.png").write_bytes(_png_bytes(_color))
    (_d / "settings.json").write_text(
        json.dumps({"name": _name, "avatar": "头像.png"}, ensure_ascii=False),
        encoding="utf-8")

import src.core.pet_manager as _pm  # noqa: E402

_pm.CHARACTERS_DIR = _TMP          # ★ 只在本进程内改指，绝不碰真实角色目录

_APPLY = None
try:
    from PySide6.QtWidgets import QApplication  # noqa: E402
    from src.ui.chat_widget import ChatWidget  # noqa: E402

    _APPLY = QApplication.instance() or QApplication([])
    _W = ChatWidget()
    _APPLY.processEvents()
    check("ChatWidget 能实例化", _W is not None)
except Exception as _e:            # noqa: BLE001
    check("ChatWidget 能实例化", False, f"{type(_e).__name__}: {_e}")


def _uri_of(html: str) -> str:
    """从渲染出的 HTML 里抠出**第一个**头像的 <img src="...">"""
    m = re.search(r'<img src="([^"]*)"', html or "")
    return m.group(1) if m else ""


if _APPLY is not None:
    # ── 1. 角色名 → 头像路径：走 pet_manager 的现成入口 ──
    print("\n== 1. 「角色名 → 头像路径」走现成入口 ==")
    _pa = ChatWidget._speaker_avatar_path(_A)
    _pb = ChatWidget._speaker_avatar_path(_B)
    check("★两个角色各自解析到自己的头像文件（路径不相同）",
          bool(_pa) and bool(_pb) and Path(_pa) != Path(_pb),
          f"{Path(_pa).parent.name}/{Path(_pa).name} vs {Path(_pb).parent.name}/{Path(_pb).name}")
    check("★解析出的路径确实在扫描到的角色目录里",
          Path(_pa).parent.name == _A and Path(_pb).parent.name == _B)
    check("未知角色 → 空串（调用方回退当前形象头像）",
          ChatWidget._speaker_avatar_path("这个角色不存在") == "")
    check("空说话者 → 空串", ChatWidget._speaker_avatar_path("") == "")

    # ── 2. 渲染：不同 speaker 的头像 URI 必须不同 ──
    print("\n== 2. 头像按说话者取 ==")
    _h_a = _W._render_message_html("assistant", "我是甲说的", _A)
    _h_b = _W._render_message_html("assistant", "我是乙说的", _B)
    _u_a, _u_b = _uri_of(_h_a), _uri_of(_h_b)
    check("★★两个不同 speaker 的 assistant 消息 → 头像 URI **不相等**",
          bool(_u_a) and bool(_u_b) and _u_a != _u_b,
          f"甲={_u_a[:32]}… 乙={_u_b[:32]}…")
    check("两个 speaker 的头像都是真的解码出来的图片（data:image/png;base64）",
          _u_a.startswith("data:image/png;base64,")
          and _u_b.startswith("data:image/png;base64,"))

    _h_empty = _W._render_message_html("assistant", "改造前没有 speaker 的老发言")
    check("★speaker 为空（历史遗留）→ 用**当前形象**头像",
          _uri_of(_h_empty) == _W._avatar_uri["bot"])
    check("★speaker 为空时不会去用别的形象的头像",
          _uri_of(_h_empty) != _u_a and _uri_of(_h_empty) != _u_b)

    _W.set_conversation_info(character=_A)
    _h_self = _W._render_message_html("assistant", "我是甲本人说的", _A)
    check("★speaker 就是当前形象 → 用**当前形象**头像（原行为不变）",
          _uri_of(_h_self) == _W._avatar_uri["bot"])
    check("★当前形象走的是「桌宠头像」那条路，不是「按说话者解析目录」那条路",
          _uri_of(_h_self) == _uri_of(_h_empty) and _uri_of(_h_self) != _u_a)

    # 实时那条（流式/待机/工具文本）与重画后同一条，头像必须一致
    _live = _uri_of(_W._render_message_html("assistant", "刚说完的这句", _W._char_name))
    _reload = _uri_of(_W._render_message_html("assistant", "刚说完的这句", _A))
    check("★★同一条消息：实时显示与重画历史 → 头像一致", _live == _reload)

    # user 侧不受影响
    _h_user = _W._render_message_html("user", "主人说的话")
    check("user 消息仍用用户头像（本次不动用户头像逻辑）",
          _uri_of(_h_user) == _W._avatar_uri["user"])
    check("旧签名 _avatar_html(True) 仍然兼容（默认当前形象头像）",
          _W._avatar_html(True) == _W._avatar_html(True, ""))

    # ── 3. 缓存与失效 ──
    print("\n== 3. speaker 头像 URI 缓存 / 失效 ==")
    # 先把"当前形象"换回空 —— 否则 _A 就是当前形象，走的是"当前形象头像"那条路，
    # 跟缓存无关了（上面第 2 节故意把它设成 _A）。
    _W.set_conversation_info(character="")
    check("★按说话者缓存了 URI（渲染过谁就有谁的条目）",
          _A in _W._speaker_avatar_uri and _B in _W._speaker_avatar_uri,
          str(sorted(_W._speaker_avatar_uri.keys())))
    _u_a2 = _uri_of(_W._render_message_html("assistant", "再说一句", _A))
    check("★第二次渲染直接命中缓存（同一 URI，不再解码）", _u_a2 == _u_a)

    _user_png = _TMP / "user_avatar.png"
    _user_png.write_bytes(_png_bytes((0, 200, 0, 255)))
    _W.set_avatars(str(_user_png), "")
    check("★★set_avatars() 刷新头像 → 说话者缓存整体失效",
          _W._speaker_avatar_uri == {}, str(sorted(_W._speaker_avatar_uri.keys())))
    check("★刷新后用户头像真的换成了新图",
          _uri_of(_W._render_message_html("user", "换头像后的用户消息"))
          == _W._avatar_uri["user"]
          and _W._avatar_uri["user"].startswith("data:image/png;base64,"))
    _u_a3 = _uri_of(_W._render_message_html("assistant", "缓存清掉后重来", _A))
    check("★失效后重新渲染，说话者头像仍是那个角色自己的图",
          _u_a3 == _u_a and _u_a3 != _uri_of(_W._render_message_html("assistant", "x", _B)))

    # ── 4. 调用点静态检查（AST）──
    print("\n== 4. 调用点确实传了 speaker（AST 静态检查）==")
    _MW = BASE / "src" / "ui" / "main_window.py"
    _CW = BASE / "src" / "ui" / "chat_widget.py"
    _mw_src = _MW.read_text(encoding="utf-8")
    _cw_src = _CW.read_text(encoding="utf-8")

    def _find_func(tree, name):
        for n in ast.walk(tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
                return n
        return None

    def _arg3_text(src, call):
        """取这次调用"说话者"那一个实参的源码片段（第 3 个位置参数，或 speaker= 关键字）"""
        if len(call.args) >= 3:
            return ast.get_source_segment(src, call.args[2]) or ""
        for kw in call.keywords:
            if kw.arg == "speaker":
                return "speaker=<关键字>"
        return ""

    def _speaker_arg_ok(src, call, markers=("speaker",)):
        """说话者那个实参是不是"真的指说话者"（表达式名里带 speaker / 当前形象名）"""
        seg = _arg3_text(src, call)
        return bool(seg) and any(m in seg for m in markers)

    def _is_user_call(call):
        return (bool(call.args) and isinstance(call.args[0], ast.Constant)
                and call.args[0].value in ("user", "system"))

    def _append_calls(func):
        if func is None:
            return []
        return [n for n in ast.walk(func)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "_append_message"]

    _mw_tree = ast.parse(_mw_src)
    _reload = _find_func(_mw_tree, "_reload_chat_view")
    _calls = _append_calls(_reload)
    _assistant_calls = [c for c in _calls if not _is_user_call(c)]
    check("★_reload_chat_view 里确实有 assistant 的 _append_message 调用点"
          "（归档段 + 当前话题）",
          len(_assistant_calls) >= 2, f"assistant {len(_assistant_calls)} 处 / 共 {len(_calls)} 处")
    _bad = [f"L{c.lineno}" for c in _assistant_calls if not _speaker_arg_ok(_mw_src, c)]
    check("★★_reload_chat_view 的**每一处** assistant 消息都传了 speaker",
          not _bad, "没传的: " + ", ".join(_bad))
    check("★user 消息仍然只传两个参数（本次不动用户侧）",
          all(len(c.args) == 2 for c in _calls if _is_user_call(c)))

    _prepend = _find_func(_mw_tree, "_try_prepend_history_round")
    _pkeys = set()
    if _prepend is not None:
        for _d in ast.walk(_prepend):
            if isinstance(_d, ast.Dict):
                for _k in _d.keys:
                    if isinstance(_k, ast.Constant) and isinstance(_k.value, str):
                        _pkeys.add(_k.value)
    check("★「↑ 历史」prepend 那批 item 也带了 speaker（否则插上来的头像不对）",
          "speaker" in _pkeys, str(sorted(_pkeys)))

    _cw_tree = ast.parse(_cw_src)
    _stream = _find_func(_cw_tree, "append_streaming_text")
    _stream_calls = [n for n in ast.walk(_stream)
                     if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                     and n.func.attr == "_render_message_html"] if _stream else []
    check("★★流式那条路也把说话者传给了 _render_message_html",
          bool(_stream_calls)
          and all(len(c.args) >= 3 for c in _stream_calls),
          f"{len(_stream_calls)} 处")

    # 全仓：凡是 assistant 的 _append_message 调用都必须带说话者
    #   （历史重画传 DB 里的 speaker；实时那条传当前形象名 —— 两者都算"带上了"）
    _LIVE_MARKERS = ("speaker", "_char_name", "character")
    _miss = []
    for _p in (BASE / "src").rglob("*.py"):
        _t = _p.read_text(encoding="utf-8", errors="replace")
        try:
            _tree = ast.parse(_t)
        except SyntaxError:
            continue
        for _c in ast.walk(_tree):
            if (isinstance(_c, ast.Call) and isinstance(_c.func, ast.Attribute)
                    and _c.func.attr == "_append_message"
                    and _c.args and isinstance(_c.args[0], ast.Constant)
                    and _c.args[0].value == "assistant"):
                if not _speaker_arg_ok(_t, _c, _LIVE_MARKERS):
                    _miss.append(f"{_p.name}:{_c.lineno}")
    check("★★全仓 assistant 的 _append_message 调用都带上了说话者",
          not _miss, "; ".join(_miss))

# 收尾
shutil.rmtree(_TMP, ignore_errors=True)

print(f"\n==== 结果: {len(PASS)} PASS / {len(FAIL)} FAIL ====")
for _f in FAIL:
    print("  FAIL:", _f)
sys.exit(1 if FAIL else 0)
