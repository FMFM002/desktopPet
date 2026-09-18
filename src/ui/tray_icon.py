"""
系统托盘组件
"""
import json
import logging
from pathlib import Path
from PySide6.QtGui import QIcon, QAction, QPixmap, QColor
from PySide6.QtWidgets import QSystemTrayIcon, QMenu, QApplication, QStyle

_log = logging.getLogger("Tray")

BASE_DIR = Path(__file__).resolve().parent.parent.parent


class TrayManager:
    """系统托盘管理"""

    def __init__(self, app: QApplication):
        self.app = app
        self._tray = QSystemTrayIcon()
        self._tray.setToolTip("铃兰桌宠")

        # 图标: 优先 icon.ico / 图标.ico, 再角色目录下 ico.ico / 头像.jpg, 再 PNG, 最后系统默认
        icon_loaded = False
        candidates = [
            BASE_DIR / "data" / "icon.ico",
            BASE_DIR / "data" / "图标.ico",
            BASE_DIR / "data" / "icon.png",
        ]
        # ★ 1.5.0: 统一结构后图标位于 data/桌宠形象/<角色>/ 下
        chars_dir = BASE_DIR / "data" / "桌宠形象"
        if chars_dir.exists():
            for d in sorted(chars_dir.iterdir()):
                if d.is_dir() and not d.name.startswith("."):
                    candidates.append(d / "ico.ico")
                    candidates.append(d / "头像.jpg")
                    candidates.append(d / "头像.png")
        for path in candidates:
            if path and path.exists():
                self._tray.setIcon(QIcon(str(path)))
                icon_loaded = True
                _log.debug(f"托盘图标: {path}")
                break

        if not icon_loaded:
            # PySide6: QStyle.StandardPixmap 枚举
            fallback_icon = app.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon)
            if fallback_icon.isNull():
                # 最终兜底：画一个 16x16 纯色图标
                pixmap = QPixmap(16, 16)
                pixmap.fill(QColor("#4A90D9"))
                self._tray.setIcon(QIcon(pixmap))
            else:
                self._tray.setIcon(fallback_icon)
            _log.warning("使用系统默认图标")

        # ★ 1.5.0: 同时设置窗口图标（任务栏/ALT+TAB 可见，且避免某些系统托盘不显示空白图标）
        try:
            if icon_loaded:
                app.setWindowIcon(self._tray.icon())
        except Exception:
            pass

        # 菜单项保存为实例属性，防止 GC 回收
        # ★ 接管（2026-09-17 22:30，你的要求）：原来是一个「显示/隐藏」开关 ——
        #   你分不清当前是哪种状态，点下去结果不确定。拆成两个明确项：
        #   「显示」强制显示、「隐藏」强制隐藏。
        self._menu_show = QAction("显示桌宠")
        self._menu_show.triggered.connect(self._force_show)
        self._menu_hide = QAction("隐藏桌宠")
        self._menu_hide.triggered.connect(self._force_hide)
        self._menu_settings = QAction("设置")
        self._menu_settings.triggered.connect(self._on_settings)
        self._menu_quit = QAction("退出")
        self._menu_quit.triggered.connect(self._on_quit)

        # 菜单
        self._menu = QMenu()
        self._menu.addAction(self._menu_show)
        self._menu.addAction(self._menu_hide)
        self._menu.addAction(self._menu_settings)
        self._menu.addSeparator()
        self._menu.addAction(self._menu_quit)

        # Windows 上 setContextMenu 常有兼容问题，改用 activated 手动弹出
        self._tray.activated.connect(self._on_activated)

        self.on_toggle = None      # 老接口（双击托盘 = 切换），保留
        self.on_show = None        # ★ 新增：强制显示
        self.on_hide = None        # ★ 新增：强制隐藏
        self.on_settings = None
        self.on_quit = None

    def show(self):
        self._tray.show()
        _log.debug("托盘已显示")

    def _toggle_visibility(self):
        if self.on_toggle:
            self.on_toggle()

    def _force_show(self):
        """★ 新增：菜单「显示桌宠」—— 不管当前什么状态，强制显示"""
        if self.on_show:
            self.on_show()
        elif self.on_toggle:
            self.on_toggle()

    def _force_hide(self):
        """★ 新增：菜单「隐藏桌宠」—— 不管当前什么状态，强制隐藏"""
        if self.on_hide:
            self.on_hide()
        elif self.on_toggle:
            self.on_toggle()

    def _on_activated(self, reason):
        if reason == QSystemTrayIcon.DoubleClick:
            self._toggle_visibility()
        elif reason == QSystemTrayIcon.Context:
            # 手动弹出菜单 —— 比 setContextMenu 更可靠
            self._menu.popup(self._tray.geometry().center())

    def _on_settings(self):
        if self.on_settings:
            self.on_settings()

    def _on_quit(self):
        _log.info("托盘退出")
        self._tray.hide()
        if self.on_quit:
            self.on_quit()
        # 注意：不在此处调用 self.app.quit()，由 main_window._quit() 统一处理

    def set_icon_tooltip(self, text: str):
        self._tray.setToolTip(text)

    def update_character_icon(self, char_name: str):
        """★ 1.5.0: 切换形象时同步更新托盘图标（按角色目录 / settings.name 匹配）"""
        try:
            chars_dir = BASE_DIR / "data" / "桌宠形象"
            if not chars_dir.exists():
                return
            target = None
            for d in sorted(chars_dir.iterdir()):
                if not (d.is_dir() and not d.name.startswith(".")):
                    continue
                if d.name == char_name:
                    target = d
                    break
                sfile = d / "settings.json"
                if sfile.exists():
                    try:
                        nm = json.loads(sfile.read_text(encoding="utf-8")).get("name", "")
                        if nm == char_name:
                            target = d
                            break
                    except Exception:
                        pass
            if target is None:
                return
            for p in (target / "ico.ico", target / "图标.ico",
                      target / "头像.jpg", target / "头像.png"):
                if p.exists():
                    self._tray.setIcon(QIcon(str(p)))
                    try:
                        self.app.setWindowIcon(self._tray.icon())
                    except Exception:
                        pass
                    self.set_icon_tooltip(f"{char_name}桌宠")
                    _log.debug(f"托盘图标已切换为: {target.name}")
                    return
        except Exception as e:
            _log.debug(f"切换托盘图标失败: {e}")
