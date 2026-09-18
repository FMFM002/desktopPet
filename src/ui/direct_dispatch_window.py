"""
★ 1.9.1 新功能：「指令直传」窗口。

用户原话（1.9.1 末尾的新要求）：
  "增加新的功能，用户指令直传。在聊天窗口最下面，'终止对话''终止工具'
   按钮右边再加一个'指令直传'按钮。点击后，屏幕弹出全新窗口，里面是工具
   名称列表+详情。100px  ＋ 420px（大概，最好参考设置界面）
   tools列表+当前选中tool详情。刚进入没选择时，显示默认图片，和设置一样。
   详情的内容就是一横排，action：'这里做下拉菜单'，值'用户编辑'。每个
   工具都不一样，要单独适配。然后下面一个子窗口写简介。主窗口不做滚轮，
   子窗口做。注意文字排版，提行避免单行超长。
   最后，右下角一个'发送'按钮。
   整个界面和设置界面几乎一模一样。
   然后，工具返回的内容，包括下载的文件，自动发送到聊天窗口。样式是形象
   发言的样式，只不过第一排写明<工具直传>
   整个过程不经过LLM，不计入上下文，不入库，不计入长期记忆。"

布局完全照搬设置界面 1.9.1 第四轮改过的"上下两块纯文字标题 + 一层
滚动区"那一版（HTML v4 单层）。区别：这里是"工具列表 + 工具参数详情"。

每个工具的参数编辑是**单独适配**的（用户明确要求）——
所以这里用一个分发表 `_TOOL_SCHEMAS`：key 是工具名，
value 是「action 列表 + 对每个 action 的字段定义」。通用 action 字段
类型（string/int/boolean）由系统派生编辑器；只有"路径/列表/特殊"等
需要手工提示的地方在 `_TOOL_SCHEMAS` 里给出 placeholder/说明。

发送：emit `dispatch_request(tool_name, action, params: dict)`。
主窗口侧接收后：
  · tool_manager.execute_tool(name, **params) 同步跑（不并发，自己玩）
  · 把结果投到聊天窗口（用 bot_image_in / file_card_in / 新加的 bot_text_in），
    **走形象发言样式，但最顶上一行写 `<工具直传>`**
  · 不动 session.messages、不写 chat_records、不喂 LTM
"""

import json
import logging
import re
from pathlib import Path
from typing import Any, Optional

from PySide6.QtCore import Qt, Signal, QTimer
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QScrollArea,
    QFormLayout, QLineEdit, QSpinBox, QDoubleSpinBox, QCheckBox, QComboBox,
    QPlainTextEdit,
)

_log = logging.getLogger("DirectDispatch")

_BASE_DIR = Path(__file__).resolve().parent.parent.parent
_DEFAULT_IMG = _BASE_DIR / "data" / "assets" / "t&PRTS.jpg"


# ─────────────────────────────────────────────────────────────────────
# 工具参数 schema 适配器：把"工具级" + "action 级"参数定义映射成 UI
# ─────────────────────────────────────────────────────────────────────

class _FieldSpec:
    """一个表单字段的定义。

    `name`    - 参数名（喂给工具时的 kwargs 键）
    `label`   - 表单左侧显示的名字
    `type`    - "string" | "int" | "float" | "bool" | "multiline" | "list"
    `default` - 默认值（可选）
    `placeholder` / `help` / `choices` - 各种修饰
    """
    __slots__ = ("name", "label", "type", "default", "placeholder", "help", "choices")

    def __init__(self, name, label="", type_="string", default=None,
                 placeholder="", help="", choices=None):
        self.name = name
        self.label = label or name
        self.type = type_
        self.default = default
        self.placeholder = placeholder
        self.help = help
        self.choices = choices


