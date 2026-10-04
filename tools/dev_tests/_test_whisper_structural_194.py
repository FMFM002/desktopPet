# -*- coding: utf-8 -*-
"""★ 1.9.4：**whisper 转写的结构性验证**（把"环境依赖的 SKIP"补上等价覆盖）。

## 背景
`_test_a190` 里有一条 "真实语音转写" 断言，长期是 **SKIP**：
    本机 SAPI 没装语音、原语音素材在 2026-10-04 的私有目录清理中丢了
它打印 NOTE 并跳过（**没有假装通过**，这点是对的），但结果是：
**"whisper 这条路到底通不通"在回归里其实没被验过。**

## 本套件的做法（和当年给视觉注入 `vision_fn` 一个思路）
不依赖真实模型、不依赖真实语音 —— 只验**我们的接线**：
  1. 造两个哑文件当 `whisper-cli.exe` / `ggml-small.bin`（`available` 只看 isfile）
  2. **monkeypatch `subprocess.run`**，让它返回 whisper.cpp 风格的 JSON
  3. 断言：命令行参数**固化正确**、JSON **能解析出文字**、各种坏输出**不炸**

## ★ 为什么值得单独一条
`_build_cmd` 的 docstring 里记着**真机踩过的坑**（2026-09-16）：
    `-m <绝对路径>` 会让这个 whisper.cpp 构建**栈溢出崩掉**（0xC0000409）
    ⇒ 所以固化成了 "cwd 切到模型目录 + `-m` 只给文件名 + `-f` 给绝对路径"
**这条约束只有断言能守住** —— 谁哪天"顺手"改成绝对路径，用户那边就是崩溃。
"""
import os
import subprocess
import sys
import types
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PRTS_NO_COMFY_START", "1")
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools" / "dev_tests"))

from _fixtures_193 import fixture_dir, make_wav          # noqa: E402

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))


print("== 准备：哑可执行/哑模型 + 真 WAV（都不含真实语音，本套件不需要）==")
_D = fixture_dir("whisper_194")
_exe = _D / "whisper-cli.exe"
_model = _D / "ggml-small.bin"
_exe.write_bytes(b"dummy")            # available 只看 isfile
_model.write_bytes(b"dummy")
_wav = make_wav(_D / "sample.wav", seconds=0.3)
check("★哑文件与 fixture WAV 就位", _exe.is_file() and _model.is_file() and _wav.is_file())

from src.core import whisper_local as wl                 # noqa: E402

_cli = wl.WhisperCLI(executable=str(_exe), model=str(_model), language="zh", threads=2)
check("★构造出的 WhisperCLI 判定为 available", bool(_cli.available))

print("\n== A. ★★ 命令行参数固化（守住真机踩过的坑 —— 谁改成绝对路径用户就崩）==")
_cmd, _cwd, _hint = _cli._build_cmd(str(_wav), "zh", json_out=True)
check("★★`-m` 只给**文件名**（不是绝对路径 —— 绝对路径会让该构建栈溢出 0xC0000409）",
      _cmd[_cmd.index("-m") + 1] == _model.name,
      _cmd[_cmd.index("-m") + 1])
check("★`-f`（音频）给**绝对路径**（音频可能在 %TEMP% 或任意下载目录）",
      os.path.isabs(_cmd[_cmd.index("-f") + 1]),
      _cmd[_cmd.index("-f") + 1])
check("★cwd 切到**模型所在目录**（配合上面的 -m 只给文件名）",
      Path(_cwd).resolve() == _model.parent.resolve(), str(_cwd))
check("★带 `-ng`（防御性禁 GPU 探测）", "-ng" in _cmd)
check("★带 `-oj`（要 JSON 输出）", "-oj" in _cmd)

print("\n== B. 正常 JSON 能被解析出文字 ==")
_seen = {}


def _fake_run(cmd, **kw):
    _seen["cmd"], _seen["kw"] = cmd, kw
    return types.SimpleNamespace(
        returncode=0,
        stdout='{"text": "这是结构性验证的文字"}\n',
        stderr="",
    )


_orig = wl.subprocess.run
wl.subprocess.run = _fake_run
try:
    _txt = _cli.transcribe_file(str(_wav), "zh")
    check("★★转写返回了解析出的文字", _txt == "这是结构性验证的文字", repr(_txt))
    check("★真的走了 subprocess（且用的是 cwd=模型目录、有 timeout）",
          _seen.get("kw", {}).get("cwd") is not None and "timeout" in _seen.get("kw", {}),
          str({k: v for k, v in _seen.get("kw", {}).items() if k != "cwd"}))

    print("\n== C. 坏输出不许炸（稳健性）==")
    for _name, _stdout, _rc, _want in (
        ("进程返回非零 → None", '{"text": "不该被采用"}', 1, None),
        ("JSON 里 text 为空 → None", '{"text": "   "}', 0, None),
        ("前面有噪音、最后一行才是 JSON → 仍能取到", 'noise\n{"text": "OK"}\n', 0, "OK"),
        # ★ 这条我第一版写错了预期：以为"没有 JSON 就返回 None"。
        #   实际它**回落到 `_text_from_raw_output`** —— 那是**有真机依据的既有设计**：
        #   真机上这个构建的 `-oj` 并没有给出预期 JSON，但 stdout 里**就是干净的转写文本**；
        #   老代码在那里只打 warning 就 return None → "识别明明成功了却返回空"，
        #   用户看到的是"音频识别失败"。所以兜底必须**真的把文本取出来**。
        ("stdout 无 JSON → **回落到原始输出**（老版本 whisper-cli 的兜底）",
         "loading model...\ndone", 0, "loading model... done"),
    ):
        wl.subprocess.run = (lambda so, rc: (lambda cmd, **kw: types.SimpleNamespace(
            returncode=rc, stdout=so, stderr="boom")))(_stdout, _rc)
        try:
            _r = _cli.transcribe_file(str(_wav), "zh")
        except Exception as e:
            _r = f"<抛异常 {type(e).__name__}>"
        check(f"★{_name}", _r == _want, repr(_r))

    check("★★上一条钉的是**兜底本身**：`_text_from_raw_output` 不许被删"
          "（真机上 `-oj` 没给 JSON 时全靠它，删了就会'识别成功却返回空'）",
          hasattr(_cli, "_text_from_raw_output")
          and "从原始输出取到文本" in (ROOT / "src" / "core" / "whisper_local.py")
          .read_text(encoding="utf-8"))

    print("\n== D. ★ 反向守卫：判据本身有效（不是碰巧过）==")
    # 故意把 -m 改成绝对路径 → 上面那条"只给文件名"的断言必须能抓到
    _bad_cmd = ["x", "-m", str(_model.resolve()), "-f", str(_wav), "-ng", "-oj"]
    check("★★一条'用绝对路径 -m'的假命令，确实会被 A 段判据判为不合规",
          _bad_cmd[_bad_cmd.index("-m") + 1] != _model.name)
finally:
    wl.subprocess.run = _orig                      # ★ 必须还原，别污染别的套件

print(f"\n==== 结果: {len(PASS)} PASS / {len(FAIL)} FAIL ====")
for f in FAIL:
    print("  FAIL:", f)
sys.exit(1 if FAIL else 0)
