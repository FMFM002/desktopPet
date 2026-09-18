"""
表情包库（★ 1.9.0 新增）

素材在 `data/memes/`，由 `tools/fetch_memes.py` 抓取并生成索引
`data/memes/index.json`（每条带 tag / caption / keywords）。

这个模块只做三件事：
  1. 给**系统提示词**生成一份紧凑目录 —— 让 LLM 能按语义挑表情包
     （只给"标签 + 一句话说明 + 文件名"，不带路径，省 token）
  2. 把 LLM 挑的**文件名**解析成真实路径（并校验存在），
     避免模型编出一个不存在的名字
  3. 需要时随机挑一张（可按标签）

为什么不让 LLM 直接给路径：模型很容易写错路径/编造文件名；
统一"只挑文件名 → 这里解析"更稳，也方便以后换素材库。
"""
import json
import logging
import random
from pathlib import Path

_log = logging.getLogger("MemeLibrary")

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
MEMES_DIR = PROJECT_ROOT / "data" / "memes"
INDEX_FILE = MEMES_DIR / "index.json"

_IMG_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp")

# 索引缓存（按 mtime 失效，用户重新抓素材后不用重启）
_cache: dict = {"mtime": 0.0, "index": None}


def load_index(force: bool = False) -> dict:
    """读取 data/memes/index.json（带缓存）"""
    global _cache
    try:
        mtime = INDEX_FILE.stat().st_mtime if INDEX_FILE.exists() else 0.0
    except Exception:
        mtime = 0.0
    if not force and _cache["index"] is not None and _cache["mtime"] == mtime:
        return _cache["index"]
    data = {"packs": [], "total": 0}
    if INDEX_FILE.exists():
        try:
            data = json.loads(INDEX_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            _log.warning(f"表情包索引读取失败: {e}")
    _cache = {"mtime": mtime, "index": data}
    return data


def all_memes() -> list:
    """展平成一条条表情包：{file, path, pack, tag, caption, keywords}"""
    out = []
    for pack in load_index().get("packs", []) or []:
        pdir = PROJECT_ROOT / (pack.get("dir") or "")
        for m in pack.get("memes", []) or []:
            f = m.get("file") or ""
            if not f:
                continue
            p = pdir / f
            if not p.exists():
                continue
            out.append({
                "file": f, "path": p, "pack": pack.get("id", ""),
                "pack_name": pack.get("name", ""),
                "tag": m.get("tag", "") or "",
                "caption": m.get("caption", "") or "",
                "keywords": m.get("keywords", "") or "",
            })
    return out


def stats() -> dict:
    """给日志/自测看：每个包多少张"""
    idx = load_index()
    return {"total": sum(p.get("got", 0) for p in idx.get("packs", []) or []),
            "packs": {p.get("id", "?"): p.get("got", 0)
                      for p in idx.get("packs", []) or []},
            "available": len(all_memes())}


def _prefer_character_packs(memes: list) -> list:
    """当前角色的相关包排前面（deepseek → deepseek-chan / 大肥鱼）"""
    try:
        from src.utils.config import get_config
        ch = (get_config().pet.character or "").lower()
    except Exception:
        ch = ""
    if not ch:
        return memes
    def rank(m):
        blob = f"{m['pack']} {m['pack_name']} {m['keywords']}".lower()
        return 0 if ch in blob or "dafeiyu" in blob or "deepseek" in blob else 1
    return sorted(memes, key=lambda m: (rank(m), 0 if m["caption"] else 1))


def prompt_catalog(max_items: int = 60) -> str:
    """给提示词的紧凑目录。

    269 张全塞进去太费 token，所以：
      · 优先"当前角色相关 + 带 caption"的
      · 再按 **标签轮转**取，保证情绪覆盖面（不会全是 happy）
      · 末尾说明"这是抽样，共 N 张"
    """
    memes = _prefer_character_packs(all_memes())
    if not memes:
        return ""
    total = len(memes)
    if total <= max_items:
        picked = memes
    else:
        # 按标签分桶后轮转（尽量保留有 caption 的）
        buckets: dict = {}
        for m in memes:
            buckets.setdefault(m["tag"] or "其它", []).append(m)
        picked, i = [], 0
        keys = sorted(buckets.keys(), key=lambda k: -len(buckets[k]))
        while len(picked) < max_items and any(buckets[k] for k in keys):
            k = keys[i % len(keys)]
            if buckets[k]:
                picked.append(buckets[k].pop(0))
            i += 1
    lines = []
    for m in picked:
        desc = m["caption"] or m["keywords"] or m["pack_name"]
        tag = m["tag"] or "-"
        lines.append(f"  {m['file']}  [{tag}] {desc[:40]}")
    head = (f"可用表情包（共 {total} 张，这里列了 {len(picked)} 张"
            f"{'，按情绪标签抽样' if len(picked) < total else ''}）：")
    return head + "\n" + "\n".join(lines)


def resolve(name_or_path: str):
    """把 LLM 给的名字解析成真实路径。

    支持三种写法（都不区分大小写）：
      · 精确文件名           "007.webp"
      · 文件名主干           "007"
      · 文件名片段 / caption 片段 / keywords 片段
    返回 Path 或 None（调用方据此决定"挑不到就别发"）。
    """
    key = (name_or_path or "").strip().strip('"').strip("'")
    if not key:
        return None
    # 允许直接给绝对/相对路径
    try:
        p = Path(key)
        if p.is_absolute() and p.exists() and p.suffix.lower() in _IMG_EXTS:
            return p
    except Exception:
        pass
    memes = all_memes()
    low = key.lower()
    for m in memes:                                   # 1) 精确文件名
        if m["file"].lower() == low:
            return m["path"]
    for m in memes:                                   # 2) 主干
        if Path(m["file"]).stem.lower() == low:
            return m["path"]
    for m in memes:                                   # 3) 片段 / caption / keywords
        # ★ 1.9.0: 补上 keywords —— 索引里本来就有这个字段，以前没参与匹配，
        #   等于白写（为了让"按语义找图"更好使，把检索词也用上）。
        if (low in m["file"].lower()
                or (m["caption"] and low in m["caption"].lower())
                or (m.get("keywords") and low in m["keywords"].lower())):
            return m["path"]
    # 4) 多词查询（"躺平 摆烂"）：每个词都能在 file/caption/keywords 里找到才算命中
    words = [w for w in low.replace("　", " ").split() if w]
    if len(words) > 1:
        for m in memes:
            hay = " ".join([m["file"], m["caption"] or "", m.get("keywords") or ""]).lower()
            if all(w in hay for w in words):
                return m["path"]
    return None


def random_meme(tag: str = ""):
    """随机挑一张（可按标签），返回条目 dict 或 None

    ⚠ 历史行为：tag 写了但库里没有 → **静默退化成全库随机**（第 184 行的 `or memes`）。
      这正是 1.9.1 那个真机 bug 的根源：模型乱传/不传标签，照样能"成功"发一张
      和回复内容毫无关系的图。所以新代码请改用 `random_meme_strict()`。
      这里保持老语义，只为兼容（不再有生产调用方）。
    """
    memes = all_memes()
    if tag:
        t = tag.strip().lower()
        memes = [m for m in memes if (m["tag"] or "").lower() == t] or memes
    return random.choice(memes) if memes else None


def available_tags() -> list:
    """库里真实存在的标签（按张数从多到少）"""
    n = {}
    for m in all_memes():
        t = (m.get("tag") or "").strip().lower()
        if t:
            n[t] = n.get(t, 0) + 1
    return [t for t, _ in sorted(n.items(), key=lambda kv: -kv[1])]


def random_meme_strict(tag: str = ""):
    """★ 1.9.1：严格版 —— 标签没命中就返回 None，**绝不退化成全库随机**。

    给 meme 工具用：标签是模型自己选的，选错必须让它知道并重挑，
    否则发出来的表情包和回复内容没有联系（用户报的那个问题）。
    """
    memes = all_memes()
    if not memes:
        return None
    if not tag:
        return random.choice(memes)
    t = tag.strip().lower()
    hit = [m for m in memes if (m["tag"] or "").lower() == t]
    return random.choice(hit) if hit else None


def find_meme(name: str):
    """解析 + 返回条目（给工具层展示用）"""
    p = resolve(name)
    if p is None:
        return None
    for m in all_memes():
        if m["path"] == p:
            return m
    return {"file": p.name, "path": p, "pack": "", "pack_name": "",
            "tag": "", "caption": "", "keywords": ""}


def describe(meme_or_path) -> str:
    """★ 1.9.2（用户要求，任务 3）：返回这张表情包的**人话描述**。

    用户原话："传回数据时，增加参数'表情包描述'"。
    为什么需要：模型挑的时候只看得到文件名（009.webp），它并不知道这张图画的是
    什么；把描述回传给它，它才能①核对自己挑的图是否跟这轮情绪对得上、
    ②在需要时把图意说进回复里。日志里排查"发了张莫名其妙的图"也有据可查。

    取值优先级：caption（人工/拼音补的）→ keywords（索引关键词）→
    包名 pack_name → 兜底空串。
    """
    m = None
    if isinstance(meme_or_path, dict):
        m = meme_or_path
    elif meme_or_path:
        try:
            m = find_meme(str(meme_or_path))
        except Exception:
            m = None
    if not m:
        return ""
    for k in ("caption", "keywords", "pack_name"):
        v = (m.get(k) or "").strip()
        if v:
            return v
    return ""
