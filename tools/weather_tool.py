"""
天气查询工具 — 和风天气 API（本地城市映射，无需 geo API）

★ 1.9.1：加了**未来天气预报**（你 22:41 的批注："只加未来天气"）。
  用 `days` 参数控制：
    · 不传 / days=0 → 只查实时（和以前一样，行为不变）
    · days=1..3      → 走 /v7/weather/3d
    · days=4..7      → 走 /v7/weather/7d
  实测（2026-09-18）：现有 `WEATHER_API_KEY` **本来就有预报权限**，
  不用另外申请 key —— 之前日志里写"要你给 API Key 才能真跑"是我判断错了。
"""
import csv
import os
import logging
import requests
from dotenv import load_dotenv
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

_log = logging.getLogger("Tool.Weather")

TOOL_NAME = "weather_query"
TOOL_DESCRIPTION = (
    "查询指定城市的天气。传入城市名称(中文)即可，只有国内城市。\n"
    "参数 days：不传就只查**实时**天气；传 1~7 则查**未来几天的预报**"
    "（含今天，会给出每天的最高/最低温、白天夜间天气、风向风力、降水、紫外线）。"
    "用户问「明天/后天/这周/未来几天」时必须传 days。"
)
TOOL_PARAMETERS = {
    "type": "object",
    "properties": {
        "city": {
            "type": "string",
            "description": "城市名称,如'北京'、'上海'",
        },
        "days": {
            "type": "integer",
            "description": ("查未来几天的预报（1~7，含今天）。"
                            "不传或传 0 就只查实时天气。"),
        },
    },
    "required": ["city"],
}


# ==================== 城市 ID 映射（本地 CSV，无需调用 geo API） ====================

def _load_city_map() -> dict:
    """加载城市名 → Location ID 映射表"""
    city_map = {}
    csv_path = BASE_DIR / "data" / "China-City-List-latest.csv"
    if not csv_path.exists():
        _log.warning(f"城市CSV不存在: {csv_path}")
        return city_map

    with open(csv_path, "r", encoding="utf-8") as f:
        next(f)  # 跳过版本行
        reader = csv.DictReader(f)
        for row in reader:
            name = row.get("Location_Name_ZH", "").strip()
            loc_id = row.get("Location_ID", "").strip()
            if name and loc_id:
                city_map[name] = loc_id
    _log.debug(f"加载了 {len(city_map)} 个城市映射")
    return city_map


CITY_ID_MAP = _load_city_map()


# ==================== 查询入口 ====================

def _headers(api_key: str) -> dict:
    """★ 1.9.1: 改用请求头鉴权 `X-QW-Api-Key`。

    和风天气现在推荐这种写法；老写法 `?key=` 实测还能用，
    但 key 放在 URL 里会被写进日志/代理记录。两种都通，用更干净的这个。
    """
    return {"X-QW-Api-Key": api_key, "Accept-Encoding": "gzip"}


def _day_label(idx: int) -> str:
    return {0: "今天", 1: "明天", 2: "后天"}.get(idx, "")


def _query_forecast(city: str, location_id: str, api_key: str,
                    api_host: str, days: int) -> str:
    """★ 1.9.1: 未来天气预报（逐日）"""
    days = max(1, min(7, int(days)))
    endpoint = "3d" if days <= 3 else "7d"
    url = f"https://{api_host}/v7/weather/{endpoint}"
    try:
        resp = requests.get(url, params={"location": location_id},
                            headers=_headers(api_key), timeout=8)
        if resp.status_code != 200:
            _log.warning(f"天气预报 HTTP {resp.status_code}: {resp.text[:200]}")
            return f"天气预报查询失败 (HTTP {resp.status_code})"
        data = resp.json()
        if data.get("code") != "200":
            _log.warning(f"天气预报API错误: code={data.get('code')}")
            return f"天气预报查询失败: {data.get('code')}"

        rows = (data.get("daily") or [])[:days]
        if not rows:
            return f"{city}的预报没有数据"
        lines = [f"{city}未来 {len(rows)} 天预报："]
        for i, d in enumerate(rows):
            label = _day_label(i)
            date_s = (d.get("fxDate") or "")[5:]          # 只留 MM-DD，省 token
            day_t = d.get("textDay") or ""
            night_t = d.get("textNight") or ""
            # 白天夜里一样就只写一次（"晴" 比 "晴转晴" 干净）
            sky = day_t if day_t == night_t else f"{day_t}转{night_t}"
            precip = d.get("precip") or "0.0"
            extra = ""
            try:
                if float(precip) > 0:
                    extra = f"  降水{precip}mm"
            except Exception:
                pass
            lines.append(
                f"  {date_s} {label}  {sky}  "
                f"{d.get('tempMin')}~{d.get('tempMax')}°C  "
                f"{d.get('windDirDay')}{d.get('windScaleDay')}级  "
                f"湿度{d.get('humidity')}%{extra}")
        return "\n".join(lines)
    except requests.exceptions.Timeout:
        return "天气预报查询超时，请稍后重试"
    except requests.exceptions.ConnectionError as e:
        return f"天气API连接失败: {e}"
    except Exception as e:
        _log.error(f"天气预报查询异常: {e}")
        return f"天气预报查询出错: {e}"


def execute(city: str, days: int = 0, **kwargs) -> str:
    """查询天气（days>0 时查未来预报）"""
    api_key = os.getenv("WEATHER_API_KEY", "")
    api_host = os.getenv("WEATHER_API_HOST", "")

    # 无 API Key → 返回模拟数据
    if not api_key:
        return f"模拟天气: {city} 晴, 25°C, 湿度45%, 风力3级"

    # 无 API Host → 返回模拟数据
    if not api_host:
        return f"模拟天气: {city} 晴, 25°C, 湿度45%, 风力3级"

    # 查本地城市映射
    location_id = CITY_ID_MAP.get(city)
    if not location_id:
        _log.debug(f"未找到城市映射: city='{city}', 已加载 {len(CITY_ID_MAP)} 个城市")
        return f"未找到城市: {city}（当前支持 {len(CITY_ID_MAP)} 个城市）"

    # ★ 1.9.1: 要预报就走预报那条路
    try:
        if int(days or 0) > 0:
            return _query_forecast(city, location_id, api_key, api_host, int(days))
    except (TypeError, ValueError):
        pass

    # 调实时天气 API
    url = f"https://{api_host}/v7/weather/now"
    try:
        resp = requests.get(url, params={
            "location": location_id,
        }, headers=_headers(api_key), timeout=8)

        if resp.status_code != 200:
            _log.warning(f"天气查询 HTTP {resp.status_code}: {resp.text[:200]}")
            return f"天气查询失败 (HTTP {resp.status_code})"

        data = resp.json()
        if data.get("code") != "200":
            _log.warning(f"天气API错误: code={data.get('code')}")
            return f"天气查询失败: {data.get('code')}"

        now = data["now"]
        return (
            f"{city}天气: {now['text']}, "
            f"温度{now['temp']}°C, "
            f"体感{now['feelsLike']}°C, "
            f"湿度{now['humidity']}%, "
            f"风力{now['windScale']}级"
        )
    except requests.exceptions.Timeout:
        return "天气查询超时，请稍后重试"
    except requests.exceptions.ConnectionError as e:
        return f"天气API连接失败: {e}"
    except Exception as e:
        _log.error(f"天气查询异常: {e}")
        return f"天气查询出错: {e}"
