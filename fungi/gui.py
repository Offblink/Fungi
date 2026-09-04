"""Fungi GUI launcher: three pages — 发起房间 / 加入房间 / 模型配置.

Entry: ``python start.py`` (or ``python -m fungi --gui``). Wire identity stays
the machine host name; the nickname is the display layer (--display). The hub
binds the fixed anchor port 8899 (Face convention) so a joiner only types the
host's IP + token. Each launch spawns the room process (tray + WebUI) in its
own console window.

The whole UI scales proportionally (fonts and window together) via
QT_SCALE_FACTOR — set FUNGI_GUI_SCALE to override (default 1.0; bump it up if the UI feels small).
"""

import os
import secrets
import socket
import subprocess
import sys

# The global qfluentwidgets install is the PyQt5 build (PySide6-Fluent-Widgets is
# not installed and its import name would clobber this one), so the GUI rides
# PyQt5; the fluent components are the same library Face uses (same look).
from PyQt5.QtCore import QSettings, Qt
from PyQt5.QtGui import QGuiApplication, QKeySequence
from PyQt5.QtWidgets import QApplication, QHBoxLayout, QShortcut, QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel,
    EditableComboBox,
    FluentIcon,
    FluentWindow,
    InfoBar,
    LineEdit,
    PrimaryPushButton,
    SubtitleLabel,
    ToolButton,
)

from .config import DEFAULT_ENDPOINT, load_config, save_config
from .protocol import BAD_NAME_MSG, valid_host_name

GUI_PORT = 8899  # fixed anchor port: join pages only ever ask for an IP
SETTINGS_ORG = "Offblink"
SETTINGS_APP = "FungiGUI"
GUI_SCALE = float(os.environ.get("FUNGI_GUI_SCALE", "1.0"))  # fonts + window, uniform