# ★ 这里只列"用户直传时一定会碰"的字段，**自动适配兜底**走下面 schema 通用适配。
#   真做不到的手填一填；用户原话："每个工具都不一样，要单独适配"。
_TOOL_SCHEMAS: dict[str, dict] = {
    # ★★ 1.9.1 二次修正（真机日志报错后逐工具核对 src/core/tool_manager 的
    #    `TOOL_PARAMETERS` 与 tools/*.py 的 `def execute(...)` **真实签名**）：
    #    旧版这里是"凭想象写的"，actions 大多是编的（比如 file_ops 写了
    #    read/write/delete/copy/move，真实是 view/read/write/edit/search/list/
    #    send/list_downloads），工具名也少了后缀对不上（jmcomic_tool ≠ jmcomic）。
    #    更坑的是 ToolInfo.execute 遇到 TypeError 会**回退成无参调用**，
    #    所以参数名写错不会立刻报错，而是悄悄跑出个莫名其妙的结果。
    #    现在一律以真实签名为准；没有 action 参数的工具就 actions: []。

    # ── weather_query：execute(city, days=0) —— 无 action ──
    "weather_query": {
        "actions": [],
        "fields": [
            _FieldSpec("city", "城市", "string",
                       placeholder="北京 / 上海 / Tokyo",
                       help="中文城市名 / 英文；必填"),
            _FieldSpec("days", "未来几天", "int", default=0,
                       help="0 = 只查实时；1~7 = 未来 N 天（含今天）；范围外会被夹。"),
        ],
        "intro": (
            "和风天气。真实参数只有 city / days：days=0 查实时，"
            "days=1~7 查未来 N 天。city 支持「北京」「Shanghai」。"
        ),
    },
    # ── web_search：execute(query) —— 只有一个参数，多传就 TypeError ──
    "web_search": {
        "actions": [],
        "fields": [
            _FieldSpec("query", "搜索关键词", "string",
                       placeholder="明日方舟 阿米娅", help="必填"),
        ],
        "intro": "通用网页搜索。真实参数只有 query（一次一个关键词）。",
    },
    # ── download：execute(url, filename, bucket) —— 无 action ──
    "download": {
        "actions": [],
        "fields": [
            _FieldSpec("url", "URL", "string", placeholder="https://...", help="必填"),
            _FieldSpec("filename", "另存为", "string", placeholder="留空从 URL 推断",
                       help="含扩展名；不带也行。"),
            _FieldSpec("bucket", "子目录", "string", default="websearch",
                       help="存到 data/downloads/<bucket>/ 下，默认 websearch。"),
        ],
        "intro": (
            "通用 HTTP 下载（图片/PDF/音频/视频/任意二进制）。"
            "真实参数：url / filename / bucket。"
        ),
    },
    # ── file_ops：execute(action, **kwargs) —— 真实 action 8 个 ──
    "file_ops": {
        "actions": ["view", "read", "write", "edit", "search", "list",
                    "send", "list_downloads"],
        "fields": [],
        "action_fields": {
            "view": [
                _FieldSpec("path", "路径", "string",
                           placeholder="相对项目根 或 绝对路径"),
                _FieldSpec("method", "查看方式", "string", default="auto",
                           help="auto / multimodal / vision / stt / text，一般留 auto"),
                _FieldSpec("pages", "页码", "string",
                           placeholder='空 = 默认 / "3" / "3-5" / "1,4,7"'),
                _FieldSpec("max_chars", "文本上限", "int", default=0,
                           help="0 = 用默认 20000 字，超出自动抽样头尾"),
                _FieldSpec("prompt", "视觉提示词", "string", placeholder="一般留空"),
            ],
            "read": [
                _FieldSpec("path", "路径", "string", placeholder="相对项目根 或 绝对路径"),
                _FieldSpec("offset", "起始行", "int", default=0, help="0 = 从头"),
                _FieldSpec("limit", "读取行数", "int", default=0, help="0 = 全部"),
            ],
            "write": [
                _FieldSpec("path", "路径", "string", placeholder="要写入的文件"),
                _FieldSpec("content", "内容", "multiline", placeholder="写入的文本"),
            ],
            "edit": [
                _FieldSpec("path", "路径", "string"),
                _FieldSpec("old_string", "被替换的旧文本", "multiline"),
                _FieldSpec("new_string", "替换成", "multiline"),
            ],
            "search": [
                _FieldSpec("path", "目录", "string", placeholder="从哪个目录开始搜"),
                _FieldSpec("pattern", "搜索模式", "string", placeholder="关键词 / 正则"),
                _FieldSpec("file_types", "文件类型", "string", placeholder="如 .py,.md"),
                _FieldSpec("case_sensitive", "区分大小写", "bool", default=False),
            ],
            "list": [
                _FieldSpec("path", "目录", "string", placeholder="留空 = 项目根"),
                _FieldSpec("pattern", "文件过滤", "string", default="*",
                           placeholder="如 *.py"),
            ],
            "send": [
                _FieldSpec("path", "要发送的文件", "string",
                           placeholder="完整路径；会显示在聊天窗口"),
            ],
            "list_downloads": [
                _FieldSpec("pattern", "文件过滤", "string", default="*"),
            ],
        },
        "intro": (
            "本地文件操作。真实 action：view / read / write / edit / search / "
            "list / send / list_downloads。相对路径以项目根为基准。"
        ),
    },
    # ── shell_exec：execute(command, cwd) —— 无 action ──
    "shell_exec": {
        "actions": [],
        "fields": [
            _FieldSpec("command", "命令", "string",
                       placeholder="dir / echo hello / pip list", help="必填"),
            _FieldSpec("cwd", "工作目录", "string", placeholder="留空 = 项目根"),
        ],
        "intro": (
            "在本进程内同步执行命令；输出会截断。"
            "Windows 下临时文件请写 data\\xxx，别用 /tmp/。"
        ),
    },
    # ── system_command：execute(action, param="") ──
    "system_command": {
        "actions": ["get_ip_info", "get_local_ip", "open_app", "open_calc",
                    "open_notepad", "open_explorer", "open_browser",
                    "system_info", "get_time",
                    "list_processes", "disk_usage", "memory_usage",
                    "kill_process", "open_url"],
        "fields": [
            _FieldSpec("param", "param", "string",
                       placeholder="应用名/exe完整路径、网址、目录、进程名",
                       help="open_app 填应用名或 exe 路径；其余 action 留空即可"),
        ],
        "intro": (
            "系统预注册命令。真实参数只有 action / param："
            "open_app 填应用名或 exe 完整路径（成功启动会自动记住路径）、"
            "open_url 填网址、open_explorer 填目录、kill_process 填进程名/PID。"
        ),
    },
    # ── meme：execute(tag, name, reason) —— 无 action ──
    "meme": {
        "actions": [],
        "fields": [
            _FieldSpec("tag", "情绪标签", "string",
                       placeholder="happy / daily / angry / confused / sad / shy ...",
                       help="★必填：库里真实存在的标签"),
            _FieldSpec("name", "文件名", "string",
                       placeholder="如 009.webp，一般不用"),
            _FieldSpec("reason", "为什么发这张", "string",
                       placeholder="一句话，只记日志"),
        ],
        "intro": (
            "发表情包（先进入待发送队列，回复结束后单独发出）。★ tag 必填："
            "常用 happy/daily/angry/confused/sad/shy；另有 like/see/cpu/color/"
            "sleep/work/meow/sigh/surprised/fool/baka/givemoney/morning。"
        ),
    },
    # ── office：execute(action, file, format, path, type, props, mode, commands, data) ──
    "office": {
        "actions": ["create", "add", "set", "view", "get", "batch", "help", "install"],
        "fields": [],
        "action_fields": {
            "create": [
                _FieldSpec("file", "文件名", "string",
                           placeholder="report.pptx（存 data/downloads/ 下）"),
                _FieldSpec("format", "格式", "string", default="pptx",
                           placeholder="pptx / docx / xlsx", help="必填"),
            ],
            "add": [
                _FieldSpec("file", "文件", "string", placeholder="report.pptx"),
                _FieldSpec("path", "元素路径", "string", default="/",
                           placeholder="/slide[1]/shape[1]"),
                _FieldSpec("type", "元素类型", "string",
                           placeholder="slide / shape / image / sheet / table"),
                _FieldSpec("props", "属性 (JSON)", "json",
                           placeholder='{"title":"标题","text":"正文"}'),
            ],
            "set": [
                _FieldSpec("file", "文件", "string"),
                _FieldSpec("path", "元素路径", "string", default="/"),
                _FieldSpec("props", "属性 (JSON)", "json",
                           placeholder='{"text":"改后的文字"}'),
            ],
            "get": [
                _FieldSpec("file", "文件", "string"),
                _FieldSpec("path", "元素路径", "string", default="/"),
            ],
            "view": [
                _FieldSpec("file", "文件", "string"),
                _FieldSpec("mode", "查看模式", "string", default="outline",
                           placeholder="outline / text / html"),
            ],
            "batch": [
                _FieldSpec("file", "文件", "string"),
                _FieldSpec("path", "元素路径", "string", default="/"),
                _FieldSpec("commands", "命令数组 (JSON)", "json",
                           placeholder='[{"op":"add","path":"/slide","type":"slide"}]'),
            ],
            "help": [
                _FieldSpec("format", "格式", "string", placeholder="pptx / docx / xlsx"),
            ],
            "install": [],
        },
        "intro": (
            "Office 文档增删改查（officecli）。真实 action：create / add / set / "
            "view / get / batch / help / install。file 是 data/downloads/ 下的"
            "文件名；props / commands 填 JSON。"
        ),
    },
    # ── jmcomic：execute(action, keyword, album_id, page, limit) ──
    "jmcomic": {
        "actions": ["search", "info", "download", "progress"],
        "fields": [],
        "action_fields": {
            "search": [
                _FieldSpec("keyword", "搜索词", "string", placeholder="必填"),
                _FieldSpec("page", "页码", "int", default=1),
                _FieldSpec("limit", "返回条数", "int", default=10),
            ],
            "info": [
                _FieldSpec("album_id", "本子 ID", "string", placeholder="搜索结果里的 id"),
            ],
            "download": [
                _FieldSpec("album_id", "本子 ID", "string", placeholder="要下载的 id"),
            ],
            "progress": [],
        },
        "intro": "JMComic 本子搜索/下载。产物自动合成 PDF（后台任务）。",
    },
    # ── bilibili_video：execute(action, bv_id, raw_message) ──
    "bilibili_video": {
        "actions": ["info", "cover", "download"],
        "fields": [
            _FieldSpec("bv_id", "BV 号", "string",
                       placeholder="1k24y1B7Ef（可带 BV 前缀）",
                       help="与 raw_message 二选一"),
            _FieldSpec("raw_message", "原始消息", "string",
                       placeholder="粘整段含链接的话，自动提 BV/b23 短链"),
        ],
        "intro": (
            "B 站视频。真实 action：info（详情）/ cover（下封面，会自动显示在"
            "聊天窗口）/ download（下视频）。bv_id 与 raw_message 给一个就行。"
        ),
    },
    # ── pixiv：execute(action, **kwargs) ──
    "pixiv": {
        "actions": ["info", "search", "ranking", "user", "download"],
        "fields": [],
        "action_fields": {
            "info": [
                _FieldSpec("illust_id", "作品 ID", "string",
                           placeholder="纯数字 ID 或整段 P 站链接"),
            ],
            "search": [
                _FieldSpec("keyword", "搜索词", "string", placeholder="必填"),
                _FieldSpec("limit", "返回条数", "int", default=10),
            ],
            "ranking": [
                _FieldSpec("mode", "榜单", "string", default="day",
                           placeholder="day / week / month / day_male / ..."),
                _FieldSpec("limit", "返回条数", "int", default=10),
            ],
            "user": [
                _FieldSpec("user_id", "画师 ID", "string", placeholder="ID 或主页链接"),
                _FieldSpec("limit", "返回条数", "int", default=10),
            ],
            "download": [
                _FieldSpec("illust_id", "作品 ID", "string", placeholder="必填"),
                _FieldSpec("pages", "要下哪几页", "string",
                           placeholder='空 = 全部 / "1" / "1-3"'),
            ],
        },
        "intro": "Pixiv 图片。真实 action：info / search / ranking / user / download。",
    },
    # ── knowledge_graph：execute(action, **kwargs) ──
    "knowledge_graph": {
        "actions": ["analyze_project", "add_entity", "add_relation", "query_entity",
                    "query_relation", "search", "list_types", "stats"],
        "fields": [],
        "action_fields": {
            "analyze_project": [
                _FieldSpec("project_path", "项目根目录", "string",
                           placeholder="绝对路径"),
                _FieldSpec("project_name", "项目名", "string",
                           placeholder="留空取目录名"),
            ],
            "add_entity": [
                _FieldSpec("name", "实体名", "string"),
                _FieldSpec("entity_type", "类型", "string", default="note",
                           placeholder="person / concept / file / module ..."),
                _FieldSpec("description", "描述", "string"),
                _FieldSpec("properties", "额外属性", "string",
                           placeholder='JSON，如 {"lang":"py"}'),
            ],
            "add_relation": [
                _FieldSpec("source", "源实体", "string"),
                _FieldSpec("target", "目标实体", "string"),
                _FieldSpec("relation", "关系", "string", default="related_to",
                           placeholder="knows / contains / depends_on ..."),
                _FieldSpec("rel_description", "关系描述", "string"),
            ],
            "query_entity": [
                _FieldSpec("name", "实体名", "string"),
            ],
            "query_relation": [
                _FieldSpec("source", "源实体", "string"),
                _FieldSpec("target", "目标实体", "string"),
            ],
            "search": [
                _FieldSpec("query", "关键词", "string"),
                _FieldSpec("entity_type_filter", "类型过滤", "string"),
            ],
            "list_types": [
                _FieldSpec("entity_type_filter", "类型过滤", "string"),
            ],
            "stats": [],
        },
        "intro": "知识图谱（实体-关系）。真实 action：analyze_project / add_entity / "
                 "add_relation / query_entity / query_relation / search / list_types / stats。",
    },
    # ── endfield：execute(action, keyword) ──
    "endfield": {
        "actions": ["announce", "login", "login_status", "logout",
                    "char_list", "signin", "help"],
        "fields": [],
        "action_fields": {
            "char_list": [
                _FieldSpec("keyword", "角色名筛选", "string", placeholder="可选"),
            ],
        },
        "intro": (
            "终末地官方工具。announce = 公告；login / login_status / signin 走扫码"
            "登录（state 未恢复，详见 1.9.1 记录【任务 B】）。"
        ),
    },
    # ── long_term_memory：execute(text) —— 只有一个参数 ──
    "long_term_memory": {
        "actions": [],
        "fields": [
            _FieldSpec("text", "待分析文本", "multiline",
                       placeholder="把要记住的整段对话/事件粘进来（<10 字直接跳过）",
                       help="真实参数只有 text；它会用 LLM 判断值不值得记"),
        ],
        "intro": (
            "长期记忆写入通道：给一段文本，它自己判断值不值得记、再结构化提取。"
            "注意：指令直传 ≠ LTM 写入路径，这里只是手动触发一次分析。"
        ),
    },
    # ── comfyui：execute(action, prompt, raw, width, height, steps, seed, negative, lora, timeout) ──
    "comfyui": {
        "actions": ["draw", "status", "queue", "models", "free", "start", "stop"],
        "fields": [],
        "action_fields": {
            "draw": [
                _FieldSpec("prompt", "提示词", "multiline",
                           placeholder="画什么（直传不做规范化，直接写清楚）",
                           help="必填"),
                _FieldSpec("negative", "反向提示词", "multiline",
                           placeholder="不想出现的特征，留空用内置"),
                _FieldSpec("width", "宽", "int", default=512),
                _FieldSpec("height", "高", "int", default=512),
                _FieldSpec("steps", "步数", "int", default=35,
                           help="越大越慢越细"),
                _FieldSpec("seed", "种子", "int", default=-1,
                           help="-1 = 随机；想复现同一张就固定一个数"),
                _FieldSpec("lora", "LoRA", "string", placeholder="留空用 NBfym.safetensors"),
                _FieldSpec("timeout", "等图超时(秒)", "int", default=600),
            ],
            "status": [], "queue": [], "models": [],
            "free": [], "start": [], "stop": [],
        },
        "intro": (
            "ComfyUI 画图（后台任务）。draw 秒回；出图后自动发到聊天窗口。"
            "产物落在 data/桌宠私有data/comfyui/。"
        ),
    },
}


