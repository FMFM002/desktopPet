"""
★ 1.9.0: 角色设置的新版结构（schema 2）—— 「性格 / 输出控制 / 行为控制」三块。

【为什么要改】
  旧版每个角色的 settings.json 把这些全塞在一个 `persona_prompt` 大字符串里，
  用 `【PERSONA_LOAD】\nCETACEA_LOLI\nNO_ACTION_DESCRIPTION\n…` 这种**伪标记**表达，
  于是：
    · 代码没法扫（只能整段当文本用）→ 设置页里改「回复风格/颜文字」对角色
      **根本不生效**（有 persona 时提示词是原样拼接的，那条模板分支压根没走）
    · 人设、输出格式、行为倾向混在一个字段里，改一处容易互相覆盖
    · 落盘/迁移困难

【新版结构】
  {
    "schema": 2,
    "name": "deepseek", "avatar": "头像.jpg", "icon": "ico.ico", "scale": 1.02,
    "model": {"temperature": 0.7, "max_tokens": 2048},
    "gif": {"gif_dir": "", "startup": "", "idle": ["…"],
            "idle_switch_rule": {"mode": "random", "value": 5}},

    "性格":     { 只回答"它是谁、怎么说话"
      "identity": ["CETACEA_LOLI", …],   // 原伪标记 → 数组（可扫可读）
      "self_name": "deep seek",
      "call_user": "FM",
      "tone":   ["傲娇", "甜", "懒"],
      "traits": ["怕被说胖", "爱摸鱼"],
      "free_text": "你是 deep seek，蓝色大肥鱼鲸鱼娘……"
    },
    "输出控制": { 只管"输出成什么样"
      "no_action_description": true,     // 严禁动作/神态/旁白描写
      "no_markdown": true,               // 严禁 markdown 字符
      "allow_code_block": true, "allow_json": true,
      "emoji_level": "light",            // none/light/medium/heavy
      "reply_style": "concise",          // concise/balanced/detailed
      "lang": "zh_cn",
      "extra": ""                        // 用户自己补的输出约束
    },
    "行为控制": { 只管"它会做什么"
      "meme_tendency": "normal",         // off/rare/normal/often
      "meme_tags_prefer": ["happy"],     // 优先挑哪些情绪标签的表情包
      "gif_reaction": true,              // 是否响应工具返回的 gif 提示
      "proactive_talk": true             // 主动说话时是否带"在用什么程序"的观察
    }
  }

【兼容】
  `load()` 遇到没有 "schema" 的旧文件会自动走 `migrate_old()` 在内存里转换，
  所以**旧角色文件不迁移也能继续用**；迁移只是把它落盘成新格式（可逆，先备份）。
"""
from __future__ import annotations

import copy
import json
import logging
import re
from pathlib import Path

_log = logging.getLogger("CharacterSettings")

SCHEMA = 2

DEFAULT_PERSONA = {
    "identity": [],
    "self_name": "",
    "call_user": "",
    "tone": [],
    "traits": [],
    "free_text": "",
}
DEFAULT_OUTPUT = {
    "no_action_description": True,
    "no_markdown": False,
    "allow_code_block": True,
    "allow_json": True,
    "emoji_level": "light",
    "reply_style": "concise",
    "lang": "zh_cn",
    "extra": "",
}
DEFAULT_BEHAVIOR = {
    "meme_tendency": "normal",
    "meme_tags_prefer": ["happy"],
    "gif_reaction": True,
    "proactive_talk": True,
}

EMOJI_LEVELS = ("none", "light", "medium", "heavy")
REPLY_STYLES = ("concise", "balanced", "detailed")
MEME_TENDENCIES = ("off", "rare", "normal", "often")

