# -*- coding: utf-8 -*-
"""最终统一口径：全部用 len(read().splitlines()) 统计。只读。"""
import ast, os, collections

ROOT = os.path.dirname(os.path.abspath(__file__))
def rel(p): return os.path.relpath(p, ROOT).replace("\\", "/")

files = []
for d in ["src", "tools"]:
    for dp, dns, fns in os.walk(os.path.join(ROOT, d)):
        dns[:] = [x for x in dns if x != "__pycache__"]
        for fn in fns:
            if fn.endswith(".py"): files.append(os.path.join(dp, fn))
files.sort()

info = {}
for f in files:
    src = open(f, encoding="utf-8").read()
    lines = len(src.splitlines())
    try:
        tree = ast.parse(src)
        cls = [n.name for n in tree.body if isinstance(n, ast.ClassDef)]
    except SyntaxError:
        cls = []
    info[rel(f)] = (lines, cls)

print("### BUCKETS (splitlines)")
b = collections.Counter(); c = collections.Counter()
def bucket(p):
    for k, pre in [("src/core", "src/core/"), ("src/ui", "src/ui/"),
                   ("src/utils", "src/utils/"), ("src/genie_tts", "src/genie_tts/"),
                   ("tools/dev_tests", "tools/dev_tests/"), ("tools/_vendor", "tools/_vendor/")]:
        if p.startswith(pre): return k
    return "tools/(top)"
for p, (n, _) in info.items():
    k = bucket(p); b[k] += n; c[k] += 1
tot = 0
for k in sorted(b, key=lambda x: -b[x]):
    print(f"{k:<16} files={c[k]:>4}  lines={b[k]:>6}"); tot += b[k]
print(f"{'TOTAL':<16} files={sum(c.values()):>4}  lines={tot:>6}")

print()
print("### TOP 20 BY LINES (all)")
for p, (n, cls) in sorted(info.items(), key=lambda x: -x[1][0])[:20]:
    print(f"{n:>6}  {p}  classes={len(cls)}")

print()
print("### TOP 15 BY LINES (exclude tools/dev_tests)")
for p, (n, cls) in sorted([x for x in info.items() if "dev_tests" not in x[0]],
                          key=lambda x: -x[1][0])[:15]:
    print(f"{n:>6}  {p}  classes={len(cls)}")

print()
print("### >1 CLASS (exclude dev_tests / _vendor), sorted by class count desc")
for p, (n, cls) in sorted(info.items(), key=lambda x: -len(x[1][1])):
    if len(cls) > 1 and "dev_tests" not in p and "_vendor" not in p:
        print(f"{len(cls):>3}  {p:<48} {cls}")

print()
print("### >1 CLASS in tools/dev_tests (top 10)")
for p, (n, cls) in sorted([x for x in info.items() if "dev_tests" in x[0]],
                          key=lambda x: -len(x[1][1]))[:10]:
    if len(cls) > 1:
        print(f"{len(cls):>3}  {p:<48} lines={n}")