# ─────────────────────────────────────────────────────────────────────
# 表单字段编辑器
# ─────────────────────────────────────────────────────────────────────

class _FieldEditor(QWidget):
    """单个字段的编辑器：左边 label，右边输入控件，根据 type_ 切换。"""

    def __init__(self, spec: _FieldSpec, parent=None):
        super().__init__(parent)
        self.spec = spec
        layout = QFormLayout(self)
        layout.setContentsMargins(0, 4, 0, 4)
        layout.setSpacing(8)
        layout.setLabelAlignment(Qt.AlignRight)

        type_hint = {
            "string": "文本", "int": "整数", "float": "小数", "bool": "布尔",
            "multiline": "多行", "list": "列表(JSON)", "json": "JSON",
        }.get(spec.type, spec.type)
        name_lbl = QLabel(f"<b>{spec.label}</b>")
        name_lbl.setStyleSheet("color:#1a3d6d; font-size:12px;")
        type_lbl = QLabel(f"<span style='color:#999;font-size:10px;'>{type_hint}</span>")
        if spec.help:
            type_lbl.setToolTip(spec.help)
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(2)
        ll.addWidget(name_lbl)
        ll.addWidget(type_lbl)
        layout.addRow(left, self._make_editor())

    def _make_editor(self) -> QWidget:
        t = self.spec.type
        if t == "int":
            self.editor = QSpinBox()
            self.editor.setRange(-2_000_000_000, 2_000_000_000)
            v = self.spec.default if isinstance(self.spec.default, int) else 0
            self.editor.setValue(v)
            return self.editor
        if t == "float":
            self.editor = QDoubleSpinBox()
            self.editor.setDecimals(2)
            self.editor.setRange(-1e6, 1e6)
            v = float(self.spec.default) if isinstance(self.spec.default, (int, float)) else 0.0
            self.editor.setValue(v)
            return self.editor
        if t == "bool":
            self.editor = QCheckBox()
            self.editor.setChecked(bool(self.spec.default))
            return self.editor
        if t == "multiline":
            self.editor = QPlainTextEdit()
            self.editor.setMaximumHeight(80)
            if isinstance(self.spec.default, str):
                self.editor.setPlainText(self.spec.default)
            elif self.spec.default is not None:
                self.editor.setPlainText(str(self.spec.default))
            self.editor.setPlaceholderText(self.spec.placeholder or "")
            return self.editor
        if t == "list":
            self.editor = QLineEdit()
            self.editor.setPlaceholderText(self.spec.placeholder or '[1, 2, 3]')
            self.editor.setText("[]" if self.spec.default is None else str(self.spec.default))
            return self.editor
        if t == "json":
            # ★ 1.9.1 二次修正：office 的 props / commands / data 是 dict / list，
            #   让用户直接填 JSON；空着就**不传**这个参数（用工具默认值）。
            self.editor = QPlainTextEdit()
            self.editor.setMaximumHeight(64)
            self.editor.setPlaceholderText(self.spec.placeholder or "{}")
            if self.spec.default is not None:
                self.editor.setPlainText(
                    json.dumps(self.spec.default, ensure_ascii=False))
            return self.editor
        # default: string
        self.editor = QLineEdit()
        self.editor.setPlaceholderText(self.spec.placeholder or "")
        if self.spec.default is not None:
            self.editor.setText(str(self.spec.default))
        return self.editor

    def value(self) -> Any:
        t = self.spec.type
        if t == "int":
            return int(self.editor.value())
        if t == "float":
            return float(self.editor.value())
        if t == "bool":
            return bool(self.editor.isChecked())
        if t == "multiline":
            return self.editor.toPlainText()
        if t == "list":
            txt = (self.editor.text() or "").strip() or "[]"
            try:
                v = json.loads(txt)
                if not isinstance(v, list):
                    raise ValueError("not a list")
                return v
            except Exception as e:
                raise ValueError(f"[{self.spec.name}] 列表字段不是合法 JSON：{e}")
        if t == "json":
            txt = (self.editor.toPlainText() or "").strip()
            if not txt:
                return None            # 空 = 不传这个参数
            try:
                return json.loads(txt)
            except Exception as e:
                raise ValueError(f"[{self.spec.name}] 不是合法 JSON：{e}")
        return self.editor.text() or ""


