"""工具包。

★ 1.8.10: 这里放一份「跨模块重载依然有效」的回调登记表。
★ 1.9.2: 再加上一份「跨模块重载依然有效」的**长流程状态**登记表。

背景（真实 bug）：ToolManager 用 importlib 把 tools/*.py 加载成**私有模块名**
`_prts_tool_<name>`（见 src/core/tool_manager.py 的 _load_module），
而 MainWindow 注册回调时 import 的是 `tools.endfield_tool` / `tools.jmcomic_tool`
—— 这是**两个不同的 module 对象**。

于是工具里那句 `global _on_login_done; _on_login_done = cb` 设的是"另一个模块"
的全局变量，真正在跑的那份模块里它仍然是 None：
终末地扫码登录成功后回调永不触发、jmcomic 下载完也不弹文件卡片。

把回调放进包级字典（`tools` 包只被导入一次、不随工具重载而重建），
两边读到的就是同一个值。

★ 1.9.2 追加（这条是"解耦调研"里机械复现出来的，对应遗留项 D1）：
回调解决了还不够 —— **长流程的中间状态**同样是模块级变量，一样会丢。
LAZY 工具空闲 30 秒就被卸载（src/core/tool_manager.py 的 IDLE_UNLOAD_TIMEOUT），
而终末地扫码登录会开一个后台轮询线程（tools/endfield_tool.py 的 _poll_login），
用户慢慢扫码很容易超过 30 秒：

    实测（_tmp_endfield_state_proof.py 的复现）：
      加载后 _login_state id = 2144863097088  {'running': True, ...}
      卸载   → sys.modules 里的 _prts_tool_endfield 被移除
      重载后 _login_state id = 2144863095680  {}          ← 空的新 dict！
      正在跑的轮询线程继续往【旧 dict】写结果，
      新调用读到的是【空的新 dict】→ 登录状态机错乱。

所以长流程状态也放进包级字典。锁（Lock）同理：卸载后重建的锁是两个不同的锁，
等于临界区失效。
"""

import threading as _threading

# 工具名 → 回调。键名用 TOOL_NAME（如 "endfield" / "jmcomic"）
LOGIN_CALLBACKS: dict = {}        # endfield: (ok: bool, message: str, role: dict)
FILE_READY_CALLBACKS: dict = {}   # jmcomic: (label: str, path: str, size: str)

# ★ 1.9.2: 长流程的中间状态。工具名 → {"state": {...}, "lock": Lock}
#   由各工具在**首次使用时**自行填充（用下面的 get_session）。
LOGIN_SESSIONS: dict = {}
LOGIN_SESSIONS_LOCK = _threading.Lock()


def get_session(name: str) -> dict:
    """取（或首次创建）某个工具的**跨卸载长流程会话**。

    返回稳定对象 `{"state": {...}, "lock": Lock}`：同一个 name 永远拿到同一份，
    即使该工具模块被 LAZY 卸载又重新 import。工具的写法：

        from tools import get_session
        _S = get_session("endfield")
        _login_state = _S["state"]      # 全文照旧写 _login_state[...]
        _login_lock  = _S["lock"]       # 全文照旧写 with _login_lock:
    """
    s = LOGIN_SESSIONS.get(name)
    if s is None:
        with LOGIN_SESSIONS_LOCK:
            s = LOGIN_SESSIONS.get(name)
            if s is None:
                s = {"state": {}, "lock": _threading.Lock()}
                LOGIN_SESSIONS[name] = s
    return s
