"""
系统操作工具 - 快捷系统操作和应用管理
"""
import subprocess
import os
import platform
import datetime

TOOL_NAME = "system_command"
TOOL_DESCRIPTION = "执行快捷系统操作。支持: 查本机/公网IP、打开应用、查询系统信息、进程管理、时间日期、磁盘内存等。"
TOOL_PARAMETERS = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": [
                "get_ip_info", "get_local_ip", "open_app",
                "open_calc", "open_notepad", "open_explorer", "open_browser",
                "system_info", "get_time",
                "list_processes", "disk_usage", "memory_usage",
                "kill_process", "open_url",
            ],
            "description": (
                "操作类型:\n"
                "- get_ip_info: ★推荐。本机内网IP + 公网IP + 归属地"
                "（国家/省份/城市/运营商/ASN/时区/经纬度）\n"
                "- get_local_ip: 只要局域网IPv4（ipconfig里那个）\n"
                "- open_app: ★启动应用(需param=应用名或exe完整路径)。"
                "会先查长期记忆/注册表/开始菜单，成功启动后自动把exe路径写进长期记忆\n"
                "- open_calc / open_notepad / open_explorer / open_browser: 打开对应程序\n"
                "- system_info: 系统信息\n"
                "- get_time: 当前日期+时间（含星期与周数）\n"
                "- list_processes: 列出高CPU进程\n"
                "- disk_usage: 磁盘使用情况\n"
                "- memory_usage: 内存使用情况\n"
                "- kill_process: 终止进程(需param=进程名或PID)\n"
                "- open_url: 打开网址(需param=URL)"
            ),
        },
        "param": {
            "type": "string",
            "description": "[open_app/open_url/open_explorer/kill_process] 额外参数",
        },
    },
    "required": ["action"],
}


def _lan_ip() -> tuple:
    """取局域网 IPv4 + 主机名 → (ip, hostname)"""
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(1)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip, socket.gethostname()
    except Exception:
        try:
            hostname = socket.gethostname()
            return socket.gethostbyname(hostname), hostname
        except Exception:
            return "", ""


def _public_ip_info() -> dict:
    """★ 1.9.2（用户要求）：查公网 IP 归属地。

    用户点名要这些字段：IP / 国家 / 省份 / 城市 / 运营商 / ASN / 时区 / 经纬度。
    数据源（都免费、都不用 key）：
      · 主：`https://ipapi.co/json/`（HTTPS，1000 次/天）
      · 备：`http://ip-api.com/json/?lang=zh-CN`（HTTP 免费版；只是因为免费版不带 HTTPS，
            查 IP 归属地不涉及密钥，用 HTTP 可接受）
    """
    import requests
    # ── 主：ipapi.co ──
    try:
        r = requests.get("https://ipapi.co/json/", timeout=8,
                         headers={"User-Agent": "PTRTS-DesktopPet/1.9.2"})
        if r.status_code == 200:
            j = r.json()
            if not j.get("error"):
                return {
                    "ip": j.get("ip", ""),
                    "country": j.get("country_name") or j.get("country") or "",
                    "region": j.get("region") or "",
                    "city": j.get("city") or "",
                    "isp": j.get("org") or "",
                    "asn": j.get("asn") or "",
                    "timezone": j.get("timezone") or "",
                    "lat": j.get("latitude"),
                    "lon": j.get("longitude"),
                    "src": "ipapi.co",
                }
    except Exception:
        pass
    # ── 备：ip-api.com ──
    try:
        r = requests.get("http://ip-api.com/json/?lang=zh-CN", timeout=8)
        if r.status_code == 200:
            j = r.json()
            if j.get("status") == "success":
                return {
                    "ip": j.get("query", ""),
                    "country": j.get("country", ""),
                    "region": j.get("regionName", ""),
                    "city": j.get("city", ""),
                    "isp": j.get("isp", ""),
                    "asn": j.get("as", ""),
                    "timezone": j.get("timezone", ""),
                    "lat": j.get("lat"),
                    "lon": j.get("lon"),
                    "src": "ip-api.com",
                }
    except Exception:
        pass
    return {}


def execute(action: str, param: str = "") -> str:
    """执行系统操作"""

    if action == "open_app":
        # ★ 1.9.2（用户要求）：启动任意应用，成功一次就记住 exe 路径。
        if not param:
            return ("[错误] open_app 需要 param=应用名或exe完整路径。"
                    "例如 param=\"QQ\" 或 param=\"D:\\\\Apps\\\\QQ\\\\QQ.exe\"")
        try:
            from src.core.app_launcher import launch, known_apps
        except Exception as e:
            return f"[错误] 启动器不可用: {e}"
        ok, text, _proc = launch(param)
        if not ok:
            apps = known_apps()
            if apps:
                names = ", ".join(a["name"] for a in apps[:15])
                text += f"\n（长期记忆里已有这些应用：{names}）"
        return text

    if action == "get_ip_info":
        # ★ 1.9.2（用户要求）：一次查全 —— 内网IP + 公网IP + 完整归属地
        lan_ip, hostname = _lan_ip()
        lines = []
        if lan_ip:
            lines.append(f"内网IP: {lan_ip}")
        if hostname:
            lines.append(f"主机名: {hostname}")
        info = _public_ip_info()
        if not info:
            lines.append("公网IP: [查询失败] 网络不通或接口限流")
            lines.append("(内网IP仍然可用；公网归属地需要联网查询)")
            return "\n".join(lines)
        lines.append(f"公网IP: {info.get('ip') or '未知'}")
        lines.append(f"国家: {info.get('country') or '未知'}")
        lines.append(f"省份: {info.get('region') or '未知'}")
        lines.append(f"城市: {info.get('city') or '未知'}")
        lines.append(f"运营商: {info.get('isp') or '未知'}")
        asn = info.get("asn")
        if asn:
            if not str(asn).upper().startswith("AS"):
                asn = f"AS{asn}"
            lines.append(f"ASN: {asn}")
        else:
            lines.append("ASN: 未知")
        lines.append(f"时区: {info.get('timezone') or '未知'}")
        lat, lon = info.get("lat"), info.get("lon")
        if lat is not None and lon is not None:
            lines.append(f"经纬度: {lat}, {lon}")
        else:
            lines.append("经纬度: 未知")
        return "\n".join(lines)

    if action == "get_local_ip":
        lan_ip, hostname = _lan_ip()
        if not lan_ip:
            return "[错误] 无法获取本机IP"
        return f"本机IP: {lan_ip}\n主机名: {hostname}\n说明: 这是局域网IPv4地址（ipconfig里那个）"

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
        # ★ 1.9.2（用户要求）：原 get_time / get_date 功能重复，合并为一个。
        # 输出同时含 日期 + 时间 + 星期 + 周数，模型一次调用拿全，不必再分两次问。
        now = datetime.datetime.now()
        weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        iso = now.isocalendar()
        return (
            f"当前时间: {now.strftime('%Y-%m-%d %H:%M:%S')} "
            f"({weekdays[now.weekday()]}) 第{iso[1]}周\n"
            f"中文写法: {now.strftime('%Y年%m月%d日 %H:%M')}"
        )

    elif action == "get_date":
        # 兼容旧调用：get_date 已并入 get_time，此处保留为同义别名（枚举里不再暴露）
        return execute("get_time", param)

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
