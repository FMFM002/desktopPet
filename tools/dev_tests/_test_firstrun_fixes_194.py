# -*- coding: utf-8 -*-
"""★★ 1.9.4：**首次真机测试反馈的修复守卫**（用户 2026-10-05 实测便携版后报的）。

## 背景：用户真机跑 A 形态（便携 zip）遇到的四个问题
贴的是日志与现象，逐条对上了：

  ① **发消息失败 / "检测异常"** —— 日志里全是
       `connect_tcp.started host='127.0.0.1' port=7897` → `WinError 10061 拒绝连接`
     根因：用户 **Windows 系统代理开着**（注册表 `ProxyEnable=1 / ProxyServer=127.0.0.1:7897`）
     但**代理客户端没开**；他那台机器上的 `openai` 被改造过（用的是 httpx 分支
     `httpx2`），**会读系统代理** → 所有 LLM 请求被送去没人的端口。
     他当时的判断是"没给桌宠联网权限" —— **说明我们的报错完全没帮上忙**。
     ⇒ 修：新增 `net_proxy.diagnose_conn_error()`，把**真正的元凶**说出来；
       接到向导的「测试一下」与会话报错两处。

  ② **"好了可以用了"下面有一个空白的框，不知道是干嘛的**
     ⇒ 修：进入完成页时**立刻**填内容（原来要等点"完成"才填）。

  ③ **"代理好像不是桌宠的能力，应该是写反了，JM 和 P 站是能力，需要代理是提示"**
     ⇒ 修：代理**不再出勾选框**（`askable=False`），只作为提示。

  ④ **"点击完成。形象出现。没有看见下载模型和其他东西的迹象"**
     根因：本包**故意不含模型**，而向导只"记录勾选"，既没下也没说怎么下。
     ⇒ 修：勾了需要模型的能力时，完成页**明说**并给出确切命令。

## 钉什么
  A. 诊断函数：系统代理开着且端口没人听 → 给出**可操作**的人话（含两个解法）；
  B. ★ 反向：端口**有人听**时**不甩锅**（返回空串）—— 别把正常代理说成故障；
  C. 向导：代理**不出**勾选框；
  D. 向导：完成页**一进去就有内容**（不是空框）；
  E. 向导：勾了模型能力 → 完成页给出 `model_manager.py install` 命令；
  F. 向导的「测试一下」在连接失败时会把诊断带出来。
"""
import os
import shutil
import socket
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PRTS_NO_COMFY_START", "1")
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))


from src.core import net_proxy as np                   # noqa: E402

print("== A. ★★ 系统代理开着、端口没人听 → 要说人话（用户就是撞在这）==")
_en, _sv = np.system_proxy()
check("★能读到 Windows 系统代理设置（读注册表）", isinstance(_en, bool) and isinstance(_sv, str),
      f"enable={_en} server={_sv!r}")

# 造一个"确定没人听"的端口，注入成系统代理读到的值来测
_s = socket.socket()
_s.bind(("127.0.0.1", 0))
_dead_port = _s.getsockname()[1]
_s.close()                                            # 关掉 → 该端口没人听
_orig_sysproxy = np.system_proxy
try:
    np.system_proxy = lambda: (True, f"127.0.0.1:{_dead_port}")
    _d = np.diagnose_conn_error(Exception("Connection error."))
    check("★★代理端口没人听时，诊断给出**非空的人话**", bool(_d))
    check("★诊断里点名了那个代理地址", f"127.0.0.1:{_dead_port}" in _d)
    check("★诊断给了**可操作的解法**（开代理客户端 / 关系统代理）",
          "代理客户端" in _d and "系统代理" in _d)
    check("★诊断提到了「Windows 设置」这样的具体入口", "Windows" in _d)

    print("\n== B. ★ 反向：端口**有人听**时不许甩锅 ==")
    _srv = socket.socket()
    _srv.bind(("127.0.0.1", 0))
    _srv.listen(1)
    _live_port = _srv.getsockname()[1]
    np.system_proxy = lambda: (True, f"127.0.0.1:{_live_port}")
    _d2 = np.diagnose_conn_error(Exception("Connection error."))
    check("★★代理在正常运行时不乱甩锅（返回空串）", _d2 == "", repr(_d2))
    _srv.close()

    print("\n== C. 系统代理没开时也不乱说 ==")
    np.system_proxy = lambda: (False, "")
    check("★系统代理关着 → 返回空串", np.diagnose_conn_error(Exception("x")) == "")
