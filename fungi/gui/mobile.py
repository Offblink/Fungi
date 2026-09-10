"""Mobile access page: the LAN URL + QR code for the phone UI."""

import io
import urllib.error
import urllib.parse
import urllib.request

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QPixmap
from PyQt5.QtWidgets import (
    QLabel,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    FluentIcon,
    LineEdit,
    PushButton,
    SubtitleLabel,
)

from .widgets import _copy, _copy_button, _row


class MobilePage(QWidget):
    """手机端：房间启动后生成移动版 WebUI 的局域网二维码，手机扫码即用。"""

    def __init__(self, window):
        super().__init__()
        self.window_ref = window
        self.setObjectName("mobilePage")

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 24, 24, 24)
        root.setSpacing(12)

        title = SubtitleLabel("手机端")
        root.addWidget(title)

        hint = BodyLabel(
            "发起或加入房间后，用手机相机扫码即可在手机上打开移动版 WebUI。\n"
            "手机连不上时检查 Windows 防火墙（公用网络常拦 Python 入站）；换网络后点「刷新二维码」。"
        )
        hint.setWordWrap(True)
        root.addWidget(hint)

        self.qr_label = QLabel()
        self.qr_label.setAlignment(Qt.AlignCenter)
        self.qr_label.setMinimumSize(260, 260)
        root.addWidget(self.qr_label, 1)

        self.url_edit = LineEdit()
        self.url_edit.setReadOnly(True)
        copy_btn = _copy_button()
        copy_btn.clicked.connect(
            lambda: _copy(self.url_edit.text(), self.window_ref, "手机端地址")
        )
        root.addWidget(_row("手机端地址", self.url_edit, copy_btn))

        self.refresh_btn = PushButton(FluentIcon.SYNC, "刷新二维码")
        self.refresh_btn.clicked.connect(self.refresh)
        root.addWidget(self.refresh_btn)

        self.refresh()

    def showEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        super().showEvent(event)
        # The room may have started (or left) since the last visit.
        self.refresh()

    def refresh(self) -> None:
        rooms = self.window_ref.rooms()
        if not rooms:
            self.qr_label.setPixmap(QPixmap())
            self.qr_label.setText(
                "先在「发起房间」或「加入房间」页启动房间，再回到这里生成二维码。"
            )
            self.url_edit.clear()
            return
        try:
            import segno  # noqa: PLC0415 (graceful degrade when not installed)

            from ..server import lan_payload  # noqa: PLC0415 (lazy: heavy module)
        except ImportError as exc:
            self.qr_label.setPixmap(QPixmap())
            self.qr_label.setText(f"生成二维码失败：缺少依赖 {exc.name}（pip install segno）")
            return
        webui_url = rooms[0].open_webui(open_browser=False)  # "http://localhost:PORT"
        port = urllib.parse.urlparse(webui_url).port
        mobile_url = lan_payload(port, loopback=True)["url"]
        self.url_edit.setText(mobile_url)

        buf = io.BytesIO()
        segno.make(mobile_url, error="m").save(
            buf, kind="png", scale=8, border=2, dark="#1f1f1f", light="#ffffff"
        )
        pm = QPixmap()
        pm.loadFromData(buf.getvalue())
        self.qr_label.setText("")
        self.qr_label.setPixmap(
            pm.scaled(
                self.qr_label.width(),
                self.qr_label.height(),
                Qt.KeepAspectRatio,
                Qt.SmoothTransformation,
            )
        )
