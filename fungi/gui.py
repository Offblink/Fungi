"""Fungi GUI launcher: three pages — 发起房间 / 加入房间 / 模型配置.

Entry: ``python start.py`` (or ``python -m fungi --gui``). The GUI hosts the
room IN-PROCESS (hub/clones/poller run on daemon threads; closing the window
stops the room) — no extra console script. Wire identity stays the machine
host name; the nickname is the display layer (--display semantics).

Ports follow the Face convention: the server scans upward from the 8899 anchor
for the first free port; a joiner types only IP + token and the GUI scans the
same range for a hub that accepts the token (wrong-token hubs are skipped).
Set FUNGI_GUI_SCALE to scale the whole UI proportionally (default 1.0).
"""

import os
import re
import secrets
import socket
import sys
import threading
import urllib.error
import urllib.request

from PyQt5.QtCore import QSettings, Qt, pyqtSignal
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
    PushButton,
    SubtitleLabel,
    ToolButton,
)

# The global qfluentwidgets install is the PyQt5 build (PySide6-Fluent-Widgets is
# not installed and its import name would clobber this one), so the GUI rides
# PyQt5; the fluent components are the same library Face uses (same look).
from .config import DEFAULT_ENDPOINT, PROJECT_ROOT, load_config, save_config
from .protocol import valid_host_name

GUI_PORT = 8899  # scan anchor (Face convention); actual port found by scanning up
PORT_SCAN_LIMIT = 32
PROBE_TIMEOUT = 1.5
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


def _wire_candidate(text: str) -> str | None:
    """ASCII-safe wire name derived from arbitrary input, or None.

    The wire name rides envelope addresses, URLs, and file names, so it must
    stay ASCII (an emoji name breaks http.client's ASCII URL selector); the
    nickname carries everything the user actually wants to be called."""
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "-", text).strip("-_")[:32]
    return cleaned if valid_host_name(cleaned) else None


def _resolve_wire_name(host: str, nick: str) -> tuple[str, str]:
    """Self-heal an invalid wire name instead of rejecting it: sanitize what is
    sanitizable, else fall back to the machine name (or "pc"). The original
    input becomes the nickname when that field is empty. Returns (wire, nick)."""
    if valid_host_name(host):
        return host, nick
    wire = _wire_candidate(host) or _wire_candidate(default_host_name()) or "pc"
    return wire, (nick or host)


def _port_bindable(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("0.0.0.0", port))
            return True
        except OSError:
            return False


def find_free_port(start: int = GUI_PORT, limit: int = PORT_SCAN_LIMIT) -> int:
    """Face-style upward scan: first bindable port from `start`, else OSError."""
    for port in range(start, start + limit):
        if _port_bindable(port):
            return port
    raise OSError(f"no free port in [{start}, {start + limit})")


def _port_open(ip: str, port: int, timeout: float = PROBE_TIMEOUT) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((ip, port)) == 0


def probe_room_port(ip: str, token: str, start: int = GUI_PORT, limit: int = PORT_SCAN_LIMIT):
    """Scan upward for a Fungi hub that accepts `token`; returns its port or None.

    Distinguish via /api/peers: 404 = token accepted (our room), 403 = a hub
    with a different token (skip), refused/timeout = nothing there (skip).
    """
    for port in range(start, start + limit):
        if not _port_open(ip, port):
            continue
        url = f"http://{ip}:{port}/api/peers?token={urllib.request.quote(token)}&host=probe"
        try:
            with urllib.request.urlopen(url, timeout=PROBE_TIMEOUT) as resp:
                if resp.status == 200:  # valid token and "probe" listed: our room
                    return port
        except urllib.error.HTTPError as exc:
            if exc.code == 404:  # token OK, host "probe" just unknown: our room
                return port
            continue  # 403: different room / wrong token
        except (urllib.error.URLError, OSError, TimeoutError):
            continue
    return None


def start_server_room(host: str, display: str, token: str, port: int):
    """Host the room inside this GUI process: hub + clones run on threads."""
    from .events import ConsoleSink  # noqa: PLC0415 (Qt-free, cheap)
    from .room import RoomServer  # noqa: PLC0415

    room = RoomServer(
        host,
        load_config(),
        ConsoleSink(),
        token,
        PROJECT_ROOT / "data",
        display=display,
        port=port,
    )
    room.start()
    return room