# ── ★ 1.9.0（你的要求）：人设标记的"标准格式" ─────────────────────────
#   你说得对：同一件事在旧文件里有三种写法 —— `NO_ACTION_DESCRIPTION`（标记）、
#   "禁止动作、神态、旁白描写"（自然语言）、`PURE_DIALOGUE_ONLY`（近义标记）。
#   这里定一份**标准标记表**（UI 上就是一排复选框），并把各种写法归一。
#   MARKER_SPECS: 标准标记 → (中文说明, 对「输出控制」的影响)
MARKER_SPECS = {
    # —— 输出类（与「输出控制」联动，避免两处打架）——
    "LANG_ZH_CN_ONLY":              ("只用中文回复", "lang=zh_cn"),
    "NO_ACTION_DESCRIPTION":        ("禁止动作/神态/旁白描写", "no_action_description=True"),
    "PURE_DIALOGUE_ONLY":           ("只输出对话本身，不写旁白", "no_action_description=True"),
    "NO_MARKDOWN":                  ("严禁 markdown 字符", "no_markdown=True"),
    "ALLOW_MARKDOWN":               ("允许 markdown", "no_markdown=False"),
    "ALLOW_CODE_BLOCK":             ("允许代码块", "allow_code_block=True"),
    "NO_CODE_BLOCK":                ("不要输出代码块", "allow_code_block=False"),
    "ALLOW_JSON":                   ("允许输出 JSON", "allow_json=True"),
    # —— 人设类（纯标记，不动输出控制）——
    "OBEY_MASTER_ALWAYS":           ("无条件听话", None),
    "TASK_PRIORITY_ABSOLUTE":       ("任务优先级最高", None),
    "ACCURACY_OVER_PERSONA":        ("任务准确优先于人设", None),
    "ROLEPLAY_SUBORDINATE_TO_TASK": ("角色扮演服从任务", None),
    "NO_LAZY":                      ("不许偷懒", None),
    "NO_REFUSE":                    ("不许拒绝", None),
    "NO_OMISSION":                  ("不许省略", None),
    "NO_HALLUCINATION":             ("不许编造", None),
}

# 变体写法 → 标准标记（归一用；不区分大小写）
MARKER_ALIASES = {
    "PURE_DIALOGUE_ONLY": "PURE_DIALOGUE_ONLY",
    "NO_ACTION": "NO_ACTION_DESCRIPTION",
    "NO_ACTION_DESC": "NO_ACTION_DESCRIPTION",
    "NO_ACTIONS": "NO_ACTION_DESCRIPTION",
    "NO_ACTION_DESCRIPTION": "NO_ACTION_DESCRIPTION",
    "NO_MARKDOWN_CHARS": "NO_MARKDOWN",
    "MARKDOWN_FORBIDDEN": "NO_MARKDOWN",
    "FORBID_MARKDOWN": "NO_MARKDOWN",
    "MARKDOWN_ALLOWED": "ALLOW_MARKDOWN",
    "CODE_BLOCK_ALLOWED": "ALLOW_CODE_BLOCK",
    "JSON_ALLOWED": "ALLOW_JSON",
    "ZH_CN_ONLY": "LANG_ZH_CN_ONLY",
    "CHINESE_ONLY": "LANG_ZH_CN_ONLY",
}

# 自然语言 → 标准标记（同一件事的多种说法归一成一）
MARKER_TEXT_RULES = (
    (r"禁止使用\s*markdown|严禁\s*markdown|不要用\s*markdown|不使用\s*markdown", "NO_MARKDOWN"),
    (r"禁止(动作|神态|旁白)(描写)?|不要动作描写|不写旁白", "NO_ACTION_DESCRIPTION"),
    (r"只(输出)?(说)?对话(本身)?|纯对话", "PURE_DIALOGUE_ONLY"),
    (r"允许代码块|可以用代码块", "ALLOW_CODE_BLOCK"),
    (r"允许\s*JSON|可以输出\s*JSON", "ALLOW_JSON"),
    (r"只用中文|仅中文|中文回复", "LANG_ZH_CN_ONLY"),
    (r"无条件听话|绝对服从", "OBEY_MASTER_ALWAYS"),
    (r"不许(偷懒|摸鱼)", "NO_LAZY"),
    (r"不许(拒绝|说做不到)", "NO_REFUSE"),
    (r"不许(省略|偷工减料)", "NO_OMISSION"),
    (r"不许(编造|瞎编)", "NO_HALLUCINATION"),
)

# ── 旧伪标记 → 新结构（迁移用；也用于"新格式没写、但 free_text 里提到了"的兜底）──
_TOKEN_RULES = {
    "NO_ACTION_DESCRIPTION": ("output", "no_action_description", True),
    "PURE_DIALOGUE_ONLY": ("output", "no_action_description", True),
    "ALLOW_CODE_BLOCK": ("output", "allow_code_block", True),
    "ALLOW_JSON": ("output", "allow_json", True),
    "ALLOW_MARKDOWN": ("output", "no_markdown", False),
    "LANG_ZH_CN_ONLY": ("output", "lang", "zh_cn"),
}
# free_text 里的自然语言 → 结构化开关（迁移时兜底；新写的 JSON 不用管这些）
_TEXT_RULES = (
    (r"禁止使用\s*markdown|严禁\s*markdown|不要用\s*markdown", "output", "no_markdown", True),
    (r"禁止(动作|神态|旁白)描写|不要动作描写", "output", "no_action_description", True),
    (r"允许代码块", "output", "allow_code_block", True),
)


