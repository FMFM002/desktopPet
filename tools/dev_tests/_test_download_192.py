# -*- coding: utf-8 -*-
"""
★ 1.9.2 任务 8 回归：download 工具升级（参考 Motrix）
==============================================================================
用户原话：
  ###升级download工具，参考项目D:\\…\\参考项目\\Motrix-main下载器\\Motrix-main

Motrix 是 Electron + aria2 的完整下载器，整体搬没意义。本套件验证我们**取过来的
四件事**是否真的生效（对着 Motrix 源码逐条）：
  1) 分段并发下载  ← src/core/download/segment-downloader.ts
  2) 断点续传      ← src/core/task/final-name-picker.ts 的 .part/.aria2 控制文件思路
  3) 重试 + 退避   ← aria2 的 max-tries / retry-wait
  4) 文件名与去重  ← final-name-picker.ts 的 ` (n)` 格式

★ 测试方法：起一个**自己实现的**本地 HTTP 服务器（Python 自带的
  http.server **不支持 Range**，用它根本测不出分段）。这个服务器支持：
    · HEAD/GET + Content-Length + Accept-Ranges
    · Range 请求 → 206 + 正确切片
    · Content-Disposition（含 RFC 5987 的 filename*）
    · 可切换：忽略 Range（测单流退化）、中途断流（测续传）、前 N 次失败（测重试）
==============================================================================
"""
import hashlib
import json
import os
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE))

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


# ══════════════════════════════════════════════════════════════
# 本地测试服务器（支持 Range / 可控故障）
# ══════════════════════════════════════════════════════════════
BODY = bytes(bytearray((i * 7 + 13) % 256 for i in range(3 * 1024 * 1024)))  # 3MB
SHA = hashlib.sha256(BODY).hexdigest()

STATE = {
    "ignore_range": False,     # True → 忽略 Range，永远回 200 全量
    "fail_first": 0,           # 前 N 次请求直接 500
    "cut_after": None,         # 只发 N 字节然后断（测续传）
    "requests": [],            # 记录 (method, range)
    "served": 0,               # 累计发出的字节数
    "content_disposition": "",
}


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _hdr(self, code, length, extra=None):
        self.send_response(code)
        self.send_header("Content-Length", str(length))
        self.send_header("Content-Type", "application/octet-stream")
        if not STATE["ignore_range"]:
            self.send_header("Accept-Ranges", "bytes")
        if STATE["content_disposition"]:
            self.send_header("Content-Disposition", STATE["content_disposition"])
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()

    def _parse_range(self):
        r = self.headers.get("Range", "")
        if STATE["ignore_range"] or not r.startswith("bytes="):
            return None
        try:
            s, _, e = r[6:].partition("-")
            start = int(s)
            end = int(e) if e else len(BODY) - 1
            return max(0, start), min(end, len(BODY) - 1)
        except Exception:
            return None

    def _maybe_fail(self):
        if STATE["fail_first"] > 0:
            STATE["fail_first"] -= 1
            self.send_response(500)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return True
        return False

    def do_HEAD(self):
        STATE["requests"].append(("HEAD", None))
        if self._maybe_fail():
            return
        if self.path.split("?")[0] != "/testfile.bin":
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self._hdr(200, len(BODY))

    def do_GET(self):
        rng = self._parse_range()
        STATE["requests"].append(("GET", rng))
        if self._maybe_fail():
            return
        if self.path.split("?")[0] != "/testfile.bin":
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if rng is None:
            self._hdr(200, len(BODY))
            self._send(BODY)
        else:
            start, end = rng
            chunk = BODY[start:end + 1]
            self._hdr(206, len(chunk),
                      {"Content-Range": f"bytes {start}-{end}/{len(BODY)}"})
            self._send(chunk)

    def _send(self, data):
        cut = STATE["cut_after"]
        try:
            if cut is not None:
                left = max(0, cut - STATE["served"])
                self.wfile.write(data[:left])
                STATE["served"] += min(left, len(data))
                self.close_connection = True
                return
            self.wfile.write(data)
            STATE["served"] += len(data)
        except Exception:
            pass


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


PORT = _free_port()
SRV = ThreadingHTTPServer(("127.0.0.1", PORT), H)
threading.Thread(target=SRV.serve_forever, daemon=True).start()
URL = f"http://127.0.0.1:{PORT}/testfile.bin"
print(f"  测试服务器: {URL}  (3MB, sha256={SHA[:12]}…)")

