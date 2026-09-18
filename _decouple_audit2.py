# -*- coding: utf-8 -*-
"""解耦调研 第2部分：模块级可变态 / 单例 / 跨层调用 / 重复逻辑。只读。"""
import ast, os, collections, re

ROOT = os.path.dirname(os.path.abspath(__file__))
DIRS = ["src", "tools"]

def rel(p):
    return os.path.relpath(p, ROOT).replace("\\", "/")

files = []
for d in DIRS:
    for dp, dns, fns in os.walk(os.path.join(ROOT, d)):
        dns[:] = [x for x in dns if x != "__pycache__"]
        for fn in fns:
            if fn.endswith(".py"):
                files.append(os.path.join(dp, fn))
files.sort()

def is_immutable_literal(node):
    """True only for clearly immutable constant values."""
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, ast.Tuple):
        return all(is_immutable_literal(e) for e in node.elts)
    return False

def classify(node):
    if isinstance(node, ast.Dict): return "dict-literal"
    if isinstance(node, ast.List): return "list-literal"
    if isinstance(node, ast.Set): return "set-literal"
    if isinstance(node, (ast.DictComp,)): return "dict-comp"
    if isinstance(node, (ast.ListComp,)): return "list-comp"
    if isinstance(node, (ast.SetComp,)): return "set-comp"
    if isinstance(node, ast.Call):
        return "call"
    if isinstance(node, ast.Constant) and node.value is None:
        return "None"
    if isinstance(node, ast.Name): return "name"
    if isinstance(node, ast.Attribute): return "attr"
    if isinstance(node, ast.Tuple): return "tuple"
    if isinstance(node, (ast.Constant,)): return "const"
    return type(node).__name__

rows = []
for f in files:
    src = open(f, encoding="utf-8").read()
    try:
        tree = ast.parse(src)
    except SyntaxError:
        continue
    r = rel(f)
    targets = []
    # module-level statements, including inside if/try/with at module level
    def walk_mod(body, guarded=False):
        for st in body:
            if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if isinstance(st, (ast.If, ast.Try, ast.For, ast.While, ast.With, ast.AsyncWith)):
                walk_mod(st.body, True)
                walk_mod(getattr(st, "orelse", []), True)
                walk_mod(getattr(st, "finalbody", []), True)
                for h in getattr(st, "handlers", []):
                    walk_mod(h.body, True)
                continue
            if isinstance(st, ast.Assign):
                if is_immutable_literal(st.value):
                    continue
                for t in st.targets:
                    if isinstance(t, ast.Name):
                        targets.append((st.lineno, t.id, classify(st.value), guarded))
                    elif isinstance(t, ast.Tuple) and all(isinstance(e, ast.Name) for e in t.elts):
                        for e in t.elts:
                            targets.append((st.lineno, e.id, classify(st.value), guarded))
            elif isinstance(st, ast.AnnAssign):
                if st.value is None:
                    continue
                if is_immutable_literal(st.value):
                    continue
                if isinstance(st.target, ast.Name):
                    targets.append((st.lineno, st.target.id, classify(st.value), guarded))
            elif isinstance(st, ast.AugAssign):
                if isinstance(st.target, ast.Name):
                    targets.append((st.lineno, st.target.id, "augassign", guarded))
    walk_mod(tree.body)
    for ln, name, kind, guarded in targets:
        rows.append((r, ln, name, kind, guarded))

print("=" * 100)
print("### 3A. MODULE-LEVEL MUTABLE / NON-CONSTANT GLOBALS IN src/ (NOT unloaded)")
print("=" * 100)
print(f"{'file:line':<52} {'name':<34} {'kind':<12}")
for r, ln, n, k, g in rows:
    if r.startswith("src/") and "genie_tts" not in r:
        print(f"{r+':'+str(ln):<52} {n:<34} {k:<12}{' [guarded]' if g else ''}")

print()
print("=" * 100)
print("### 3B. MODULE-LEVEL MUTABLE / NON-CONSTANT GLOBALS IN tools/ (UNLOADED when tool unloaded)")
print("=" * 100)
print(f"{'file:line':<52} {'name':<34} {'kind':<12}")
for r, ln, n, k, g in rows:
    if r.startswith("tools/") and "dev_tests" not in r and "_vendor" not in r:
        print(f"{r+':'+str(ln):<52} {n:<34} {k:<12}{' [guarded]' if g else ''}")

print()
print("counts: src(non-genie)=", sum(1 for r, *_ in rows if r.startswith("src/") and "genie_tts" not in r),
      " tools(non-test)=", sum(1 for r, *_ in rows if r.startswith("tools/") and "dev_tests" not in r and "_vendor" not in r))

# ---------- 4. singletons ----------
print()
print("=" * 100)
print("### 4. SINGLETON / GLOBAL-ACCESSOR DEFINITIONS + CALLSITE COUNTS")
print("=" * 100)

allpy = files + [os.path.join(ROOT, "main.py")]
allpy = [p for p in allpy if os.path.exists(p)]
texts = {rel(p): open(p, encoding="utf-8").read() for p in allpy}
srclines = {k: v.splitlines() for k, v in texts.items()}