# ─────────────────────────────────────────────────────────────────────
# 主窗口
# ─────────────────────────────────────────────────────────────────────

class DirectDispatchWindow(QWidget):
    """★ 1.9.1: 用户指令直传窗口。

    按钮放聊天窗口 ctrl_row「终止工具」右边；点击后弹出本窗口。
    选定工具 → 选 action → 填字段 → 点右下角「发送」。
    """

    # 用户点「发送」时发出：tool_name 一定是已注册的工具；
    # action 是 action 字段下拉里的字符串；params 是除 action 外的其它参数字典。
    dispatch_request = Signal(str, str, dict)

    # 窗口被用户关闭（closeEvent 触发）
    window_closed = Signal()

    DEFAULT_WIDTH = 520
    DEFAULT_HEIGHT = 640
    MIN_WIDTH = 460
    MIN_HEIGHT = 480

    def __init__(self, tool_manager=None, parent=None):
        super().__init__(parent, Qt.Window)
        self.setWindowTitle("指令直传（不经过 LLM）")
        self.setMinimumSize(self.MIN_WIDTH, self.MIN_HEIGHT)
        self.resize(self.DEFAULT_WIDTH, self.DEFAULT_HEIGHT)

        self._tm = tool_manager
        self._tools: list[dict] = self._collect_tools()
        self._field_editors: list = []
        self._current_tool: Optional[str] = None
        self._current_action: Optional[str] = None
        self._action_combo: Optional[QComboBox] = None
        # ★ 修复：字段单独装在一个容器里，避免 _rebuild_fields 用
        #   "插到最后一个控件前面"这种脆逻辑插错位置。
        self._fields_box: Optional[QWidget] = None
        self._fields_box_lay: Optional[QVBoxLayout] = None

        self._build_ui()
        if self._tools:
            self._select_tool(self._tools[0]["name"])

    # ── 数据 ───────────────────────────────────────────────────────

    def _collect_tools(self) -> list[dict]:
        """从 ToolManager 拿到所有工具，再叠加本模块的 schema 适配。"""
        out = []
        tm = self._tm
        # ★ 修复：以前只要 `tm is not None` 就直接调用，若外面把 MainWindow
        #   之类的对象当第一个位置参数传进来，就会炸 "no attribute get_all_tools"。
        #   现在按"能力"判断：拿不出 get_all_tools 就回退到全局单例。
        if tm is None or not hasattr(tm, "get_all_tools"):
            try:
                from src.core.tool_manager import get_tool_manager
                tm = get_tool_manager()
            except Exception as e:
                _log.debug(f"拿不到 ToolManager：{e}")
                return out
        try:
            tools = tm.get_all_tools()
        except Exception as e:
            _log.warning(f"列出工具失败：{e}")
            return out
        # ★ 1.9.1 二次修正：只列"当前启用"的工具（用户原话："当前启用的工具
        #   才会出现"）。get_all_tools() 返回的是**全部**工具（含设置里关掉的）。
        disabled_policy = None
        try:
            from src.core.tool_manager import ToolLoadPolicy as _TLP
            disabled_policy = _TLP.DISABLED
        except Exception:
            pass
        for t in tools:
            try:
                name = t.name
                if name == "idle_talk":
                    continue
                if not getattr(t, "enabled", True):
                    continue
                if disabled_policy is not None and getattr(t, "load_policy", None) == disabled_policy:
                    continue
                desc = (t.description or "").strip()
                params = getattr(t, "parameters", {}) or {}
                generic_actions = []
                act_prop = (params.get("properties") or {}).get("action") or {}
                if isinstance(act_prop.get("enum"), list):
                    generic_actions = [str(x) for x in act_prop["enum"]]
                custom = _TOOL_SCHEMAS.get(name) or {}
                # ★ 1.9.1 二次修正：**以工具自己 TOOL_PARAMETERS 里的 action 枚举
                #   为准**（那是真的），schema 里写的只当兜底 —— 免得再出现
                #   "下拉里是编的 action、点发送直接传错"。
                if generic_actions:
                    actions = generic_actions
                elif custom.get("actions"):
                    actions = list(custom["actions"])
                else:
                    actions = []
                out.append({
                    "name": name,
                    "description": desc,
                    "actions": actions,
                    "custom": custom,
                    "params": params,
                })
            except Exception as e:
                _log.debug(f"工具 {getattr(t, 'name', '?')} 元数据收集失败：{e}")
        out.sort(key=lambda x: x["name"])
        return out

    # ── UI ────────────────────────────────────────────────────────

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 10, 12, 10)
        outer.setSpacing(8)

        # 顶部小提示
        tip = QLabel(
            "<span style='color:#666;'>"
            "本窗口 = <b>跳过 LLM</b> 直接调工具。结果会出现在聊天窗口，"
            "<b>不计上下文</b>、<b>不入库</b>、<b>不进长期记忆</b>。"
            "</span>"
        )
        tip.setWordWrap(True)
        tip.setTextFormat(Qt.RichText)
        tip.setStyleSheet("font-size:11.5px; padding:2px 4px;")
        outer.addWidget(tip)

        # ── 上：工具列表 ──
        ttl1 = QLabel("工具列表 <span style='color:#999; font-weight:400;'>"
                      "（点工具名选中；当前启用的工具才会出现）</span>")
        ttl1.setTextFormat(Qt.RichText)
        ttl1.setStyleSheet("font-size:12.5px; color:#444; font-weight:600;"
                           " padding:0 2px; background:transparent;")
        outer.addWidget(ttl1)

        self._tools_scroll = QScrollArea()
        self._tools_scroll.setWidgetResizable(True)
        self._tools_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._tools_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._tools_scroll.setStyleSheet(
            "QScrollArea { background:#fff; border:1px solid #e2e2e6; border-radius:10px; }")
        self._tools_scroll.setMinimumHeight(120)
        outer.addWidget(self._tools_scroll, 3)

        self._tools_box = QWidget()
        self._tools_layout = QVBoxLayout(self._tools_box)
        self._tools_layout.setContentsMargins(6, 6, 6, 6)
        self._tools_layout.setSpacing(4)
        self._tools_scroll.setWidget(self._tools_box)
        self._populate_tools_list()

        # ── 下：详情区 ──
        ttl2 = QLabel("工具详情 <span style='color:#999; font-weight:400;'>"
                      "（先选 action，再填字段；字段定义见每个工具的适配）</span>")
        ttl2.setTextFormat(Qt.RichText)
        ttl2.setStyleSheet("font-size:12.5px; color:#444; font-weight:600;"
                           " padding:0 2px; background:transparent;")
        outer.addWidget(ttl2)

        self._detail_scroll = QScrollArea()
        self._detail_scroll.setWidgetResizable(True)
        self._detail_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._detail_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._detail_scroll.setStyleSheet(
            "QScrollArea { background:#fff; border:1px solid #e2e2e6; border-radius:10px; }")
        self._detail_scroll.setMinimumHeight(160)
        outer.addWidget(self._detail_scroll, 4)

        self._detail_box = QWidget()
        self._detail_layout = QVBoxLayout(self._detail_box)
        self._detail_layout.setContentsMargins(12, 12, 12, 12)
        self._detail_layout.setSpacing(8)
        self._detail_scroll.setWidget(self._detail_box)

        # ── 底部：右下角"发送"按钮 ──
        # ★ 修复：必须**先**建按钮，因为下面 _show_default_detail() 里要用
        #   self._send_btn.setEnabled(False)。以前顺序反了 → AttributeError。
        bottom = QHBoxLayout()
        bottom.addStretch()
        self._send_btn = QPushButton("发送")
        self._send_btn.setFixedSize(96, 32)
        self._send_btn.setStyleSheet(
            "QPushButton { background:#007aff; color:#fff; border:none;"
            " border-radius:6px; font-size:13px; font-weight:bold; }"
            "QPushButton:hover { background:#0066dd; }"
            "QPushButton:pressed { background:#0055cc; }"
            "QPushButton:disabled { background:#ccc; color:#888; }")
        self._send_btn.clicked.connect(self._on_send_clicked)
        bottom.addWidget(self._send_btn)
        outer.addLayout(bottom)

        self._show_default_detail()

    # ── 工具列表 ─────────────────────────────────────────────────

    def _populate_tools_list(self):
        while self._tools_layout.count():
            it = self._tools_layout.takeAt(0)
            w = it.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        if not self._tools:
            empty = QLabel("（没有可用工具——到「设置 → 工具设置」确认加载策略）")
            empty.setStyleSheet("color:#999; font-size:11.5px; padding:6px;")
            self._tools_layout.addWidget(empty)
            self._tools_layout.addStretch()
            return
        for t in self._tools:
            row = QPushButton(t["name"])
            row.setCheckable(True)
            row.setCursor(Qt.PointingHandCursor)
            row.setStyleSheet(
                "QPushButton { background:transparent; border:none;"
                " text-align:left; padding:6px 10px; font-size:12px; color:#222;"
                " border-radius:4px; }"
                "QPushButton:hover { background:#f0f0f5; }"
                "QPushButton:checked { background:#007aff; color:#fff; font-weight:bold; }"
            )
            row.setToolTip(t["description"][:200] or t["name"])
            row.clicked.connect(lambda _=False, n=t["name"]: self._select_tool(n))
            self._tools_layout.addWidget(row)
        self._tools_layout.addStretch()

    def _select_tool(self, name: str):
        self._current_tool = name
        for i in range(self._tools_layout.count()):
            it = self._tools_layout.itemAt(i)
            w = it.widget() if it else None
            if isinstance(w, QPushButton):
                w.setChecked(w.text() == name)
        tool = next((t for t in self._tools if t["name"] == name), None)
        if tool is None:
            self._show_default_detail()
            return
        self._show_tool_detail(tool)

    # ── 详情区 ────────────────────────────────────────────────────

    def _clear_detail(self):
        while self._detail_layout.count():
            it = self._detail_layout.takeAt(0)
            w = it.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        self._field_editors = []
        self._current_action = None
        self._action_combo = None
        self._fields_box = None
        self._fields_box_lay = None

    def _show_default_detail(self):
        self._clear_detail()
        lbl = QLabel()
        lbl.setAlignment(Qt.AlignCenter)
        lbl.setStyleSheet("background:transparent;")
        if _DEFAULT_IMG.exists():
            pm = QPixmap(str(_DEFAULT_IMG))
            if not pm.isNull():
                lbl.setPixmap(pm.scaled(480, 200, Qt.KeepAspectRatio,
                                        Qt.SmoothTransformation))
        else:
            lbl.setText("（默认图缺失：data/assets/t&PRTS.jpg）")
        self._detail_layout.addWidget(lbl)
        tip = QLabel("从左边列表选一个工具开始。")
        tip.setAlignment(Qt.AlignCenter)
        tip.setStyleSheet("color:#888; font-size:12px; background:transparent;")
        self._detail_layout.addWidget(tip)
        self._detail_layout.addStretch()
        self._send_btn.setEnabled(False)

    def _show_tool_detail(self, tool: dict):
        self._clear_detail()
        title = QLabel(f"<b>{tool['name']}</b>")
        title.setStyleSheet("font-size:14px; background:transparent;")
        self._detail_layout.addWidget(title)

        actions = tool.get("actions") or []
        if actions:
            row = QWidget()
            row_lay = QHBoxLayout(row)
            row_lay.setContentsMargins(0, 0, 0, 0)
            row_lay.setSpacing(8)
            lbl = QLabel("<b>action</b>")
            lbl.setStyleSheet("color:#1a3d6d; font-size:12px;")
            lbl.setFixedWidth(72)
            row_lay.addWidget(lbl)
            self._action_combo = QComboBox()
            self._action_combo.setStyleSheet(
                "QComboBox { background:#fafafa; border:1px solid #d0d0d5;"
                " border-radius:4px; padding:4px 6px; font-size:12px;"
                " selection-background-color:#007aff; selection-color:#fff; }")
            for a in actions:
                self._action_combo.addItem(a)
            self._action_combo.currentTextChanged.connect(self._on_action_changed)
            row_lay.addWidget(self._action_combo, 1)
            self._detail_layout.addWidget(row)
            # ★ 修复：这里**先不**触发 _on_action_changed —— 那时字段容器
            #   还没建，字段会被 insertWidget(count-1) 插到 action 上面去。
            #   等容器 + 简介 + 弹性都摆好，最后统一触发一次。
        else:
            lbl = QLabel("（此工具没有 action 参数 —— 直接填下面字段、点发送）")
            lbl.setStyleSheet("color:#888; font-size:11.5px; background:transparent;")
            self._detail_layout.addWidget(lbl)

        # 字段容器：所有字段编辑器都挂在这一个 box 里，位置永远在 action 之下、
        # 简介之上（不再依赖"插到倒数第二个 widget 前"这种脆弱算法）。
        self._fields_box = QWidget()
        self._fields_box_lay = QVBoxLayout(self._fields_box)
        self._fields_box_lay.setContentsMargins(0, 0, 0, 0)
        self._fields_box_lay.setSpacing(6)
        self._detail_layout.addWidget(self._fields_box)

        custom = tool.get("custom") or {}
        intro = custom.get("intro") or tool.get("description") or "（没有说明）"
        intro_lbl = QLabel(self._format_intro(intro))
        intro_lbl.setWordWrap(True)
        intro_lbl.setTextFormat(Qt.RichText)
        intro_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        intro_lbl.setStyleSheet(
            "color:#333; font-size:11.5px; background:transparent; padding-top:6px;")
        self._detail_layout.addWidget(intro_lbl)

        self._detail_layout.addStretch()
        self._send_btn.setEnabled(True)

        # 布局就绪 → 现在才真正铺字段
        if actions:
            self._action_combo.currentTextChanged.connect(self._on_action_changed)
            self._on_action_changed(actions[0])
        else:
            self._rebuild_fields(tool, action=None)

    def _on_action_changed(self, action_text: str):
        self._current_action = action_text or None
        tool = next((t for t in self._tools if t["name"] == self._current_tool), None)
        if tool is None:
            return
        self._rebuild_fields(tool, action=self._current_action)

    def _rebuild_fields(self, tool: dict, action):
        # 清掉挂在字段容器里的旧编辑器
        if self._fields_box_lay is not None:
            while self._fields_box_lay.count():
                it = self._fields_box_lay.takeAt(0)
                w = it.widget()
                if w is not None:
                    w.setParent(None)
                    w.deleteLater()
        self._field_editors = []

        custom = tool.get("custom") or {}
        all_specs: list = list(custom.get("fields") or [])
        if action:
            all_specs += list((custom.get("action_fields") or {}).get(action) or [])

        # ★ 1.9.1 二次修正：只有"完全没适配过"的工具才用 properties 兜底派生。
        #   只要 _TOOL_SCHEMAS 里给这个工具写过东西（custom 非空），就**完全**
        #   按 schema 来 —— 否则 file_ops / comfyui 这种 fields 为空的工具会被
        #   兜底塞进十几个无关字段。
        if not all_specs and not custom:
            props = (tool.get("params") or {}).get("properties") or {}
            for pname, prop in props.items():
                if pname == "action":
                    continue
                if not isinstance(prop, dict):
                    continue
                ptype = prop.get("type", "string")
                if ptype not in ("string", "integer", "number", "boolean"):
                    ptype = "string"
                if ptype == "integer":
                    ptype = "int"
                if ptype == "number":
                    ptype = "float"
                if ptype == "boolean":
                    ptype = "bool"
                all_specs.append(_FieldSpec(
                    pname, pname, ptype,
                    placeholder=(prop.get("description", "") or "")[:60],
                ))
        if not all_specs:
            none = QLabel("（此 action 无需参数 —— 直接点右下角「发送」）")
            none.setStyleSheet("color:#888; font-size:11.5px; background:transparent;")
            if self._fields_box_lay is not None:
                self._fields_box_lay.addWidget(none)
            return

        for spec in all_specs:
            ed = _FieldEditor(spec)
            self._field_editors.append(ed)
            if self._fields_box_lay is not None:
                self._fields_box_lay.addWidget(ed)

    # ── 简介排版（和设置界面同款）───────────────────────────────

    @staticmethod
    def _format_intro(text: str, width: int = 30) -> str:
        """★ 1.9.1：把一坨说明排成**短行分点**，避免单行超长。"""
        t = (text or "").strip()
        if not t:
            return "（没有说明）"
        t = re.sub(r"支持[:：]\s*", "支持：\n", t)
        out = []
        for para in [p.strip() for p in t.split("\n") if p.strip()]:
            for sent in [s.strip() for s in re.split(r"(?<=[。；;！!？?])", para) if s.strip()]:
                s = sent
                while len(s) > width:
                    cut = max(s.rfind("，", 0, width), s.rfind("、", 0, width),
                              s.rfind(",", 0, width), s.rfind(" ", 0, width))
                    if cut < 8:
                        cut = width - 1
                    out.append(s[:cut + 1].strip())
                    s = s[cut + 1:].strip()
                if s:
                    out.append(s)
        if not out:
            return t
        merged = []
        for ln in out:
            if merged and merged[-1].endswith(("：", ":")):
                merged[-1] = merged[-1] + ln
            else:
                merged.append(ln)
        return "<br>".join("· " + ln for ln in merged)

    # ── 发送 ──────────────────────────────────────────────────────

    def _on_send_clicked(self):
        if not self._current_tool:
            return
        action = self._current_action or ""
        params: dict = {}
        try:
            for ed in self._field_editors:
                v = ed.value()
                # ★ 1.9.1 二次修正：值为 None（JSON 字段留空）→ 不传这个参数，
                #   让工具用它自己的默认值。否则会被当成"用户显式传了 None"。
                if v is None:
                    continue
                params[ed.spec.name] = v
        except ValueError as e:
            try:
                QApplication_beep()
            except Exception:
                pass
            self._flash_send_status(f"[错误] {e}", ok=False)
            return
        if action:
            params["action"] = action
        self._flash_send_status("已发送", ok=True)
        self.dispatch_request.emit(self._current_tool, action, params)

    def _flash_send_status(self, msg: str, ok: bool = True):
        try:
            self._send_btn.setText(msg[:16])
            self._send_btn.setEnabled(False)
            QTimer.singleShot(1500, lambda: self._restore_send_btn())
        except Exception:
            pass

    def _restore_send_btn(self):
        try:
            self._send_btn.setText("发送")
            self._send_btn.setEnabled(bool(self._current_tool))
        except Exception:
            pass

    # ── 窗口关闭 ──────────────────────────────────────────────────

    def closeEvent(self, event):
        try:
            self.window_closed.emit()
        except Exception:
            pass
        super().closeEvent(event)


def QApplication_beep():
    """小工具：跨 Qt 版本的提示音（部分旧版没有 QApplication.beep）。"""
    try:
        from PySide6.QtWidgets import QApplication as _QA
        _QA.beep()
    except Exception:
        pass