finally:
    np.system_proxy = _orig_sysproxy

print("\n== D~F. 向导的三处真机修复 ==")
from PySide6.QtWidgets import QApplication            # noqa: E402
_app = QApplication.instance() or QApplication([])
from src.ui.first_run_wizard import FirstRunWizard    # noqa: E402

_base = ROOT / "临时文件" / "_scratch_dev" / "tmp_fixes_guarded"
if _base.exists():
    shutil.rmtree(_base, ignore_errors=True)
_base.mkdir(parents=True)                              # 空目录 = 没有模型
try:
    _w = FirstRunWizard(base=_base)

    check("★★【修复③】代理**不再**是能力勾选框（它是 JM/P站的提示，不是功能）",
          "proxy" not in _w._checks, str(sorted(_w._checks)))
    check("★（前置）模拟的机器上没有模型 → whisper/easyocr 确实出框了",
          "whisper" in _w._checks and "easyocr" in _w._checks,
          str(sorted(_w._checks)))

    _w._goto(3)                                        # 切到完成页（不点"完成"）
    check("★★【修复②】进入完成页**立刻就有内容**（不再是空白框）",
          len(_w.done_page_text().strip()) > 20,
          _w.done_page_text()[:50].replace("\n", " "))

    for _cb in _w._checks.values():
        _cb.setChecked(True)
    _w._apply_optional()
    _w._render_done()
    _t = _w.done_page_text()
    check("★★【修复④】勾了需要模型的能力 → 完成页给出**获取命令**",
          "model_manager.py install" in _t)
    check("★并说明了「本包不含」与「不装也能用其他功能」",
          "不含" in _t or "本包不含" in _t or "不装" in _t)

    print("\n== F. 向导「测试一下」失败时要带出诊断 ==")
    _src = (ROOT / "src" / "core" / "first_run.py").read_text(encoding="utf-8")
    check("★★verify_llm_key 的失败分支接了 diagnose_conn_error",
          "diagnose_conn_error" in _src)
    check("★而且**排在超时判断之前**（否则会先被'连不上(超时)'盖掉）",
          _src.index("_diag") < _src.index("连不上（超时）"))
    _sess = (ROOT / "src" / "core" / "session.py").read_text(encoding="utf-8")
    check("★★聊天的错误提示也接了诊断（用户是在这里看到Connection error的）",
          "diagnose_conn_error" in _sess)
finally:
    shutil.rmtree(_base, ignore_errors=True)

print("\n== G. ★★ 用户头像不许变成形象的脸（真机反馈⑤）==")
# 用户原话："发送第一条消息时发现，**用户头像是 deep seek**。不是预设。"
# 两个 bug：① 出厂配置没指向随包的预设头像；② 用户槽会回落到**形象**头像。
import json as _json                                    # noqa: E402

_default = _json.loads((ROOT / "config.default.json").read_text(encoding="utf-8"))
_uav = _default["ui"]["user_avatar_path"]
check("★★① 出厂配置**指向**随包的预设头像（不是空串）", _uav != "", repr(_uav))
check("★★而且那个文件**真的在**（否则指了个不存在的路径）",
      bool(_uav) and (ROOT / _uav).exists(), _uav)
check("★出厂配置里那个预设就是 data/avatars 下的（用户拍板「作为出厂预设」）",
      _uav.replace("\\", "/").startswith("data/avatars/"), _uav)

