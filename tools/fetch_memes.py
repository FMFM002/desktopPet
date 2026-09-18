# -*- coding: utf-8 -*-
"""
表情包素材抓取器（★ 1.9.0 新增）

把 `素材-不可改-可复制提取/dsh-meme-packs-main/` 里的表情包**下载/复制到
data/memes/**，并生成一份给 LLM 用的索引 `data/memes/index.json`。

为什么要生成索引：表情包要"让 LLM 自己能挑"，光有图片不行 ——
索引里每条都带 `tag / caption / keywords`（例如 happy/"这题我可太会了"），
提示词里只放"标签 + 一句话说明"，LLM 就能按语义挑，挑完再按文件名发送。

用法：
    python tools/fetch_memes.py                 # 抓全部可抓的包（幂等，已存在会跳过）
    python tools/fetch_memes.py --only official-001
    python tools/fetch_memes.py --list          # 只列出源里有哪些包
    python tools/fetch_memes.py --source <目录>  # 指定素材库目录

设计要点：
  · **幂等**：单张图已存在且大小 > 0 就跳过，可反复跑/断点续传
  · 单个包失败不影响其它包；最后统一统计
  · 只写 data/memes/ 与 data/memes/index.json，不动源素材库（它是"不可改"的）
"""
import argparse
import json
import logging
import shutil
import sys
import urllib.request
import zipfile
import io
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_MEMES = PROJECT_ROOT / "data" / "memes"
DEFAULT_SOURCE = Path(r"D:\Dtopdesk\普瑞赛斯在哪\倒腾\桌宠\素材-不可改-可复制提取"
                      r"\dsh-meme-packs-main\dsh-meme-packs-main")
_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) PRTS-meme-fetcher"}

_log = logging.getLogger("Memes")
logging.basicConfig(level=logging.INFO, format="%(message)s")


def _download(url: str, dest: Path, timeout: int = 25) -> bool:
    """下载单个文件（已存在且非空则跳过）。返回是否成功。"""
    if dest.exists() and dest.stat().st_size > 0:
        return True
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        req = urllib.request.Request(url, headers=_UA)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read()
        if not data:
            return False
        tmp = dest.with_suffix(dest.suffix + ".part")
        tmp.write_bytes(data)
        tmp.replace(dest)
        return True
    except Exception as e:
        _log.warning(f"    下载失败 {dest.name}: {type(e).__name__}: {e}")
        return False


# ── 包 1：manifests/*.json（每条带 tag/caption/keywords + 直链）──

def fetch_manifest_pack(src: Path, mf: Path, only: bool = False) -> dict:
    d = json.loads(mf.read_text(encoding="utf-8"))
    pid = d.get("id") or mf.stem
    out = DATA_MEMES / pid
    memes = d.get("memes") or []
    ok, fail = 0, 0
    entries = []
    _log.info(f"  [{pid}] {d.get('name', '')} —— {len(memes)} 张")
    for i, m in enumerate(memes, 1):
        url = m.get("url") or ""
        fname = m.get("file") or f"{i:03d}.webp"
        if not url:
            fail += 1
            continue
        dest = out / fname
        if _download(url, dest):
            ok += 1
            entries.append({
                "file": fname,
                "tag": m.get("tag", ""),
                "caption": m.get("caption", ""),
                "keywords": m.get("keywords", ""),
            })
        else:
            fail += 1
        if i % 20 == 0:
            _log.info(f"    … {i}/{len(memes)}")
    return {"id": pid, "name": d.get("name", pid), "dir": str(out.relative_to(PROJECT_ROOT)),
            "license": d.get("license", ""), "site": d.get("site", ""),
            "total": len(memes), "got": ok, "failed": fail, "memes": entries}


# ── 包 2：previews/<id>/*.jpg（源里已经是图片，直接复制）──

def fetch_preview_pack(src: Path, pid: str) -> dict:
    srcdir = src / "previews" / pid
    out = DATA_MEMES / pid
    files = sorted(p for p in srcdir.glob("*") if p.suffix.lower() in
                   (".jpg", ".jpeg", ".png", ".webp", ".gif"))
    ok = 0
    entries = []
    _log.info(f"  [{pid}] 本地预览图 —— {len(files)} 张")
    out.mkdir(parents=True, exist_ok=True)
    for p in files:
        dest = out / p.name
        try:
            if not (dest.exists() and dest.stat().st_size > 0):
                shutil.copy2(p, dest)
            ok += 1
            entries.append({"file": p.name, "tag": "learned", "caption": "", "keywords": ""})
        except Exception as e:
            _log.warning(f"    复制失败 {p.name}: {e}")
    return {"id": pid, "name": f"{pid}（本地预览图）",
            "dir": str(out.relative_to(PROJECT_ROOT)), "license": "见源素材库",
            "total": len(files), "got": ok, "failed": len(files) - ok, "memes": entries}


# ── 包 3：catalog 里有 archiveUrl 的（下 zip 解压）──

