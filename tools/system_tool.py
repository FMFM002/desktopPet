"""
系统操作工具 - 快捷系统操作和应用管理
"""
import subprocess
import os
import platform
import datetime

TOOL_NAME = "system_command"
TOOL_DESCRIPTION = "执行快捷系统操作。支持: 获取本机IP、打开应用、查询系统信息、进程管理、时间日期、磁盘内存等。"
TOOL_PARAMETERS = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": [
                "get_local_ip", "open_calc", "open_notepad", "open_explorer", "open_browser",
                "system_info", "get_time", "get_date",
                "list_processes", "disk_usage", "memory_usage",
                "kill_process", "open_url",
            ],
            "description": (
                "操作类型:\n"
                "- get_local_ip: 获取本机局域网IP地址（即ipconfig里那个IPv4，不是公网IP）\n"
                "- open_calc: 打开计算器\n"
                "- open_notepad: 打开记事本\n"
                "- open_explorer: 打开文件资源管理器\n"
                "- open_browser: 打开浏览器\n"
                "- system_info: 系统信息\n"
                "- get_time: 当前时间\n"
                "- get_date: 当前日期\n"
                "- list_processes: 列出高CPU进程\n"
                "- disk_usage: 磁盘使用情况\n"
                "- memory_usage: 内存使用情况\n"
                "- kill_process: 终止进程(需param=进程名或PID)\n"
                "- open_url: 打开网址(需param=URL)"
            ),
        },
        "param": {
            "type": "string",
            "description": "[open_url/open_explorer/kill_process] 额外参数",
        },
    },
    "required": ["action"],
}


def execute(action: str, param: str = "") -> str:
    """执行系统操作"""

    if action == "get_local_ip":
        import socket
        try:
            # 通过连接外部地址获取本机实际使用的网卡IP
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(1)
            s.connect(("8.8.8.8", 80))
            local_ip = s.getsockname()[0]
            s.close()
            hostname = socket.gethostname()
            return f"本机IP: {local_ip}\n主机名: {hostname}\n说明: 这是局域网IPv4地址（ipconfig里那个）"
        except Exception:
            # 备用方案：遍历网卡
            try:
                hostname = socket.gethostname()
                local_ip = socket.gethostbyname(hostname)
                return f"本机IP: {local_ip}\n主机名: {hostname}\n说明: 这是局域网IPv4地址"
            except Exception as e:
                return f"[错误] 无法获取本机IP: {e}"

    if action == "open_calc":
        subprocess.Popen("calc.exe" if os.name == "nt" else "gnome-calculator")
        return "✅ 已打开计算器"

    elif action == "open_notepad":
        subprocess.Popen("notepad.exe" if os.name == "nt" else "gedit")
        return "✅ 已打开记事本"

    elif action == "open_explorer":
        path = param or "."
        if os.name == "nt":
            subprocess.Popen(["explorer", os.path.abspath(path)])
        else:
            subprocess.Popen(["xdg-open", path])
        return f"✅ 已打开文件管理器: {path}"

    elif action == "open_browser":
        import webbrowser
        webbrowser.open(param or "https://www.google.com")
        return "✅ 已打开浏览器"

    elif action == "open_url":
        import webbrowser
        url = param or "https://www.google.com"
        webbrowser.open(url)
        return f"✅ 已打开网址: {url}"

    elif action == "system_info":
        info = [
            f"系统: {platform.system()} {platform.release()}",
            f"架构: {platform.machine()}",
            f"处理器: {platform.processor()}",
            f"用户: {os.getlogin()}",
            f"Python: {platform.python_version()}",
        ]
        try:
            info.append(f"主机名: {platform.node()}")
        except Exception:
            pass
        return "\n".join(info)

    elif action == "get_time":
        now = datetime.datetime.now()
        weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        return f"当前时间: {now.strftime('%Y-%m-%d %H:%M:%S')} ({weekdays[now.weekday()]})"

    elif action == "get_date":
        now = datetime.datetime.now()
        weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        return f"当前日期: {now.strftime('%Y年%m月%d日')} ({weekdays[now.weekday()]}) 第{now.isocalendar()[1]}周"

    elif action == "list_processes":
        try:
            import psutil
            procs = []
            for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent"]):
                try:
                    info = p.info
                    if info["cpu_percent"] and info["cpu_percent"] > 0.1:
                        procs.append(
                            f"  PID {info['pid']:6d}  {info['name'][:30]:30s}"
                            f"  CPU {info['cpu_percent']:5.1f}%  MEM {info['memory_percent']:5.1f}%"
                        )
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            procs.sort(
                key=lambda x: float(x.split("CPU")[-1].split("%")[0]) if "CPU" in x else 0,
                reverse=True,
            )
            return "主要进程 (CPU>0.1%):\n" + "\n".join(procs[:15])
        except ImportError:
            return "[提示] psutil未安装，无法列出进程"

    elif action == "disk_usage":
        try:
            import psutil
            lines = ["磁盘使用情况:"]
            for part in psutil.disk_partitions():
                try:
                    usage = psutil.disk_usage(part.mountpoint)
                    gb_total = usage.total / (1024 ** 3)
                    gb_used = usage.used / (1024 ** 3)
                    pct = usage.percent
                    lines.append(
                        f"  {part.mountpoint}: "
                        f"{gb_used:.1f}GB / {gb_total:.1f}GB ({pct:.0f}%)"
                    )
                except PermissionError:
                    continue
            return "\n".join(lines)
        except ImportError:
            return "[提示] psutil未安装"

    elif action == "memory_usage":
        try:
            import psutil
            mem = psutil.virtual_memory()
            gb_total = mem.total / (1024 ** 3)
            gb_used = mem.used / (1024 ** 3)
            gb_avail = mem.available / (1024 ** 3)
            return (
                f"内存: {gb_used:.1f}GB / {gb_total:.1f}GB ({mem.percent:.0f}%)\n"
                f"可用: {gb_avail:.1f}GB"
            )
        except ImportError:
            return "[提示] psutil未安装"

    elif action == "kill_process":
        if not param:
            return "[错误] kill_process 需要进程名或PID参数"
        try:
            import psutil
            killed = []
            for p in psutil.process_iter(["pid", "name"]):
                try:
                    if str(p.info["pid"]) == param or (
                        p.info["name"] and param.lower() in p.info["name"].lower()
                    ):
                        p.terminate()
                        killed.append(f"  {p.info['name']} (PID {p.info['pid']})")
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            if killed:
                return "✅ 已终止进程:\n" + "\n".join(killed)
            return f"未找到匹配进程: {param}"
        except ImportError:
            return "[提示] psutil未安装"

    return f"[错误] 未知操作: {action}"
