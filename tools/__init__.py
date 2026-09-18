"""工具包。

★ 1.8.10: 这里放一份「跨模块重载依然有效」的回调登记表。

背景（真实 bug）：ToolManager 用 importlib 把 tools/*.py 加载成**私有模块名**
`_prts_tool_<name>`（见 src/core/tool_manager.py 的 _load_module），
而 MainWindow 注册回调时 import 的是 `tools.endfield_tool` / `tools.jmcomic_tool`
—— 这是**两个不同的 module 对象**。

于是工具里那句 `global _on_login_done; _on_login_done = cb` 设的是"另一个模块"
的全局变量，真正在跑的那份模块里它仍然是 None：
终末地扫码登录成功后回调永不触发、jmcomic 下载完也不弹文件卡片。

把回调放进包级字典（`tools` 包只被导入一次、不随工具重载而重建），
两边读到的就是同一个值。
"""

# 工具名 → 回调。键名用 TOOL_NAME（如 "endfield" / "jmcomic"）
LOGIN_CALLBACKS: dict = {}        # endfield: (ok: bool, message: str, role: dict)
FILE_READY_CALLBACKS: dict = {}   # jmcomic: (label: str, path: str, size: str)
