"""
Office 文档工具 — 基于 OfficeCLI 的 Word/Excel/PowerPoint 操作
支持: 创建文档、添加幻灯片/形状/文字/图片、修改属性、查看大纲、批量操作
"""
import json
import subprocess
import os
import logging
from pathlib import Path

_log = logging.getLogger("Tool.Office")

TOOL_NAME = "office"
TOOL_DESCRIPTION = (
    "Office文档操作工具，支持创建和编辑 Word(.docx)、Excel(.xlsx)、PowerPoint(.pptx) 文件。"
    "基于OfficeCLI命令行，可创建文档、添加幻灯片/表格/形状/文字/图片、修改属性、查看大纲。"
    "支持的操作: create(创建空白文档), add(添加元素), set(修改元素), view(查看大纲/内容), batch(批量JSON操作)。"
    "生成的文件保存在 桌宠私有data/office/ 下的对应子文件夹(pptx/docx/xlsx)中，用完后可调用file_ops的send操作发送到聊天窗口。"
    "注意: 图片路径必须使用本地绝对路径，图片素材在data/downloads/目录下，可先用shell_exec下载图片。"
)
TOOL_PARAMETERS = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "description": "操作类型",
            "enum": ["create", "add", "set", "view", "get", "batch", "help", "install"]
        },
        "file": {
            "type": "string",
            "description": "文档路径(相对于data/downloads/)，如 'report.pptx'、'data.xlsx'"
        },
        "format": {
            "type": "string",
            "description": "文档格式（仅 create 时需要）",
            "enum": ["pptx", "docx", "xlsx"]
        },
        "path": {
            "type": "string",
            "description": "元素路径，如 '/' 表示根、'/slide[1]' 第一张幻灯片、'/slide[1]/shape[1]' 第一个形状"
        },
        "type": {
            "type": "string",
            "description": "添加的元素类型: slide(幻灯片), shape(形状/文本框), image(图片), sheet(工作表), table(表格), paragraph(段落)"
        },
        "props": {
            "type": "object",
            "description": "属性键值对。PPT常用: title(标题), text(文字), x/y(位置如'2cm'), w/h(宽高), font(字体), size(字号), color/fill(颜色), background(背景色), bold(加粗)。Excel: name(表名), value(值), formula(公式)。图片: src(本地图片绝对路径)"
        },
        "mode": {
            "type": "string",
            "description": "view 查看模式",
            "enum": ["outline", "text", "html"]
        },
        "commands": {
            "type": "array",
            "description": "batch操作的JSON命令数组，每条命令含op(操作:add/set/remove)和对应参数。示例: [{\"op\":\"add\",\"path\":\"/\",\"type\":\"slide\",\"props\":{\"title\":\"标题\"}}]"
        },
        "data": {
            "type": "object",
            "description": "merge模板合并时的JSON数据"
        }
    },
    "required": ["action"],
}

# ★ 1.5.0: Office 文件目录迁移到 桌宠私有data/office（与沙箱可写区一致），按文件类型分子文件夹
PROJECT_ROOT = Path(__file__).resolve().parent.parent
OFFICE_DIR = PROJECT_ROOT / "data" / "桌宠私有data" / "office"
DOWNLOAD_DIR = PROJECT_ROOT / "data" / "downloads"  # 保留通用下载目录（图片等素材）

# Office 子文件夹映射
_FMT_DIR = {
    "pptx": OFFICE_DIR / "pptx",
    "docx": OFFICE_DIR / "docx",
    "xlsx": OFFICE_DIR / "xlsx",
    "pdf":  OFFICE_DIR / "pdf",
}
for _d in _FMT_DIR.values():
    _d.mkdir(parents=True, exist_ok=True)

# OfficeCLI 路径检测
_OFFICECLI_PATH = None


def _find_officecli() -> str | None:
    """查找 officecli 可执行文件"""
    global _OFFICECLI_PATH
    if _OFFICECLI_PATH is not None:
        return _OFFICECLI_PATH

    # 1. 检查 PATH 中是否有
    import shutil
    found = shutil.which("officecli")
    if found:
        _OFFICECLI_PATH = found
        return _OFFICECLI_PATH

    # 2. 检查常见安装位置
    candidates = [
        Path.home() / ".officecli" / "officecli.exe",
        Path.home() / ".officecli" / "officecli",
        PROJECT_ROOT / "data" / "officecli.exe",
        PROJECT_ROOT / "data" / "officecli",
    ]
    for c in candidates:
        if c.exists():
            _OFFICECLI_PATH = str(c)
            return _OFFICECLI_PATH

    # 3. 尝试直接运行（首次运行会自动安装）
    try:
        result = subprocess.run(["officecli", "--version"], capture_output=True, timeout=10,
                                encoding="utf-8", errors="replace")
        if result.returncode == 0:
            _OFFICECLI_PATH = "officecli"
            return _OFFICECLI_PATH
    except Exception:
        pass

    return None