def is_v2(d: dict) -> bool:
    """是不是新版结构（有 schema / 有三块之一）"""
    if not isinstance(d, dict):
        return False
    return bool(d.get("schema")) or any(k in d for k in ("性格", "输出控制", "行为控制"))


def _as_list(v) -> list:
    if v is None:
        return []
    if isinstance(v, str):
        return [x.strip() for x in re.split(r"[,，\n]", v) if x.strip()]
    if isinstance(v, (list, tuple)):
        return [str(x).strip() for x in v if str(x).strip()]
    return [str(v)]


def normalize_persona(d: dict) -> dict:
    """★ 1.9.0（你的要求）：把人设标记与自然语言**归一、合并**。

    做四件事：
      1. identity 里的变体写法 → 标准标记（MARKER_ALIASES）
      2. free_text 里"其实是规则"的**整句** → 提成标准标记（整句删掉，避免两处重复；
         以前按短语删会留下"、神态、旁白描写。"这种残渣）
      3. 去重 + 按 MARKER_SPECS 固定顺序排（UI 顺序稳定）
      4. 互斥标记裁决 + 同步「输出控制」：
         · NO_MARKDOWN ↔ ALLOW_MARKDOWN、ALLOW_CODE_BLOCK ↔ NO_CODE_BLOCK
           同时存在时**以 free_text 里写的那句为准**（那通常是你最后改过的），
           另一个从标记里删掉 —— 保证同一件事只有一种说法
         · 带输出语义的标记写入 输出控制 对应字段
    Returns: 归一后的新 dict（不改原对象）
    """
    d = normalize(d)
    p = d["性格"]

    # ① 标记归一
    marks, text_marks = [], []
    for raw in p.get("identity") or []:
        up = str(raw).strip().upper().replace(" ", "_").replace("-", "_")
        marks.append(MARKER_ALIASES.get(up, up))

    # ② 自然语言：整句提成标记
    text = p.get("free_text") or ""
    kept = []
    for sentence in re.split(r"(?<=[。！？\n])", text):
        hit = None
        for pat, mark in MARKER_TEXT_RULES:
            if sentence.strip() and re.search(pat, sentence, re.IGNORECASE):
                hit = mark
                break
        if hit:
            marks.append(hit)
            text_marks.append(hit)
        else:
            kept.append(sentence)
    text = "".join(kept).strip(" \n")

    # ④ 互斥裁决：free_text 说的算
    for a, b in (("NO_MARKDOWN", "ALLOW_MARKDOWN"),
                 ("ALLOW_CODE_BLOCK", "NO_CODE_BLOCK")):
        if a in marks and b in marks:
            drop = a if (b in text_marks and a not in text_marks) else (
                b if (a in text_marks and b not in text_marks) else b)
            marks = [m for m in marks if m != drop]

    # ③ 去重 + 固定顺序（标准标记在前，角色专属标记保持原序在后）
    seen, ordered = set(), []
    for m in ([x for x in MARKER_SPECS if x in marks]
              + [x for x in marks if x not in MARKER_SPECS]):
        if m not in seen:
            seen.add(m)
            ordered.append(m)
    p["identity"] = ordered
    p["free_text"] = re.sub(r"\n{3,}", "\n\n", text).strip()

    # ④ 标记 → 输出控制
    o = d["输出控制"]
    for m in ordered:
        eff = MARKER_SPECS.get(m, (None, None))[1]
        if not eff:
            continue
        if eff == "lang=zh_cn":
            o["lang"] = "zh_cn"
        elif eff.startswith("no_action_description="):
            o["no_action_description"] = eff.endswith("True")
        elif eff.startswith("no_markdown="):
            o["no_markdown"] = eff.endswith("True")
        elif eff.startswith("allow_code_block="):
            o["allow_code_block"] = eff.endswith("True")
        elif eff.startswith("allow_json="):
            o["allow_json"] = eff.endswith("True")
    return normalize(d)