from tools import download_tool as D                                # noqa: E402

_TMPBUCKET = "_test_dl_192"


def _clean():
    d = D.download_dir(_TMPBUCKET)
    for f in d.glob("*"):
        try:
            f.unlink()
        except Exception:
            pass
    return d


def _reset(**kw):
    STATE.update({"ignore_range": False, "fail_first": 0, "cut_after": None,
                  "requests": [], "served": 0, "content_disposition": ""})
    STATE.update(kw)


# ══════════════════════════════════════════════════════════════
head("A. 模块结构与参数（对 LLM 暴露的新能力）")
for fn in ("download_url", "execute", "_probe", "_plan_segments",
           "_auto_threads", "_from_content_disposition", "_load_ctrl",
           "_save_ctrl", "_seg_worker", "_unique", "_ctrl_path"):
    check(f"★导出 {fn}()", hasattr(D, fn))

_props = D.TOOL_PARAMETERS["properties"]
check("★新增 threads 参数（分段并发）", "threads" in _props)
check("★新增 resume 参数（断点续传）", "resume" in _props)
check("★新增 max_mb 参数（大小上限）", "max_mb" in _props)
check("★保留原有 url/filename/bucket/referer",
      all(k in _props for k in ("url", "filename", "bucket", "referer")))
check("★url 仍是唯一必填", D.TOOL_PARAMETERS["required"] == ["url"])
check("★TOOL_DESCRIPTION 说明了分段/续传",
      "分段" in D.TOOL_DESCRIPTION and "续传" in D.TOOL_DESCRIPTION)
check("★说明里点名参考了 Motrix", "Motrix" in D.TOOL_DESCRIPTION)
check("★沿用 .part 不完整标记（Motrix 是 .motrix）",
      D.PART_SUFFIX == ".part")
check("★沿用 .aria2 控制文件命名（照 Motrix）", D.CTRL_SUFFIX == ".aria2")
check("★重试/退避常量照 aria2 的 max-tries / retry-wait",
      D.DEFAULT_RETRIES >= 1 and D.RETRY_WAIT > 0)

# ══════════════════════════════════════════════════════════════
head("B. 分段规划（照 segment-downloader 的均分思路）")
_s = D._plan_segments(3 * 1024 * 1024, 4)
check("★3MB 分 4 段 → 4 段", len(_s) == 4, str(_s))
check("★各段首尾相接、不重叠",
      all(_s[i][1] + 1 == _s[i + 1][0] for i in range(len(_s) - 1)), str(_s))
check("★最后一段正好到文件末尾",
      _s[-1][1] == 3 * 1024 * 1024 - 1, str(_s[-1]))
check("★覆盖全部字节",
      sum(e - s + 1 for s, e in _s) == 3 * 1024 * 1024)
check("★小于 1MB 不分段（分段开销不划算）",
      len(D._plan_segments(200 * 1024, 4)) == 1)
check("★总长未知 → 不分段", D._plan_segments(0, 4) == [])
check("★并发数被 MAX_THREADS 夹住",
      len(D._plan_segments(100 * 1024 * 1024, 999)) == D.MAX_THREADS)
check("★小文件自动单线程", D._auto_threads(100 * 1024) == 1)
check("★中等文件自动 2 线程", D._auto_threads(5 * 1024 * 1024) == 2)
check("★大文件自动多线程", D._auto_threads(200 * 1024 * 1024) >= 4)
check("★大小未知自动单线程", D._auto_threads(None) == 1)

# ══════════════════════════════════════════════════════════════
head("C. 文件名：Content-Disposition（照 final-name-picker 的取名规则）")
check("★RFC 5987 的 filename*=UTF-8''… 能解码",
      D._from_content_disposition(
          "attachment; filename*=UTF-8''%E5%B0%81%E9%9D%A2.jpg") == "封面.jpg")
check("★普通 filename=\"…\" 能取",
      D._from_content_disposition('attachment; filename="a b.pdf"') == "a b.pdf")
check("★不带引号的 filename 能取",
      D._from_content_disposition("attachment; filename=x.zip") == "x.zip")
check("★没有 Content-Disposition → 空串", D._from_content_disposition("") == "")
check("★只有 attachment 没 filename → 空串",
      D._from_content_disposition("attachment") == "")
