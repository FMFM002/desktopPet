# -*- coding: utf-8 -*-
"""把我误 revert 掉的用户真实配置重建回来（证据来自用户 09-19 16:56 真机日志）。

我干了什么：看到 git 里 config.json 是「测试形象(deepseek)」，就 git checkout 回了
HEAD 的「铃兰」。但日志证明**反了** —— 用户的真机一直跑 deepseek，而且日志里
"多模态直传: 开启/关闭"、"[resident] long_term_memory 已绑定" 两条都跟工作区那份
对得上（用户在 17:07 亲口说"我吧多模态关闭了"）。所以工作区那份才是用户在用的，
HEAD 那份是 01:24 之前被测试写脏后我又提交进去的。

做法：用**程序自己的**合成器/写入器重建，避免我手抄提示词抄错：
  · persona  → character_settings.persona_text(deepseek/settings.json)
  · 输出控制/行为控制/model/scale → 同文件
  · vision.multimodal = False（用户 17:07 亲自关的）
  · tool_policies.long_term_memory = "resident" + enabled_tools 里补回该工具
"""
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(".").resolve()
sys.path.insert(0, str(ROOT))
SCRATCH = ROOT / "data" / "downloads" / "_scratch"
SCRATCH.mkdir(parents=True, exist_ok=True)

# 0. 先把当前(我搞错的)两份都留档，万一要回退
shutil.copy2(ROOT / "config.json", SCRATCH / "_config.wrong_linglan.json")
if (ROOT / ".env").exists():
    shutil.copy2(ROOT / ".env", SCRATCH / "_env_before.json.txt")

from src.core import character_settings as cs           # noqa: E402
from src.utils.config import get_config                 # noqa: E402

char = "deepseek"
cdir = ROOT / "data" / "桌宠形象" / char
sd, was_old = cs.load(cdir / "settings.json")
print(f"读 {char}/settings.json: schema旧版={was_old}")

cfg = get_config()
p = cfg.pet
p.character = char
p.scale = float(sd.get("scale", 1.0) or 1.0)
p.personality_prompt = cs.persona_text(sd)

out, beh = sd["输出控制"], sd["行为控制"]
p.reply_style = out["reply_style"]
p.emoji_level = out["emoji_level"]
for k in ("no_action_description", "no_markdown", "allow_code_block", "allow_json"):
    setattr(p, k, bool(out[k]))
for k in ("meme_tendency", "gif_reaction", "proactive_talk"):
    setattr(p, k, beh[k])
p.meme_tags_prefer = list(beh["meme_tags_prefer"])
for k in ("temperature", "max_tokens"):
    if sd.get("model", {}).get(k) is not None:
        setattr(cfg.llm, k, sd["model"][k])

cfg.vision.multimodal = False          # 用户 17:07 亲口"我把多模态关闭了"
cfg.tool_policies["long_term_memory"] = "resident"
if "long_term_memory" not in cfg.enabled_tools:
    cfg.enabled_tools.append("long_term_memory")
    cfg.enabled_tools.sort()

cfg.save()
print("已用程序的 save() 写回 config.json")

d = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
print("\n== 结果核对 ==")
print("  character      :", d["pet"]["character"])
print("  scale          :", d["pet"]["scale"])
print("  reply/emoji    :", d["pet"]["reply_style"], "/", d["pet"]["emoji_level"])
print("  no_markdown    :", d["pet"]["no_markdown"])
print("  multimodal     :", d["vision"]["multimodal"])
print("  ltm policy     :", d["tool_policies"]["long_term_memory"])
print("  ltm in enabled :", "long_term_memory" in d["enabled_tools"])
print("  persona 字数    :", len(d["pet"]["personality_prompt"]))
print("  persona 含人设标记:", "人设标记" in d["pet"]["personality_prompt"])
print("  persona 首行    :", d["pet"]["personality_prompt"].splitlines()[0][:40])