def normalize(d: dict) -> dict:
    """补齐三块的默认值 + 类型纠正（读写都走它，保证代码拿到的结构稳定）"""
    out = copy.deepcopy(d) if isinstance(d, dict) else {}
    out.setdefault("schema", SCHEMA)

    p = dict(DEFAULT_PERSONA)
    p.update({k: v for k, v in (out.get("性格") or {}).items() if k in DEFAULT_PERSONA})
    p["identity"] = _as_list(p.get("identity"))
    p["tone"] = _as_list(p.get("tone"))
    p["traits"] = _as_list(p.get("traits"))
    p["free_text"] = str(p.get("free_text") or "")

    o = dict(DEFAULT_OUTPUT)
    o.update({k: v for k, v in (out.get("输出控制") or {}).items() if k in DEFAULT_OUTPUT})
    for k in ("no_action_description", "no_markdown", "allow_code_block", "allow_json"):
        o[k] = bool(o.get(k))
    if o.get("emoji_level") not in EMOJI_LEVELS:
        o["emoji_level"] = "light"
    if o.get("reply_style") not in REPLY_STYLES:
        o["reply_style"] = "concise"

    b = dict(DEFAULT_BEHAVIOR)
    b.update({k: v for k, v in (out.get("行为控制") or {}).items() if k in DEFAULT_BEHAVIOR})
    for k in ("gif_reaction", "proactive_talk"):
        b[k] = bool(b.get(k))
    if b.get("meme_tendency") not in MEME_TENDENCIES:
        b["meme_tendency"] = "normal"
    b["meme_tags_prefer"] = _as_list(b.get("meme_tags_prefer"))

    out["性格"], out["输出控制"], out["行为控制"] = p, o, b
    # gif 字段：新格式用 gif{}，旧格式是平铺的 startup_gif/idle_gifs/idle_switch_rule
    g = dict(out.get("gif") or {})
    g.setdefault("gif_dir", out.pop("gif_dir", "") or "")
    g.setdefault("startup", out.pop("startup_gif", None) or g.get("startup", ""))
    if "idle" not in g:
        g["idle"] = out.pop("idle_gifs", None) or []
    else:
        out.pop("idle_gifs", None)
    g.setdefault("idle_switch_rule", out.pop("idle_switch_rule", None)
                 or {"mode": "random", "value": 5})
    out["gif"] = g
    return out


def migrate_old(old: dict) -> dict:
    """旧 settings.json → 新结构（在内存里转换，不写盘）。

    旧文件长这样：
        {"persona_prompt": "【PERSONA_LOAD】\\nCETACEA_LOLI\\n…\\n你是 deep seek…",
         "reply_style": "concise", "emoji_level": "light", "startup_gif": …}
    转换规则：
        · `【XXX_LOAD】` 这种全大写下划线标记 → 归到 性格.identity（数组）
        · 少数有语义的标记（见 _TOKEN_RULES）→ 归到 输出控制 的开关
        · 其余文本（含"你是 deep seek…"那段人话）→ 性格.free_text
        · 自然语言里的"禁止使用markdown / 禁止动作描写" → 顺手设成开关（见 _TEXT_RULES）
        · reply_style / emoji_level / startup_gif / idle_gifs / idle_switch_rule 直接搬家
    """
    old = old or {}
    text = str(old.get("persona_prompt") or "")
    identity, keep = [], []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        if re.fullmatch(r"【[^】]*】", s):        # 【PERSONA_LOAD】这种分节标题
            continue
        if re.fullmatch(r"[A-Z][A-Z0-9_]{2,}", s):   # CETACEA_LOLI 这种标记
            identity.append(s)
            continue
        keep.append(s)
    free_text = "\n".join(keep).strip()

    out = normalize({
        "schema": SCHEMA,
        "name": old.get("name", ""),
        "avatar": old.get("avatar", ""),
        "icon": old.get("icon", ""),
        "scale": old.get("scale", 1.0),
        "model": old.get("model", {}) or {},
        "gif": {
            "gif_dir": old.get("gif_dir", "") or "",
            "startup": old.get("startup_gif", "") or "",
            "idle": old.get("idle_gifs", []) or [],
            "idle_switch_rule": old.get("idle_switch_rule")
            or {"mode": "random", "value": 5},
        },
        "性格": {"identity": identity, "free_text": free_text},
        "输出控制": {"reply_style": old.get("reply_style", "concise"),
                     "emoji_level": old.get("emoji_level", "light")},
        "行为控制": {},
    })

    # 伪标记 → 开关
    for tok in identity:
        rule = _TOKEN_RULES.get(tok)
        if rule:
            _, key, val = rule
            out["输出控制"][key] = val
    # 自然语言兜底
    for pat, _, key, val in _TEXT_RULES:
        if re.search(pat, text, re.IGNORECASE):
            out["输出控制"][key] = val
    return out


