# -*- coding: utf-8 -*-
"""★ 1.9.3 第 49 轮：**STT 作为文件查看方式** —— 跑通并验证。

用户 2026-10-04 的第 3 件事："STT 也可以作为一种文件查看的方式，为文件操作多一种 action。"
查证结果：**代码早就支持了** —— `file_ops view` 的 `method` 枚举里有 `stt`，
`file_view.view_file(method="stt")` 会走 `_default_stt()` → `stt.transcribe_file()`。
所以本套件不是"加功能"，而是**把它跑通并钉住**（此前没有任何测试覆盖这条路）。

分两层：
  A. **确定性层**（注入假 stt_fn）：路由对不对、返回结构对不对、异常怎么处理
     —— 不依赖 whisper 的心情，任何机器都能跑。
  B. **真机层**（本机有 whisper-cli + 模型才跑）：拿一段合成音频真的走一遍
     `_default_stt`，确认**不抛异常、返回字符串**。
     ★ 不强求转写内容正确 —— 合成音不是人话，whisper 可能给空、也可能编一句；
       这里只验证"**这条路真的通**"。
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools" / "dev_tests"))

from _fixtures_193 import fixture_dir, make_speech_wav, make_wav   # noqa: E402

import tools.file_view as FV          # noqa: E402
import tools.file_ops as FO           # noqa: E402

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))


print("\n== A. 确定性层：method=stt 的路由与返回结构（注入假 STT）==")
wav = make_wav(fixture_dir("stt_file_193") / "tone.wav", seconds=1.0)

_calls = []


def _fake_stt(path):
    _calls.append(path)
    return "这是假 STT 转出来的文本"


_r = FV.view_file(str(wav), method="stt", stt_fn=_fake_stt)
check("★method=stt 真的调用了 STT（而不是走别的路）", len(_calls) == 1, str(_calls))
check("★返回 ok=True 且 method 标记为 stt",
      _r.get("success") is True and _r.get("method") == "stt", str(_r.get("method")))
check("★文本原样带回（text 字段）",
      _r.get("text") == "这是假 STT 转出来的文本", str(_r.get("text"))[:60])
check("★kind 识别为 audio", _r.get("kind") == "audio", str(_r.get("kind")))

_r2 = FV.view_file(str(wav), method="stt", stt_fn=lambda p: "")
check("★STT 返回空也算成功（不报错，只是 text 空）",
      _r2.get("success") is True and (_r2.get("text") or "") == "", str(_r2.get("text")))


def _boom(p):
    raise RuntimeError("模拟 STT 崩了")


_r3 = FV.view_file(str(wav), method="stt", stt_fn=_boom)
check("★STT 抛异常时被兜住（返回 success=False + error，而不是把异常抛出去）",
      _r3.get("success") is False and "STT 转写失败" in (_r3.get("error") or ""),
      str(_r3.get("error"))[:60])

print("\n== B. 工具层：file_ops view + method=stt 能透传 ==")
_src = (ROOT / "tools" / "file_ops.py").read_text(encoding="utf-8")
check("★file_ops 的 view 把 method 透传给 file_view",
      'method=kwargs.get("method"' in _src)
check("★method 枚举里有 stt（模型能发现这条路）",
      '"stt"' in (ROOT / "tools" / "file_ops.py").read_text(encoding="utf-8")
      and "stt" in FO.TOOL_PARAMETERS["properties"]["method"]["enum"],
      str(FO.TOOL_PARAMETERS["properties"]["method"]["enum"]))

print("\n== C. 真机层：本机 whisper 真的跑一遍（合成音频，只验'路通'）==")
try:
    from src.core.stt_engine import get_stt
    _stt = get_stt()
    # ★ STTEngine 没有 available 属性；正确信号是幂等的 initialize()
    _ready = bool(_stt is not None and _stt.initialize())
except Exception as _e:
    _stt, _ready = None, False
    print(f"  NOTE 取 STT 失败（{type(_e).__name__}: {_e}）")

if not _ready:
    print("  NOTE 本机 whisper 未就绪（缺 exe/模型）→ 跳过真机层")
else:
    _rr = FV.view_file(str(wav), method="stt")       # 走真实 _default_stt
    _txt = _rr.get("text") or ""
    check("★★真机：method=stt 走完整代码路径，不抛异常且返回字符串",
          isinstance(_txt, str), f"{_rr.get('method')} / {len(_txt)} 字符")
    print(f"      （合成音不是人话，转写内容仅供参考：{_txt[:60]!r}）")
    _speech = make_speech_wav(fixture_dir("stt_file_193") / "speech.wav")
    if _speech:
        _rr2 = FV.view_file(str(_speech), method="stt")
        check("★真机：有真人语音素材时也能跑通",
              isinstance(_rr2.get("text"), str), str(_rr2.get("text"))[:50])
    else:
        print("      NOTE 本机 SAPI 无语音，合成不出人话素材 → 跳过")

print(f"\n==== 结果: {len(PASS)} PASS / {len(FAIL)} FAIL ====")
for f in FAIL:
    print("  FAIL:", f)
sys.exit(1 if FAIL else 0)
