"""System tray: runtime-drawn mushroom icon, menu, and notifications.

Menu: open WebUI / open data directory / quit. Double-click and message-click
both open the WebUI.
"""

import webbrowser
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QIcon, QPainter, QPixmap
from PyQt6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

_ACCENT = "#e07a5f"


def make_icon(size: int = 64) -> QIcon:
    """运行时绘制蘑菇图标（菌盖 + 菌柄 + 斑点），避免二进制资源文件。"""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)

    # cap: upper half-circle
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(_ACCENT))
    painter.drawPie(
        int(size * 0.10), int(size * 0.12), int(size * 0.80), int(size * 0.80), 0, 180 * 16
    )
    # stem
    painter.drawRoundedRect(
        int(size * 0.40),
        int(size * 0.48),
        int(size * 0.20),
        int(size * 0.40),
        int(size * 0.08),
        int(size * 0.08),
    )
    # cap spots (knocked out)
    painter.setBrush(QColor("#ffffff"))
    for cx, cy, r in ((0.32, 0.30, 0.06), (0.52, 0.22, 0.05), (0.66, 0.34, 0.055)):
        painter.drawEllipse(
            int(size * (cx - r)), int(size * (cy - r)), int(size * r * 2), int(size * r * 2)
        )
    painter.end()
    return QIcon(pixmap)


class TrayController(QSystemTrayIcon):
    """托盘控制器：图标 + 菜单 + 通知；动作回调由 room 装配层注入。"""

    def __init__(self, on_open_webui=None, data_root: Path | None = None):
        super().__init__(make_icon())
        self._on_open_webui = on_open_webui or (lambda: None)
        self._data_root = data_root
        self.setToolTip("Fungi")
        menu = QMenu()
        menu.addAction("打开 WebUI", self._on_open_webui)
        menu.addAction("打开数据目录", self._open_data_dir)
        menu.addSeparator()
        menu.addAction("退出", QApplication.quit)
        self.setContextMenu(menu)
        self.activated.connect(self._on_activated)
        # Qt delivers toast clicks via a dedicated signal, not an activation reason
        self.messageClicked.connect(self._on_open_webui)

    def _open_data_dir(self) -> None:
        if self._data_root is not None:
            webbrowser.open(self._data_root.as_uri())

    def _on_activated(self, reason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self._on_open_webui()

    # ── 通知 ──
    def notify(self, title: str, body: str) -> None:
        self.showMessage(title, body, QSystemTrayIcon.MessageIcon.Information, 8000)