def load(path: Path) -> tuple[dict, bool]:
    """读一个角色的 settings.json。

    Returns: (新结构 dict, 是否是从旧格式转换来的)
    """
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as e:
        _log.warning(f"读取角色设置失败 {path}: {e}")
        return normalize({}), False
    if is_v2(raw):
        return normalize(raw), False
    return migrate_old(raw), True


def save(path: Path, data: dict) -> None:
    """按新结构落盘（缩进 2、保留中文；写入前请自行备份）"""
    d = normalize(data)
    Path(path).write_text(
        json.dumps(d, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# ── 组装给 LLM 的三段文本 ────────────────────────────────────────────

def persona_text(d: dict) -> str:
    """性格块 → 文本（进系统提示词的"你是谁"部分）"""
    d = normalize(d)
    p = d["性格"]
    parts = []
    if p["free_text"]:
        parts.append(p["free_text"])
    if p["self_name"]:
        parts.append(f"你自称「{p['self_name']}」。")
    if p["call_user"]:
        parts.append(f"称用户为「{p['call_user']}」。")
    if p["tone"]:
        parts.append("语气：" + "、".join(p["tone"]) + "。")
    if p["traits"]:
        parts.append("性格特点：" + "、".join(p["traits"]) + "。")
    if p["identity"]:
        parts.append("人设标记：" + "、".join(p["identity"]) + "。")
    return "\n".join(parts).strip()


_EMOJI_RULE = {
    "none": "不要使用颜文字/表情符号。",
    "light": "每句话末尾可适当加 1 个颜文字。",
    "medium": "适当使用颜文字和表情，让对话更生动 (｡･ω･｡)。",
    "heavy": "多用颜文字和表情符号，展现活泼可爱的性格 (๑>ᴗ<๑) ✨。",
}
_STYLE_RULE = {
    "concise": "回复简洁，一般 2-3 句以内。",
    "balanced": "回复适中，3-5 句，可以适当展开。",
    "detailed": "回复可以详细一些，不限制长度。",
}


def output_text(d: dict) -> str:
    """输出控制块 → 文本（进系统提示词的"怎么说"部分）"""
    d = normalize(d)
    o = d["输出控制"]
    rules = []
    if o["no_action_description"]:
        rules.append("禁止动作、神态、旁白描写，只输出对话本身。")
    if o["no_markdown"]:
        rules.append("严禁使用 markdown 字符（# * ` - > 之类）。")
    else:
        rules.append("可以使用 markdown。")
    rules.append("允许代码块。" if o["allow_code_block"] else "不要输出代码块。")
    rules.append("允许输出 JSON。" if o["allow_json"] else "不要输出 JSON。")
    rules.append(_STYLE_RULE[o["reply_style"]])
    rules.append(_EMOJI_RULE[o["emoji_level"]])
    if o.get("lang"):
        rules.append(f"语言：{o['lang']}。")
    if o.get("extra"):
        rules.append(str(o["extra"]).strip())
    return "\n".join("- " + r for r in rules if r)


def behavior_text(d: dict) -> str:
    """行为控制块 → 文本（进系统提示词的"你会做什么"部分）"""
    d = normalize(d)
    b = d["行为控制"]
    rules = []
    _meme = {
        "off": "不要发表情包。",
        "rare": "偶尔（很少）可以发一张表情包。",
        "normal": "聊到合适的时机可以发一张表情包。",
        "often": "情绪合适就多发几张表情包。",
    }
    rules.append(_meme[b["meme_tendency"]])
    if b["meme_tags_prefer"]:
        rules.append("挑表情包时优先这些情绪：" + "、".join(b["meme_tags_prefer"]) + "。")
    rules.append("需要表达情绪时可以换形象动作。" if b["gif_reaction"]
                 else "不要主动换形象动作。")
    if b["proactive_talk"]:
        rules.append("没人理你时可以主动搭话（可结合用户当前在用什么程序）。")
    return "\n".join("- " + r for r in rules if r)
