"""
终端命令执行工具 — 执行系统命令并返回结果
素材来源: 提取终端工具部分/shell_exec.py (codex-rs 移植)
"""
import re
import subprocess
import os
import tempfile
import time
import logging
from pathlib import Path

_log = logging.getLogger("Tool.ShellExec")

# ★ 1.8.10: PROJECT_ROOT 必须在下面的 try 之前定义。
#   兜底分支原来引用了 PROJECT_ROOT，而它是在本文件第 58 行才赋值的
#   → 兜底分支一执行就 NameError，模块直接导入失败 ——
#   偏偏这就是那条兜底代码存在的意义（tools.file_ops 不可用时）。
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# ★ 1.5.0 安全沙箱：复用 file_ops 的可写区定义
try:
    from tools.file_ops import _is_within_writable, WRITABLE_ROOT
except Exception:  # 兜底：避免导入异常导致工具不可用
    WRITABLE_ROOT = PROJECT_ROOT / "data" / "桌宠私有data"

    def _is_within_writable(p: str) -> bool:
        try:
            np = Path(os.path.normpath(os.path.abspath(p))).resolve()
            return str(np).startswith(str(WRITABLE_ROOT.resolve()))
        except Exception:
            return False

TOOL_NAME = "shell_exec"
TOOL_DESCRIPTION = (
    "执行终端/shell命令并返回stdout和stderr。"
    "Windows用cmd语法，Linux/macOS用bash语法。"
    "★ 沙箱限制：用户电脑全盘只读，仅 data/桌宠私有data 目录可写可读。"
    "写入/下载/生成的文件请使用绝对路径 data/桌宠私有data/... 或先 cd 到该目录。"
    "★ Windows重要提示: 临时文件请写到 data/桌宠私有data/，不要用 /tmp/（Windows 不存在 /tmp/）。"
    "限制: 超时60秒、输出≤50000字符。"
)
TOOL_PARAMETERS = {
    "type": "object",
    "properties": {
        "command": {
            "type": "string",
            "description": "要执行的命令。"
                           "★Windows: 临时输出文件请用 data\\xxx 或 .\\xxx，不要用 /tmp/。"
                           "Windows常用: ipconfig /all, tasklist, dir, ping, nslookup, systeminfo, curl/wget等。"
                           "Linux/macOS常用: ifconfig, ps aux, ls -la, ping, nslookup, curl/wget等。",
        },
        "cwd": {
            "type": "string",
            "description": "工作目录（可选，默认为项目目录）",
        },
    },
    "required": ["command"],
}

# 安全限制
MAX_OUTPUT_BYTES = 50000
TIMEOUT_SECONDS = 60
# PROJECT_ROOT 已在文件顶部定义（供导入失败时的兜底分支使用）


def _sandbox_guard(command: str, cwd: str) -> str | None:
    """★ 1.5.0 沙箱静态检查（best-effort）。

    规则：
      1) 工作目录必须位于项目内（防止逃逸到用户全盘）。
      2) 命令若包含写入/破坏性操作，其目标路径必须位于沙箱可写区
         (data/桌宠私有data)，否则拦截。
    返回 None = 放行；返回字符串 = 拒绝原因。
    """
    # 1) cwd 必须在项目根内
    if cwd:
        cwd_abs = str(Path(os.path.abspath(cwd)).resolve())
        if not cwd_abs.startswith(str(PROJECT_ROOT.resolve())):
            return (f"[拒绝] 沙箱限制：工作目录必须位于项目内（当前: {cwd}）。\n"
                    f"请在项目目录或桌宠私有data下操作。")

    # 2) 收集潜在"写入目标"路径
    targets: list[str] = []
    # 重定向 > 或 >>
    for m in re.finditer(r">>?\s*\"?([^\s\"|>]+)", command):
        targets.append(m.group(1))
    # copy / move / cp / mv / xcopy / robocopy 的疑似目标（取最后一个路径形态 token）
    low = command.lower()
    if any(k in low for k in ("copy ", "xcopy", "move ", " ren ", "cp ", " mv ", "robocopy")):
        toks = re.findall(
            r"\"?([A-Za-z]:\\[^\s\"|<>]+|/(?:[^\s\"|<>]+))\"?"
            r"|([^\s\"|<>]+\\[^\s\"|<>]+)", command)
        flat = [a or b for a, b in toks]
        if flat:
            targets.append(flat[-1])
    # 破坏性动词 + 绝对路径
    destructive = ("del ", "erase ", "rmdir ", "rd ", "rm ", "format ", "shutdown ")
    abs_paths = re.findall(r"[A-Za-z]:\\[^\s\"|<>]+|/(?:usr|home|opt|var|tmp|etc|mnt|root|Users|Windows)[^\s\"|<>]*", command)
    if any(k in low for k in destructive):
        targets.extend(abs_paths)

    for t in targets:
        t = t.strip('"')
        if not t:
            continue
        if not os.path.isabs(t):
            t = os.path.normpath(os.path.join(cwd or str(PROJECT_ROOT), t))
        if not _is_within_writable(t):
            return (f"[拒绝] 沙箱限制：禁止向只读区域写入/修改: {t}\n"
                    f"仅允许在 {WRITABLE_ROOT} 下操作。")
    return None


