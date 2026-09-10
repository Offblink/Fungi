"""The system-tray icon (menu, notifications, close-to-tray)."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # the window imports this module, so no runtime cycle
    from .app import FungiGui

from PyQt5.QtGui import QCursor
from PyQt5.QtWidgets import (
    QSystemTrayIcon,
)
from qfluentwidgets import (
    Action,
    SystemTrayMenu,
)

from ..tray import make_icon


class _Tray(QSystemTrayIcon):
    """托盘：房间后台驻留期间提供 显示主界面 / 打开 WebUI / 退出（fluent 菜单）。"""

    def __init__(self, window: "FungiGui"):
        super().__init__(make_icon())
        self._window = window
        self.setToolTip("Fungi")
        self._menu = SystemTrayMenu(title="Fungi")  # keep referenced: the tray does not own it
        self._menu.addAction(Action("显示主界面", triggered=window.show_and_raise))
        self._menu.addAction(Action("打开 WebUI", triggered=window.open_webui_from_tray))
        self._menu.addSeparator()
        self._menu.addAction(Action("退出", triggered=window.quit_from_tray))
        self.activated.connect(self._on_activated)

    def _on_activated(self, reason) -> None:
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self._window.show_and_raise()
        elif reason == QSystemTrayIcon.Context:
            self._menu.exec_(QCursor.pos())

    def notify(self, title: str, body: str) -> None:
        self.showMessage(title, body, QSystemTrayIcon.Information, 8000)
