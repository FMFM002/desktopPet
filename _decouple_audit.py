# -*- coding: utf-8 -*-
"""解耦调研：AST 静态分析。只读，不修改任何源文件。"""
import ast, os, sys, json, collections

ROOT = os.path.dirname(os.path.abspath(__file__))
TARGET_DIRS = ["src", "tools"]

def rel(p):
    return os.path.relpath(p, ROOT).replace("\\", "/")

files = []
for d in TARGET_DIRS:
    for dp, dns, fns in os.walk(os.path.join(ROOT, d)):
        dns[:] = [x for x in dns if x != "__pycache__"]
        for fn in fns:
            if fn.endswith(".py"):
                files.append(os.path.join(dp, fn))
files.sort()

# ---------- 1. size + classes ----------
info = {}
for f in files:
    src = open(f, encoding="utf-8").read()
    lines = src.count("\n") + (0 if src.endswith("\n") else 1)
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        info[rel(f)] = {"lines": lines, "classes": [], "err": str(e)}
        continue
    classes = [n.name for n in tree.body if isinstance(n, ast.ClassDef)]
    info[rel(f)] = {"lines": lines, "classes": classes}

print("=" * 70)
print("### 1A. TOP 20 BY LINES (all)")
print("=" * 70)
for k, v in sorted(info.items(), key=lambda x: -x[1]["lines"])[:20]:
    print(f"{v['lines']:>6}  {k}   classes={len(v['classes'])}")

print()
print("=" * 70)
print("### 1B. TOP 15 BY LINES (exclude tools/dev_tests)")
print("=" * 70)
for k, v in sorted([x for x in info.items() if "dev_tests" not in x[0]],
                   key=lambda x: -x[1]["lines"])[:15]:
    print(f"{v['lines']:>6}  {k}")

print()
print("=" * 70)
print("### 1C. FILES WITH >1 CLASS")
print("=" * 70)
for k, v in sorted(info.items(), key=lambda x: -len(x[1]["classes"])):
    if len(v["classes"]) > 1 and "dev_tests" not in k:
        print(f"{len(v['classes']):>3}  {k}  -> {v['classes']}")

print()
print("=" * 70)
print("### 1D. TOTALS")
print("=" * 70)
tot = sum(v["lines"] for v in info.values())
tot_nt = sum(v["lines"] for k, v in info.items() if "dev_tests" not in k)
print(f"files={len(info)} total_lines={tot} non_devtests_lines={tot_nt}")

# ---------- 2. import graph ----------
def modname(f):
    r = rel(f)
    if r.endswith("/__init__.py"):
        r = r[: -len("/__init__.py")]
    elif r.endswith("__init__.py"):
        r = r[: -len("__init__.py")].rstrip("/")
    else:
        r = r[:-3]
    return r.replace("/", ".")

mod_of = {f: modname(f) for f in files}
all_mods = set(mod_of.values())

def resolve(base_mod, node, level):
    """resolve an ImportFrom module string to an absolute module name."""
    if level and level > 0:
        parts = base_mod.split(".")
        # base_mod is the module itself; package = base_mod if __init__ else parent
        if not base_mod.endswith("__init__"):
            pkg = parts[:-1]
        else:
            pkg = parts
        # find which file this is
        return None
    return node.module

def is_pkg_init(f):
    return os.path.basename(f) == "__init__.py"

edges = collections.defaultdict(set)   # module -> set(module)  (only internal targets)
raw_imports = collections.defaultdict(list)  # module -> [(lineno, text)]

for f in files:
    src = open(f, encoding="utf-8").read()
    try:
        tree = ast.parse(src)
    except SyntaxError:
        continue
    m = mod_of[f]
    pkg_parts = m.split(".")
    pkg_prefix = pkg_parts if is_pkg_init(f) else pkg_parts[:-1]

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                raw_imports[m].append((node.lineno, f"import {a.name}"))
                if a.name in all_mods or any(x.startswith(a.name + ".") for x in all_mods):
                    edges[m].add(a.name)
        elif isinstance(node, ast.ImportFrom):
            txt = f"from {'.'*node.level}{node.module or ''} import " + ", ".join(a.name for a in node.names)
            raw_imports[m].append((node.lineno, txt))
            if node.level:  # relative
                up = node.level - 1
                base = pkg_prefix[: len(pkg_prefix) - up] if up else pkg_prefix
                tgt = ".".join(base + ([node.module] if node.module else []))
            else:
                tgt = node.module or ""
            if tgt in all_mods:
                edges[m].add(tgt)
            else:
                # maybe importing a submodule x.y from package x
                for x in all_mods:
                    if x.startswith(tgt + "."):
                        pass
                # add deep target if any imported name makes it a module
                for a in node.names:
                    cand = (tgt + "." + a.name) if tgt else a.name
                    if cand in all_mods:
                        edges[m].add(cand)

