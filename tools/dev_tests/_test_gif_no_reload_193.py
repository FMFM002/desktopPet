# -*- coding: utf-8 -*-
"""★ 1.9.3 第 55 轮：**同一个 GIF 就不要重载**（用户要求的优化）。

用户原话："切换到同一个 gif 时，或者命令是保持，就不切换，不用加载了。"

背景：换 GIF 要重建 QMovie（解码/加载），是掉帧来源之一 ——
抽帧探针抓到过"最差 125ms"与 `切换行为: deepseek/GIF` **同一秒**。
而日志里 `[形象] 切换命令 → GIF「铃兰」` / `应用动作 GIF: 铃兰.gif` /
`切换角色: 铃兰` 会**反复出现**：原来 `set_character()` 无条件
`_current_behavior = "" + set_behavior("待机")`，每次重复命令都把 GIF 整个重载一遍。

本套件钉三件事：
  A. `set_character(同一个角色)` + 电影在播 → **一个字都不动**；
  B. `set_gif_file(同一个文件)` + 电影在播 → **不重建 QMovie**；
  C. ★ **自愈不能被挡掉**：电影停了/没了时，`set_gif_file` 仍必须真的重载
     （`repaint_current()` 就靠它把"凭空消失"的形象救回来）。
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from PySide6.QtWidgets import QApplication        # noqa: E402
from PySide6.QtGui import QMovie                   # noqa: E402

app = QApplication.instance() or QApplication([])

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))


# ── 造一个合法的 1×1 GIF（避免依赖真实素材）──
_TMP = ROOT / "_test_tmp_gif193"
_TMP.mkdir(exist_ok=True)
_GIF = _TMP / "a.gif"
_GIF2 = _TMP / "b.gif"
_ONE_PX_GIF = bytes.fromhex(
    "47494638396101000100800000ffffff00000021f90401000000002c00000000"
    "010001000002024401003b")
_GIF.write_bytes(_ONE_PX_GIF)
_GIF2.write_bytes(_ONE_PX_GIF)

from src.ui.pet_animation import PetAnimationWidget     # noqa: E402

w = PetAnimationWidget()
w._characters = {"甲": {"待机": [str(_GIF)], "默认": [str(_GIF)]},
                 "乙": {"待机": [str(_GIF2)]}}
w._current_character = "甲"
w.set_gif_file(str(_GIF))
_movie_before = w._movie
check("★前置：真加载了一个 QMovie", _movie_before is not None)
check("★前置：它在跑（否则下面的跳过判据不成立）",
      _movie_before is not None and _movie_before.state() != QMovie.NotRunning,
      str(_movie_before.state() if _movie_before else None))

print("\n== A. set_character(同一个角色) 不动它 ==")
w.set_character("甲")
check("★★同一个角色 → QMovie **没有被重建**（用户要的'不用加载了'）",
      w._movie is _movie_before)
check("★同一个角色 → 行为名也没被清掉（原来会被塞成空再重设待机）",
      w._current_behavior != "", repr(w._current_behavior))

print("\n== B. set_gif_file(同一个文件) 不重建 QMovie ==")
w._current_behavior = "待机"
w.set_gif_file(str(_GIF))
check("★★同一个 GIF 文件 → QMovie 没有被重建", w._movie is _movie_before)
w.set_gif_file(str(_GIF), behavior_label="默认")
check("★同一个文件但行为名不同 → 仍不重建（看的是 gif，不是行为名）",
      w._movie is _movie_before)
check("★行为名被更新了（标签要跟上）", w._current_behavior == "默认", w._current_behavior)

print("\n== C. 换到**不同**文件时照常加载 ==")
w.set_gif_file(str(_GIF2))
check("★★不同 GIF → 真的重建了 QMovie", w._movie is not _movie_before
      and w._movie is not None)
check("★_last_file 跟着更新", w._last_file == str(_GIF2), str(w._last_file))

print("\n== D. ★自愈必须仍能生效（不能被跳过挡掉）==")
# D1：电影没了（None）→ 同一个文件也必须重载
w._last_file = str(_GIF)
w._movie = None
w.set_gif_file(str(_GIF))
check("★★电影为 None 时，同一个文件也必须重载（自愈靠这条路）",
      w._movie is not None and w._movie is not _movie_before)
# D2：电影被停掉（NotRunning）→ 同一个文件也必须重载
_m2 = w._movie
w._movie.stop()
check("★前置：电影已停在 NotRunning", w._movie.state() == QMovie.NotRunning)
w.set_gif_file(str(_GIF))
check("★★电影停了时，同一个文件也必须重载（否则形象会一直空白）",
      w._movie is not _m2)
# D3：once=True → 不跳过（它播完要发 gif_once_finished，跳过会吞掉通知）
_m3 = w._movie
w.set_gif_file(str(_GIF), once=True)
check("★★once=True 时即使同一个文件也不跳过（通知不能丢）", w._movie is not _m3)
check("★once=True 设定为不循环", w._loop_gif is False)

w.close()
import shutil                                     # noqa: E402
shutil.rmtree(_TMP, ignore_errors=True)

print(f"\n==== 结果: {len(PASS)} PASS / {len(FAIL)} FAIL ====")
for f in FAIL:
    print("  FAIL:", f)
sys.exit(1 if FAIL else 0)
