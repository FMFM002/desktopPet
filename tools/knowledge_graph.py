"""
知识图谱工具 v2.0 — 代码项目分析 + HTML 可视化
- analyze_project: 分析项目目录结构，生成知识图谱
- 增删查改: 实体/关系的标准 CRUD
- 输出到 桌宠私有data/knowledge_graph/<project_name>/ 独立子文件夹
- HTML 可视化文件可直接在浏览器打开
"""
import json
import os
import threading
import re
from pathlib import Path
from datetime import datetime

TOOL_NAME = "knowledge_graph"
TOOL_DESCRIPTION = (
    "知识图谱工具，用于存储和查询结构化知识。支持:\n"
    "- analyze_project: 分析代码项目结构，自动生成知识图谱和 HTML 可视化\n"
    "- add_entity: 添加知识实体（如人物、概念、事件等）\n"
    "- add_relation: 添加实体间关系\n"
    "- query_entity: 查询实体及其所有关系\n"
    "- query_relation: 查询两个实体间的关系路径\n"
    "- search: 按名称/描述搜索实体\n"
    "- list_types: 列出所有实体类型\n"
    "- stats: 图谱统计信息\n"
    "analyze_project 会自动分析代码项目的文件结构、模块依赖、类/函数定义等，\n"
    "生成可交互的 HTML 知识图谱文件，发送给用户。"
)
TOOL_PARAMETERS = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["analyze_project", "add_entity", "add_relation", "query_entity",
                     "query_relation", "search", "list_types", "stats"],
            "description": "操作类型。analyze_project: 分析代码项目生成图谱",
        },
        "project_path": {"type": "string", "description": "[analyze_project] 项目根目录的绝对路径"},
        "project_name": {"type": "string", "description": "[analyze_project] 项目名称（可选，默认取目录名）"},
        "name": {"type": "string", "description": "[add_entity/query_entity] 实体名称"},
        "entity_type": {"type": "string", "description": "[add_entity] 实体类型，如 person/concept/event/location/file/note/module/class/function"},
        "description": {"type": "string", "description": "[add_entity] 实体描述"},
        "properties": {"type": "string", "description": "[add_entity] JSON格式的额外属性（可选）"},
        "source": {"type": "string", "description": "[add_relation/query_relation] 源实体名称"},
        "target": {"type": "string", "description": "[add_relation/query_relation] 目标实体名称"},
        "relation": {"type": "string", "description": "[add_relation] 关系类型，如 knows/contains/depends_on/related_to/part_of/imports/calls/inherits"},
        "rel_description": {"type": "string", "description": "[add_relation] 关系描述（可选）"},
        "query": {"type": "string", "description": "[search] 搜索关键词"},
        "entity_type_filter": {"type": "string", "description": "[list_types/search] 按类型过滤（可选）"},
    },
    "required": ["action"],
}

# 数据目录 ★ 1.5.0: 迁移到 桌宠私有data（与沙箱可写区一致）
BASE_DIR = Path(__file__).resolve().parent.parent
KG_DIR = BASE_DIR / "data" / "桌宠私有data" / "knowledge_graph"
KG_DIR.mkdir(parents=True, exist_ok=True)

_lock = threading.Lock()

# ── 代码分析配置 ──
CODE_EXTENSIONS = {
    ".py": "Python", ".js": "JavaScript", ".ts": "TypeScript",
    ".java": "Java", ".cpp": "C++", ".c": "C", ".h": "C/C++ Header",
    ".cs": "C#", ".go": "Go", ".rs": "Rust", ".rb": "Ruby",
    ".php": "PHP", ".swift": "Swift", ".kt": "Kotlin",
    ".html": "HTML", ".css": "CSS", ".scss": "SCSS",
    ".json": "JSON", ".xml": "XML", ".yaml": "YAML", ".yml": "YAML",
    ".toml": "TOML", ".ini": "INI", ".cfg": "Config",
    ".md": "Markdown", ".txt": "Text", ".sql": "SQL",
    ".sh": "Shell", ".bat": "Batch", ".ps1": "PowerShell",
}

PROJECT_FILE_INDICATORS = {
    "requirements.txt": "Python依赖",
    "setup.py": "Python包",
    "pyproject.toml": "Python项目",
    "package.json": "Node.js项目",
    "tsconfig.json": "TypeScript配置",
    "Cargo.toml": "Rust项目",
    "go.mod": "Go模块",
    "pom.xml": "Maven项目",
    "build.gradle": "Gradle项目",
    "CMakeLists.txt": "CMake项目",
    "Makefile": "Make构建",
    "Dockerfile": "Docker镜像",
    "docker-compose.yml": "Docker编排",
    ".env": "环境变量",
    "config.yaml": "YAML配置",
    "config.json": "JSON配置",
    ".gitignore": "Git忽略",
    "README.md": "项目说明",
}

IMPORT_PATTERNS = {
    ".py": [
        (r'(?:from\s+(\S+)\s+import|import\s+(\S+))', 1),
    ],
    ".js": [
        (r'(?:require\([\'"](.+?)[\'"]\)|from\s+[\'"](.+?)[\'"]|import\s+[\'"](.+?)[\'"])', 1),
    ],
    ".ts": [
        (r'(?:from\s+[\'"](.+?)[\'"]|import\s+[\'"](.+?)[\'"])', 1),
    ],
}

CLASS_PATTERNS = {
    ".py": r'class\s+(\w+)\s*(?:\(([^)]*)\))?:',
    ".java": r'class\s+(\w+)\s*(?:extends\s+(\w+))?',
    ".ts": r'(?:class|interface)\s+(\w+)',
    ".js": r'class\s+(\w+)',
    ".cpp": r'class\s+(\w+)\s*(?::\s*public\s+(\w+))?',
}

FUNC_PATTERNS = {
    ".py": r'(?:async\s+)?def\s+(\w+)\s*\(',
    ".js": r'(?:async\s+)?function\s+(\w+)\s*\(|const\s+(\w+)\s*=\s*(?:async\s+)?\([^)]*\)\s*=>',
    ".ts": r'(?:async\s+)?function\s+(\w+)\s*\(|const\s+(\w+)\s*=\s*(?:async\s+)?\([^)]*\)\s*=>|(\w+)\s*\(',
    ".go": r'func\s+(?:\([^)]*\)\s+)?(\w+)\s*\(',
}


# ── 核心：项目分析 ──

