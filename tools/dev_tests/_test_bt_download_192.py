# -*- coding: utf-8 -*-
"""
★ 1.9.2 任务 10 回归：download 支持 BT / 磁力链
==============================================================================
用户批注：###下载工具升级后希望可以下载BT

★ 现状（必须先说清楚）：**本机没有 BT 引擎**。
  Motrix 的 BT 能力全来自 aria2，而 aria2 的二进制不在 Motrix 源码仓库里
  （只有 extra/aria2.conf 那份配置模板 + TS 侧的 RPC 适配层）。
  本机实测：aria2c 不在 PATH；libtorrent / aria2p / torrentp / torrentool /
  bencodepy 全都没装。纯 Python 自己实现 BT（bencode+tracker+DHT+peer wire）
  不现实。
  所以本套件测的是**所有不需要真引擎就能验的部分**，以及最重要的一条：
  **找不到引擎时不许擅自下载安装，要给可操作提示**（开发规矩第 5 条）。

测什么：
  A. BT 目标识别（磁力 / 32位与40位 infohash / .torrent / 反例）
  B. aria2c 定位顺序 + 找不到时返回空串（不抛异常）
  C. ★ 参数逐条对齐 Motrix 的 extra/aria2.conf（含它注释里的理由）
  D. 找不到引擎时的行为：明确报错 + 可操作提示 + **不联网下载**
  E. 任务记录与状态查询（用假进程，不真跑 aria2）
  F. download 工具的接线（BT 分支 / action / schema）
==============================================================================
"""
import json
import os
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE))
os.environ.setdefault("DEEPSEEK_API_KEY", "test")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PASS = FAIL = 0
FAILS = []


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        FAILS.append(name)
        print(f"  [FAIL] {name}  {extra}")


def head(t):
    print(f"\n== {t} ==")


from src.core import torrent_engine as BT                          # noqa: E402
from tools import download_tool as DL                              # noqa: E402

# ══════════════════════════════════════════════════════════════
head("A. BT 目标识别")
_BTIH40 = "c12fe1c06bba254a9dc9f519b335aa7c1367a88a"
_BTIH32 = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
check("★识别标准磁力链", BT.is_magnet(f"magnet:?xt=urn:btih:{_BTIH40}"))
check("★识别带 dn/tr 参数的磁力链",
      BT.is_magnet(f"magnet:?xt=urn:btih:{_BTIH40}&dn=abc&tr=udp://x:80"))
check("★大写 MAGNET: 也认（大小写不敏感）",
      BT.is_magnet(f"MAGNET:?xt=urn:btih:{_BTIH40}"))
check("★识别 .torrent 本地路径", BT.is_torrent_file(r"D:\x\a.torrent"))
check("★识别 .torrent 网址", BT.is_torrent_file("https://x/y.torrent"))
check("★.TORRENT 大写也认", BT.is_torrent_file("https://x/y.TORRENT"))
check("★_BTIH_RE 认得 40 位 hex",
      bool(BT._BTIH_RE.search(f"xt=urn:btih:{_BTIH40}"))
      and BT.magnet_infohash(f"magnet:?xt=urn:btih:{_BTIH40}") == _BTIH40)
check("★_BTIH_RE 对裸 hash 不匹配（要求 xt=urn:btih: 前缀，避免误判）",
      BT._BTIH_RE.search(_BTIH40) is None)
check("★_BTIH_RE 认得 32 位 base32",
      BT.magnet_infohash(f"magnet:?xt=urn:btih:{_BTIH32}") == _BTIH32.lower())
check("★取不到 infohash 时返回空串（不报错）",
      BT.magnet_infohash("magnet:?dn=abc") == "")
check("★is_bt_target 对普通 http 链接为假",
      not BT.is_bt_target("https://x/a.jpg"))
check("★is_bt_target 对普通图片链接为假（别把 .torrent 之外的后缀误判）",
      not BT.is_bt_target("https://x/a.torrent.jpg"))
