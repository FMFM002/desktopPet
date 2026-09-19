# -*- coding: utf-8 -*-
"""量一下"保存设置 / 启动"这条路上，主线程到底在等什么。

只做纯 Python 侧计时（不起 Qt）：CharacterInfo 扫描、settings.json 读写、
config.save() 落盘。这些恰恰是 GUI 线程里同步做的部分。
"""
import json
import sys
import time
from pathlib import Path

ROOT = Path(".").resolve()
sys.path.insert(0, str(ROOT))   # 脚本被 `python tools\...` 起时 sys.path[0] 是脚本目录


def t(fn, n=5):
    fn()  # 预热
    best = 1e9
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        best = min(best, (time.perf_counter() - t0) * 1000)
    return best


cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
char = cfg["pet"]["character"]
chars_dir = ROOT / "data" / "桌宠形象"
cdir = chars_dir / char
print(f"当前角色: {char}   目录: {cdir.relative_to(ROOT)}")
print(f"角色目录存在: {cdir.exists()}")

# 角色目录里的文件规模（rglob 扫的就是这些）
if cdir.exists():
    files = [f for f in cdir.rglob("*") if f.is_file()]
    tot = sum(f.stat().st_size for f in files)
    imgs = [f for f in files if f.suffix.lower() in (".gif", ".png", ".webp")]
    print(f"文件数: {len(files)}   其中图片: {len(imgs)}   总大小: {tot/1024/1024:.1f} MB")

print("\n== 1. 目录遍历（rglob）自身 ==")
print(f"   cdir.rglob('*') 全量走一遍: {t(lambda: [f for f in cdir.rglob('*')], 3):.2f} ms")

print("\n== 2. CharacterInfo(角色目录) —— 设置页保存时**在 GUI 线程**构造的那个 ==")
try:
    from src.core.pet_manager import CharacterInfo

    def build():
        return CharacterInfo(cdir)

    ms = t(build, 5)
    info = build()
    print(f"   CharacterInfo(...)          : {ms:.2f} ms   (GIF×{len(info.gifs)})")
except Exception as e:
    print("   构造失败:", e)

print("\n== 3. character_settings.load / save ==")
try:
    from src.core import character_settings as cs

    sp = cdir / "settings.json"
    print(f"   load(settings.json)         : {t(lambda: cs.load(sp), 5):.2f} ms")
    _d, _old = cs.load(sp)
    print(f"   save(内存里那份 -> 磁盘)     : {t(lambda: cs.save(sp, _d), 5):.2f} ms"
          f"   (settings.json {sp.stat().st_size/1024:.1f} KB)")

    def noop():
        return None
    print(f"   save 到临时文件（不覆盖真文件）: {t(lambda: cs.save(ROOT / 'data' / 'downloads' / '_scratch' / '_cs_probe.json', _d), 5):.2f} ms")
except Exception as e:
    print("   character_settings 失败:", e)

print("\n== 4. config.save() 落盘（设置页保存时同步调用）==")
try:
    from src.utils.config import get_config

    c = get_config()
    print(f"   config.save()               : {t(c.save, 5):.2f} ms"
          f"   (config.json {(ROOT/'config.json').stat().st_size/1024:.1f} KB)")
except Exception as e:
    print("   失败:", e)

print("\n== 5. 角色表规模（切角色时要扫全部角色目录）==")
t0 = time.perf_counter()
n_char, n_all = 0, 0
for d in sorted(chars_dir.iterdir()):
    if d.is_dir() and not d.name.startswith("."):
        n_char += 1
        n_all += len([f for f in d.rglob("*") if f.is_file()])
print(f"   角色数: {n_char}   全部角色文件合计: {n_all}   全扫耗时: {(time.perf_counter()-t0)*1000:.2f} ms")
