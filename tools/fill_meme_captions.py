#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""给表情包补 caption / keywords —— ★ 1.9.0（你的要求）

【为什么需要它】
  `src/core/meme_library.py` 给 LLM 看的目录行是
      `  文件名  [标签] caption（没有 caption 就退化成 keywords，再没有就退化成包名）`
  —— 所以 `data/memes/dafeiyu-001` 那 24 张虽然 tag 分好了，但 caption/keywords
  是空的，LLM 只能看到"dafeiyu-001"，等于挑不了。

【怎么补的（你指定的做法）】
  那一包每个文件名都是**拼音**（bixin / shengqi / tangping …），
  所以**不需要图片识别**，直接按拼音推意图 —— 下表的映射就是"推测"的全部依据。
  不在表里的文件**原样跳过**（宁可不补，也不瞎编）。

【用法】
    python tools/fill_meme_captions.py            # 补全部缺 caption 的条目
    python tools/fill_meme_captions.py --dry-run  # 只看会补什么
  幂等：只写 caption/keywords 为空的条目，已有内容不覆盖。改盘前备份 index.json.bak。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "data" / "memes" / "index.json"

# 拼音文件名 → (caption, keywords)
# caption 是"这张图想说什么"，keywords 是给检索/挑图用的补充词（含拼音，方便按名字找）
BY_STEM = {
    # ── 情绪：生气/嫌弃/无语/震惊/害怕 ──
    "shengqi":      ("哼，我生气了！（炸毛）", "生气 愤怒 炸毛 shengqi angry"),
    "xianqi":       ("你好恶心哦……（一脸嫌弃）", "嫌弃 鄙视 恶心 xianqi"),
    "wuyu":         ("……我无语了（沉默）", "无语 沉默 无奈 wuyu speechless"),
    "zhenjing":     ("震惊！（瞳孔地震）", "震惊 惊讶 吓到 zhenjing surprised"),
    "xia":          ("吓我一跳！（惊恐）", "吓到 惊恐 害怕 xia"),
    "yun":          ("我晕……（转圈圈）", "晕 头大 蒙了 yun"),
    # ── 情绪：开心/得意/害羞/心动 ──
    "deyi":         ("嘿嘿，我厉害吧（得意）", "得意 骄傲 炫耀 deyi happy"),
    "piaole":       ("我飘了～（得意忘形）", "飘了 得意 膨胀 piaole"),
    "ok":           ("OK，没问题（点头）", "ok 好的 没问题 收到 ok"),
    "haixiu":       ("别看我啦（害羞）", "害羞 脸红 不好意思 haixiu shy"),
    "bixin":        ("给你比个心（爱心）", "比心 喜欢 爱心 bixin like"),
    "huachi":       ("哇，好帅……（花痴脸）", "花痴 心动 好色 huachi color"),
    "maozhua":      ("喵～（伸爪子）", "猫爪 喵 可爱 maozhua meow"),
    # ── 情绪：难过/求饶/拒绝 ──
    "qiurao":       ("饶了我吧……（哭）", "求饶 认输 可怜 哭 qiurao sad"),
    "buyao":        ("不要不要！（摆手拒绝）", "不要 拒绝 不行 buyao no"),
    "tangping":     ("算了，躺平（摊成一片）", "躺平 放弃 摆烂 累 tangping"),
    # ── 日常：吃饭/睡觉/上班/打招呼 ──
    "ganfan":       ("干饭时间到！（端碗）", "干饭 吃饭 饿了 ganfan daily"),
    "shuijiao":     ("困了，睡觉去了（打哈欠）", "睡觉 困了 晚安 shuijiao sleep"),
    "jiaban":       ("又要加班……（瘫）", "加班 上班 累了 jiaban work"),
    "zaoshanghao":  ("早上好呀（打招呼）", "早上好 早安 打招呼 zaoshanghao morning"),
    "yaoqian":      ("给钱！快给钱！（伸手）", "要钱 打钱 伸手 yaoqian money"),
    # ── 其它：加载/问号/偷看 ──
    "jiazai":       ("正在加载中……别催（转圈）", "加载 卡住 等待 jiazai loading"),
    "wenhao":       ("？？？你在说什么（满头问号）", "问号 疑惑 不解 什么 wenhao confused"),
    "toukan":       ("偷偷看一眼……（暗中观察）", "偷看 暗中观察 探头 toukan peek"),
}


def fill(index_path: Path = INDEX, dry_run: bool = False) -> tuple:
    """补 caption/keywords。Returns: (补了几条, 跳过几条, 改动的文件名列表)"""
    data = json.loads(index_path.read_text(encoding="utf-8"))
    filled, skipped, touched = 0, 0, []
    for pack in data.get("packs", []) or []:
        for m in pack.get("memes", []) or []:
            if (m.get("caption") or "").strip():
                continue                       # 已有内容 → 不覆盖
            stem = Path(m.get("file") or "").stem.lower()
            hit = BY_STEM.get(stem)
            if not hit:
                skipped += 1
                continue
            cap, kw = hit
            if dry_run:
                touched.append(f"{m.get('file')} → {cap}")
            else:
                m["caption"] = cap
                if not (m.get("keywords") or "").strip():
                    m["keywords"] = kw
                touched.append(m.get("file"))
            filled += 1
    if not dry_run and filled:
        bak = index_path.with_suffix(index_path.suffix + ".bak")
        if not bak.exists():
            shutil.copy2(index_path, bak)
        index_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return filled, skipped, touched


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    try:
        filled, skipped, touched = fill(dry_run=args.dry_run)
    except Exception as e:
        print(f"失败: {e}")
        return 1
    for t in touched:
        print("  ·", t)
    print(f"{'会补' if args.dry_run else '已补'} {filled} 条；"
          f"表里没有、原样跳过 {skipped} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