check("★is_bt_target 对空串为假", not BT.is_bt_target(""))

# ══════════════════════════════════════════════════════════════
head("B. aria2c 定位（找不到就返回空串，不抛异常）")
_found = BT.find_aria2()
print(f"        本机 aria2c: {_found or '（没有）'}")
check("★find_aria2() 返回字符串", isinstance(_found, str))
check("★找不到时返回空串而不是抛异常（本机预期就是空）",
      _found == "" or Path(_found).exists(), _found)
check("★支持环境变量 PRTS_ARIA2C 指定（不用改代码）",
      "PRTS_ARIA2C" in (BASE / "src" / "core" / "torrent_engine.py")
      .read_text(encoding="utf-8"))
check("★本地优先目录是 data/tools（东西留在项目里）",
      str(BT.ARIA2_LOCAL_DIR).endswith(os.path.join("data", "tools")),
      str(BT.ARIA2_LOCAL_DIR))
# 造一个假的 aria2c 放进 data/tools，验证优先级真的生效
_fake_dir = BT.ARIA2_LOCAL_DIR
_fake_dir.mkdir(parents=True, exist_ok=True)
_fake = _fake_dir / ("aria2c.exe" if os.name == "nt" else "aria2c")
_created = False
if not _fake.exists():
    _fake.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    _created = True
try:
    _f2 = BT.find_aria2()
    check("★★data/tools 里放了 aria2c 就能被找到（优先于 PATH）",
          _f2 == str(_fake), f"{_f2} vs {_fake}")
finally:
    if _created:
        try:
            _fake.unlink()
        except Exception:
            pass
check("★拿走后又回到找不到（清理干净）", BT.find_aria2() == "" or not _created)

# ══════════════════════════════════════════════════════════════
head("C. ★ 参数逐条对齐 Motrix 的 extra/aria2.conf")
_args = BT.build_bt_args("/x/aria2c", f"magnet:?xt=urn:btih:{_BTIH40}",
                         Path("/tmp/dl"))
_joined = " ".join(_args)


def _has(flag):
    return any(a.startswith(flag) for a in _args)


check("★--seed-time=0（我们是桌宠不是做种机）", _has("--seed-time=0"))
check("★--bt-detach-seed-only=true（做种不占并发名额）",
      "--bt-detach-seed-only=true" in _args)
check("★--bt-hash-check-seed=true", "--bt-hash-check-seed=true" in _args)
check("★tracker 超时 10/10（aria2 默认 60 太慢，照 Motrix）",
      "--bt-tracker-connect-timeout=10" in _args
      and "--bt-tracker-timeout=10" in _args)
check("★--bt-stop-timeout=0（不因 0B/s 自动停，断网环境会误停）",
      "--bt-stop-timeout=0" in _args)
check("★--enable-peer-exchange=true（无 tracker 的种子靠 PEX）",
      "--enable-peer-exchange=true" in _args)
check("★★peer-id-prefix 与 peer-agent 成对且一致（不一致会被私有 tracker 当冒充）",
      "--peer-id-prefix=-TR3000-" in _args
      and "--peer-agent=Transmission/3.00" in _args)
check("★DHT 引导节点用的是 Transmission 那两个",
      "--dht-entry-point=dht.transmissionbt.com:6881" in _args
      and "--dht-entry-point6=dht.transmissionbt.com:6881" in _args)
check("★--content-disposition-default-utf8=true（中文种子名不乱码）",
      "--content-disposition-default-utf8=true" in _args)
check("★--no-file-allocation-limit=64M（小文件跳过预分配）",
      "--no-file-allocation-limit=64M" in _args)
check("★--max-file-not-found=10（容忍暂时故障又不无限重试）",
      "--max-file-not-found=10" in _args)