def fetch_archive_pack(pack: dict) -> dict:
    pid = pack.get("id") or "pack"
    url = pack.get("archiveUrl") or ""
    out = DATA_MEMES / pid
    _log.info(f"  [{pid}] {pack.get('name', '')} —— 压缩包 {pack.get('count', '?')} 张")
    if not url:
        return {"id": pid, "name": pack.get("name", pid), "dir": "", "total": 0,
                "got": 0, "failed": 0, "memes": [], "error": "没有 archiveUrl"}
    try:
        req = urllib.request.Request(url, headers=_UA)
        with urllib.request.urlopen(req, timeout=90) as r:
            blob = r.read()
        _log.info(f"    压缩包 {len(blob)/1024/1024:.1f} MB，解压中…")
        out.mkdir(parents=True, exist_ok=True)
        entries = []
        exts = (".jpg", ".jpeg", ".png", ".webp", ".gif")
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            names = [n for n in z.namelist() if n.lower().endswith(exts)]
            for n in names:
                data = z.read(n)
                if not data:
                    continue
                # 只取文件名；tag 取压缩包里的一级目录名（通常就是分类）
                parts = [x for x in Path(n).parts if x not in (".", "")]
                tag = parts[-2] if len(parts) >= 2 else ""
                fname = Path(n).name
                dest = out / fname
                if not (dest.exists() and dest.stat().st_size > 0):
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(data)
                entries.append({"file": fname, "tag": tag, "caption": "", "keywords": ""})
        return {"id": pid, "name": pack.get("name", pid),
                "dir": str(out.relative_to(PROJECT_ROOT)),
                "license": pack.get("license", ""), "homepage": pack.get("homepage", ""),
                "total": len(entries), "got": len(entries), "failed": 0, "memes": entries}
    except Exception as e:
        _log.warning(f"    [{pid}] 失败: {type(e).__name__}: {e}")
        return {"id": pid, "name": pack.get("name", pid), "dir": "", "total": 0,
                "got": 0, "failed": 0, "memes": [], "error": f"{type(e).__name__}: {e}"}


def _write_index(packs: list):
    DATA_MEMES.mkdir(parents=True, exist_ok=True)
    packs = [p for p in packs if p.get("got")]
    index = {
        "schema": 1,
        "note": "由 tools/fetch_memes.py 生成；给 LLM 挑表情包用（tag/caption/keywords）",
        "packs": packs,
        "total": sum(p.get("got", 0) for p in packs),
    }
    (DATA_MEMES / "index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    return index


def main():
    ap = argparse.ArgumentParser(description="表情包素材抓取器")
    ap.add_argument("--source", default=str(DEFAULT_SOURCE), help="素材库目录")
    ap.add_argument("--only", default="", help="只抓指定 id（逗号分隔）")
    ap.add_argument("--list", action="store_true", help="只列出源里有哪些包")
    args = ap.parse_args()

    src = Path(args.source)
    only = {x.strip() for x in args.only.split(",") if x.strip()}
    if not src.exists():
        _log.error(f"素材库目录不存在: {src}")
        return 2

    # 列包
    available = []
    for mf in sorted((src / "manifests").glob("*.json")) if (src / "manifests").is_dir() else []:
        try:
            d = json.loads(mf.read_text(encoding="utf-8"))
            available.append(("manifest", d.get("id") or mf.stem, mf, d))
        except Exception:
            pass
    pv = src / "previews"
    if pv.is_dir():
        for d0 in sorted(x for x in pv.iterdir() if x.is_dir()):
            available.append(("preview", d0.name, d0, {}))
    catalog = {}
    cpath = src / "catalog.json"
    if cpath.exists():
        try:
            catalog = json.loads(cpath.read_text(encoding="utf-8"))
            for p in catalog.get("packs", []):
                available.append(("archive", p.get("id", "?"), None, p))
        except Exception:
            pass

    _log.info(f"源: {src}")
    # 去重：同一个 id 可能既是 manifest/preview 又是 archive —— 按
    # manifest(带 caption) > preview(本地现成) > archive(压缩包) 的优先级只取一个
    _prio = {"manifest": 0, "preview": 1, "archive": 2}
    best = {}
    order = []
    for kind, pid, _p, _d in available:
        if pid not in best:
            best[pid] = (kind, pid, _p, _d)
            order.append(pid)
        elif _prio[kind] < _prio[best[pid][0]]:
            best[pid] = (kind, pid, _p, _d)
    available = [best[pid] for pid in order]
    _log.info(f"去重后 {len(available)} 个包：")
    for kind, pid, _p, _d in available:
        _log.info(f"  - [{kind}] {pid}")
    if args.list:
        return 0

    packs = []
    for kind, pid, path, meta in available:
        if only and pid not in only:
            continue
        if kind == "manifest":
            packs.append(fetch_manifest_pack(src, path))
        elif kind == "preview":
            packs.append(fetch_preview_pack(src, pid))
        elif kind == "archive":
            packs.append(fetch_archive_pack(meta))

    index = _write_index(packs)
    _log.info("")
    _log.info("=== 汇总 ===")
    for p in packs:
        _log.info(f"  {p['id']:22} 成功 {p.get('got', 0):4}/{p.get('total', 0):4}"
                  + (f"  失败 {p.get('failed')}" if p.get("failed") else "")
                  + (f"  ({p.get('error')})" if p.get("error") else ""))
    _log.info(f"索引: {DATA_MEMES / 'index.json'}  共 {index['total']} 张")
    return 0


if __name__ == "__main__":
    sys.exit(main())