def _resolve_path(file: str | None) -> Path:
    """解析文件路径到 Office 对应子文件夹"""
    if not file:
        return OFFICE_DIR
    p = Path(file)
    if p.is_absolute():
        return p
    # ★ 1.4.0: 根据扩展名自动路由到 office 子文件夹
    ext = p.suffix.lower().lstrip(".")
    target = _FMT_DIR.get(ext, OFFICE_DIR)
    return target / p.name


def _run_officecli(*args, timeout: int = 60) -> dict:
    """运行 officecli 命令并返回 JSON 结果"""
    cli = _find_officecli()
    if not cli:
        return {"success": False, "error": "OfficeCLI 未安装。请先执行 install 操作或手动安装: irm https://raw.githubusercontent.com/iOfficeAI/OfficeCLI/main/install.ps1 | iex"}

    cmd = [cli, *args, "--json"]
    _log.debug(f"执行: {' '.join(cmd)}")
    try:
        result = subprocess.run(
            cmd, capture_output=True, timeout=timeout, cwd=str(OFFICE_DIR),
            encoding="utf-8", errors="replace",  # ★ Windows GBK 兼容
        )
        stdout = (result.stdout or "").strip()
        stderr = (result.stderr or "").strip()
        if stderr:
            _log.debug(f"stderr: {stderr[:200]}")

        # ★ 1.4.0+: 优先解析 stdout JSON — OfficeCLI 有时 exit code ≠ 0 但 JSON 里 success:true
        parsed_stdout = None
        if stdout:
            try:
                parsed_stdout = json.loads(stdout)
            except json.JSONDecodeError:
                parsed_stdout = None

        if result.returncode != 0:
            # ★ 如果 stdout JSON 明确说 success:true，以 JSON 为准
            if isinstance(parsed_stdout, dict) and parsed_stdout.get("success"):
                _log.debug(f"OfficeCLI exit={result.returncode} 但 JSON success=true，以 JSON 为准")
                return parsed_stdout
            return {"success": False, "error": stderr or stdout or f"退出码 {result.returncode}"}

        # officecli 有些命令（如 view）即使加 --json 也可能输出纯文本
        if parsed_stdout is not None:
            return parsed_stdout if isinstance(parsed_stdout, dict) else {"success": True, "result": parsed_stdout}
        if stdout:
            return {"success": True, "text": stdout}
        return {"success": True, "message": "操作完成"}
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "执行超时"}
    except FileNotFoundError:
        return {"success": False, "error": f"找不到 officecli 命令。请先安装 OfficeCLI。"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _install() -> str:
    """安装 OfficeCLI"""
    import platform
    system = platform.system().lower()

    if system == "windows":
        # ★ 使用 -UseBasicParsing 避免交互式确认弹框
        cmd = (
            'powershell -NoProfile -NonInteractive -Command '
            '"& { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; '
            'irm -UseBasicParsing https://raw.githubusercontent.com/iOfficeAI/OfficeCLI/main/install.ps1 | iex }"'
        )
    elif system == "darwin":
        cmd = 'curl -fsSL https://raw.githubusercontent.com/iOfficeAI/OfficeCLI/main/install.sh | bash'
    else:
        cmd = 'curl -fsSL https://raw.githubusercontent.com/iOfficeAI/OfficeCLI/main/install.sh | bash'

    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, timeout=120,
            encoding="utf-8", errors="replace",
        )
        if result.returncode == 0:
            # 重新检测
            found = _find_officecli()
            if found:
                return f"OfficeCLI 安装成功，路径: {found}"
            return "安装脚本执行完成，但未能自动检测。请重启程序后重试。"
        return f"安装失败: {result.stderr[:300]}"
    except subprocess.TimeoutExpired:
        return "安装超时（120秒）。请手动安装: https://github.com/iOfficeAI/OfficeCLI/releases"
    except Exception as e:
        return f"安装出错: {e}"


