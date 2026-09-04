"""Fungi GUI launcher: four pages — 发起房间 / 加入房间 / 模型配置 / 帮助.

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
from concurrent.futures import ThreadPoolExecutor

from PyQt5.QtCore import QSettings, QSharedMemory, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QCursor, QGuiApplication, QKeySequence
from PyQt5.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QMessageBox,
    QScrollArea,
    QShortcut,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    Action,
    BodyLabel,
    FluentIcon,
    FluentWindow,
    InfoBar,
    LineEdit,
    PrimaryPushButton,
    PushButton,
    SubtitleLabel,
    SystemTrayMenu,
    TitleLabel,
    ToolButton,
)

# The global qfluentwidgets install is the PyQt5 build (PySide6-Fluent-Widgets is
# not installed and its import name would clobber this one), so the GUI rides
# PyQt5; the fluent components are the same library Face uses (same look).
from .config import DEFAULT_ENDPOINT, PROJECT_ROOT, load_config, save_config
from .protocol import valid_host_name
from .tray import make_icon

GUI_PORT = 8899  # scan anchor (Face convention); actual port found by scanning up
PORT_SCAN_LIMIT = 32
SWEEP_TIMEOUT = 0.2  # TCP connect sweep: LAN answers are ms-fast, dead IPs wait it out
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


def _port_open(ip: str, port: int, timeout: float = SWEEP_TIMEOUT) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((ip, port)) == 0


def local_subnet_hosts() -> list[str]:
    """All /24 neighbors of the current LAN IP (own IP included: same-machine
    rooms are reachable through the LAN address too)."""
    prefix = lan_ip().rsplit(".", 1)[0]
    return [f"{prefix}.{i}" for i in range(1, 255)]


def _room_accepts(ip: str, port: int, token: str) -> bool:
    url = f"http://{ip}:{port}/api/peers?token={urllib.request.quote(token)}&host=probe"
    try:
        with urllib.request.urlopen(url, timeout=PROBE_TIMEOUT) as resp:
            return resp.status == 200  # valid token and "probe" listed: our room
    except urllib.error.HTTPError as exc:
        return exc.code == 404  # token OK, host unknown: our room
    except (urllib.error.URLError, OSError, TimeoutError):
        return False


def discover_room(token: str) -> tuple[str, int] | None:
    """Find the LAN room that accepts `token` — no IP input needed.

    Same subnet is a LAN given, so sweep the /24 one port at a time across all
    hosts (parallel): the common case (room at the 8899 anchor) lands in the
    first round instead of walking a 32-port window per host."""
    hosts = local_subnet_hosts()
    with ThreadPoolExecutor(max_workers=128) as pool:
        for offset in range(PORT_SCAN_LIMIT):
            port = GUI_PORT + offset
            for ip, ok in zip(
                hosts, pool.map(lambda h, p=port: _port_open(h, p), hosts), strict=True
            ):
                if ok and _room_accepts(ip, port, token):
                    return ip, port
    return None


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


_ACCENT = "#e07a5f"


HELP_SECTIONS = [
    ("Fungi 是什么",
     "一款局域网多主机 Orchestrator：一台机器「发起房间」，同一局域网的同伴"
     "「加入房间」，各自通过 WebUI 与本机 clone 对话，让多个 AI 跨主机协作。"
     "无需公网，所有流量不出局域网。"),
    ("发起房间",
     "一台机器点「发起房间」：得到房间 IP 和 Token，发给要加入的同伴。"
     "房主点「离开房间」即解散房间。"),
    ("加入房间",
     "同伴点「加入房间」：填 IP + Token（同一局域网可留空 IP，自动全网段发现）。"
     "Token 即身份；Token 对不上的房间会被自动跳过。"),
    ("WebUI 与 clone 能力",
     "双方点「打开 WebUI」进入各自的聊天界面，和本机 clone 对话让它干活。"
     "clone 能力：跨主机 delegate 任务、send_peer 传话、send_file 传文件"
     "（对方 WebUI 会弹确认卡片）、读写 public/ 共享目录和 homes/<主机>/ 私人目录。"),
    ("托盘与后台",
     "关闭窗口不停房间：转入托盘后台；托盘菜单可回主界面、开 WebUI、退出。"
     "只有托盘「退出」或页面「离开房间」才真正停房。"),
    ("模型配置与文件",
     "模型配置页填 api_key / endpoint / model；收到的文件在仓库根 inbox/<来源主机>/。"),
    ("更多文档",
     "细节见仓库 README 与 docs/spec.md。"),
]


class HelpPage(QScrollArea):
    """帮助页：Face 式分节说明（侧栏常驻入口，替代旧的帮助按钮弹窗）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("helpPage")
        self.setWidgetResizable(True)
        self.setFrameShape(QScrollArea.NoFrame)

        body = QWidget()
        lay = QVBoxLayout(body)
        lay.setContentsMargins(48, 32, 48, 32)
        lay.setSpacing(10)

        lay.addWidget(TitleLabel("帮助"))
        lay.addSpacing(6)
        for heading, text in HELP_SECTIONS:
            lay.addWidget(SubtitleLabel(heading))
            label = BodyLabel(text)
            label.setWordWrap(True)
            label.setAlignment(Qt.AlignLeft | Qt.AlignTop)
            lay.addWidget(label)
            lay.addSpacing(8)
        lay.addStretch(1)

        self.setWidget(body)


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