def start_client_room(host: str, display: str, url: str, token: str):
    """Join a room inside this GUI process (poller + clones on threads)."""
    from .events import ConsoleSink  # noqa: PLC0415
    from .room import RoomClient  # noqa: PLC0415

    room = RoomClient(host, load_config(), ConsoleSink(), url, token, display=display)
    room.start()
    return room


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
    box.addWidget(widget)
    if parent is not None:
        box.addWidget(parent)
    box.addStretch(1)
    holder = QWidget()
    holder.setLayout(box)
    return holder


class HostPage(QWidget):
    """发起房间：GUI 进程内直启 hub，随后显示 IP / Token / WebUI 入口。"""

    def __init__(self, window):
        super().__init__()
        self.window_ref = window
        self.setObjectName("hostPage")
        self.room = None
        self._token = ""

        root = QVBoxLayout(self)
        root.setContentsMargins(48, 32, 48, 32)
        root.setSpacing(14)

        title = SubtitleLabel("发起房间")
        root.addWidget(title)

        self.name_edit = LineEdit()
        self.name_edit.setFixedWidth(360)
        self.name_edit.setText(default_host_name())
        self.name_edit.setPlaceholderText("本机主机名（房间内的 wire 身份）")
        root.addWidget(_row("主机名", self.name_edit))

        self.nick_edit = LineEdit()
        self.nick_edit.setFixedWidth(360)
        self.nick_edit.setPlaceholderText("你的昵称（中文/emoji 均可，留空用主机名）")
        root.addWidget(_row("昵称", self.nick_edit))

        btn_row = QHBoxLayout()
        self.start_btn = PrimaryPushButton(FluentIcon.SHARE, "发起房间")
        self.start_btn.clicked.connect(self._start)
        self.leave_btn = PushButton(FluentIcon.CLOSE, "离开房间")
        self.leave_btn.clicked.connect(self._leave)
        btn_row.addWidget(self.start_btn, 1)
        btn_row.addWidget(self.leave_btn, 1)
        root.addLayout(btn_row)

        # -- status card (populated after launch) --
        self.ip_edit = LineEdit()
        self.ip_edit.setFixedWidth(360)
        self.ip_edit.setReadOnly(True)
        self.ip_btn = _copy_button()
        self.ip_btn.clicked.connect(lambda: _copy(self.ip_edit.text(), window, "房间 IP"))
        self.ip_refresh_btn = ToolButton(FluentIcon.SYNC)
        self.ip_refresh_btn.setToolTip("刷新 IP（网络切换 / DHCP 续租后使用；房间绑定所有网卡，端口不变）")
        self.ip_refresh_btn.clicked.connect(self._refresh_ip)
        self.token_edit = LineEdit()
        self.token_edit.setFixedWidth(360)
        self.token_edit.setReadOnly(True)
        self.token_btn = _copy_button()
        self.token_btn.clicked.connect(lambda: _copy(self.token_edit.text(), window, "房间 Token"))
        self.webui_btn = PushButton(FluentIcon.GLOBE, "打开 WebUI")
        self.webui_btn.clicked.connect(self._open_webui)
        ip_buttons = QWidget()
        ip_box = QHBoxLayout(ip_buttons)
        ip_box.setContentsMargins(0, 0, 0, 0)
        ip_box.setSpacing(4)
        ip_box.addWidget(self.ip_refresh_btn)
        ip_box.addWidget(self.ip_btn)
        self.ip_row = _row("房间 IP", self.ip_edit, ip_buttons)
        self.token_row = _row("Token", self.token_edit, self.token_btn)
        self.webui_row = _row("WebUI", self.webui_btn)
        root.addWidget(self.ip_row)
        root.addWidget(self.token_row)
        root.addWidget(self.webui_row)

        root.addStretch(1)

        self.status = BodyLabel(self._idle_status)
        self.status.setWordWrap(True)
        root.addWidget(self.status)

        # page-scoped copy shortcut: Ctrl+C copies the room IP
        shortcut = QShortcut(QKeySequence("Ctrl+C"), self)
        shortcut.activated.connect(lambda: _copy(self.ip_edit.text(), window, "房间 IP"))

        self._set_started(False)

    def _set_started(self, started: bool) -> None:
        for row in (self.ip_row, self.token_row, self.webui_row):
            row.setVisible(started)
        self.start_btn.setEnabled(not started)
        self.leave_btn.setVisible(started)

    def _refresh_ip(self) -> None:
        """Re-detect the LAN IP: DHCP renewals / Wi-Fi switches change it while
        the room keeps running (the hub binds 0.0.0.0, so only the display ages)."""
        ip = lan_ip()
        changed = ip != self.ip_edit.text()
        self.ip_edit.setText(ip)
        if ip == "127.0.0.1":
            InfoBar.warning(
                "未检测到局域网",
                "当前 IP 显示为 127.0.0.1，请检查网络连接",
                duration=4000,
                parent=self.window_ref,
            )
        elif changed:
            InfoBar.success("IP 已刷新", f"当前房间 IP：{ip}", duration=3000, parent=self.window_ref)
        else:
            InfoBar.info("IP 未变化", ip, duration=2000, parent=self.window_ref)

    _idle_status = (
        "尚未发起。\n"
        "· 端口从 8899 起自动向上寻找，加入方只需填写 IP 和 Token\n"
        "· 请确认加入方与本机在同一局域网（同一路由器）"
    )

    def _leave(self) -> None:
        if self.room is None:
            return
        self.room.stop()
        self.room = None
        self._set_started(False)
        self.ip_edit.clear()
        self.token_edit.clear()
        self.status.setText(self._idle_status)
        InfoBar.info("已离开", "房间已停止", duration=2500, parent=self.window_ref)

    def _open_webui(self) -> None:
        if self.room is not None:
            self.room.open_webui()

    def _start(self) -> None:
        if self.room is not None:
            return
        host = self.name_edit.text().strip()
        display = self.nick_edit.text().strip()
        wire, display = _resolve_wire_name(host, display)
        if wire != host:
            self.name_edit.setText(wire)
            self.nick_edit.setText(display)
            InfoBar.info(
                "已自动调整",
                f"wire 身份用「{wire}」（进地址和文件名，仅限 ASCII）；「{host}」留作昵称展示",
                duration=5000,
                parent=self.window_ref,
            )
            host = wire
        try:
            port = find_free_port()
        except OSError as exc:
            InfoBar.error("无可用端口", str(exc), duration=5000, parent=self.window_ref)
            return
        self._token = secrets.token_urlsafe(12)
        self.room = start_server_room(host, display, self._token, port)
        self.ip_edit.setText(lan_ip())
        self.token_edit.setText(self._token)
        self._set_started(True)
        self.status.setText(
            "房间已在本窗口内运行（关闭窗口即停止房间）。\n"
            f"· 端口 {port}（自动向上寻找）· 把 Token 发给好友即可加入\n"
            "· Ctrl+C 复制房间 IP"
        )
        InfoBar.success(
            "房间已发起", f"{host} · {lan_ip()}:{port}", duration=3000, parent=self.window_ref
        )