def _analyze_project(project_path: str, project_name: str = None) -> dict:
    """分析代码项目，生成知识图谱数据"""
    root = Path(project_path).resolve()
    if not root.exists():
        return {"error": f"路径不存在: {project_path}"}
    if not root.is_dir():
        return {"error": f"不是目录: {project_path}"}

    name = project_name or root.name
    entities = {}
    relations = []
    stats = {"files": 0, "dirs": 0, "code_files": 0, "classes": 0, "functions": 0}

    # ── Step 1: 扫描目录结构 ──
    dir_nodes = {}  # relative_path → entity_name

    # 项目根实体
    root_name = f"[项目] {name}"
    entities[root_name] = {
        "type": "project",
        "description": f"代码项目: {name}",
        "properties": {"path": str(root), "name": name},
        "created_at": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat(),
    }

    # 扫描所有文件和目录
    skip_dirs = {
        "__pycache__", ".git", ".svn", ".hg", "node_modules", ".venv", "venv",
        ".tox", ".eggs", "*.egg-info", ".mypy_cache", ".pytest_cache",
        ".idea", ".vscode", ".DS_Store", "dist", "build", "__pycache__",
    }

    for dirpath, dirnames, filenames in os.walk(root):
        # 过滤不需要的目录
        dirnames[:] = [d for d in dirnames if d not in skip_dirs and not d.startswith(".")]

        rel_dir = Path(dirpath).relative_to(root)
        dir_key = str(rel_dir) if str(rel_dir) != "." else ""

        # 目录实体
        if dir_key:
            dir_name = f"[目录] {dir_key}"
            parent_key = str(rel_dir.parent) if str(rel_dir.parent) != "." else ""
            parent_name = f"[目录] {parent_key}" if parent_key else root_name
            entities[dir_name] = {
                "type": "directory",
                "description": f"目录: {dir_key}",
                "properties": {"path": str(rel_dir)},
                "created_at": datetime.now().isoformat(),
                "updated_at": datetime.now().isoformat(),
            }
            relations.append({
                "source": parent_name, "target": dir_name,
                "relation": "contains",
                "description": "",
                "created_at": datetime.now().isoformat(),
            })
            stats["dirs"] += 1
            current_dir_entity = dir_name
        else:
            current_dir_entity = root_name

        # 文件实体
        for fname in sorted(filenames):
            fpath = os.path.join(dirpath, fname)
            rel_fpath = str(Path(fpath).relative_to(root))
            ext = Path(fname).suffix.lower()
            file_size = os.path.getsize(fpath)

            file_name = f"[文件] {rel_fpath}"
            lang = CODE_EXTENSIONS.get(ext, "其他")
            is_code = ext in CODE_EXTENSIONS

            entities[file_name] = {
                "type": "file",
                "description": f"{lang}文件: {fname} ({_format_size(file_size)})",
                "properties": {
                    "path": rel_fpath,
                    "language": lang,
                    "size": file_size,
                    "extension": ext,
                },
                "created_at": datetime.now().isoformat(),
                "updated_at": datetime.now().isoformat(),
            }
            relations.append({
                "source": current_dir_entity, "target": file_name,
                "relation": "contains",
                "description": "",
                "created_at": datetime.now().isoformat(),
            })
            stats["files"] += 1
            if is_code:
                stats["code_files"] += 1

            # ── Step 2: 分析代码内容 ──
            if is_code and file_size < 500_000:  # 跳过超大文件
                try:
                    content = Path(fpath).read_text(encoding="utf-8", errors="ignore")
                    _analyze_code_content(
                        file_name, ext, content, entities, relations, stats, fname, rel_fpath)
                except Exception:
                    pass

            # ── 项目标记文件 ──
            if fname in PROJECT_FILE_INDICATORS:
                relations.append({
                    "source": root_name, "target": file_name,
                    "relation": "defined_by",
                    "description": PROJECT_FILE_INDICATORS[fname],
                    "created_at": datetime.now().isoformat(),
                })

    return {
        "project_name": name,
        "project_path": str(root),
        "entities": entities,
        "relations": relations,
        "stats": stats,
        "created_at": datetime.now().isoformat(),
        "version": 3,
    }


def _analyze_code_content(file_entity: str, ext: str, content: str,
                          entities: dict, relations: list, stats: dict,
                          fname: str, rel_path: str):
    """解析代码文件中的类、函数和导入关系"""
    module_name = fname.rsplit(".", 1)[0]

    # 类定义
    if ext in CLASS_PATTERNS:
        for match in re.finditer(CLASS_PATTERNS[ext], content):
            cls_name = match.group(1)
            cls_entity = f"[类] {module_name}.{cls_name}"
            entities[cls_entity] = {
                "type": "class",
                "description": f"类: {cls_name} (位于 {fname})",
                "properties": {"file": rel_path, "module": module_name, "name": cls_name},
                "created_at": datetime.now().isoformat(),
                "updated_at": datetime.now().isoformat(),
            }
            relations.append({
                "source": file_entity, "target": cls_entity,
                "relation": "defines", "description": f"定义类 {cls_name}",
                "created_at": datetime.now().isoformat(),
            })
            stats["classes"] += 1

            # 继承关系 (Python)
            if ext == ".py" and match.group(2):
                bases = [b.strip() for b in match.group(2).split(",")]
                for base in bases:
                    base = base.split(".")[-1]  # 简化
                    base_entity = f"[类] {module_name}.{base}"
                    if base_entity not in entities:
                        entities[base_entity] = {
                            "type": "class",
                            "description": f"类: {base}",
                            "properties": {"name": base},
                            "created_at": datetime.now().isoformat(),
                            "updated_at": datetime.now().isoformat(),
                        }
                    relations.append({
                        "source": cls_entity, "target": base_entity,
                        "relation": "inherits", "description": f"{cls_name} 继承 {base}",
                        "created_at": datetime.now().isoformat(),
                    })

    # 函数定义
    if ext in FUNC_PATTERNS:
        for match in re.finditer(FUNC_PATTERNS[ext], content):
            func_name = next((g for g in match.groups() if g), None)
            if func_name and not func_name.startswith("_"):
                func_entity = f"[函数] {module_name}.{func_name}"
                entities[func_entity] = {
                    "type": "function",
                    "description": f"函数: {func_name}() (位于 {fname})",
                    "properties": {"file": rel_path, "module": module_name, "name": func_name},
                    "created_at": datetime.now().isoformat(),
                    "updated_at": datetime.now().isoformat(),
                }
                relations.append({
                    "source": file_entity, "target": func_entity,
                    "relation": "defines", "description": f"定义函数 {func_name}()",
                    "created_at": datetime.now().isoformat(),
                })
                stats["functions"] += 1

    # 导入关系
    if ext in IMPORT_PATTERNS:
        for pattern, group_idx in IMPORT_PATTERNS.get(ext, []):
            for match in re.finditer(pattern, content):
                imported = match.group(1) or match.group(2) or ""
                if imported and not imported.startswith("_"):
                    # 简单处理：取顶级模块名
                    top_module = imported.split(".")[0]
                    if top_module and top_module != module_name:
                        target_file = f"[文件] {top_module}.py"
                        # 尝试匹配已知文件
                        found = False
                        for e_name in entities:
                            if top_module in e_name.lower() and entities[e_name].get("type") == "file":
                                relations.append({
                                    "source": file_entity, "target": e_name,
                                    "relation": "imports",
                                    "description": f"导入 {imported}",
                                    "created_at": datetime.now().isoformat(),
                                })
                                found = True
                                break
                        if not found:
                            # 创建外部模块实体
                            ext_entity = f"[模块] {top_module}"
                            if ext_entity not in entities:
                                entities[ext_entity] = {
                                    "type": "module",
                                    "description": f"外部/内部模块: {top_module}",
                                    "properties": {"name": top_module, "imported_by": fname},
                                    "created_at": datetime.now().isoformat(),
                                    "updated_at": datetime.now().isoformat(),
                                }
                            relations.append({
                                "source": file_entity, "target": ext_entity,
                                "relation": "imports",
                                "description": f"导入 {imported}",
                                "created_at": datetime.now().isoformat(),
                            })