# ---------- 2a. cycles (Tarjan SCC) ----------
import sys as _s
_s.setrecursionlimit(10000)

index = {}
low = {}
onstack = {}
stack = []
sccs = []
counter = [0]

def strongconnect(v):
    index[v] = low[v] = counter[0]
    counter[0] += 1
    stack.append(v)
    onstack[v] = True
    for w in sorted(edges.get(v, ())):
        if w not in index:
            strongconnect(w)
            low[v] = min(low[v], low[w])
        elif onstack.get(w):
            low[v] = min(low[v], index[w])
    if low[v] == index[v]:
        comp = []
        while True:
            w = stack.pop()
            onstack[w] = False
            comp.append(w)
            if w == v:
                break
        if len(comp) > 1:
            sccs.append(comp)

for v in sorted(all_mods):
    if v not in index:
        strongconnect(v)

print()
print("=" * 70)
print("### 2A. CYCLES (SCC size>1)")
print("=" * 70)
if not sccs:
    print("none")
for comp in sorted(sccs, key=lambda c: -len(c)):
    print(f"\n[SCC size={len(comp)}]")
    for m in sorted(comp):
        outs = sorted(x for x in edges[m] if x in comp)
        print(f"   {m}")
        for o in outs:
            print(f"      -> {o}")

# find explicit short cycles among SCC members via DFS
print()
print("--- minimal cycles within each SCC (length<=6) ---")
found = []
for comp in sccs:
    cs = set(comp)
    sub = {m: sorted(x for x in edges[m] if x in cs) for m in comp}

    def dfs(start, cur, path, seen):
        if len(path) > 6:
            return
        for nxt in sub[cur]:
            if nxt == start and len(path) >= 2:
                cyc = tuple(path)
                key = tuple(sorted(cyc))
                if key not in [f[0] for f in found]:
                    found.append((key, list(path) + [start]))
            elif nxt not in seen:
                seen.add(nxt)
                path.append(nxt)
                dfs(start, nxt, path, seen)
                path.pop()
                seen.discard(nxt)

    for m in sorted(comp):
        dfs(m, m, [m], {m})
seen_out = set()
for key, path in sorted(found, key=lambda x: len(x[1])):
    if key in seen_out:
        continue
    seen_out.add(key)
    print("  " + "  ->  ".join(path))

# ---------- 2b. layer violations ----------
print()
print("=" * 70)
print("### 2B. LAYER VIOLATIONS (by file:line)")
print("=" * 70)

def scan(pattern_prefixes, src_prefix, label):
    print(f"\n--- {label} ---")
    hits = 0
    for f in files:
        r = rel(f)
        if not r.startswith(src_prefix):
            continue
        src = open(f, encoding="utf-8").read().splitlines()
        for i, line in enumerate(src, 1):
            s = line.strip()
            if s.startswith("#"):
                continue
            for p in pattern_prefixes:
                if p in s:
                    print(f"{r}:{i}: {s[:160]}")
                    hits += 1
                    break
    if hits == 0:
        print("(none)")

scan(["import tools", "from tools"], "src/ui", "src/ui -> tools")
scan(["import src.ui", "from src.ui", "from ..ui", "import src.ui."], "tools", "tools -> src/ui")
scan(["import tools", "from tools", "src.ui", "from src.ui"], "src/core", "src/core -> tools or ui")
scan(["import tools", "from tools"], "src/utils", "src/utils -> tools")

# ---------- 2c. fan-in ----------
print()
print("=" * 70)
print("### 2C. FAN-IN TOP 20")
print("=" * 70)
fanin = collections.Counter()
for m, outs in edges.items():
    for o in outs:
        fanin[o] += 1
for m, c in fanin.most_common(20):
    print(f"{c:>4}  {m}")

print()
print("### 2D. fan-in per module, with importers (top10)")
for m, c in fanin.most_common(10):
    importers = sorted(x for x in edges if m in edges[x])
    print(f"\n{m}  ({c})")
    for i in importers:
        print(f"    {i}")

json.dump({"info": info, "edges": {k: sorted(v) for k, v in edges.items()}},
          open(os.path.join(ROOT, "_decouple_graph.json"), "w", encoding="utf-8"),
          ensure_ascii=False, indent=1)