class JoinPage(QWidget):
    """加入房间：IP + Token + 昵称（端口从 8899 起自动向上探测）。"""

    join_done = pyqtSignal(object)  # port (int) or None; emitted from scan thread

    def __init__(self, window):
        super().__init__()
        self.window_ref = window
        self.setObjectName("joinPage")
        self.settings = QSettings(SETTINGS_ORG, SETTINGS_APP)
        self.room = None
        self.join_done.connect(self._finish_join)

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
        self.token_edit.setFixedWidth(360)
        self.token_edit.setPlaceholderText("房主发给你的 Token")
        self.token_edit.setFixedWidth(360)
        root.addWidget(_row("Token", self.token_edit))

        self.nick_edit = LineEdit()
        self.nick_edit.setFixedWidth(360)
        self.nick_edit.setPlaceholderText("你的昵称（中文/emoji 均可，仅用于展示）")
        self.nick_edit.setFixedWidth(360)
        root.addWidget(_row("昵称", self.nick_edit))

        self.name_edit = LineEdit()
        self.name_edit.setFixedWidth(360)
        self.name_edit.setText(default_host_name())
        self.name_edit.setPlaceholderText("本机主机名（wire 身份，一般不用改）")
        root.addWidget(_row("主机名", self.name_edit))

        btn_row = QHBoxLayout()
        self.join_btn = PrimaryPushButton(FluentIcon.CONNECT, "加入房间")
        self.join_btn.clicked.connect(self._join)
        self.leave_btn = PushButton(FluentIcon.CLOSE, "离开房间")
        self.leave_btn.clicked.connect(self._leave)
        btn_row.addWidget(self.join_btn, 1)
        btn_row.addWidget(self.leave_btn, 1)
        root.addLayout(btn_row)
        self.leave_btn.setVisible(False)

        root.addStretch(1)

        self.status = BodyLabel("加入后房间在本窗口内运行，关闭窗口即离开。")
        root.addWidget(self.status)

        self._restore()

    def _restore(self) -> None:
        last_ip = self.settings.value("last_ip", "")
        last_token = self.settings.value("last_token", "")
        last_nick = self.settings.value("last_nick", "")
        if last_ip:
            self.ip_combo.setText(str(last_ip))
        if last_token:
            self.token_edit.setText(str(last_token))
        if last_nick:
            self.nick_edit.setText(str(last_nick))

    def _join(self) -> None:
        if self.room is not None:
            return
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
        wire, nick = _resolve_wire_name(host, nick)
        if wire != host:
            self.name_edit.setText(wire)
            self.nick_edit.setText(nick)
            InfoBar.info(
                "已自动调整",
                f"wire 身份用「{wire}」（进地址和文件名，仅限 ASCII）；「{host}」留作昵称展示",
                duration=5000,
                parent=self.window_ref,
            )
            host = wire
        self.join_btn.setEnabled(False)
        self.status.setText(f"正在扫描 {ip} 的房间端口（8899 起）…")

        def scan():
            try:
                port = probe_room_port(ip, token)
            except Exception:  # network hiccup: report as "not found"
                port = None
            self.join_done.emit(port)

        threading.Thread(target=scan, name="port-probe", daemon=True).start()
        self._pending = (ip, token, nick, host)

    def _leave(self) -> None:
        if self.room is None:
            return
        self.room.stop()  # sends leave to the hub
        self.room = None
        self.leave_btn.setVisible(False)
        self.join_btn.setEnabled(True)
        self.status.setText("已离开房间。")
        InfoBar.info("已离开", "已从房间退出", duration=2500, parent=self.window_ref)

    def _finish_join(self, port) -> None:
        self.join_btn.setEnabled(True)
        ip, token, nick, host = self._pending
        if port is None:
            self.status.setText(
                f"在 {ip} 的 8899–{GUI_PORT + PORT_SCAN_LIMIT - 1} 未找到接受该 Token 的房间。\n"
                "请确认房主已发起、Token 正确、且在同一局域网。"
            )
            InfoBar.error(
                "未找到房间", "扫描范围内没有匹配的房间", duration=4000, parent=self.window_ref
            )
            return
        try:
            self.room = start_client_room(host, nick, f"http://{ip}:{port}", token)
        except Exception as exc:  # hub refused the join (left / restarted)
            self.status.setText(f"加入失败：{exc}")
            InfoBar.error("加入失败", str(exc), duration=5000, parent=self.window_ref)
            return
        self.leave_btn.setVisible(True)
        self.settings.setValue("last_ip", ip)
        self.settings.setValue("last_token", token)
        self.settings.setValue("last_nick", nick)
        self.status.setText(
            f"已加入 {ip}:{port}（昵称 {nick or host}）。\n"
            "房间在本窗口内运行，关闭窗口即离开；WebUI 请让房主打开或从托盘查看。"
        )
        InfoBar.success(
            "已加入房间", f"{host} → {ip}:{port}", duration=3000, parent=self.window_ref
        )