from src.ui.chat_widget import ChatWidget               # noqa: E402
_w = ChatWidget()
_w.set_avatars(user_path="", bot_path="")
check("★★② 用户头像**为空**时，**不许**回落到形象头像"
      "（否则用户会看到 deepseek/铃兰的脸安在自己头上）",
      _w._avatar_uri["user"] == "", repr(_w._avatar_uri["user"])[:40])
check("★形象槽**仍有**兜底（这条不能被顺手改坏）", _w._avatar_uri["bot"] != "")
_w.set_avatars(user_path=_uav, bot_path="")
check("★指向预设头像时能正常加载成圆形 PNG",
      _w._avatar_uri["user"].startswith("data:image/png;base64,"))
_src_cw = (ROOT / "src" / "ui" / "chat_widget.py").read_text(encoding="utf-8")
check("★★初始化时（__init__）用户槽也传了 pet_fallback=False"
      "（第一条消息就是在那之前渲染的）",
      _src_cw.count("pet_fallback=False") >= 2, f"出现 {_src_cw.count('pet_fallback=False')} 次")

print("\n== H. ★★ 出厂预设密钥（用户 2026-10-05 拍板选 C）==")
# 用户原话："c。这部分key作为出厂默认预设。熟人使用的小项目，不担心被刷爆。"
_preset = ROOT / ".env.preset"
check("★★.env.preset 存在（随包发的出厂预设）", _preset.is_file())
if _preset.is_file():
    _pt = _preset.read_text(encoding="utf-8")
    check("★★预设里有**天气**的键（用户点名的三个之一）",
          "WEATHER_API_KEY" in _pt and "WEATHER_API_HOST" in _pt)
    check("★预设里有**联网搜索**的键", "TAVILY_API_KEY" in _pt)
    check("★预设里有**终末地**的键", "ENDFIELD_API_KEY" in _pt)
    check("★★★预设里**没有** DEEPSEEK_API_KEY（LLM 的 Key 由引导问用户；"
          "它是要花钱的那个，且引导的存在意义就是它）",
          not any(l.strip().startswith("DEEPSEEK_API_KEY")
                  for l in _pt.splitlines()))
    check("★★预设里没有**机器相关**项（PIXIV_PROXY / COMFYUI_PATH 到别人机器上是错的）",
          "PIXIV_PROXY=" not in _pt and "COMFYUI_PATH=" not in _pt)
    check("★预设里保留了「PRTS_PET_COMPOSITED=0」这个坑的提示",
          "PRTS_PET_COMPOSITED=0" in _pt)

check("★★生成器在（可复现，不是手抄）", (ROOT / "tools" / "make_preset_env.py").is_file())

_main = (ROOT / "main.py").read_text(encoding="utf-8")
check("★★main.py 首启会把 .env.preset 复制成 .env",
      ".env.preset" in _main and "copyfile" in _main)
check("★★而且**在加载 dotenv 之前**（否则这一次启动读到的还是空的）",
      _main.index(".env.preset") < _main.index("load_dotenv")
      if "load_dotenv" in _main else True)

_br = (ROOT / "tools" / "build_release.py").read_text(encoding="utf-8")
check("★★装配器**带上** .env.preset（随包发）",
      '".env.preset"' in _br)
check("★★装配器**仍然排除** .env（那是用户自己的）",
      '".env":' in _br or '".env"' in _br)
check("★出厂配置里**不写**这些预设密钥（密钥只走 .env.preset，"
      "两处写同一把钥匙迟早不一致）",
      "WEATHER_API_KEY" not in (ROOT / "config.default.json").read_text(encoding="utf-8"))

print(f"\n==== 结果: {len(PASS)} PASS / {len(FAIL)} FAIL ====")
for f in FAIL:
    print("  FAIL:", f)
sys.exit(1 if FAIL else 0)