_d = _clean()
_real_dl = D.download_url
_reset(content_disposition="attachment; filename*=UTF-8''%E5%B0%81%E9%9D%A2.bin")
r = D.download_url(URL, bucket=_TMPBUCKET, threads=2)
check("★服务端给了文件名 → 就用它", r.get("success") and
      Path(r["file_path"]).name == "封面.bin",
      str(r.get("file_path") or r.get("error")))
check("★下载内容正确（sha256 一致）",
      r.get("success") and hashlib.sha256(
          Path(r["file_path"]).read_bytes()).hexdigest() == SHA)

# ══════════════════════════════════════════════════════════════
head("D. ★分段并发下载真的生效（不是单流）")
_clean()
_reset()
r = D.download_url(URL, bucket=_TMPBUCKET, threads=4)
check("★分段下载成功", r.get("success") is True, str(r.get("error"))[:150])
check("★★确实切成了 4 段并发", r.get("threads") == 4, str(r.get("threads")))
check("★★走的是分段模式（不是单流）", "段并发" in str(r.get("mode")),
      str(r.get("mode")))
check("★★服务端收到多个 Range 请求（证明是分段拉的）",
      len([1 for m, rg in STATE["requests"] if m == "GET" and rg]) >= 2,
      str(STATE["requests"][:8]))
check("★★下载内容与源文件逐字节一致（sha256 校验）",
      hashlib.sha256(Path(r["file_path"]).read_bytes()).hexdigest() == SHA)
check("★文件大小正确", r.get("bytes") == len(BODY), str(r.get("bytes")))
check("★返回里有速度与耗时（照 Motrix 的进度信息）",
      r.get("speed") and r.get("seconds") is not None, str(r.get("speed")))
check("★完成后不留 .part", not list(_clean().glob("*.part")))
check("★完成后不留 .aria2 控制文件",
      not list(D.download_dir(_TMPBUCKET).glob("*.aria2")))

# 逐段校验：把所有段的字节拼起来必须等于原文
_parts = []
_reset()
_clean()
D.download_url(URL, bucket=_TMPBUCKET, filename="seg.bin", threads=4)
got = D.download_dir(_TMPBUCKET) / "seg.bin"
check("★★分段写入的偏移没串（每字节都对）",
      got.read_bytes() == BODY,
      f"size={got.stat().st_size} vs {len(BODY)}")

# 指定 1 线程 = 单段
_clean()
_reset()
r = D.download_url(URL, bucket=_TMPBUCKET, threads=1)
check("★threads=1 时不分段（1 段）", r.get("threads") == 1, str(r.get("threads")))
check("★单段下载内容仍正确",
      hashlib.sha256(Path(r["file_path"]).read_bytes()).hexdigest() == SHA)

# ══════════════════════════════════════════════════════════════
head("E. ★断点续传（.part + .aria2 控制文件）")

# ── E1：确定性验证「只补没下完的段」 ──
# 直接造一个"前 2 段已下好"的现场（比掐断流更可控），
# 然后调 download_url，看服务端到底还发了多少字节。
_clean()
_reset()
_segs = D._plan_segments(len(BODY), 4)
_part = D.download_dir(_TMPBUCKET) / "resume.bin.part"
with open(_part, "wb") as _f:
    _f.truncate(len(BODY))
_prog = D._Progress()
for _i, (_s, _e) in enumerate(_segs[:2]):
    assert D._seg_worker(URL, D._headers(), _part, _i, _s, _e, 1, _prog, 30), \
        "测试前置：手工下前两段失败"
D._save_ctrl(_part, URL, len(BODY), {0, 1}, _segs)
_served_half = sum(_e - _s + 1 for _s, _e in _segs[:2])
STATE["requests"].clear()
STATE["served"] = 0
r = D.download_url(URL, bucket=_TMPBUCKET, filename="resume.bin", threads=4)
check("★★续传后成功", r.get("success") is True, str(r.get("error"))[:150])
check("★★续传后内容完整（sha256 一致，说明复用的一半是对的）",
      hashlib.sha256(Path(r["file_path"]).read_bytes()).hexdigest() == SHA)
check("★★只请求了缺的那 2 段（服务端发出的字节 = 全量 − 已下的一半）",
      STATE["served"] == len(BODY) - _served_half,
      f"发出 {STATE['served']}B，期望 {len(BODY) - _served_half}B")
check("★★没有重新请求已完成的分段",
      len([1 for m, rg in STATE["requests"] if m == "GET" and rg]) == 2,
      str(STATE["requests"]))
print(f"        续传实测: 已下 {_served_half}B，本轮只发 "
      f"{STATE['served']}B，全量 {len(BODY)}B")