singleton_defs = []
for f in files:
    src = texts[rel(f)]
    try:
        tree = ast.parse(src)
    except SyntaxError:
        continue
    r = rel(f)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            nm = node.name
            if re.match(r"^(get_|_)", nm) and ("instance" in nm or nm.startswith("get_")):
                singleton_defs.append((r, node.lineno, nm))
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and ("instance" in t.id or t.id.startswith("_global") or t.id.startswith("_singleton")):
                    singleton_defs.append((r, node.lineno, t.id + " = ..."))
        if isinstance(node, ast.ClassDef):
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) and sub.name in ("instance", "get_instance"):
                    singleton_defs.append((r, sub.lineno, node.name + "." + sub.name))

# dedupe by (file,lineno)
seen = set()
sdefs = []
for r, ln, nm in singleton_defs:
    if (r, ln) in seen: continue
    seen.add((r, ln)); sdefs.append((r, ln, nm))

for r, ln, nm in sorted(sdefs, key=lambda x: x[0]):
    short = nm.split(".")[-1].replace(" = ...", "")
    calls = 0
    where = []
    for fp, lines in srclines.items():
        if "dev_tests" in fp: continue
        for i, line in enumerate(lines, 1):
            if re.search(r"\b" + re.escape(short) + r"\s*\(", line):
                if fp == r and i == ln: continue
                calls += 1
                where.append(f"{fp}:{i}")
    print(f"\n{r}:{ln}  {nm}   -> {calls} call sites")
    for w in where[:25]:
        print(f"      {w}")

# ---------- 5. cross-layer ----------
print()
print("=" * 100)
print("### 5. UI LAYER DIRECT CALLS INTO core.database / tools / execute_tool")
print("=" * 100)
PAT = {
    "core.database import": r"from\s+src\.core\.database\s+import|from\s+\.\.core\.database\s+import|import\s+src\.core\.database",
    "get_db(": r"\bget_db\s*\(",
    "get_database(": r"\bget_database\s*\(",
    "Database(": r"\bDatabase\s*\(",
    "execute_tool(": r"\bexecute_tool\s*\(",
    "tool_manager": r"tool_manager|ToolManager",
    "import tools": r"^\s*(from|import)\s+tools\b",
    "raw sqlite3": r"\bsqlite3\b",
    "LLMClient(": r"\bLLMClient\s*\(|get_llm_client\s*\(",
    "ChatClient(": r"\bChatClient\s*\(",
}
for label, pat in PAT.items():
    print(f"\n--- {label} ---")
    n = 0
    for fp, lines in srclines.items():
        if not fp.startswith("src/ui/"): continue
        for i, line in enumerate(lines, 1):
            s = line.strip()
            if s.startswith("#"): continue
            if re.search(pat, line):
                print(f"{fp}:{i}: {s[:150]}")
                n += 1
    if n == 0: print("(none)")

# ---------- 6. duplicate logic ----------
print()
print("=" * 100)
print("### 6. DUPLICATED PATTERNS")
print("=" * 100)
DUP = {
    "PROJECT_ROOT def": r"^PROJECT_ROOT\s*=|^\s*PROJECT_ROOT\s*=|ROOT_DIR\s*=|BASE_DIR\s*=",
    "get_project_root(": r"def\s+get_project_root|def\s+_project_root|def\s+project_root",
    "html.escape / manual escape": r"html\.escape|escape_html|def\s+_escape|def\s+escape|&amp;|&lt;",
    "format size": r"def\s+_format_size|def\s+format_size|def\s+_human_size|def\s+_fmt_size|1024\s*\*\s*1024",
    "load config json": r"config\.json|def\s+load_config|def\s+_load_config|def\s+get_config",
    "Logger getLogger": r"getLogger\s*\(|logging\.getLogger",
    "sys._MEIPASS / frozen": r"_MEIPASS|getattr\(sys,\s*'frozen'|sys\.frozen",
    "json load json.load": r"json\.load\s*\(",
    "requests/httpx session": r"requests\.Session\(|httpx\.Client\(",
    "retry decorator": r"def\s+_retry|def\s+retry|max_retries",
    "truncate text": r"def\s+_truncate|def\s+truncate|\[:\s*100\]|\[:\s*200\]",
    "screenshot capture": r"ImageGrab\.grab|mss\.mss\(|pyautogui\.screenshot",
    "regex strip html tags": r"re\.sub\(r?['\"]<\[?\^?>",
    "ensure_dir/makedirs": r"os\.makedirs\(",
    "atomic json write": r"\.tmp['\"]|os\.replace\(",
    "def _load(self) config": r"def\s+_load_settings|def\s+_save_settings|def\s+_apply_config",
}
for label, pat in DUP.items():
    print(f"\n--- {label} ---")
    n = 0
    for fp, lines in srclines.items():
        if "dev_tests" in fp or "_vendor" in fp or "genie_tts" in fp: continue
        for i, line in enumerate(lines, 1):
            if re.search(pat, line):
                print(f"{fp}:{i}: {line.strip()[:140]}")
                n += 1
    if n == 0: print("(none)")