def execute(action: str, file: str = None, format: str = None,
            path: str = "/", type: str = None, props: dict = None,
            mode: str = "outline", commands: list = None, data: dict = None) -> str:
    """执行 Office 文档操作"""

    # --- install ---
    if action == "install":
        return _install()

    # --- help ---
    if action == "help":
        return _get_help(format)

    # --- create ---
    if action == "create":
        if not format:
            return "请指定 format 参数: pptx/docx/xlsx"
        if not file:
            return "请指定 file 参数（文件名，如 'report.pptx'）"
        fpath = _resolve_path(file)
        fpath.parent.mkdir(parents=True, exist_ok=True)
        result = _run_officecli("create", str(fpath))
        if result.get("success"):
            return f"已创建文档: {fpath.name} (完整路径: {fpath})"
        return f"创建失败: {result.get('error', result.get('text', '未知错误'))}"

    # --- view ---
    if action == "view":
        if not file:
            return "请指定 file 参数"
        fpath = _resolve_path(file)
        if not fpath.exists():
            return f"文件不存在: {fpath}"
        result = _run_officecli("view", str(fpath), mode)
        if result.get("success"):
            text = result.get("text", json.dumps(result.get("result", result), ensure_ascii=False, indent=2))
            return f"文档 {fpath.name} 的 {mode}:\n{text[:3000]}"
        return f"查看失败: {result.get('error', '未知错误')}"

    # --- get ---
    if action == "get":
        if not file:
            return "请指定 file 参数"
        fpath = _resolve_path(file)
        if not fpath.exists():
            return f"文件不存在: {fpath}"
        result = _run_officecli("get", str(fpath), path, "--depth", "2")
        if result.get("success"):
            return json.dumps(result.get("result", result), ensure_ascii=False, indent=2)
        return f"获取失败: {result.get('error', '未知错误')}"

    # --- add ---
    if action == "add":
        if not file or not type:
            return "请指定 file 和 type 参数"
        fpath = _resolve_path(file)
        if not fpath.exists():
            return f"文件不存在: {fpath}。请先用 create 创建文件。"

        args = ["add", str(fpath), path, "--type", type]
        if props:
            for k, v in props.items():
                # 特殊处理 src（图片路径）
                if k == "src" and isinstance(v, str) and not Path(v).is_absolute():
                    v = str(DOWNLOAD_DIR / v)
                args.extend(["--prop", f"{k}={v}"])

        result = _run_officecli(*args)
        if result.get("success"):
            added = result.get("path", path)
            return f"已添加到 {fpath.name}: {added}\n路径: {result.get('path', 'N/A')}"
        return f"添加失败: {result.get('error', '未知错误')}"

    # --- set ---
    if action == "set":
        if not file:
            return "请指定 file 参数"
        fpath = _resolve_path(file)
        if not fpath.exists():
            return f"文件不存在: {fpath}"

        args = ["set", str(fpath), path]
        if props:
            for k, v in props.items():
                args.extend(["--prop", f"{k}={v}"])

        result = _run_officecli(*args)
        if result.get("success"):
            return f"已修改: {fpath.name} → {path}\n属性: {json.dumps(props or {}, ensure_ascii=False)}"
        return f"修改失败: {result.get('error', '未知错误')}"

    # --- batch ---
    if action == "batch":
        if not file or not commands:
            return "请指定 file 和 commands 参数"
        fpath = _resolve_path(file)
        if not fpath.exists():
            return f"文件不存在: {fpath}。请先用 create 创建文件。"

        # 将 commands 转为 officecli batch 格式
        batch_commands = []
        for cmd in commands:
            op = cmd.get("op", "add")
            batch_cmd = {"command": op}
            if "path" in cmd:
                batch_cmd["path"] = cmd.get("path", "/")
            if "type" in cmd:
                batch_cmd["type"] = cmd.get("type")
            if "props" in cmd:
                batch_cmd["props"] = cmd.get("props")
            batch_commands.append(batch_cmd)

        batch_json = json.dumps(batch_commands, ensure_ascii=False)
        _log.debug(f"batch commands: {batch_json[:500]}")

        # 通过 stdin 传入
        try:
            cli = _find_officecli()
            proc = subprocess.run(
                [cli, "batch", str(fpath), "--json"],
                input=batch_json, capture_output=True, timeout=120,
                encoding="utf-8", errors="replace",
            )
            stdout = (proc.stdout or "").strip()
            if proc.returncode == 0:
                return f"批量操作完成 ({len(batch_commands)} 条命令)\n文件: {fpath.name}\n结果: {stdout[:500]}"
            return f"批量操作失败: {proc.stderr or stdout}"
        except subprocess.TimeoutExpired:
            return "批量操作超时"
        except Exception as e:
            return f"批量操作错误: {e}"

    return f"未知操作: {action}。支持: create, add, set, view, get, batch, help, install"


def _get_help(format_type: str = None) -> str:
    """获取帮助信息"""
    if format_type:
        result = _run_officecli("help", format_type, "set", timeout=15)
    else:
        result = _run_officecli("--help", timeout=15)

    if result.get("success"):
        return result.get("text", "帮助信息获取成功")
    return f"帮助获取失败: {result.get('error', '未知错误')}\n\n常用属性速查:\nPPT幻灯片: title, background(颜色如#1A1A2E)\nPPT形状: text, x, y, w, h, font, size(字号), color(文字颜色), fill(填充色), bold\nPPT图片: src(本地图片绝对路径), x, y, w, h\nExcel: name(工作表名), value, formula\n\n尺寸单位: 2cm, 1in, 72pt, 96px\n颜色: #FF0000, red, rgb(255,0,0)"