# ── E2：真实中断（断流）也要留下可续传的现场 ──
_clean()
_reset(cut_after=1200 * 1024)
r1 = D.download_url(URL, bucket=_TMPBUCKET, filename="cutoff.bin",
                    threads=4, retries=0)
check("★★中断时返回失败（不是假装成功）", r1.get("success") is False,
      str(r1.get("success")))
check("★★明确告知可续传", r1.get("resumable") is True, str(r1.get("resumable")))
check("★★留下了 .part 文件（不完整标记）",
      Path(r1.get("partial_file") or "").exists(),
      str(r1.get("partial_file")))
_part2 = Path(r1.get("partial_file"))
check("★★控制文件 .aria2 也留下了",
      D._ctrl_path(_part2).exists(), str(D._ctrl_path(_part2)))
check("★提示里告诉模型「再调用一次即可续传」",
      "续传" in str(r1.get("hint")), str(r1.get("hint")))
_ctrl = json.loads(D._ctrl_path(_part2).read_text(encoding="utf-8"))
check("★★控制文件记了 URL / 总长 / 分段", _ctrl.get("url") == URL
      and int(_ctrl.get("total")) == len(BODY) and _ctrl.get("segments"),
      str(list(_ctrl.keys())))
check("★★控制文件记了已完成的分段下标", isinstance(_ctrl.get("done"), list),
      str(_ctrl.get("done")))

_reset(cut_after=None)
r2 = D.download_url(URL, bucket=_TMPBUCKET, filename="cutoff.bin", threads=4)
check("★★断流后续传成功", r2.get("success") is True, str(r2.get("error"))[:150])
check("★★续传后内容完整（sha256 一致）",
      hashlib.sha256(Path(r2["file_path"]).read_bytes()).hexdigest() == SHA)
check("★★续传后 .part 已清掉",
      not Path(str(r2["file_path"]) + ".part").exists())
check("★★续传后控制文件也清掉",
      not D._ctrl_path(Path(str(r2["file_path"]) + ".part")).exists())

# resume=False → 不续传，从零开始
_clean()
_reset(cut_after=1200 * 1024)
r3 = D.download_url(URL, bucket=_TMPBUCKET, filename="norsm.bin",
                    threads=4, retries=0)
check("★先造一个不完整的 .part", r3.get("success") is False)
_reset(cut_after=None)
r4 = D.download_url(URL, bucket=_TMPBUCKET, filename="norsm.bin",
                    threads=4, resume=False)
check("★resume=False 时不续传但也必须下对（从零重来）",
      r4.get("success") is True and hashlib.sha256(
          Path(r4["file_path"]).read_bytes()).hexdigest() == SHA,
      str(r4.get("error"))[:120])

# 控制文件 URL 不匹配 → 作废重下（防止下错文件）
_clean()
_reset(cut_after=1200 * 1024)
r5 = D.download_url(URL, bucket=_TMPBUCKET, filename="mismatch.bin",
                    threads=4, retries=0)
_p5 = Path(r5.get("partial_file"))
_c5 = D._ctrl_path(_p5)
_d5 = json.loads(_c5.read_text(encoding="utf-8"))
_d5["url"] = "http://example.com/other.bin"
_c5.write_text(json.dumps(_d5), encoding="utf-8")
check("★★控制文件 URL 对不上时作废（不会拿错文件的进度续）",
      D._load_ctrl(_p5, URL, len(BODY)) == set(),
      str(D._load_ctrl(_p5, URL, len(BODY))))

# ══════════════════════════════════════════════════════════════
head("F. ★重试 + 退避（照 aria2 max-tries / retry-wait）")
_clean()
_reset(fail_first=2)
r = D.download_url(URL, bucket=_TMPBUCKET, filename="retry.bin", threads=1)
check("★★服务端前 2 次返回 500，重试后仍然成功", r.get("success") is True,
      str(r.get("error"))[:150])
check("★★重试后内容正确",
      hashlib.sha256(Path(r["file_path"]).read_bytes()).hexdigest() == SHA)

_clean()
_reset(fail_first=99)      # 一直失败 → 必须在有限次后放弃，不能死循环
_t0 = time.time()
r = D.download_url(URL, bucket=_TMPBUCKET, filename="alwaysfail.bin",
                   threads=1, retries=1)
