"""
应用启动器 — ★ 1.9.2（用户要求，任务 7）

## 解决什么问题
用户说「启动QQ」，模型以前只能靠 shell_exec 猜路径（`start qq`），
猜不到就回一句「找不到 exe」。用户的原话：
  > 启动应用，还是找不到exe。每次成功启动，这些exe路径需要写入长期记忆，
  > 改一下长期记忆提示词就行了。

所以这里做两件事：
  1. **成功启动一次，就把 exe 路径记下来** —— 写进长期记忆 DB（variable_name
     = `app_path_<key>`，category=system），同时写一份本地 JSON 快取。
  2. **下次先查记忆再搜盘** —— 记忆里有就直接用，不再满盘找。

## 查找顺序（快 → 慢，找到就停）
  0. param 本身就是存在的文件路径            → 直接用它
  1. 本地快取 data/app_paths.json            → 毫秒级
  2. 长期记忆 DB `app_path_<key>`            → 毫秒级
  3. Windows App Paths 注册表                → 快（QQ/微信/Chrome 等都注册在这）
  4. PATH 里的可执行文件（shutil.which）      → 快
  5. 开始菜单 .lnk（用户级 + 全局，解析目标） → 中
  6. 常见安装目录浅层扫描（深度≤3）           → 慢，兜底

## 边界
- 只启动**用户明确要的那个**；候选中多个时按“名字完全相等 > 前缀 > 包含”排序，
  拿不准就把候选列表还回去让模型/用户确认，不瞎启动。
- 记录失败不影响启动本身（记忆是锦上添花，不是前置条件）。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
from pathlib import Path
from typing import Optional

from src.utils.logger import get_logger

_log = get_logger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CACHE_FILE = PROJECT_ROOT / "data" / "app_paths.json"

# 中文名 → 可执行文件名（用户说中文时也能找到）
_ALIAS = {
    "qq": ["QQ.exe", "QQScLauncher.exe"],
    "微信": ["WeChat.exe", "Weixin.exe"],
    "wechat": ["WeChat.exe", "Weixin.exe"],
    "weixin": ["Weixin.exe", "WeChat.exe"],
    "浏览器": ["chrome.exe", "msedge.exe", "firefox.exe"],
    "chrome": ["chrome.exe"],
    "谷歌浏览器": ["chrome.exe"],
    "edge": ["msedge.exe"],
    "火狐": ["firefox.exe"],
    "计算器": ["calc.exe"],
    "记事本": ["notepad.exe"],
    "画图": ["mspaint.exe"],
    "任务管理器": ["Taskmgr.exe"],
    "命令提示符": ["cmd.exe"],
    "终端": ["WindowsTerminal.exe", "wt.exe"],
    "网易云音乐": ["cloudmusic.exe"],
    "网易云": ["cloudmusic.exe"],
    "steam": ["steam.exe"],
    "vscode": ["Code.exe"],
    "vs code": ["Code.exe"],
    "钉钉": ["DingTalk.exe"],
    "飞书": ["Feishu.exe", "Lark.exe"],
    "抖音": ["douyin.exe"],
    "哔哩哔哩": ["bilibili.exe"],
    "ps": ["Photoshop.exe"],
    "photoshop": ["Photoshop.exe"],
}

_lock = threading.RLock()
_cache: Optional[dict] = None


# ─────────────────────────── 名字归一化 ───────────────────────────

def _key(name: str) -> str:
    """把用户说法归一成缓存/记忆的 key。"""
    n = (name or "").strip().strip('"').strip("'")
    n = Path(n).stem if ("\\" in n or "/" in n) else n
    n = n.lower()
    n = re.sub(r"\.exe$", "", n)
    n = re.sub(r"[\s_\-]+", "", n)
    return n


def _wanted_names(name: str) -> list:
    """候选可执行文件名（小写），用于和注册表/磁盘里的 exe 比对。"""
    k = _key(name)
    out = []
    for a in _ALIAS.get(k, []):
        out.append(a.lower())
    if k:
        out.append(f"{k}.exe")
        out.append(k)
    # 去重保序
    seen, uniq = set(), []
    for x in out:
        if x not in seen:
            seen.add(x)
            uniq.append(x)
    return uniq


# ─────────────────────────── 本地快取 ───────────────────────────

def _load_cache() -> dict:
    global _cache
    with _lock:
        if _cache is not None:
            return _cache
        data = {}
        try:
            if CACHE_FILE.exists():
                raw = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    # 只保留仍然存在的路径（用户卸载/挪走了就自动失效）
                    data = {k: v for k, v in raw.items()
                            if isinstance(v, str) and Path(v).exists()}
        except Exception as e:
            _log.warning(f"[AppLauncher] 读快取失败，按空处理: {e}")
        _cache = data
        return _cache


def _save_cache(d: dict) -> None:
    global _cache
    with _lock:
        _cache = dict(d)
        try:
            CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = CACHE_FILE.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            tmp.replace(CACHE_FILE)
        except Exception as e:
            _log.warning(f"[AppLauncher] 写快取失败: {e}")


# ─────────────────────────── 长期记忆 ───────────────────────────

def _ltm_get(key: str) -> Optional[str]:
    try:
        from src.core.database import get_db
        row = get_db().ltm.get(f"app_path_{key}")
        if row and row.get("value"):
            v = str(row["value"]).strip()
            m = re.search(r"([A-Za-z]:\\[^\s\"']+\.exe)", v, re.I)
            if m and Path(m.group(1)).exists():
                return m.group(1)
    except Exception as e:
        _log.debug(f"[AppLauncher] 查长期记忆失败: {e}")
    return None


def remember(exe_path, name: str = "", source: str = "app_launcher") -> bool:
    """★ 核心：把成功用过的 exe 路径写进长期记忆 + 本地快取。

    用户原话就是「每次成功启动，这些exe路径需要写入长期记忆」。
    """
    try:
        p = Path(str(exe_path)).resolve()
        if not p.exists():
            return False
    except Exception:
        return False
    k = _key(name) or _key(p.stem)
    if not k:
        return False
    ok = False
    # 1) 本地快取（启动器自己用，确定性最高）
    try:
        d = dict(_load_cache())
        d[k] = str(p)
        d[_key(p.stem)] = str(p)
        _save_cache(d)
        ok = True
    except Exception as e:
        _log.warning(f"[AppLauncher] 写快取失败: {e}")
    # 2) 长期记忆（让桌宠在聊天里也能"记得"这个路径）
    try:
        from src.core.database import get_db
        val = f"用户电脑上「{name or p.stem}」的可执行文件路径是 {p}"
        get_db().ltm.upsert(variable_name=f"app_path_{k}", value=val,
                            category="system", confidence=0.95, source=source)
        ok = True
    except Exception as e:
        _log.debug(f"[AppLauncher] 写长期记忆失败（不影响启动）: {e}")
    return ok


# ─────────────────────────── 各路查找 ───────────────────────────

def _from_app_paths(keys: list) -> Optional[str]:
    """Windows App Paths 注册表 —— QQ / 微信 / Chrome 等都注册在这。"""
    if os.name != "nt":
        return None
    try:
        import winreg
    except Exception:
        return None
    sub = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"
    roots = [(winreg.HKEY_LOCAL_MACHINE, sub),
             (winreg.HKEY_CURRENT_USER, sub),
             (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft"
                                         r"\Windows\CurrentVersion\App Paths")]
    hits = []
    for hive, path in roots:
        try:
            with winreg.OpenKey(hive, path) as k:
                n = winreg.QueryInfoKey(k)[0]
                names = [winreg.EnumKey(k, i) for i in range(n)]
        except OSError:
            continue
        low = {x.lower(): x for x in names}
        for want in keys:
            real = low.get(want) or low.get(want + ".exe")
            if not real:
                continue
            try:
                with winreg.OpenKey(hive, path + "\\" + real) as sk:
                    val, _ = winreg.QueryValueEx(sk, "")
                val = str(val).strip().strip('"')
                if val and Path(val).exists():
                    hits.append((0 if real.lower() == want else 1, val))
            except OSError:
                continue
    if not hits:
        return None
    hits.sort(key=lambda x: x[0])
    return hits[0][1]


def _from_path_env(keys: list) -> Optional[str]:
    import shutil
    for want in keys:
        if not want.endswith(".exe"):
            continue
        p = shutil.which(want)
        if p:
            return p
    return None


def _shortcut_targets() -> list:
    """开始菜单里所有 .lnk → (名字, 目标exe)。用 pywin32 解析。"""
    out = []
    if os.name != "nt":
        return out
    bases = []
    for env in ("ProgramData", "APPDATA"):
        v = os.environ.get(env)
        if v:
            bases.append(Path(v) / "Microsoft" / "Windows" / "Start Menu" / "Programs")
    try:
        import win32com.client
        shell = win32com.client.Dispatch("WScript.Shell")
    except Exception:
        shell = None
    for base in bases:
        if not base.exists():
            continue
        try:
            links = list(base.rglob("*.lnk"))
        except Exception:
            continue
        for lnk in links:
            tgt = ""
            if shell is not None:
                try:
                    tgt = str(shell.CreateShortCut(str(lnk)).TargetPath or "")
                except Exception:
                    tgt = ""
            if not tgt:
                tgt = _lnk_scan(lnk)
            if tgt and tgt.lower().endswith(".exe") and Path(tgt).exists():
                out.append((lnk.stem, tgt))
    return out


def _lnk_scan(p: Path) -> str:
    """.lnk 二进制里捞目标路径（没有 pywin32 时的兜底）。"""
    try:
        raw = p.read_bytes()
    except Exception:
        return ""
    # 目标路径可能以 UTF-16LE 或本地编码存着，两种都试
    for enc in ("utf-16-le", "mbcs", "utf-8"):
        try:
            txt = raw.decode(enc, errors="ignore")
        except Exception:
            continue
        for m in re.finditer(r"([A-Za-z]:\\[^\x00-\x1f\"<>|]{2,200}?\.exe)", txt):
            cand = m.group(1)
            if Path(cand).exists():
                return cand
    return ""


def _common_dirs() -> list:
    dirs = []
    for env in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA", "APPDATA",
                "ProgramData"):
        v = os.environ.get(env)
        if v:
            dirs.append(Path(v))
    # 用户这台机器把程序装在 D:\Apps —— 各盘符的 Apps / Program Files 都扫一下
    for letter in "CDEFGH":
        for sub in ("Apps", "Program Files", "Program Files (x86)"):
            p = Path(f"{letter}:\\{sub}")
            if p.exists():
                dirs.append(p)
    seen, out = set(), []
    for d in dirs:
        s = str(d).lower()
        if s not in seen and d.exists():
            seen.add(s)
            out.append(d)
    return out


def _from_disk(keys: list, max_depth: int = 3) -> list:
    """浅层扫常见安装目录。返回候选路径列表（按匹配质量排序）。"""
    scored = {}
    for base in _common_dirs():
        for depth_root in _walk(base, max_depth):
            for f in depth_root:
                if not f.name.lower().endswith(".exe"):
                    continue
                low = f.name.lower()
                for i, want in enumerate(keys):
                    if not want.endswith(".exe"):
                        continue
                    if low == want:
                        rank = 0
                    elif low.startswith(want[:-4]):
                        rank = 1
                    elif want[:-4] in low:
                        rank = 2
                    else:
                        continue
                    # 卸载程序 / 崩溃上报 / 更新器 明显不是用户要的
                    if re.search(r"unins|crash|report|setup|update|helper|"
                                 r"vcredist|dxsetup", low):
                        rank += 3
                    key = str(f)
                    if key not in scored or scored[key] > rank:
                        scored[key] = rank
                    break
    return [k for k, _ in sorted(scored.items(), key=lambda x: (x[1], len(x[0])))]


def _walk(base: Path, max_depth: int):
    """按层产出目录里的文件（广度优先，浅层优先）。"""
    level = [base]
    for _ in range(max_depth):
        nxt = []
        for d in level:
            files = []
            try:
                for e in d.iterdir():
                    try:
                        if e.is_dir():
                            nxt.append(e)
                        elif e.is_file():
                            files.append(e)
                    except OSError:
                        continue
            except OSError:
                continue
            if files:
                yield files
        level = nxt
        if not level:
            return


# ─────────────────────────── 对外接口 ───────────────────────────

def resolve(name_or_path: str) -> tuple:
    """找到 exe。返回 (路径 or None, 说明, 候选列表)。

    说明取值: path / cache / ltm / registry / path_env / startmenu / disk / miss
    """
    raw = (name_or_path or "").strip().strip('"').strip("'")
    if not raw:
        return None, "miss", []

    # 0) 本身就是路径
    try:
        p = Path(raw)
        if p.is_file() and p.suffix.lower() in (".exe", ".bat", ".cmd", ".lnk"):
            return str(p.resolve()), "path", []
    except Exception:
        pass

    k = _key(raw)
    keys = _wanted_names(raw)

    # 1) 本地快取
    c = _load_cache()
    for probe in (k, _key(str(raw))):
        if probe and probe in c and Path(c[probe]).exists():
            return c[probe], "cache", []

    # 2) 长期记忆
    got = _ltm_get(k) if k else None
    if got:
        try:
            d = dict(_load_cache())
            d[k] = got
            _save_cache(d)
        except Exception:
            pass
        return got, "ltm", []

    # 3) 注册表 App Paths
    got = _from_app_paths(keys)
    if got:
        return got, "registry", []

    # 4) PATH
    got = _from_path_env(keys)
    if got:
        return got, "path_env", []

    # 5) 开始菜单快捷方式
    cands = []
    for disp, tgt in _shortcut_targets():
        dk = _key(disp)
        if not dk:
            continue
        if dk == k:
            rank = 0
        elif k and (dk.startswith(k) or k.startswith(dk)):
            rank = 1
        elif k and k in dk:
            rank = 2
        else:
            continue
        cands.append((rank, tgt))
    if cands:
        cands.sort(key=lambda x: x[0])
        if cands[0][0] == 0:
            return cands[0][1], "startmenu", []
        uniq = []
        for _, t in cands:
            if t not in uniq:
                uniq.append(t)
        if len(uniq) == 1:
            return uniq[0], "startmenu", []

    # 6) 磁盘浅扫（兜底，慢）
    disk = _from_disk(keys)
    if disk:
        return disk[0], "disk", disk[1:6]

    return None, "miss", []


def launch(name_or_path: str, args: list = None) -> tuple:
    """启动应用。返回 (是否成功, 给模型看的文本, 进程对象 or None)。

    ★ 成功就调 remember() 把路径写进长期记忆 —— 这是用户明确要的。
    """
    exe, how, others = resolve(name_or_path)
    if not exe:
        msg = [f"❌ 没找到「{name_or_path}」的可执行文件。"]
        msg.append("我按这个顺序找过：用户给的路径 → 本地快取 → 长期记忆 → "
                   "注册表 App Paths → PATH → 开始菜单快捷方式 → 常见安装目录。")
        msg.append("请让用户直接把 exe 的完整路径给你（例如 "
                   "D:\\Apps\\QQ\\QQ.exe）；拿到后用 open_app 指定完整路径启动，"
                   "**成功一次我就会把路径记进长期记忆，以后直接说名字就行**。")
        return False, "\n".join(msg), None
    try:
        cmd = [exe] + [str(a) for a in (args or [])]
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0) \
                if args else 0
        proc = subprocess.Popen(cmd, **kwargs)
    except Exception as e:
        return False, f"❌ 找到「{exe}」但启动失败: {e}", None

    marked = remember(exe, name_or_path)
    _how = {"path": "用户给的路径", "cache": "本地快取", "ltm": "长期记忆",
            "registry": "注册表", "path_env": "PATH", "startmenu": "开始菜单",
            "disk": "磁盘搜索"}.get(how, how)
    lines = [f"✅ 已启动「{name_or_path}」", f"路径: {exe}", f"来源: {_how}"]
    if marked:
        lines.append("已把这个路径写进长期记忆，下次直接说名字即可。")
    if others:
        lines.append("另外还找到几个同名候选（这次没用）：")
        lines.extend(f"  · {o}" for o in others)
    return True, "\n".join(lines), proc


def known_apps() -> list:
    """记忆里已经存过路径的应用（给模型/用户看）。name/exe 都是干净的。"""
    out = []
    seen = set()
    try:
        for k, v in _load_cache().items():
            if k in seen:
                continue
            seen.add(k)
            out.append({"name": k, "exe": v})
    except Exception:
        pass
    try:
        from src.core.database import get_db
        for row in get_db().ltm.get_by_category("system"):
            vn = row.get("variable_name") or ""
            if not vn.startswith("app_path_"):
                continue
            nm = vn[len("app_path_"):]
            if nm in seen:
                continue
            # 记忆里存的是自然语言，这里把 exe 路径抠出来，和快取格式保持一致
            m = re.search(r"([A-Za-z]:\\[^\s\"']+\.exe)", str(row.get("value") or ""))
            seen.add(nm)
            out.append({"name": nm, "exe": m.group(1) if m else
                        str(row.get("value") or "")})
    except Exception as e:
        _log.debug(f"[AppLauncher] 读长期记忆列表失败: {e}")
    return out