check("★--follow-torrent=true", "--follow-torrent=true" in _args)
check("★--bt-save-metadata=true（磁力先落 .torrent 便于复用）",
      "--bt-save-metadata=true" in _args)
check("★--continue=true（断点续传）", "--continue=true" in _args)
check("★目标在最后一位（aria2 要求）", _args[-1].startswith("magnet:?"))
check("★save_dir 传对了", any(a.startswith("--dir=") for a in _args))
check("★可选参数：不传 listen_port 就不出现该 flag", not _has("--listen-port"))
_a2 = BT.build_bt_args("aria2c", "x.torrent", Path("/d"), listen_port=6881,
                       extra_trackers="udp://a:80,udp://b:80")
check("★传了 listen_port / extra_trackers 才出现",
      "--listen-port=6881" in _a2
      and "--bt-tracker=udp://a:80,udp://b:80" in _a2)
check("★seed_time 可覆盖（默认 0，可给做种时长）",
      "--seed-time=60" in BT.build_bt_args("a", "t", Path("/d"), seed_time=60))

# 源码模式：注释里要留着"为什么照 Motrix 抄"的理由
_src = (BASE / "src" / "core" / "torrent_engine.py").read_text(encoding="utf-8")
check("★注释点名了 Motrix 的 extra/aria2.conf", "extra/aria2.conf" in _src)
check("★注释保留了 Motrix 原话理由（私有 tracker 封冒充 / 50-tracker 扫描）",
      "私有 tracker" in _src and "50-tracker" in _src)

# ══════════════════════════════════════════════════════════════
head("D. ★★ 找不到引擎时：明确报错 + 可操作提示 + 不擅自下载")
_hint = BT.aria2_hint()
check("★提示里给了 3 种可操作办法", "①" in _hint and "②" in _hint and "③" in _hint)
check("★★提示点名了推荐做法（放进 data/tools）",
      "data" in _hint and "tools" in _hint)
check("★★明说不会自己联网装（安装新依赖要先问用户）",
      "要先问你" in _hint or "我不会自己联网" in _hint)
check("★提示里给了环境变量这条路", "PRTS_ARIA2C" in _hint)
_src_all = _src
check("★★源码里没有任何「自动下载 aria2c」的代码",
      "aria2c.exe\").write" not in _src_all
      and "urlretrieve" not in _src_all
      and "wget" not in _src_all)

if not BT.find_aria2():
    _r = BT.start_bt(f"magnet:?xt=urn:btih:{_BTIH40}", Path("data/downloads/_t"))
    check("★★没有引擎时 start_bt 返回失败（不是假装成功）",
          _r.get("success") is False, str(_r)[:120])
    check("★★失败原因里带可操作提示", "aria2c" in _r.get("error", ""),
          _r.get("error", "")[:80])
    check("★标了 bt=True（让调用方能区分）", _r.get("bt") is True)
else:
    check("★本机有 aria2c，跳过「找不到」分支", True)

# 工具层面对 BT 链接的反应
_r2 = json.loads(DL.execute(url=f"magnet:?xt=urn:btih:{_BTIH40}"))
check("★★download 工具收到磁力链：没引擎时明确报错，不吞掉",
      _r2.get("success") is False and _r2.get("bt") is True, str(_r2)[:120])
_r3 = json.loads(DL.execute(url="https://x/a.jpg.torrent.jpg",
                            bucket="_test_bt_192"))
check("★类似但非种子的链接仍走普通下载（不误判成 BT）",
      _r3.get("bt") is None, str(_r3.get("error"))[:60])

# ══════════════════════════════════════════════════════════════
head("E. 任务记录与状态查询（用假进程，不真跑 aria2）")


class _FakeProc:
    def __init__(self, rc=None):
        self._rc = rc
        self.stdout = iter(["[#1] 1MiB/10MiB\n", "[#1] 5MiB/10MiB\n"])

    def poll(self):
        return self._rc

    def wait(self, timeout=None):
        return self._rc

    def terminate(self):
        self._rc = 0