_el = time.time() - _t0
check("★一直失败 → 有限次后返回失败（不死循环）", r.get("success") is False)
check("★失败返回里有原因", bool(r.get("error")), str(r.get("error"))[:100])
check("★退避是等过的（retries=1 至少要等 RETRY_WAIT）", _el >= D.RETRY_WAIT * 0.8,
      f"用了 {_el:.2f}s，期望 ≥ {D.RETRY_WAIT * 0.8:.2f}s")

# ══════════════════════════════════════════════════════════════
head("G. 服务器不支持 Range → 自动退化成单流")
_clean()
_reset(ignore_range=True)
r = D.download_url(URL, bucket=_TMPBUCKET, filename="norange.bin", threads=4)
check("★不支持 Range 时仍能下成功", r.get("success") is True,
      str(r.get("error"))[:150])
check("★走的是单流模式", "单流" in str(r.get("mode")), str(r.get("mode")))
check("★单流内容正确（不是半个文件）",
      hashlib.sha256(Path(r["file_path"]).read_bytes()).hexdigest() == SHA)

# ══════════════════════════════════════════════════════════════
head("H. 文件名去重 / 上限 / 参数校验")
_clean()
_reset()
r1 = D.download_url(URL, bucket=_TMPBUCKET, filename="dup.bin", threads=1)
r2 = D.download_url(URL, bucket=_TMPBUCKET, filename="dup.bin", threads=1)
r3 = D.download_url(URL, bucket=_TMPBUCKET, filename="dup.bin", threads=1)
_names = [Path(x["file_path"]).name for x in (r1, r2, r3)]
check("★★重名不覆盖，按 Motrix 的 ` (n)` 格式加序号",
      _names == ["dup.bin", "dup (1).bin", "dup (2).bin"], str(_names))

r = D.download_url(URL, bucket=_TMPBUCKET, max_mb=1)
check("★超过 max_mb 时拒绝并说明",
      r.get("success") is False and "上限" in str(r.get("error")),
      str(r.get("error"))[:100])
check("★拒绝时不给假文件路径", not r.get("file_path"))

r = D.download_url("ftp://x/y", bucket=_TMPBUCKET)
check("★非 http/https 被拒", r.get("success") is False and "http" in str(r.get("error")))
r = D.download_url("", bucket=_TMPBUCKET)
check("★空 url 被拒", r.get("success") is False)

r = D.download_url(f"http://127.0.0.1:{PORT}/nope", bucket=_TMPBUCKET, retries=0)
check("★404 时返回失败而不是空文件", r.get("success") is False,
      str(r.get("success")))

# 文件名清洗（防目录穿越）
check("★文件名里的路径分隔符被清掉",
      "/" not in D._safe_name("../../etc/passwd")
      and "\\" not in D._safe_name("..\\..\\win.ini"),
      D._safe_name("../../etc/passwd"))

# execute() 返回 JSON 且字段齐全
_clean()
_reset()
out = D.execute(URL, bucket=_TMPBUCKET, filename="via_execute.bin", threads=2)
try:
    j = json.loads(out)
    _ok = True
except Exception as e:
    j, _ok = {}, False
check("★execute() 返回可解析的 JSON", _ok, out[:120])
check("★execute() 结果字段齐全（file_path/is_image/message…）",
      j.get("success") and all(k in j for k in
                               ("file_path", "file_name", "file_size",
                                "is_image", "message", "url")),
      str(list(j.keys())))

# ══════════════════════════════════════════════════════════════
head("I. 源码约束（防止以后被改回单流）")
_src = (BASE / "tools" / "download_tool.py").read_text(encoding="utf-8")
check("★用 ThreadPoolExecutor 并发", "ThreadPoolExecutor" in _src)
check("★发 Range 头", '"Range"' in _src or "'Range'" in _src
      or "Range\"]" in _src)
check("★处理 206 Partial Content", "206" in _src)
check("★有指数退避", "2 ** attempt" in _src)
check("★完成后 .part → 正式名（replace）", ".replace(dest)" in _src)
check("★注释里写明了对着 Motrix 的哪个文件抄的",
      "segment-downloader.ts" in _src and "final-name-picker.ts" in _src
      and "max-tries" in _src)

_clean()

print("\n" + "=" * 60)
print(f"★ 1.9.2 任务8 download 升级（参考 Motrix）：{PASS} PASS / {FAIL} FAIL")
for f in FAILS:
    print(f"  FAIL: {f}")
print("=" * 60)
try:
    SRV.shutdown()
except Exception:
    pass
sys.exit(1 if FAIL else 0)
