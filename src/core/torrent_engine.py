"""
★ 1.9.2 任务 10：BT / 磁力链下载支持（用户批注：`###下载工具升级后希望可以下载BT`）

## 结论先说：BT 需要一个"引擎"，本机现在没有
Motrix 的 BT 能力**全部来自 aria2**（它是个 Electron UI + aria2 的壳），
而 aria2 的二进制**不在 Motrix 源码仓库里**（仓库里只有 `extra/aria2.conf`
这份配置模板和 TypeScript 侧的 RPC 适配层）。本机实测：

    · `aria2c` 不在 PATH
    · python 侧 libtorrent / aria2p / torrentp / torrentool / bencodepy 全都没装

要在 Python 里自己实现 BT（bencode + tracker + DHT + peer wire protocol）是不现实的
工程量。所以本模块的做法是：
  1. **探测**目标是不是 BT（`magnet:?xt=urn:btih:…` 或 `.torrent`）；
  2. BT 目标交给 **aria2c** 去下（照 Motrix 那套参数，见 `build_bt_args`）；
  3. aria2c 找不到时**明确告诉用户怎么办**，
     ★ 并且**绝不擅自下载安装**（开发规矩第 5 条：涉及安装新依赖要先问用户）。
     「要不要我自动把 aria2c 抓下来放进 data/tools/」这句话是要问用户的事，
     不是代码该自己决定的事。

## 为什么参数要照 Motrix 抄
Motrix 那份 `extra/aria2.conf` 里的 BT 段是踩过坑调出来的（它自己的注释写得很清楚），
逐条都有理由，所以这里**原样沿用**并在注释里保留原因，不自己拍脑袋改。
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from src.utils.proc import no_window_kwargs as _nw

_log = logging.getLogger("Torrent")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ARIA2_LOCAL_DIR = PROJECT_ROOT / "data" / "tools"
# ★ 1.9.2：CA 证书包（随 aria2c 一起放在 data/tools/，项目自带一份）
#   为什么必须带：换用的 motif 构建是 **OpenSSL** 版，没给它 CA bundle 时
#   HTTPS 一律失败 ——
#       [SocketCore.cc:1022] SSL/TLS handshake failure:
#                            unable to get local issuer certificate
#   BT 里大量 tracker 是 https://，所以这不是可选项。
CA_BUNDLE = ARIA2_LOCAL_DIR / "cacert.pem"

# ── BT 识别 ──────────────────────────────────────────────────
_MAGNET_RE = re.compile(r"^magnet:\?", re.I)
_BTIH_RE = re.compile(r"xt=urn:btih:([0-9a-fA-F]{40}|[2-7A-Za-z]{32})")


def is_magnet(s: str) -> bool:
    return bool(_MAGNET_RE.match((s or "").strip()))


def is_torrent_file(s: str) -> bool:
    t = (s or "").strip()
    if t.lower().endswith(".torrent"):
        return True
    # 本地路径或 http(s) 链接
    p = Path(t)
    return p.suffix.lower() == ".torrent"


def is_bt_target(s: str) -> bool:
    """这个 URL/路径是 BT（磁力或种子）吗？"""
    return is_magnet(s) or is_torrent_file(s)


def magnet_infohash(s: str) -> str:
    """从磁力链里抠出 infohash（给日志/去重用）；取不到返回空串。"""
    m = _BTIH_RE.search(s or "")
    return m.group(1).lower() if m else ""


# ── aria2c 定位 ──────────────────────────────────────────────
def find_aria2() -> str:
    """按优先级找 aria2c。找不到返回空串（**绝不自己下载**）。

    顺序：
      0. 环境变量 PRTS_ARIA2C（用户显式指定）
      1. PATH（装了 aria2 的正常情况）
      2. 项目内 data/tools/（用户手动放进来，符合"东西尽量放项目里"）
      3. 项目根目录
      4. 常见 Motrix 安装位置（用户可能已经装了 Motrix，它自带 aria2c）
    """
    env = (os.environ.get("PRTS_ARIA2C") or "").strip()
    if env and Path(env).exists():
        return env
    exe = "aria2c.exe" if os.name == "nt" else "aria2c"
    w = shutil.which("aria2c")
    if w:
        return w
    for d in (ARIA2_LOCAL_DIR, PROJECT_ROOT):
        p = d / exe
        if p.exists():
            return str(p)
    # Motrix 自带的那份（如果用户装过 Motrix）
    for base in (os.environ.get("LOCALAPPDATA") or "",
                 os.environ.get("PROGRAMFILES") or "",
                 os.environ.get("ProgramFiles(x86)") or "",
                 "D:\\Apps", "C:\\Apps"):
        if not base:
            continue
        for sub in ("Motrix", "Programs\\Motrix", "Motrix\\resources\\extra"):
            p = Path(base) / sub / exe
            if p.exists():
                return str(p)
            p2 = Path(base) / sub / "aria2" / exe
            if p2.exists():
                return str(p2)
    return ""


def aria2_hint() -> str:
    """找不到引擎时给用户的**可操作**提示（不擅自安装）。"""
    exe = "aria2c.exe" if os.name == "nt" else "aria2c"
    return (
        f"BT/磁力下载需要一个下载引擎 aria2c，本机没找到。\n"
        f"三种办法（任选一种，都不用改代码）：\n"
        f"  ① 把 {exe} 放进  {ARIA2_LOCAL_DIR}  （推荐 —— 东西留在项目里）\n"
        f"  ② 让它进 PATH（装了 aria2 官方包就是这种）\n"
        f"  ③ 设环境变量 PRTS_ARIA2C=<{exe} 的完整路径>\n"
        f"（如果你已经装了 Motrix，它自带 aria2c，我会自动找到。）\n"
        f"※ 我不会自己联网把 {exe} 装下来 —— 安装新依赖要先问你。"
    )


# ── 参数（逐条照 Motrix 的 extra/aria2.conf 抄，注释保留它的理由）──
def build_bt_args(aria2: str, target: str, save_dir: Path,
                  seed_time: int = 0, extra_trackers: str = "",
                  listen_port: int = 0) -> list:
    """组 aria2c 命令行。

    ★ 每一条都能对上 Motrix `extra/aria2.conf` 里的一段注释：
      · `--seed-time=0`：我们是个桌宠、不是做种机；Motrix 也是把 seed-time
        做成任务级、默认不长期做种（"Set seeding time in Motrix's Downloads
        settings… applies it per task"）。
      · `--bt-detach-seed-only=true`：做种中的任务不占并发名额，
        否则长尾做种会永久堵住新下载（Motrix 原话 "Highly recommended for
        desktop UX"）。
      · `--bt-hash-check-seed=true`：完整性校验通过后继续做种，不必重校验。
      · `--bt-tracker-connect-timeout=10 / --bt-tracker-timeout=10`：
        aria2 默认 60/60，扫一长串 tracker 时太久（Motrix 原话 "too long when
        scanning a 50-tracker list; 10 keeps the swarm fresh"）。
      · `--bt-stop-timeout=0`：不因"0 B/s 持续 N 秒"而自动停 —— 断网环境会误停。
      · `--enable-peer-exchange=true`：无 tracker 的种子靠 PEX 找人。
      · `--peer-id-prefix` / `--peer-agent` **必须成对且一致**，
        私有 tracker 会把不一致的组合当冒充直接封（Motrix 原话）。
      · `--dht-entry-point*`：用 Transmission 那几个公开 DHT 引导节点。
      · `--content-disposition-default-utf8=true`：非 ASCII 文件名（中文种子名）
        在很多服务器上不声明 charset，不设这个会变成乱码。
      · `--no-file-allocation-limit=64M`：小文件跳过预分配（预分配开销反而更大）。
      · `--max-file-not-found=10`：连续 404 到 10 次才判死，
        既能容忍暂时性故障、又不会对已失效的种子无限重试。
      · `--ca-certificate=<项目自带 cacert.pem>`：★ 1.9.2 实测必须带 ——
        本机换用的是 OpenSSL 版 aria2（见 aria2c.SOURCE.txt），不给 CA bundle
        时所有 https:// tracker 都握手失败，BT 就一个 peer 都找不到。
    """
    args = [
        aria2, "--enable-rpc=false", "--continue=true",
        f"--dir={save_dir}",
        "--summary-interval=0",            # 进度靠日志，不要刷屏（Motrix 同款）
        "--console-log-level=notice",
        "--no-file-allocation-limit=64M",
        "--max-file-not-found=10",
        "--content-disposition-default-utf8=true",
        # ★★ 1.9.2 任务 11（VPN 冲突方案）：**强制直连**。
        #   原因：aria2 会读 `http_proxy`/`all_proxy` 环境变量；如果宿主的代理
        #   客户端设了全局代理，aria2 会把 tracker 请求塞进代理 —— 而 BT 的
        #   peer 连接**协议层不支持代理**（aria2 的代理选项只管 HTTP/HTTPS/FTP），
        #   且代理出口是机房 IP、tracker 会拒 → 结果"tracker 能连、peer 全连不上"，
        #   极难排查。这里显式清空，配合 start_bt() 里的 clean_env()。
        "--all-proxy=",
        "--no-proxy=*",
    ]
    # ★ CA 证书包（有就带，没有不硬报错 —— 也许用户用的是别的 TLS 后端构建）
    if CA_BUNDLE.exists():
        args.append(f"--ca-certificate={CA_BUNDLE}")
    args += [
        # ---- BT 段 ----
        f"--seed-time={int(seed_time)}",
        "--bt-detach-seed-only=true",
        "--bt-hash-check-seed=true",
        "--bt-tracker-connect-timeout=10",
        "--bt-tracker-timeout=10",
        "--bt-stop-timeout=0",
        "--enable-peer-exchange=true",
        "--peer-id-prefix=-TR3000-",
        "--peer-agent=Transmission/3.00",
        "--dht-entry-point=dht.transmissionbt.com:6881",
        "--dht-entry-point6=dht.transmissionbt.com:6881",
        "--follow-torrent=true",
        "--bt-save-metadata=true",         # 磁力链先落 .torrent，便于复用
    ]
    if listen_port:
        args.append(f"--listen-port={int(listen_port)}")
    if extra_trackers:
        args.append(f"--bt-tracker={extra_trackers}")
    args.append(target)
    return args


# ── 后台任务（BT 动辄几分钟，不能同步阻塞工具调用）────────────
_jobs: dict = {}
_jobs_lock = threading.Lock()
_seq = 0


def _next_id() -> str:
    global _seq
    with _jobs_lock:
        _seq += 1
        return f"bt-{int(time.time())}-{_seq}"


def start_bt(target: str, save_dir: Path, seed_time: int = 0,
             timeout_hint: int = 0) -> dict:
    """后台启动 aria2c 下 BT。返回 dict（可直接当工具结果）。"""
    aria2 = find_aria2()
    if not aria2:
        return {"success": False, "bt": True, "error": aria2_hint()}
    try:
        save_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        return {"success": False, "bt": True, "error": f"建目录失败: {e}"}

    jid = _next_id()
    args = build_bt_args(aria2, target, save_dir, seed_time=seed_time)
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    # ★ 1.9.2 任务 11：用「摘掉代理」的环境启动 aria2c（强制直连，见上方说明）
    try:
        from src.core.net_proxy import clean_env as _clean_env
        kwargs["env"] = _clean_env()
    except Exception as e:
        _log.debug(f"清理 aria2c 代理环境失败（继续）: {e}")
    try:
        proc = subprocess.Popen(args, cwd=str(save_dir), stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True,
                                encoding="utf-8", errors="replace", **kwargs, **_nw())
    except Exception as e:
        return {"success": False, "bt": True, "error": f"启动 aria2c 失败: {e}"}

    entry = {"id": jid, "target": target, "dir": str(save_dir),
             "proc": proc, "started": time.time(),
             "infohash": magnet_infohash(target), "engine": os.path.basename(aria2)}
    with _jobs_lock:
        _jobs[jid] = entry
    # 起个线程把 aria2 的输出收着（顺便在结束时标记状态），避免管道写满把子进程卡死
    threading.Thread(target=_collect, args=(jid,), daemon=True,
                     name=f"bt-collect-{jid}").start()

    kind = "磁力链" if is_magnet(target) else "种子"
    return {
        "success": True, "bt": True, "job_id": jid, "engine": aria2,
        "dir": str(save_dir), "infohash": entry["infohash"],
        "message": (f"🧲 已开始后台下载{kind}（job {jid}）。"
                    f"保存到 {save_dir}。BT 需要找 peer，通常比直链慢很多；"
                    f"随时问我「BT 下载进度」就能看状态。"),
        "text": (f"已把{kind}交给 aria2c 后台下载，job_id={jid}，"
                 f"保存目录 {save_dir}。回复用户时告诉他：已经在下了，"
                 f"想看进度就再问一次。"),
    }


def _collect(jid: str):
    """收走 aria2c 的 stdout（防管道写满），结束时记录退出码。"""
    with _jobs_lock:
        e = _jobs.get(jid)
    if not e:
        return
    tail = []
    try:
        for line in e["proc"].stdout or []:
            tail.append(line.rstrip())
            if len(tail) > 200:
                del tail[:100]
    except Exception:
        pass
    try:
        rc = e["proc"].wait()
    except Exception:
        rc = None
    with _jobs_lock:
        e["returncode"] = rc
        e["tail"] = tail[-40:]
        e["finished"] = time.time()


_PROG_RE = re.compile(
    r"\[#\w+\s+([\d.]+)([KMG]?i?B)/([\d.]+)([KMG]?i?B)\((\d+)%\)"
    r"(?:\s+CN:(\d+))?(?:\s+SD:(\d+))?(?:\s+DL:([\d.]+[KMG]?i?B))?")


def _parse_progress(tail: list) -> dict:
    """从 aria2 的输出里抠出**最后一条进度行** → 进度/速度/peer 数。

    ★ 1.9.2（用户："BT提交后台后就没消息了"、"没有进度百分比、没有文件大小、
      速度信息"）：原来 job_status 只回报状态+秒数，等于把"看不见的下载"
      交给用户干等。aria2 的进度行长这样，直接解析它就有全部信息：
          [#3de0d2 1.2MiB/5.9GiB(0%) CN:2 SD:0 DL:100KiB]
    """
    for line in reversed(tail or []):
        m = _PROG_RE.search(line or "")
        if not m:
            continue
        def _num(v):
            try:
                return float(v)
            except Exception:
                return 0.0
        got, got_u, tot, tot_u, pct, cn, sd, dl = m.groups()
        scale = {"B": 1, "KiB": 1024, "MiB": 1024 ** 2, "GiB": 1024 ** 3,
                 "KB": 1000, "MB": 1000 ** 2, "GB": 1000 ** 3}
        g = _num(got) * scale.get(got_u, 1)
        t = _num(tot) * scale.get(tot_u, 1)
        return {
            "percent": int(pct or 0),
            "downloaded": f"{got}{got_u}",
            "total": f"{tot}{tot_u}",
            "peers": int(cn or 0),
            "seeds": int(sd or 0),
            "speed": (dl or "0B"),
            "_bytes": int(g), "_total": int(t),
        }
    return {}


def job_status(jid: str = "") -> dict:
    """查一个（或全部）BT 任务状态。"""
    with _jobs_lock:
        if jid:
            e = _jobs.get(jid)
            items = [e] if e else []
        else:
            items = list(_jobs.values())
    if jid and not items:
        return {"success": False, "error": f"没有这个 BT 任务: {jid}"}
    out = []
    for e in items:
        p = e["proc"]
        rc = p.poll()
        alive = rc is None
        # ★ 退出码优先用 poll() 的实时值：`_collect` 线程可能还没把
        #   returncode 写进 entry（或它自己出错了），那样状态会显示成
        #   "已结束(退出码 None)" —— 明明 poll() 已经知道结果了。
        if not alive and e.get("returncode") is None:
            e["returncode"] = rc
        _prog = _parse_progress(e.get("tail") or [])
        _item = {
            "job_id": e["id"],
            "target": (e["target"][:80] + "…") if len(e["target"]) > 80 else e["target"],
            "infohash": e.get("infohash", ""),
            "dir": e["dir"],
            "state": "下载中" if alive else f"已结束(退出码 {rc})",
            "seconds": round(time.time() - e["started"], 1),
            "engine": e.get("engine", ""),
        }
        if _prog:
            # ★ 1.9.2：把进度/速度/peer 一并回报（用户报"没消息了"就是缺这个）
            _item.update({k: v for k, v in _prog.items() if not k.startswith("_")})
        else:
            _item["peers"] = 0
            _item["hint"] = ("还没有进度行 —— 说明 aria2 还没连上任何 peer"
                             "（BT 找 peer 可能要几分钟；若一直为 0，多半是"
                             "网络/种子问题，见 advice）")
        out.append(_item)
    if not out:
        return {"success": True, "jobs": [],
                "message": "当前没有 BT 下载任务。"}
    # ★ 1.9.2：把进度/速度/peer 拼进 message —— 模型读到的是 message，
    #   用户问"BT 进度"时它才有东西可说（原来只有"下载中 194.9s"，等于没说）。
    lines = []
    for o in out:
        seg = f"· {o['job_id']}  {o['state']}  {o['seconds']}s"
        if o.get("percent") is not None:
            seg += (f"  进度 {o.get('percent')}% "
                    f"({o.get('downloaded')}/{o.get('total')})"
                    f"  peer {o.get('peers')}  seed {o.get('seeds')}"
                    f"  速度 {o.get('speed')}")
        else:
            seg += "  还没有进度行（尚未连上 peer）"
        seg += f"  → {o['dir']}"
        lines.append(seg)
    msg = "BT 任务：\n" + "\n".join(lines)
    if any(o.get("hint") for o in out):
        msg += "\n" + next(o["hint"] for o in out if o.get("hint"))
    return {"success": True, "jobs": out, "message": msg}


def stop_bt(jid: str) -> dict:
    with _jobs_lock:
        e = _jobs.get(jid)
    if not e:
        return {"success": False, "error": f"没有这个 BT 任务: {jid}"}
    try:
        e["proc"].terminate()
        return {"success": True, "message": f"已请求停止 {jid}（已下的分片留着，可续传）"}
    except Exception as ex:
        return {"success": False, "error": f"停止失败: {ex}"}


def engine_info() -> dict:
    """给状态查询/排查用：引擎在不在、参数长什么样、代理怎么分流。"""
    a = find_aria2()
    # ★ 1.9.2 任务 11：把"BT 与 P站/JM 的 VPN 冲突"方案一并回报，
    #   用户一问进度/状态就能看到该怎么办（不用翻日志）。
    try:
        from src.core import net_proxy as _np
        _proxy = _np.proxy_url()
        _sysp = _np.system_proxy_env()
        _advice = _np.advice()
    except Exception:
        _proxy, _sysp, _advice = "", {}, ""
    return {"found": bool(a), "path": a,
            "local_dir": str(ARIA2_LOCAL_DIR),
            "app_proxy": _proxy,
            "system_proxy_env": list(_sysp.keys()),
            "bt_direct_forced": True,
            "advice": _advice,
            "hint": "" if a else aria2_hint()}