class ConfigPage(QWidget):
    """模型配置：迁移自 WebUI 的配置弹窗（api_key / endpoint / model）。"""

    def __init__(self, window):
        super().__init__()
        self.window_ref = window
        self.setObjectName("configPage")

        root = QVBoxLayout(self)
        root.setContentsMargins(48, 32, 48, 32)
        root.setSpacing(14)

        title = SubtitleLabel("模型配置")
        root.addWidget(title)

        self.key_edit = LineEdit()
        self.key_edit.setFixedWidth(360)
        self.key_edit.setPlaceholderText("API Key（留空 = 保持不变）")
        root.addWidget(_row("API Key", self.key_edit))

        self.endpoint_edit = LineEdit()
        self.endpoint_edit.setFixedWidth(360)
        self.endpoint_edit.setPlaceholderText(f"接口地址（默认 {DEFAULT_ENDPOINT}）")
        root.addWidget(_row("接口地址", self.endpoint_edit))

        self.model_edit = LineEdit()
        self.model_edit.setFixedWidth(360)
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
        self.resize(900, 560)  # sidebar layout needs a little width for the nav

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt override)
        for page in (self.host_page, self.join_page):
            room = getattr(page, "room", None)
            if room is not None:
                room.stop()
                page.room = None
        super().closeEvent(event)


def run_gui() -> int:
    # QT_SCALE_FACTOR grows fonts, widgets and the window together (must be set
    # before QApplication exists); AA_EnableHighDpiScaling lets Qt5 honor it.
    os.environ.setdefault("QT_SCALE_FACTOR", str(GUI_SCALE))
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    app = QApplication(sys.argv)
    win = FungiGui()
    win.show()
    return app.exec_()