_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")  # token rides URLs / join commands


def _valid_token(token: str) -> bool:
    return _TOKEN_RE.fullmatch(token) is not None


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

        # token is an input, not a status readout: customize it before
        # launching, or edit it live while the room runs (hot-swap)
        self.token_edit = LineEdit()
        self.token_edit.setFixedWidth(360)
        self.token_edit.setPlaceholderText("留空则发起房间时自动生成")
        self.token_edit.setToolTip(
            "可自定义（字母/数字/-/_，1-64 位）。\n"
            "发起前修改：开房即用该 Token；运行中修改：即时热更新，"
            "已加入的好友需用新 Token 重新加入"
        )
        self.token_edit.editingFinished.connect(self._apply_token)
        self.token_edit.setText(secrets.token_urlsafe(12))
        self.token_btn = _copy_button()
        self.token_btn.clicked.connect(lambda: _copy(self.token_edit.text(), window, "房间 Token"))
        self.token_row = _row("Token", self.token_edit, self.token_btn)
        root.addWidget(self.token_row)

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
        self.ip_refresh_btn.setToolTip(
            "刷新 IP（网络切换 / DHCP 续租后使用；房间绑定所有网卡，端口不变）"
        )
        self.ip_refresh_btn.clicked.connect(self._refresh_ip)
        self.webui_btn = PushButton(FluentIcon.GLOBE, "打开 WebUI")
        self.webui_btn.clicked.connect(self._open_webui)
        ip_buttons = QWidget()
        ip_box = QHBoxLayout(ip_buttons)
        ip_box.setContentsMargins(0, 0, 0, 0)
        ip_box.setSpacing(4)
        ip_box.addWidget(self.ip_refresh_btn)
        ip_box.addWidget(self.ip_btn)
        self.ip_row = _row("房间 IP", self.ip_edit, ip_buttons)
        self.webui_row = _row("WebUI", self.webui_btn)
        root.addWidget(self.ip_row)
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
        for row in (self.ip_row, self.webui_row):
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
            InfoBar.success(
                "IP 已刷新", f"当前房间 IP：{ip}", duration=3000, parent=self.window_ref
            )
        else:
            InfoBar.info("IP 未变化", ip, duration=2000, parent=self.window_ref)

    def _apply_token(self) -> None:
        """Commit a token edit. Before launch: no-op (validated at _start).
        While the room runs: hot-swap hub.token — every request re-reads it,
        so the change is live; joined peers must re-join with the new token."""
        if self.room is None:
            return
        token = self.token_edit.text().strip()
        if token == self._token:
            return
        if not _valid_token(token):
            self.token_edit.setText(self._token)
            InfoBar.warning(
                "Token 未更改",
                "仅限字母、数字、- 和 _（Token 进 URL），1-64 位",
                duration=5000,
                parent=self.window_ref,
            )
            return
        self.room.hub.token = token
        self._token = token
        InfoBar.success(
            "Token 已更新",
            "新 Token 即时生效；已加入的好友需用新 Token 重新加入",
            duration=5000,
            parent=self.window_ref,
        )

    _idle_status = (
        "尚未发起。\n"
        "· 端口从 8899 起自动向上寻找，加入方无需填 IP（同网段自动发现），只需 Token\n"
        "· 请确认加入方与本机在同一局域网（同一路由器）"
    )

    def _leave(self) -> None:
        if self.room is None:
            return
        self.room.stop()
        self.room = None
        self._set_started(False)
        self.window_ref.update_tray()
        self.ip_edit.clear()
        self.token_edit.setText(secrets.token_urlsafe(12))
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
        token = self.token_edit.text().strip() or secrets.token_urlsafe(12)
        if not _valid_token(token):
            InfoBar.error(
                "Token 不合法",
                "仅限字母、数字、- 和 _（Token 进 URL），1-64 位",
                duration=5000,
                parent=self.window_ref,
            )
            return
        self._token = token
        self.room = start_server_room(host, display, self._token, port)
        self.ip_edit.setText(lan_ip())
        self.token_edit.setText(self._token)
        self._set_started(True)
        self.window_ref.update_tray()
        self.status.setText(
            "房间运行中：关闭窗口会转入托盘后台，房间不会停。\n"
            f"· 端口 {port}（自动向上寻找）· 把 Token 发给好友即可加入（同网段自动发现）\n"
            "· Ctrl+C 复制房间 IP · 退出房间请点「离开房间」"
        )
        InfoBar.success(
            "房间已发起", f"{host} · {lan_ip()}:{port}", duration=3000, parent=self.window_ref
        )