BT._jobs.clear()
BT._jobs["bt-test-1"] = {"id": "bt-test-1", "target": "magnet:?xt=x",
                         "dir": "/d", "proc": _FakeProc(None),
                         "started": BT.time.time() - 5,
                         "infohash": "abc", "engine": "aria2c"}
_s = BT.job_status()
check("★job_status() 能列出任务", len(_s.get("jobs", [])) == 1, str(_s)[:120])
check("★运行中显示「下载中」", "下载中" in _s["jobs"][0]["state"],
      _s["jobs"][0]["state"])
check("★有 job_id / dir / 用时",
      all(k in _s["jobs"][0] for k in ("job_id", "dir", "seconds", "infohash")))
_sj = BT.job_status("bt-test-1")
check("★按 job_id 单查也行", len(_sj.get("jobs", [])) == 1)
check("★查不存在的 job 返回失败且给原因",
      BT.job_status("nope").get("success") is False)
BT._jobs["bt-test-1"]["proc"] = _FakeProc(0)
check("★结束后显示退出码", "退出码 0" in BT.job_status()["jobs"][0]["state"],
      BT.job_status()["jobs"][0]["state"])
check("★stop_bt 能停", BT.stop_bt("bt-test-1").get("success") is True)
check("★stop 不存在的任务返回失败",
      BT.stop_bt("nope").get("success") is False)
BT._jobs.clear()
check("★没有任务时 job_status 说明「当前没有」",
      BT.job_status()["jobs"] == [] and "没有" in BT.job_status()["message"])

# 工具层的 action
_r4 = json.loads(DL.execute(url="", action="bt_status"))
check("★action=bt_status 能用", _r4.get("success") is True, str(_r4)[:100])
_r5 = json.loads(DL.execute(url="", action="bt_engine"))
check("★action=bt_engine 能用且报告引擎状态",
      _r5.get("success") is True and "found" in _r5, str(_r5)[:120])
_r6 = json.loads(DL.execute(url="", action="bt_stop"))
check("★action=bt_stop 缺 job_id 时明确报错",
      _r6.get("success") is False and "job_id" in _r6.get("error", ""))

# ══════════════════════════════════════════════════════════════
head("F. download 工具的 schema 接线")
_props = DL.TOOL_PARAMETERS["properties"]
check("★schema 里有 action", "action" in _props)
check("★action 枚举含 bt_status / bt_stop / bt_engine",
      {"bt_status", "bt_stop", "bt_engine"} <= set(_props["action"]["enum"]),
      str(_props["action"].get("enum")))
check("★schema 里有 job_id", "job_id" in _props)
check("★url 说明提到了磁力链与 .torrent",
      "magnet" in _props["url"]["description"]
      and ".torrent" in _props["url"]["description"])
check("★TOOL_DESCRIPTION 说明了 BT 能力",
      "BT" in DL.TOOL_DESCRIPTION and "magnet" in DL.TOOL_DESCRIPTION)
check("★url 仍是唯一必填", DL.TOOL_PARAMETERS["required"] == ["url"])
check("★BT 下载落在 <bucket>/bt 子目录（不和直链混）",
      '"bt"' in (BASE / "tools" / "download_tool.py").read_text(encoding="utf-8"))

# 清掉测试可能留下的目录
for _p in (BASE / "data" / "downloads" / "_test_bt_192",
           BASE / "data" / "downloads" / "_t"):
    try:
        if _p.exists():
            for _f in _p.rglob("*"):
                if _f.is_file():
                    _f.unlink()
            _p.rmdir()
    except Exception:
        pass

print("\n" + "=" * 60)
print(f"★ 1.9.2 任务10 BT 下载：{PASS} PASS / {FAIL} FAIL")
for f in FAILS:
    print(f"  FAIL: {f}")
print("=" * 60)
sys.exit(1 if FAIL else 0)