def _format_size(size: int) -> str:
    if size < 1024:
        return f"{size}B"
    elif size < 1024 * 1024:
        return f"{size/1024:.1f}KB"
    else:
        return f"{size/(1024*1024):.1f}MB"


# ── HTML 可视化生成 ──

def _generate_html(graph_data: dict, output_path: Path) -> str:
    """生成可交互的 HTML 知识图谱"""
    project_name = graph_data.get("project_name", "Unknown")
    stats = graph_data.get("stats", {})
    entities = graph_data.get("entities", {})
    relations = graph_data.get("relations", [])

    # 准备节点和边的 JSON 数据
    nodes = []
    node_ids = {}
    idx = 0

    # 按类型分组排序
    type_order = {"project": 0, "directory": 1, "file": 2, "module": 3,
                  "class": 4, "function": 5, "concept": 6}

    sorted_entities = sorted(
        entities.items(),
        key=lambda x: (type_order.get(x[1].get("type", ""), 99), x[0])
    )

    for name, entity in sorted_entities:
        etype = entity.get("type", "unknown")
        nid = f"n{idx}"
        node_ids[name] = nid
        idx += 1

        # 计算颜色
        colors = {
            "project": "#667eea", "directory": "#764ba2", "file": "#43e97b",
            "module": "#f093fb", "class": "#f5576c", "function": "#4facfe",
            "concept": "#fa709a", "person": "#fee140", "event": "#30cfd0",
        }
        color = colors.get(etype, "#667eea")

        desc = entity.get("description", "")[:100]
        props = entity.get("properties", {})

        nodes.append({
            "id": nid,
            "label": name,
            "type": etype,
            "color": color,
            "description": desc,
            "properties": {k: str(v)[:80] for k, v in props.items()},
        })

    # 边
    edges = []
    for rel in relations:
        src_id = node_ids.get(rel["source"])
        tgt_id = node_ids.get(rel["target"])
        if src_id and tgt_id:
            edges.append({
                "from": src_id,
                "to": tgt_id,
                "label": rel.get("relation", ""),
                "description": rel.get("description", ""),
            })

    nodes_json = json.dumps(nodes, ensure_ascii=False)
    edges_json = json.dumps(edges, ensure_ascii=False)

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>知识图谱 - {project_name}</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{
    font-family: -apple-system, BlinkMacSystemFont, "Microsoft YaHei", sans-serif;
    background: #0f0f1a; color: #e0e0e0; overflow: hidden;
}}
#header {{
    position: fixed; top: 0; left: 0; right: 0; z-index: 100;
    background: linear-gradient(135deg, #1a1a2e, #16213e);
    padding: 12px 20px; display: flex; align-items: center; gap: 16px;
    border-bottom: 1px solid #2a2a4a;
}}
#header h1 {{ font-size: 20px; color: #667eea; }}
#stats {{ display: flex; gap: 16px; font-size: 13px; color: #888; }}
#stats span {{ background: #1e1e35; padding: 4px 12px; border-radius: 12px; }}
#container {{ position: fixed; top: 60px; left: 0; right: 360px; bottom: 0; }}
#sidebar {{
    position: fixed; top: 60px; right: 0; width: 360px; bottom: 0;
    background: #1a1a2e; border-left: 1px solid #2a2a4a;
    padding: 16px; overflow-y: auto;
}}
#sidebar h3 {{ color: #667eea; margin-bottom: 12px; font-size: 16px; }}
#filter-group {{ margin-bottom: 16px; }}
#filter-group label {{
    display: inline-block; margin: 3px 6px 3px 0; cursor: pointer;
    padding: 4px 10px; border-radius: 12px; font-size: 12px;
    background: #1e1e35; transition: all 0.2s;
}}
#filter-group label.active {{ background: #667eea; color: #fff; }}
#filter-group input {{ display: none; }}
#search-box {{
    width: 100%; padding: 8px 12px; background: #1e1e35; border: 1px solid #3a3a5a;
    border-radius: 8px; color: #e0e0e0; font-size: 13px; margin-bottom: 12px;
}}
#search-box:focus {{ outline: none; border-color: #667eea; }}
#node-detail {{ font-size: 13px; line-height: 1.6; }}
#node-detail .prop {{ color: #888; font-size: 12px; margin: 2px 0; }}
#node-detail .desc {{ color: #aaa; margin: 8px 0; }}
.relation-item {{
    background: #1e1e35; border-radius: 8px; padding: 8px 10px; margin: 4px 0;
    border-left: 3px solid #667eea; font-size: 12px;
}}
.toggle-group {{
    display: flex; gap: 8px; margin-bottom: 12px;
}}
.toggle-group button {{
    flex: 1; padding: 6px; background: #1e1e35; border: 1px solid #3a3a5a;
    border-radius: 6px; color: #aaa; cursor: pointer; font-size: 12px;
}}
.toggle-group button.active {{ background: #667eea; color: #fff; border-color: #667eea; }}
#node-count {{ font-size: 12px; color: #666; margin-bottom: 8px; }}
</style>
</head>
<body>
<div id="header">
    <h1>📊 知识图谱: {project_name}</h1>
    <div id="stats">
        <span>📁 {stats.get("files", 0)} 文件</span>
        <span>📂 {stats.get("dirs", 0)} 目录</span>
        <span>📝 {stats.get("code_files", 0)} 代码文件</span>
        <span>🏷️ {stats.get("classes", 0)} 类</span>
        <span>⚡ {stats.get("functions", 0)} 函数</span>
        <span>🔗 {len(relations)} 关系</span>
    </div>
</div>
<div id="container"></div>
<div id="sidebar">
    <h3>🔍 筛选 & 搜索</h3>
    <input type="text" id="search-box" placeholder="搜索实体名称..." oninput="searchNodes()">
    <div class="toggle-group">
        <button class="active" onclick="setLayout('force', this)">力导向</button>
        <button onclick="setLayout('tree', this)">树状</button>
        <button onclick="setLayout('radial', this)">辐射</button>
    </div>
    <div id="filter-group">
        <label class="active"><input type="checkbox" checked data-type="project" onchange="filterType()">📦 项目</label>
        <label class="active"><input type="checkbox" checked data-type="directory" onchange="filterType()">📂 目录</label>
        <label class="active"><input type="checkbox" checked data-type="file" onchange="filterType()">📄 文件</label>
        <label class="active"><input type="checkbox" checked data-type="module" onchange="filterType()">📦 模块</label>
        <label class="active"><input type="checkbox" checked data-type="class" onchange="filterType()">🏷️ 类</label>
        <label class="active"><input type="checkbox" checked data-type="function" onchange="filterType()">⚡ 函数</label>
    </div>
    <div id="node-count"></div>
    <h3>📋 节点详情</h3>
    <div id="node-detail"><p style="color:#666">点击节点查看详情</p></div>
</div>

<script>
// ★ 1.6.0: 任何 JS 报错都直接显示在页面上，便于一眼定位（以前是静默失败）
window.addEventListener("error", function (e) {{
    try {{
        var b = document.getElementById("js-error-banner");
        if (!b) {{
            b = document.createElement("div");
            b.id = "js-error-banner";
            b.style.cssText = "position:fixed;left:0;right:0;top:0;z-index:999;padding:8px 14px;"
                + "background:#7a1f2b;color:#ffd9de;font:12px/1.5 monospace;white-space:pre-wrap;";
            (document.body || document.documentElement).appendChild(b);
        }}
        b.textContent += "[JS错误] " + (e.message || e.error) + " @ "
            + (e.filename || "?") + ":" + (e.lineno || "?");
    }} catch (_) {{}}
}});
</script>
<script src="https://cdn.jsdelivr.net/npm/d3@7"></script>
<script>
// ★ 1.8.0 修复：此前 d3 从 CDN 加载失败时，d3.select(...) 直接 ReferenceError，
//   整段脚本崩溃 → render()/renderFallback() 都执行不到，页面只剩顶部统计数字。
//   现在：d3 对象延迟创建（initD3）；加载失败先画内置 Canvas 兜底图，
//   再异步依次尝试 unpkg / cdnjs，全部失败则保持离线渲染。
const rawNodes = {nodes_json};
const rawEdges = {edges_json};

let nodes = [...rawNodes];
let edges = [...rawEdges];
let activeTypes = new Set(["project","directory","file","module","class","function"]);
let currentLayout = "force";
let svg = null;   // d3 可用后才创建
let g = null;

const width = () => document.getElementById("container").clientWidth;
const height = () => document.getElementById("container").clientHeight;

function initD3() {{
    const container = document.getElementById("container");
    if (svg && container.querySelector("svg")) return;
    container.innerHTML = "";
    svg = d3.select(container).append("svg")
        .attr("width", "100%").attr("height", "100%");
    g = svg.append("g");
    svg.call(d3.zoom().scaleExtent([0.15, 4])
        .on("zoom", (e) => g.attr("transform", e.transform)));
}}

// CDN 兜底链：jsdelivr(同步标签) → unpkg → cdnjs → 离线 Canvas
const D3_FALLBACK_CDNS = [
    "https://unpkg.com/d3@7/dist/d3.min.js",
    "https://cdnjs.cloudflare.com/ajax/libs/d3/7.9.0/d3.min.js",
];
let _cdnIdx = 0;
function tryLoadD3() {{
    if (typeof d3 !== "undefined") {{ initD3(); render(); return; }}
    if (_cdnIdx >= D3_FALLBACK_CDNS.length) {{
        renderFallback("所有 CDN 均不可达（离线模式）");
        return;
    }}
    renderFallback("d3 未加载，正在尝试备用 CDN (" + (_cdnIdx + 1) + "/" + D3_FALLBACK_CDNS.length + ")…");
    const s = document.createElement("script");
    s.src = D3_FALLBACK_CDNS[_cdnIdx++];
    s.onload = () => {{
        if (typeof d3 !== "undefined") {{ initD3(); render(); }}
        else {{ tryLoadD3(); }}
    }};
    s.onerror = () => tryLoadD3();
    document.head.appendChild(s);
}}

// 颜色映射
const typeColors = {{
    project: "#667eea", directory: "#764ba2", file: "#43e97b",
    module: "#f093fb", class: "#f5576c", function: "#4facfe",
    concept: "#fa709a", person: "#fee140", event: "#30cfd0",
}};

function getFilteredData() {{
    const filteredNodes = nodes.filter(n => activeTypes.has(n.type));
    const filteredIds = new Set(filteredNodes.map(n => n.id));
    const filteredEdges = edges.filter(e => filteredIds.has(e.from) && filteredIds.has(e.to));
    return {{ nodes: filteredNodes, edges: filteredEdges }};
}}

// ★ 1.6.0: 完全没有 d3（离线/被墙）时的兜底：用原生 Canvas 画一张静态图
function renderFallback(reason) {{
    const container = document.getElementById("container");
    const data = getFilteredData();
    svg = null; g = null;   // ★ 1.8.0: 兜底渲染后 d3 图层作废，避免悬空引用
    container.innerHTML = "";
    const cv = document.createElement("canvas");
    const W = container.clientWidth || 900;
    const H = container.clientHeight || 700;
    cv.width = W; cv.height = H;
    cv.style.width = "100%"; cv.style.height = "100%";
    container.appendChild(cv);
    const ctx = cv.getContext("2d");
    ctx.fillStyle = "#0f0f1a"; ctx.fillRect(0, 0, W, H);

    const tip = document.createElement("div");
    tip.style.cssText = "position:absolute;left:12px;top:8px;color:#fa709a;font-size:12px;"
        + "background:#1e1e35;padding:6px 10px;border-radius:8px;z-index:50;";
    tip.textContent = "离线渲染（d3 未加载：" + reason + "）—— 形状/连线为简化版";
    container.style.position = "relative";
    container.appendChild(tip);

    const N = data.nodes, E = data.edges;
    if (!N.length) return;
    // 按类型分层环形布局，不需要力导向库
    const cx = W / 2, cy = H / 2;
    const order = ["project","directory","file","module","class","function"];
    const byType = {{}};
    N.forEach(n => {{ (byType[n.type] = byType[n.type] || []).push(n); }});
    const pos = {{}};
    let ring = 0;
    order.forEach(t => {{
        const arr = byType[t] || [];
        if (!arr.length) return;
        const R = t === "project" ? 0 : 90 + ring * 95;
        arr.forEach((n, i) => {{
            const a = (i / arr.length) * Math.PI * 2 + ring * 0.4;
            pos[n.id] = {{ x: cx + Math.cos(a) * R, y: cy + Math.sin(a) * R }};
        }});
        ring++;
    }});
    // 画边
    ctx.strokeStyle = "rgba(120,120,170,0.35)"; ctx.lineWidth = 0.6;
    E.forEach(e => {{
        const a = pos[e.from], b = pos[e.to];
        if (!a || !b) return;
        ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke();
    }});
    // 画点 + 标签
    const sizes = {{project:9, directory:6, file:4, module:5, class:3.5, function:2.5}};
    N.forEach(n => {{
        const q = pos[n.id]; if (!q) return;
        ctx.fillStyle = typeColors[n.type] || "#667eea";
        ctx.beginPath();
        ctx.arc(q.x, q.y, sizes[n.type] || 3, 0, Math.PI * 2);
        ctx.fill();
    }});
    N.filter(n => (n.type === "project" || n.type === "file")).forEach(n => {{
        const q = pos[n.id]; if (!q) return;
        ctx.fillStyle = "#aaa"; ctx.font = "9px sans-serif";
        const label = n.label.length > 18 ? n.label.slice(0, 16) + "..." : n.label;
        ctx.fillText(label, q.x + 6, q.y - 4);
    }});
    document.getElementById("node-count").textContent =
        `显示 ${{N.length}} 个节点, ${{E.length}} 条边`;
}}

function render() {{
    if (typeof d3 === "undefined") {{
        renderFallback("d3 未加载（等待备用 CDN）");
        return;
    }}
    initD3();
    const data = getFilteredData();
    document.getElementById("node-count").textContent =
        `显示 ${{data.nodes.length}} 个节点, ${{data.edges.length}} 条边`;

    g.selectAll("*").remove();

    if (data.nodes.length === 0) return;
    if (data.nodes.length > 800) {{
        // 节点过多时用简单模式
        renderSimple(data);
        return;
    }}

        // ★ 1.8.0 修复（真正的根因）：边数据是 from/to 键，而 d3.forceLink
        //   默认读 source/target → 抛 "node not found: undefined"，render 必崩。
        //   这里构造带 source/target 的副本喂给 d3（原对象保留 from/to 供
        //   showDetail/renderFallback 使用，互不干扰）。
        const linkData = data.edges.map(e => Object.assign({{}}, e, {{source: e.from, target: e.to}}));
    const simulation = d3.forceSimulation(data.nodes)
        .force("link", d3.forceLink(linkData).id(d => d.id).distance(80))
        .force("charge", d3.forceManyBody().strength(-200))
        .force("center", d3.forceCenter(width()/2, height()/2))
        .force("collision", d3.forceCollide().radius(25));

    if (currentLayout === "tree") {{
        simulation.force("x", d3.forceX(width()/2).strength(0.05));
        simulation.force("y", d3.forceY(d => d.type === "project" ? 60 : height()/2).strength(0.1));
    }} else if (currentLayout === "radial") {{
        simulation.force("radial", d3.forceRadial(height()/3, width()/2, height()/2).strength(0.1));
    }}

    const link = g.append("g").selectAll("line")
        .data(linkData).join("line")
        .attr("stroke", "#3a3a5a").attr("stroke-width", 0.6).attr("stroke-opacity", 0.5);

    const node = g.append("g").selectAll("g")
        .data(data.nodes).join("g")
        .attr("cursor", "pointer")
        .call(d3.drag()
            .on("start", (e, d) => {{ if (!e.active) simulation.alphaTarget(0.3).restart(); d.fx = d.x; d.fy = d.y; }})
            .on("drag", (e, d) => {{ d.fx = e.x; d.fy = e.y; }})
            .on("end", (e, d) => {{ if (!e.active) simulation.alphaTarget(0); d.fx = null; d.fy = null; }}));

    node.append("circle")
        .attr("r", d => {{
            const sizes = {{ project: 18, directory: 12, file: 8, module: 10, class: 7, function: 5 }};
            return sizes[d.type] || 7;
        }})
        .attr("fill", d => typeColors[d.type] || "#667eea")
        .attr("stroke", "#1a1a2e").attr("stroke-width", 1.5);

    node.append("text")
        .text(d => d.label.length > 20 ? d.label.substring(0, 18) + "..." : d.label)
        .attr("font-size", "8px").attr("fill", "#aaa")
        .attr("text-anchor", "middle").attr("dy", d => (({{project:18,directory:12,file:8,module:10,class:7,function:5}}[d.type]||7) + 12));

    node.on("click", (e, d) => showDetail(d));
    node.on("mouseenter", function() {{ d3.select(this).select("circle").attr("stroke", "#fff").attr("stroke-width", 2.5); }});
    node.on("mouseleave", function() {{ d3.select(this).select("circle").attr("stroke", "#1a1a2e").attr("stroke-width", 1.5); }});

    simulation.on("tick", () => {{
        link.attr("x1", d => d.source.x).attr("y1", d => d.source.y)
            .attr("x2", d => d.target.x).attr("y2", d => d.target.y);
        node.attr("transform", d => `translate(${{d.x}},${{d.y}})`);
    }});
}}

function renderSimple(data) {{
    // ★ 1.8.0: 大图概览模式重写（旧版只画 4px 小方块，没连线没提示，像"坏了"）
    //   现在是可用的静态概览：连线 + 彩色节点 + 原生悬停提示 + 点击详情。
    //   按类型分列排布（每列一个类型），连线用低透明度避免糊成一团。
    const margin = {{ top: 30, left: 40, right: 20, bottom: 20 }};
    const W = width() - margin.left - margin.right;
    const H = height() - margin.top - margin.bottom;

    // 按类型分组，占用按各类型节点数比例的水平条带
    const order = ["project","directory","file","module","class","function"];
    const byType = {{}};
    data.nodes.forEach(n => {{ (byType[n.type] = byType[n.type] || []).push(n); }});
    const usedTypes = order.filter(t => (byType[t] || []).length);
    const total = data.nodes.length || 1;

    let yPos = margin.top;
    const pos = {{}};
    usedTypes.forEach(t => {{
        const arr = byType[t];
        const bandH = Math.max(24, (arr.length / total) * H);
        // 该条带内横向铺开，行数按条带高度算
        const rows = Math.max(1, Math.floor(bandH / 14));
        const perRow = Math.ceil(arr.length / rows);
        const cellW = Math.min(14, W / Math.max(1, perRow));
        arr.forEach((n, i) => {{
            pos[n.id] = {{
                x: margin.left + (i % perRow) * cellW + cellW / 2,
                y: yPos + Math.floor(i / perRow) * (bandH / rows),
            }};
        }});
        // 条带类型标签
        g.append("text")
            .attr("x", margin.left).attr("y", yPos - 4)
            .attr("font-size", "10px").attr("fill", typeColors[t] || "#888")
            .text(`${{t}} (${{arr.length}})`);
        yPos += bandH;
    }});

    data.nodes.forEach(n => {{ if (pos[n.id]) {{ Object.assign(n, pos[n.id]); }} }});

    // 连线（低透明度；用 id→坐标 映射，避免 O(n²) find）
    const px = {{}}, py = {{}};
    data.nodes.forEach(n => {{ px[n.id] = n.x; py[n.id] = n.y; }});
    g.append("g").selectAll("line")
        .data(data.edges).join("line")
        .attr("x1", d => px[d.from] || 0).attr("y1", d => py[d.from] || 0)
        .attr("x2", d => px[d.to] || 0).attr("y2", d => py[d.to] || 0)
        .attr("stroke", "#3a3a5a").attr("stroke-width", 0.4).attr("stroke-opacity", 0.25);

    // 节点
    const sizes = {{ project: 9, directory: 6, file: 4.5, module: 5.5, class: 3.5, function: 2.5 }};
    const nodeSel = g.append("g").selectAll("circle")
        .data(data.nodes).join("circle")
        .attr("cx", d => d.x).attr("cy", d => d.y)
        .attr("r", d => sizes[d.type] || 3)
        .attr("fill", d => typeColors[d.type] || "#667eea")
        .attr("stroke", "#1a1a2e").attr("stroke-width", 0.5)
        .attr("cursor", "pointer")
        .on("click", (e, d) => showDetail(d));

    // 原生悬停提示（大图下不渲染 SVG 文本标签，用 <title> 零成本显示名称）
    nodeSel.append("title").text(d => d.label);

    // 顶部提示
    g.append("text")
        .attr("x", margin.left).attr("y", 16)
        .attr("font-size", "11px").attr("fill", "#fa709a")
        .text(`概览模式: ${{data.nodes.length}} 节点过大，已按类型分带静态排布（可缩放/点节点看详情；在右侧取消勾选 file/function 等类型可降到 800 以下切回交互力导向图）`);
}}

function showDetail(d) {{
    const outgoing = edges.filter(e => e.from === d.id);
    const incoming = edges.filter(e => e.to === d.id);
    let html = `<h4 style="color:${{typeColors[d.type]||'#667eea'}};margin-bottom:4px;">${{d.label}}</h4>`;
    html += `<p style="color:#888;font-size:12px;">类型: ${{d.type}}</p>`;
    if (d.description) html += `<p class="desc">${{d.description}}</p>`;
    if (d.properties) {{
        html += `<div style="margin:8px 0;">`;
        for (const [k, v] of Object.entries(d.properties)) {{
            html += `<div class="prop">${{k}}: ${{v}}</div>`;
        }}
        html += `</div>`;
    }}
    if (outgoing.length > 0) {{
        html += `<h5 style="color:#667eea;margin-top:12px;">出边 (${{outgoing.length}}):</h5>`;
        outgoing.slice(0, 15).forEach(e => {{
            const target = nodes.find(n => n.id === e.to);
            html += `<div class="relation-item">→ [${{e.label}}] → ${{target ? target.label.substring(0,40) : e.to}}</div>`;
        }});
    }}
    if (incoming.length > 0) {{
        html += `<h5 style="color:#667eea;margin-top:12px;">入边 (${{incoming.length}}):</h5>`;
        incoming.slice(0, 15).forEach(e => {{
            const source = nodes.find(n => n.id === e.from);
            html += `<div class="relation-item">${{source ? source.label.substring(0,40) : e.from}} → [${{e.label}}]</div>`;
        }});
    }}
    document.getElementById("node-detail").innerHTML = html;
}}

function filterType() {{
    activeTypes.clear();
    document.querySelectorAll("#filter-group input").forEach(cb => {{
        if (cb.checked) activeTypes.add(cb.dataset.type);
        cb.parentElement.classList.toggle("active", cb.checked);
    }});
    render();
}}

function setLayout(layout, btn) {{
    currentLayout = layout;
    document.querySelectorAll(".toggle-group button").forEach(b => b.classList.remove("active"));
    (btn || (window.event && window.event.target)).classList.add("active");
    render();
}}

function searchNodes() {{
    const q = document.getElementById("search-box").value.toLowerCase();
    if (!q) {{
        document.querySelectorAll("#filter-group label").forEach(l => {{
            l.querySelector("input").checked = true;
            l.classList.add("active");
        }});
        activeTypes = new Set(["project","directory","file","module","class","function"]);
        render();
        return;
    }}
    const matched = nodes.filter(n => n.label.toLowerCase().includes(q) || (n.description && n.description.toLowerCase().includes(q)));
    // 高亮匹配的节点（★ 1.8.0: 离线 Canvas 模式下没有 d3 图层，跳过高亮只显示计数）
    if (g) {{
        g.selectAll("circle").attr("opacity", 0.15);
        matched.forEach(n => {{
            g.selectAll("circle").filter(d => d.id === n.id).attr("opacity", 1).attr("r", 14);
        }});
    }}
    document.getElementById("node-count").textContent = `搜索 "${{q}}": ${{matched.length}} 个匹配`;
}}

// 初始渲染：有 d3 直接画交互图；没有先画兜底图，再异步尝试备用 CDN
render();
if (typeof d3 === "undefined") tryLoadD3();
// 响应窗口大小变化
window.addEventListener("resize", () => render());
</script>
</body>
</html>"""

    output_path.write_text(html, encoding="utf-8")
    return str(output_path)


# ── 持久化 ──

def _get_project_dir(project_name: str) -> Path:
    """获取项目知识图谱子目录"""
    safe_name = re.sub(r'[<>:"/\\|?*]', '_', project_name)
    proj_dir = KG_DIR / safe_name
    proj_dir.mkdir(parents=True, exist_ok=True)
    return proj_dir


def _load_graph(project_name: str = None) -> dict:
    """加载图谱数据"""
    if project_name:
        data_file = _get_project_dir(project_name) / "graph_data.json"
    else:
        data_file = KG_DIR / "default_graph.json"

    if data_file.exists():
        try:
            with open(data_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                if "entities" not in data:
                    data["entities"] = {}
                if "relations" not in data:
                    data["relations"] = []
                return data
        except (json.JSONDecodeError, OSError):
            pass
    return {"entities": {}, "relations": [], "meta": {"created": datetime.now().isoformat(), "version": 3}}


def _save_graph(data: dict, project_name: str = None):
    """保存图谱数据"""
    if project_name:
        proj_dir = _get_project_dir(project_name)
        data_file = proj_dir / "graph_data.json"
    else:
        data_file = KG_DIR / "default_graph.json"
    data_file.parent.mkdir(parents=True, exist_ok=True)
    with open(data_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ── 主入口 ──

def execute(action: str, **kwargs) -> str:
    with _lock:
        if action == "analyze_project":
            return _do_analyze_project(**kwargs)

        # 标准 CRUD 操作使用默认图谱
        data = _load_graph()
        entities = data.setdefault("entities", {})
        relations = data.setdefault("relations", [])

        try:
            if action == "add_entity":
                return _do_add_entity(entities, relations, data, **kwargs)
            elif action == "add_relation":
                return _do_add_relation(entities, relations, data, **kwargs)
            elif action == "query_entity":
                return _do_query_entity(entities, relations, **kwargs)
            elif action == "query_relation":
                return _do_query_relation(entities, relations, **kwargs)
            elif action == "search":
                return _do_search(entities, relations, **kwargs)
            elif action == "list_types":
                return _do_list_types(entities, **kwargs)
            elif action == "stats":
                return _do_stats(entities, relations)
            else:
                return f"[错误] 未知操作: {action}。支持: analyze_project, add_entity, add_relation, query_entity, query_relation, search, list_types, stats"
        except Exception as e:
            return f"[错误] 图谱操作失败: {e}"


def _do_analyze_project(**kwargs) -> str:
    """分析项目并生成 HTML"""
    project_path = (kwargs.get("project_path") or "").strip()
    project_name = (kwargs.get("project_name") or "").strip()

    if not project_path:
        return "[错误] analyze_project 需要 project_path 参数（项目路径）"

    try:
        graph_data = _analyze_project(project_path, project_name or None)
        if "error" in graph_data:
            return f"[错误] {graph_data['error']}"

        name = graph_data["project_name"]
        stats = graph_data["stats"]

        # 保存 JSON 数据
        proj_dir = _get_project_dir(name)
        _save_graph(graph_data, name)

        # 生成 HTML
        html_path = proj_dir / f"{name}_知识图谱.html"
        html_file = _generate_html(graph_data, html_path)

        summary = (
            f"✅ 项目分析完成: {name}\n"
            f"📂 扫描: {stats['files']} 个文件 ({stats['dirs']} 个目录)\n"
            f"📝 代码文件: {stats['code_files']} 个\n"
            f"🏷️ 识别类: {stats['classes']} 个\n"
            f"⚡ 识别函数: {stats['functions']} 个\n"
            f"🔗 总关系: {len(graph_data['relations'])} 条\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 HTML 可视化文件: {html_file}\n"
            f"💾 JSON 数据文件: {proj_dir / 'graph_data.json'}"
        )

        # ★ 返回 HTML 文件路径，方便后续发送
        return summary

    except Exception as e:
        return f"[错误] 项目分析失败: {e}"


# ── CRUD 方法 ──

def _do_add_entity(entities, relations, data, **kwargs):
    name = (kwargs.get("name") or "").strip()
    entity_type = (kwargs.get("entity_type") or "note").strip()
    desc = (kwargs.get("description") or "").strip()
    props_str = kwargs.get("properties") or "{}"

    if not name:
        return "[错误] add_entity 需要 name 参数"

    try:
        props = json.loads(props_str) if props_str else {}
    except json.JSONDecodeError:
        props = {"raw": props_str}

    is_new = name not in entities
    entities[name] = {
        "type": entity_type,
        "description": desc,
        "properties": props,
        "created_at": entities[name]["created_at"] if name in entities else datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat(),
    }
    _save_graph(data)

    action_word = "添加" if is_new else "更新"
    return f"✅ {action_word}实体: '{name}' (类型: {entity_type})" + (f" — {desc[:80]}" if desc else "")


def _do_add_relation(entities, relations, data, **kwargs):
    source = (kwargs.get("source") or "").strip()
    target = (kwargs.get("target") or "").strip()
    rel_type = (kwargs.get("relation") or "related_to").strip()
    rel_desc = (kwargs.get("rel_description") or "").strip()

    if not source or not target:
        return "[错误] add_relation 需要 source 和 target 参数"

    created = []
    if source not in entities:
        entities[source] = {"type": "concept", "description": "", "properties": {},
                            "created_at": datetime.now().isoformat(), "updated_at": datetime.now().isoformat()}
        created.append(source)
    if target not in entities:
        entities[target] = {"type": "concept", "description": "", "properties": {},
                            "created_at": datetime.now().isoformat(), "updated_at": datetime.now().isoformat()}
        created.append(target)

    for rel in relations:
        if rel["source"] == source and rel["target"] == target and rel["relation"] == rel_type:
            rel["description"] = rel_desc or rel.get("description", "")
            rel["updated_at"] = datetime.now().isoformat()
            _save_graph(data)
            return f"✅ 更新关系: ({source})-[{rel_type}]->({target})" + (f" — {rel_desc}" if rel_desc else "")

    relations.append({
        "source": source, "target": target, "relation": rel_type,
        "description": rel_desc, "created_at": datetime.now().isoformat(),
    })
    _save_graph(data)

    msg = f"✅ 添加关系: ({source})-[{rel_type}]->({target})"
    if rel_desc:
        msg += f" — {rel_desc}"
    if created:
        msg += f"\n📝 自动创建实体: {', '.join(created)}"
    return msg


def _do_query_entity(entities, relations, **kwargs):
    name = (kwargs.get("name") or "").strip()
    if not name:
        return "[错误] query_entity 需要 name 参数"

    if name not in entities:
        matches = [n for n in entities if name.lower() in n.lower()]
        if matches:
            return f"未找到 '{name}'。你可能指的是: {', '.join(matches[:10])}"
        return f"未找到实体: '{name}'"

    entity = entities[name]
    lines = [f"📌 实体: {name}"]
    lines.append(f"   类型: {entity.get('type', 'unknown')}")
    if entity.get("description"):
        lines.append(f"   描述: {entity['description']}")
    if entity.get("properties"):
        for k, v in entity["properties"].items():
            lines.append(f"   {k}: {v}")
    lines.append(f"   创建: {entity.get('created_at', '?')[:19]}")
    lines.append(f"   更新: {entity.get('updated_at', '?')[:19]}")

    outgoing = [r for r in relations if r["source"] == name]
    incoming = [r for r in relations if r["target"] == name]

    if outgoing:
        lines.append(f"\n🔗 出边 ({len(outgoing)}):")
        for r in outgoing[:20]:
            desc = f" — {r['description']}" if r.get("description") else ""
            lines.append(f"   → [{r['relation']}] → {r['target']}{desc}")

    if incoming:
        lines.append(f"\n🔗 入边 ({len(incoming)}):")
        for r in incoming[:20]:
            desc = f" — {r['description']}" if r.get("description") else ""
            lines.append(f"   {r['source']} → [{r['relation']}] →{desc}")

    if not outgoing and not incoming:
        lines.append("\n(无关系)")

    return "\n".join(lines)


def _do_query_relation(entities, relations, **kwargs):
    source = (kwargs.get("source") or "").strip()
    target = (kwargs.get("target") or "").strip()

    if not source or not target:
        return "[错误] query_relation 需要 source 和 target 参数"

    direct = [r for r in relations if r["source"] == source and r["target"] == target]
    reverse = [r for r in relations if r["source"] == target and r["target"] == source]

    two_hop = []
    source_out = [r for r in relations if r["source"] == source]
    target_in = {r["source"] for r in relations if r["target"] == target}
    for r in source_out:
        if r["target"] in target_in:
            mid = r["target"]
            mid_out = [r2 for r2 in relations if r2["source"] == mid and r2["target"] == target]
            for r2 in mid_out:
                two_hop.append((r, r2))

    lines = [f"🔍 关系查询: {source} ↔ {target}"]
    if direct:
        lines.append(f"\n直接关系 ({len(direct)}):")
        for r in direct:
            desc = f" — {r['description']}" if r.get("description") else ""
            lines.append(f"   {source} -[{r['relation']}]-> {target}{desc}")
    if reverse:
        lines.append(f"\n反向关系 ({len(reverse)}):")
        for r in reverse:
            desc = f" — {r['description']}" if r.get("description") else ""
            lines.append(f"   {target} -[{r['relation']}]-> {source}{desc}")
    if two_hop:
        lines.append(f"\n二跳路径 ({len(two_hop)}):")
        for r1, r2 in two_hop[:10]:
            lines.append(f"   {source} -[{r1['relation']}]-> {r1['target']} -[{r2['relation']}]-> {target}")
    if not direct and not reverse and not two_hop:
        lines.append("\n未找到任何关系路径。")

    return "\n".join(lines)


def _do_search(entities, relations, **kwargs):
    query = (kwargs.get("query") or "").strip().lower()
    type_filter = (kwargs.get("entity_type_filter") or "").strip()

    if not query:
        return "[错误] search 需要 query 参数"

    results = []
    for name, entity in entities.items():
        if type_filter and entity.get("type") != type_filter:
            continue
        if query in name.lower() or query in entity.get("description", "").lower():
            results.append((name, entity))
        elif any(query in str(v).lower() for v in entity.get("properties", {}).values()):
            results.append((name, entity))

    if not results:
        return f"未找到匹配 '{query}' 的实体。"

    lines = [f"🔍 搜索 '{query}' — 找到 {len(results)} 个实体:"]
    for name, entity in results[:20]:
        type_str = entity.get("type", "?")
        desc = entity.get("description", "")[:60]
        line = f"   [{type_str}] {name}"
        if desc:
            line += f" — {desc}"
        lines.append(line)

    if len(results) > 20:
        lines.append(f"   ... 还有 {len(results) - 20} 个结果")

    return "\n".join(lines)


def _do_list_types(entities, **kwargs):
    type_filter = (kwargs.get("entity_type_filter") or "").strip()
    type_counts = {}
    for entity in entities.values():
        t = entity.get("type", "unknown")
        if type_filter and t != type_filter:
            continue
        type_counts[t] = type_counts.get(t, 0) + 1

    if not type_counts:
        return "图谱为空。"

    lines = ["📊 实体类型统计:"]
    for t, count in sorted(type_counts.items(), key=lambda x: -x[1]):
        lines.append(f"   {t}: {count} 个")
    lines.append(f"   总计: {len(entities)} 个实体")
    return "\n".join(lines)


def _do_stats(entities, relations):
    type_counts = {}
    for entity in entities.values():
        t = entity.get("type", "unknown")
        type_counts[t] = type_counts.get(t, 0) + 1

    rel_counts = {}
    for r in relations:
        rt = r.get("relation", "unknown")
        rel_counts[rt] = rel_counts.get(rt, 0) + 1

    lines = [
        "📊 知识图谱统计",
        f"   实体: {len(entities)} 个",
        f"   关系: {len(relations)} 条",
    ]
    if type_counts:
        lines.append("   实体类型分布:")
        for t, c in sorted(type_counts.items(), key=lambda x: -x[1]):
            lines.append(f"     {t}: {c}")
    if rel_counts:
        lines.append("   关系类型分布:")
        for r, c in sorted(rel_counts.items(), key=lambda x: -x[1]):
            lines.append(f"     {r}: {c}")

    # 统计所有子文件夹
    if KG_DIR.exists():
        proj_dirs = [d for d in KG_DIR.iterdir() if d.is_dir()]
        lines.append(f"   项目图谱数: {len(proj_dirs)} 个")
        for pd in sorted(proj_dirs):
            size = sum(f.stat().st_size for f in pd.rglob("*") if f.is_file())
            size_str = f"{size:,}B" if size < 1024 else f"{size/1024:.1f}KB"
            lines.append(f"     {pd.name} ({size_str})")

    return "\n".join(lines)


# ── 清理旧占位文件 ──
_old_placeholder = BASE_DIR / "data" / "knowledge_graph.json"
if _old_placeholder.exists():
    try:
        _old_placeholder.unlink()
    except Exception:
        pass