def lan_ip() -> str:
    """Best-effort LAN address (UDP connect trick: no packet is sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def default_host_name() -> str:
    return socket.gethostname().split(".")[0]


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        return sock.connect_ex(("127.0.0.1", port)) == 0


def spawn_room(args: list[str]) -> None:
    """Run the room process in its own console window (independent of the GUI)."""
    flags = subprocess.CREATE_NEW_CONSOLE if sys.platform == "win32" else 0
    subprocess.Popen([sys.executable, "-m", "fungi", *args], creationflags=flags)


def _copy(text: str, parent, what: str) -> None:
    QGuiApplication.clipboard().setText(text)
    InfoBar.success("已复制", what, duration=1500, parent=parent)


def _copy_button() -> ToolButton:
    return ToolButton(FluentIcon.COPY)


def _row(label: str, widget: QWidget, parent=None) -> QWidget:
    box = QHBoxLayout()
    labelw = BodyLabel(label)
    labelw.setFixedWidth(90)
    box.addWidget(labelw)
    box.addWidget(widget, 1)
    if parent is not None:
        box.addWidget(parent)
    holder = QWidget()
    holder.setLayout(box)
    return holder


class HostPage(QWidget):
    """发起房间：launch the hub, then show IP / token / join command with copies."""

    def __init__(self, window: FluentWindow):
        super().__init__()
        self.window_ref = window
        self.setObjectName("hostPage")
        self._token = ""

        root = QVBoxLayout(self)
        root.setContentsMargins(48, 32, 48, 32)
        root.setSpacing(14)

        title = SubtitleLabel("发起房间")
        root.addWidget(title)

        self.name_edit = LineEdit()
        self.name_edit.setText(default_host_name())
        self.name_edit.setPlaceholderText("本机主机名（房间内的 wire 身份）")
        root.addWidget(_row("主机名", self.name_edit))

        self.start_btn = PrimaryPushButton(FluentIcon.SHARE, "发起房间")
        self.start_btn.clicked.connect(self._start)
        root.addWidget(self.start_btn)

        # -- status card (populated after launch) --
        self.ip_edit = LineEdit()
        self.ip_edit.setReadOnly(True)
        self.ip_btn = _copy_button()
        self.ip_btn.clicked.connect(lambda: _copy(self.ip_edit.text(), window, "房间 IP"))
        self.token_edit = LineEdit()
        self.token_edit.setReadOnly(True)
        self.token_btn = _copy_button()
        self.token_btn.clicked.connect(lambda: _copy(self.token_edit.text(), window, "房间 Token"))
        self.cmd_edit = LineEdit()
        self.cmd_edit.setReadOnly(True)
        self.cmd_btn = _copy_button()
        self.cmd_btn.clicked.connect(lambda: _copy(self.cmd_edit.text(), window, "CLI 加入命令"))
        self.ip_row = _row("房间 IP", self.ip_edit, self.ip_btn)
        self.token_row = _row("Token", self.token_edit, self.token_btn)
        self.cmd_row = _row("CLI 命令", self.cmd_edit, self.cmd_btn)
        root.addWidget(self.ip_row)
        root.addWidget(self.token_row)
        root.addWidget(self.cmd_row)

        root.addStretch(1)

        self.status = BodyLabel(
            "尚未发起。\n"
            "· 房间绑定固定端口 8899，加入方只需填写 IP 和 Token\n"
            "· 请确认加入方与本机在同一局域网（同一路由器）"
        )
        self.status.setWordWrap(True)
        root.addWidget(self.status)

        # page-scoped copy shortcut: Ctrl+C copies the room IP
        shortcut = QShortcut(QKeySequence("Ctrl+C"), self)
        shortcut.activated.connect(lambda: _copy(self.ip_edit.text(), window, "房间 IP"))

        self._set_started(False)

    def _set_started(self, started: bool) -> None:
        for row in (self.ip_row, self.token_row, self.cmd_row):
            row.setVisible(started)

    def _start(self) -> None:
        host = self.name_edit.text().strip()
        if not valid_host_name(host):
            InfoBar.error("主机名不合法", BAD_NAME_MSG, duration=4000, parent=self.window_ref)
            return
        if port_in_use(GUI_PORT):
            InfoBar.error(
                "端口被占用",
                f"端口 {GUI_PORT} 已有服务（可能是另一个 Fungi 房间），请先释放它。",
                duration=5000,
                parent=self.window_ref,
            )
            return
        self._token = secrets.token_urlsafe(12)
        spawn_room(["--server", "--name", host, "--token", self._token, "--port", str(GUI_PORT)])
        ip = lan_ip()
        join_cmd = (
            f"python -m fungi --join http://{ip}:{GUI_PORT} --token {self._token} --name CLIENT"
        )
        self.ip_edit.setText(ip)
        self.token_edit.setText(self._token)
        self.cmd_edit.setText(join_cmd)
        self._set_started(True)
        self.status.setText(
            "房间已发起（独立控制台窗口运行，托盘常驻）。\n"
            "· 把上方 Token 发给好友，好友用「加入房间」页填 IP + Token + 昵称\n"
            "· Ctrl+C 复制房间 IP"
        )
        InfoBar.success(
            "房间已发起", f"{host} · {ip}:{GUI_PORT}", duration=3000, parent=self.window_ref
        )


class JoinPage(QWidget):
    """加入房间：IP + Token + 昵称（无需端口，固定 8899）。"""

    def __init__(self, window: FluentWindow):
        super().__init__()
        self.window_ref = window
        self.setObjectName("joinPage")
        self.settings = QSettings(SETTINGS_ORG, SETTINGS_APP)

        root = QVBoxLayout(self)
        root.setContentsMargins(48, 32, 48, 32)
        root.setSpacing(14)

        title = SubtitleLabel("加入房间")
        root.addWidget(title)

        self.ip_combo = EditableComboBox()
        self.ip_combo.setPlaceholderText("房主 IP，如 192.168.1.20（无需端口）")
        self.ip_combo.setFixedWidth(360)
        root.addWidget(_row("房主 IP", self.ip_combo))

        self.token_edit = LineEdit()
        self.token_edit.setPlaceholderText("房主发给你的 Token")
        self.token_edit.setFixedWidth(360)
        root.addWidget(_row("Token", self.token_edit))

        self.nick_edit = LineEdit()
        self.nick_edit.setPlaceholderText("你的昵称（中文/emoji 均可，仅用于展示）")
        self.nick_edit.setFixedWidth(360)
        root.addWidget(_row("昵称", self.nick_edit))

        self.name_edit = LineEdit()
        self.name_edit.setText(default_host_name())
        self.name_edit.setPlaceholderText("本机主机名（wire 身份，一般不用改）")
        root.addWidget(_row("主机名", self.name_edit))

        self.join_btn = PrimaryPushButton(FluentIcon.CONNECT, "加入房间")
        self.join_btn.clicked.connect(self._join)
        root.addWidget(self.join_btn)

        root.addStretch(1)

        self.status = BodyLabel("加入后本机进入托盘常驻模式，WebUI 从托盘打开。")
        root.addWidget(self.status)

        self._restore()

    def _restore(self) -> None:
        last_ip = self.settings.value("last_ip", "")
        last_token = self.settings.value("last_token", "")
        last_nick = self.settings.value("last_nick", "")
        if last_ip:
            self.ip_combo.setCurrentText(str(last_ip))
        if last_token:
            self.token_edit.setText(str(last_token))
        if last_nick:
            self.nick_edit.setText(str(last_nick))

    def _join(self) -> None:
        ip = self.ip_combo.currentText().strip()
        token = self.token_edit.text().strip()
        nick = self.nick_edit.text().strip()
        host = self.name_edit.text().strip()
        if not ip:
            InfoBar.warning(
                "缺少 IP", "请填写房主的局域网 IP", duration=3000, parent=self.window_ref
            )
            return
        if not token:
            InfoBar.warning(
                "缺少 Token", "请向房主索要房间 Token", duration=3000, parent=self.window_ref
            )
            return
        if not valid_host_name(host):
            InfoBar.error("主机名不合法", BAD_NAME_MSG, duration=4000, parent=self.window_ref)
            return
        args = ["--join", f"http://{ip}:{GUI_PORT}", "--token", token, "--name", host]
        if nick:
            args += ["--display", nick]
        spawn_room(args)
        self.settings.setValue("last_ip", ip)
        self.settings.setValue("last_token", token)
        self.settings.setValue("last_nick", nick)
        self.status.setText(
            f"正在加入 {ip}:{GUI_PORT}（昵称 {nick or host}）。详见新开的控制台窗口。"
        )
        InfoBar.success(
            "已发起加入", f"{host} → {ip}:{GUI_PORT}", duration=3000, parent=self.window_ref
        )


class ConfigPage(QWidget):
    """模型配置：迁移自 WebUI 的配置弹窗（api_key / endpoint / model）。"""

    def __init__(self, window: FluentWindow):
        super().__init__()
        self.window_ref = window
        self.setObjectName("configPage")

        root = QVBoxLayout(self)
        root.setContentsMargins(48, 32, 48, 32)
        root.setSpacing(14)

        title = SubtitleLabel("模型配置")
        root.addWidget(title)

        self.key_edit = LineEdit()
        self.key_edit.setPlaceholderText("API Key（留空 = 保持不变）")
        root.addWidget(_row("API Key", self.key_edit))

        self.endpoint_edit = LineEdit()
        self.endpoint_edit.setPlaceholderText(f"接口地址（默认 {DEFAULT_ENDPOINT}）")
        root.addWidget(_row("接口地址", self.endpoint_edit))

        self.model_edit = LineEdit()
        self.model_edit.setPlaceholderText("模型名（留空 = 保持不变）")
        root.addWidget(_row("模型", self.model_edit))

        self.save_btn = PrimaryPushButton(FluentIcon.SAVE, "保存配置")
        self.save_btn.clicked.connect(self._save)
        root.addWidget(self.save_btn)

        root.addStretch(1)
        self.status = BodyLabel()
        self.status.setWordWrap(True)
        root.addWidget(self.status)
        self._refresh_status()

    def _refresh_status(self) -> None:
        cfg = load_config()
        state = "已配置" if cfg.configured else "未配置（使用占位 key，无法对话）"
        self.status.setText(f"当前状态：{state} · 模型 {cfg.model} · 接口 {cfg.endpoint}")

    def _save(self) -> None:
        cfg = load_config()
        if self.key_edit.text().strip():
            cfg.api_key = self.key_edit.text().strip()
        if self.endpoint_edit.text().strip():
            cfg.endpoint = self.endpoint_edit.text().strip()
        if self.model_edit.text().strip():
            cfg.model = self.model_edit.text().strip()
        save_config(cfg)
        self.key_edit.clear()
        self.endpoint_edit.clear()
        self.model_edit.clear()
        self._refresh_status()
        InfoBar.success(
            "已保存", "模型配置已写入 config.json", duration=2500, parent=self.window_ref
        )


class FungiGui(FluentWindow):
    def __init__(self):
        super().__init__()
        self.host_page = HostPage(self)
        self.join_page = JoinPage(self)
        self.cfg_page = ConfigPage(self)
        self.addSubInterface(self.host_page, FluentIcon.HOME, "发起房间")
        self.addSubInterface(self.join_page, FluentIcon.PEOPLE, "加入房间")
        self.addSubInterface(self.cfg_page, FluentIcon.SETTING, "模型配置")
        self.setWindowTitle("Fungi")
        self.resize(860, 500)  # logical; physical = x GUI_SCALE (~1032x600)


def run_gui() -> int:
    # QT_SCALE_FACTOR grows fonts, widgets and the window together (must be set
    # before QApplication exists); AA_EnableHighDpiScaling lets Qt5 honor it.
    os.environ.setdefault("QT_SCALE_FACTOR", str(GUI_SCALE))
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    app = QApplication(sys.argv)
    win = FungiGui()
    win.show()
    return app.exec_()