class JoinPage(QWidget):
    """加入房间：房间 IP 自动发现（全网段扫描），无需手填；Token 即身份。"""

    join_done = pyqtSignal(object)  # (ip, port) tuple or None; emitted from scan thread
    discover_done = pyqtSignal(object)  # (ip, port) tuple or None; refresh/autofill path

    def __init__(self, window):
        super().__init__()
        self.window_ref = window
        self.setObjectName("joinPage")
        self.settings = QSettings(SETTINGS_ORG, SETTINGS_APP)
        self.room = None
        self.join_done.connect(self._finish_join)
        self.discover_done.connect(self._fill_ip)

        root = QVBoxLayout(self)
        root.setContentsMargins(48, 32, 48, 32)
        root.setSpacing(14)

        title = SubtitleLabel("加入房间")
        root.addWidget(title)

        self.ip_edit = LineEdit()
        self.ip_edit.setPlaceholderText("留空 = 自动扫描局域网（无需手填）")
        self.ip_edit.setFixedWidth(360)
        self.ip_refresh_btn = ToolButton(FluentIcon.SYNC)
        self.ip_refresh_btn.setToolTip("重新扫描局域网，自动填入房间 IP（网络变化后使用）")
        self.ip_refresh_btn.clicked.connect(self._refresh_ip)
        ip_buttons = QWidget()
        ip_box = QHBoxLayout(ip_buttons)
        ip_box.setContentsMargins(0, 0, 0, 0)
        ip_box.setSpacing(4)
        ip_box.addWidget(self.ip_refresh_btn)
        self.ip_row = _row("房主 IP", self.ip_edit, ip_buttons)
        root.addWidget(self.ip_row)

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

        self.webui_btn = PushButton(FluentIcon.GLOBE, "打开 WebUI")
        self.webui_btn.clicked.connect(self._open_webui)
        self.webui_row = _row("WebUI", self.webui_btn)
        root.addWidget(self.webui_row)
        self.webui_row.setVisible(False)

        root.addStretch(1)

        self.status = BodyLabel(
            "加入成功后可点「打开 WebUI」聊天；关闭窗口会转入托盘后台，房间不停。"
        )
        root.addWidget(self.status)

        self._restore()

    def _restore(self) -> None:
        last_ip = self.settings.value("last_ip", "")
        last_token = self.settings.value("last_token", "")
        last_nick = self.settings.value("last_nick", "")
        if last_ip:
            self.ip_edit.setText(str(last_ip))
        if last_token:
            self.token_edit.setText(str(last_token))
        if last_nick:
            self.nick_edit.setText(str(last_nick))

    def _join(self) -> None:
        if self.room is not None:
            return
        ip = self.ip_edit.text().strip()
        token = self.token_edit.text().strip()
        nick = self.nick_edit.text().strip()
        host = self.name_edit.text().strip()
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
        self.status.setText(
            f"正在扫描局域网（{lan_ip().rsplit('.', 1)[0]}.*，房间端口 8899 起）…"
            if not ip
            else f"正在扫描 {ip} 的房间端口（8899 起）…"
        )

        def scan():
            try:
                if not ip:
                    found = discover_room(token)
                    if found is None:
                        self.join_done.emit(None)
                        return
                    auto_ip, port = found
                    self.discover_done.emit((auto_ip, port))  # visible autofill
                else:
                    port = probe_room_port(ip, token)
            except Exception:  # network hiccup: report as "not found"
                self.join_done.emit(None)
                return
            self.join_done.emit((ip or auto_ip, port))

        threading.Thread(target=scan, name="room-discovery", daemon=True).start()
        self._pending = (token, nick, host)

    def _refresh_ip(self) -> None:
        """Re-run subnet discovery and auto-fill the room IP (networks change)."""
        token = self.token_edit.text().strip()
        if not token:
            InfoBar.warning(
                "缺少 Token", "自动发现需要房间 Token", duration=3000, parent=self.window_ref
            )
            return
        self.ip_refresh_btn.setEnabled(False)
        self.status.setText(f"正在扫描局域网（{lan_ip().rsplit('.', 1)[0]}.*，房间端口 8899 起）…")

        def scan():
            try:
                found = discover_room(token)
            except Exception:  # network hiccup: report as "not found"
                found = None
            self.discover_done.emit(found)

        threading.Thread(target=scan, name="ip-discovery", daemon=True).start()

    def _fill_ip(self, found) -> None:
        self.ip_refresh_btn.setEnabled(True)
        if found is None:
            self.status.setText(
                "局域网内没有找到接受该 Token 的房间（端口 8899 起）。\n"
                "请确认房主已发起、Token 正确、且在同一局域网。"
            )
            InfoBar.error(
                "未发现房间", "扫描范围内没有匹配的房间", duration=4000, parent=self.window_ref
            )
            return
        ip, _port = found
        self.ip_edit.setText(ip)
        self.status.setText("已自动填入房间 IP，点「加入房间」即可。")
        InfoBar.success("已自动发现房间", f"房间 IP：{ip}", duration=3000, parent=self.window_ref)

    def _open_webui(self) -> None:
        if self.room is not None:
            self.room.open_webui()

    def _leave(self) -> None:
        if self.room is None:
            return
        self.room.stop()  # sends leave to the hub
        self.room = None
        self.leave_btn.setVisible(False)
        self.webui_row.setVisible(False)
        self.join_btn.setEnabled(True)
        self.window_ref.update_tray()
        self.status.setText("已离开房间。")
        InfoBar.info("已离开", "已从房间退出", duration=2500, parent=self.window_ref)

    def _finish_join(self, found) -> None:
        token, nick, host = self._pending
        if found is None:
            self.join_btn.setEnabled(True)  # scan failed: allow retrying
            self.status.setText(
                "局域网内没有找到接受该 Token 的房间（端口 8899 起）。\n"
                "请确认房主已发起、Token 正确、且在同一局域网。"
            )
            InfoBar.error(
                "未找到房间", "扫描范围内没有匹配的房间", duration=4000, parent=self.window_ref
            )
            return
        ip, port = found
        try:
            self.room = start_client_room(host, nick, f"http://{ip}:{port}", token)
        except Exception as exc:  # hub refused the join (left / restarted)
            self.join_btn.setEnabled(True)  # join refused: allow retrying
            self.status.setText(f"加入失败：{exc}")
            InfoBar.error("加入失败", str(exc), duration=5000, parent=self.window_ref)
            return
        self.leave_btn.setVisible(True)
        self.webui_row.setVisible(True)
        self.window_ref.update_tray()
        self.settings.setValue("last_ip", ip)
        self.settings.setValue("last_token", token)
        self.settings.setValue("last_nick", nick)
        self.status.setText(
            f"已加入 {ip}:{port}（昵称 {nick or host}）。\n"
            "关闭窗口会转入托盘后台，房间不会停；「打开 WebUI」进你自己的聊天界面，退出房间点「离开房间」。"
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
        self.help_page = HelpPage()
        self.addSubInterface(self.host_page, FluentIcon.HOME, "发起房间")
        self.addSubInterface(self.join_page, FluentIcon.PEOPLE, "加入房间")
        self.addSubInterface(self.cfg_page, FluentIcon.SETTING, "模型配置")
        self.addSubInterface(self.help_page, FluentIcon.INFO, "帮助")
        self.setWindowTitle("Fungi")
        self.resize(900, 560)  # sidebar layout needs a little width for the nav
        self._tray: _Tray | None = None

    # ── tray / background lifecycle ──

    def rooms(self) -> list:
        """Live rooms across pages (a page holds at most one)."""
        return [
            page.room
            for page in (self.host_page, self.join_page)
            if getattr(page, "room", None) is not None
        ]

    def update_tray(self) -> None:
        """The tray icon lives exactly while a room runs (it IS the backend)."""
        if self.rooms():
            if self._tray is None:
                self._tray = _Tray(self)
            self._tray.show()
        elif self._tray is not None:
            self._tray.hide()

    def show_and_raise(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def open_webui_from_tray(self) -> None:
        rooms = self.rooms()
        if rooms:
            rooms[0].open_webui()

    def quit_from_tray(self) -> None:
        """Real exit: stop rooms (proper leave envelopes), then quit."""
        for page in (self.host_page, self.join_page):
            room = getattr(page, "room", None)
            if room is not None:
                room.stop()
                page.room = None
        if self._tray is not None:
            self._tray.hide()
        # Deferred: calling quit() inside the current dispatch races the
        # callback teardown (flaky silent exit / hard crash on Windows).
        QTimer.singleShot(0, QApplication.quit)

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt override)
        if self.rooms():
            # Closing the window parks the room in the tray; only the tray
            # menu's 退出 (or a page's 离开房间 button) actually stops it.
            event.ignore()
            self.hide()
            if self._tray is None:
                self.update_tray()
            self._tray.notify(
                "Fungi 已最小化到托盘",
                "房间仍在后台运行；双击托盘回到主界面，菜单可打开 WebUI 或退出。",
            )
            return
        if self._tray is not None:
            self._tray.hide()
        super().closeEvent(event)


def _singleton_taken(key: str = "FungiGuiSingleton") -> bool:
    """True when another process already holds the GUI singleton slot."""

    shared = QSharedMemory(key)
    return shared.attach() or not shared.create(1)


def run_gui() -> int:
    # QT_SCALE_FACTOR grows fonts, widgets and the window together (must be set
    # before QApplication exists); AA_EnableHighDpiScaling lets Qt5 honor it.
    os.environ.setdefault("QT_SCALE_FACTOR", str(GUI_SCALE))
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    app = QApplication(sys.argv)
    # Single instance: a second launcher tells the user and exits. Without
    # this, two GUI windows (each able to host a room) could coexist
    # (2026-09-04 real-machine finding; tray-room mode had the same guard).
    if _singleton_taken():
        QMessageBox.warning(None, "Fungi", "Fungi GUI 已在运行。")
        return 0
    win = FungiGui()
    win.show()
    return app.exec_()
