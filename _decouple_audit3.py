# -*- coding: utf-8 -*-
"""解耦调研 第3部分：区分 top-level import 与函数内 lazy import 的依赖图。只读。"""
import ast, os, collections, sys

ROOT = os.path.dirname(os.path.abspath(__file__))
DIRS = ["src", "tools"]

def rel(p): return os.path.relpath(p, ROOT).replace("\\", "/")

files = []
for d in DIRS:
    for dp, dns, fns in os.walk(os.path.join(ROOT, d)):
        dns[:] = [x for x in dns if x != "__pycache__"]
        for fn in fns:
            if fn.endswith(".py"): files.append(os.path.join(dp, fn))
files.sort()

def modname(f):
    r = rel(f)
    if r.endswith("__init__.py"):
        r = r[: -len("__init__.py")].rstrip("/")
    else:
        r = r[:-3]
    return r.replace("/", ".")

mod_of = {f: modname(f) for f in files}
all_mods = set(mod_of.values())

edges_tl = collections.defaultdict(dict)   # (top-level only) m -> {target: [lineno,...]}
edges_all = collections.defaultdict(dict)  # incl. nested

for f in files:
    m = mod_of[f]
    src = open(f, encoding="utf-8").read()
    try: tree = ast.parse(src)
    except SyntaxError: continue
    pkg_prefix = m.split(".") if os.path.basename(f) == "__init__.py" else m.split(".")[:-1]

    # map node -> depth info: mark top-level by walking tree.body sequence
    def handle(node, top):
        tgts = []
        if isinstance(node, ast.Import):
            for a in node.names:
                tgts.append(a.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                up = node.level - 1
                base = pkg_prefix[: len(pkg_prefix) - up] if up else pkg_prefix
                tgt = ".".join(base + ([node.module] if node.module else []))
            else:
                tgt = node.module or ""
            tgts.append(tgt)
            for a in node.names:
                cand = (tgt + "." + a.name) if tgt else a.name
                if cand in all_mods: tgts.append(cand)
        resolved = []
        for t in tgts:
            if t in all_mods: resolved.append(t)
        d = edges_tl if top else None
        for t in resolved:
            edges_all[m].setdefault(t, [])
            if node.lineno not in edges_all[m][t]: edges_all[m][t].append(node.lineno)
            if top:
                edges_tl[m].setdefault(t, [])
                if node.lineno not in edges_tl[m][t]: edges_tl[m][t].append(node.lineno)

    for st in tree.body:
        handle(st, True) if isinstance(st, (ast.Import, ast.ImportFrom)) else None
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            # skip if it is a direct top-level statement
            if node in tree.body: continue
            handle(node, False)

# ---- SCC helper ----
sys.setrecursionlimit(20000)
def sccs(edges):
    index, low, onstack, stack, out = {}, {}, {}, [], []
    cnt = [0]
    def sc(v):
        index[v] = low[v] = cnt[0]; cnt[0] += 1
        stack.append(v); onstack[v] = True
        for w in sorted(edges.get(v, {})):
            if w not in index:
                sc(w); low[v] = min(low[v], low[w])
            elif onstack.get(w): low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            c = []
            while True:
                w = stack.pop(); onstack[w] = False; c.append(w)
                if w == v: break
            if len(c) > 1: out.append(c)
    for v in sorted(all_mods):
        if v not in index: sc(v)
    return out

def minimal_cycles(edges, comp, maxlen=8):
    cs = set(comp)
    sub = {m: sorted(t for t in edges[m] if t in cs) for m in comp}
    res = []
    def dfs(start, cur, path, seen):
        if len(path) > maxlen: return
        for nxt in sub.get(cur, ()):
            if nxt == start and len(path) >= 2:
                res.append(list(path) + [start])
            elif nxt not in seen:
                seen.add(nxt); path.append(nxt)
                dfs(start, nxt, path, seen)
                path.pop(); seen.discard(nxt)
    for m in sorted(comp): dfs(m, m, [m], {m})
    # dedupe by canonical rotation
    seen = set(); out = []
    for c in sorted(res, key=len):
        body = c[:-1]
        key = min(tuple(body[i:] + body[:i]) for i in range(len(body)))
        if key in seen: continue
        seen.add(key); out.append(c)
    return out

print("=" * 100)
print("### 2A-1. SCC / CYCLES — TOP-LEVEL IMPORTS ONLY (breaks at import time)")
print("=" * 100)
for comp in sccs(edges_tl):
    print(f"\n[SCC size={len(comp)}] " + ", ".join(sorted(comp)))
    for cyc in minimal_cycles(edges_tl, comp):
        print("   cycle: " + " -> ".join(cyc))
        for a, b in zip(cyc, cyc[1:]):
            print(f"      {a}:{edges_tl[a][b]} -> {b}")

print()
print("=" * 100)
print("### 2A-2. SCC / CYCLES — INCLUDING FUNCTION-LEVEL (LAZY) IMPORTS")
print("=" * 100)
for comp in sccs(edges_all):
    print(f"\n[SCC size={len(comp)}] " + ", ".join(sorted(comp)))
    for cyc in minimal_cycles(edges_all, comp, maxlen=6)[:12]:
        print("   cycle: " + " -> ".join(cyc))

print()
print("=" * 100)
print("### 2C-refined. FAN-IN (excluding tools/dev_tests as importers)")
print("=" * 100)
fanin_mods = collections.Counter()
fanin_stmts = collections.Counter()
for m, tgts in edges_all.items():
    if "dev_tests" in m: continue
    for t, lns in tgts.items():
        fanin_mods[t] += 1
        fanin_stmts[t] += len(lns)
rows = sorted(fanin_mods.items(), key=lambda x: (-x[1], -fanin_stmts[x[0]]))
print(f"{'module':<44} {'#importers':>10} {'#stmts':>7}")
for m, c in rows[:25]:
    print(f"{m:<44} {c:>10} {fanin_stmts[m]:>7}")

print()
print("--- top 12 with importer list (prod only) ---")
for m, c in rows[:12]:
    imp = sorted(x for x in edges_all if m in edges_all[x] and "dev_tests" not in x)
    print(f"\n{m}  ({c} prod importers)")
    for i in imp: print(f"    {i}")

print()
print("=" * 100)
print("### 2E. tools/* -> src/* edges (production tools only, any depth)")
print("=" * 100)
n = 0
for m in sorted(edges_all):
    if not m.startswith("tools.") or "dev_tests" in m or "_vendor" in m: continue
    for t in sorted(edges_all[m]):
        if t.startswith("src."):
            print(f"{m} -> {t}   at lines {sorted(edges_all[m][t])}")
            n += 1
print("(none)" if n == 0 else f"total={n}")

print()
print("=" * 100)
print("### 2F. src/* -> tools.* edges (production only)")
print("=" * 100)
for m in sorted(edges_all):
    if not m.startswith("src.") or "genie_tts" in m: continue
    for t in sorted(edges_all[m]):
        if t.startswith("tools"):
            print(f"{m} -> {t}   at lines {sorted(edges_all[m][t])}")

print()
print("=" * 100)
print("### 2G. src/ui/* -> src/core/* edges (file:line)")
print("=" * 100)
for m in sorted(edges_all):
    if not m.startswith("src.ui"): continue
    for t in sorted(edges_all[m]):
        if t.startswith("src.core"):
            print(f"{m}:{sorted(edges_all[m][t])} -> {t}")

print()
print("=" * 100)
print("### 2H. top-level (non-lazy) imports inside src/core/* and src/ui/* — module import order weight")
print("=" * 100)
for m in sorted(edges_tl):
    if m.startswith("src.ui"):
        print(f"{m}  ->  {sorted(edges_tl[m])}")