def execute(command: str, cwd: str = "") -> str:
    """执行shell命令并返回格式化结果"""
    work_dir = cwd or str(PROJECT_ROOT)
    if not os.path.isdir(work_dir):
        work_dir = str(PROJECT_ROOT)

    # ★ 1.5.0 沙箱：拦截向只读区的写入
    deny = _sandbox_guard(command, work_dir)
    if deny:
        _log.warning(f"沙箱拦截命令: {command[:100]}")
        return deny

    start_time = time.time()
    _log.info(f"执行命令: {command[:100]} (cwd={work_dir})")

    # Windows 预处理: % 转义 + /tmp/ 路径转换
    if os.name == "nt":
        # ★ 精准转义: 只转义 URL 编码的 %XX（如 %E5），保留 CMD 变量 %VAR%
        command_escaped = re.sub(r'%([0-9A-Fa-f]{2})', r'%%\1', command)
        # ★ /tmp/ → Windows 临时目录
        win_temp = tempfile.gettempdir()  # C:\Users\xxx\AppData\Local\Temp
        command_escaped = re.sub(r'\b/tmp/', win_temp.replace('\\', '\\\\') + '\\\\', command_escaped)
        cmd_list = ["cmd.exe", "/c", command_escaped]
    else:
        cmd_list = ["/bin/sh", "-c", command]

    try:
        proc = subprocess.Popen(
            cmd_list,
            cwd=work_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
        )

        try:
            stdout, stderr = proc.communicate(timeout=TIMEOUT_SECONDS)
            exit_code = proc.returncode
            timed_out = False
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
            exit_code = -1
            timed_out = True

    except FileNotFoundError:
        _log.error(f"命令未找到: {command[:80]}")
        return f"[错误] 命令未找到: {command}"
    except Exception as e:
        _log.error(f"执行失败: {e}", exc_info=True)
        return f"[错误] 执行失败: {e}"

    duration_ms = (time.time() - start_time) * 1000

    # 截断过长输出
    truncated = False
    if len(stdout) > MAX_OUTPUT_BYTES:
        stdout = stdout[:MAX_OUTPUT_BYTES] + "\n... [输出已截断]"
        truncated = True
    if len(stderr) > MAX_OUTPUT_BYTES:
        stderr = stderr[:MAX_OUTPUT_BYTES] + "\n... [stderr已截断]"
        truncated = True

    _log.info(f"命令完成: exit={exit_code}, 耗时={duration_ms:.0f}ms, "
              f"stdout={len(stdout)}字节, stderr={len(stderr)}字节"
              + (" (超时)" if timed_out else "")
              + (" (截断)" if truncated else ""))

    # 格式化结果
    lines = [f"命令: {command}"]
    lines.append(f"工作目录: {work_dir}")
    lines.append(f"退出码: {exit_code}")
    lines.append(f"耗时: {duration_ms:.0f}ms")
    if timed_out:
        lines.append(f"⚠ 执行超时 (>{TIMEOUT_SECONDS}秒)")
    if truncated:
        lines.append("⚠ 输出已截断")

    if stdout:
        lines.append(f"\n--- STDOUT ---\n{stdout.rstrip()}")
    if stderr:
        lines.append(f"\n--- STDERR ---\n{stderr.rstrip()}")
    if not stdout and not stderr:
        lines.append("(无输出)")

    return "\n".join(lines)
