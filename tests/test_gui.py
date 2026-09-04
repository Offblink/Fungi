"""GUI launcher smoke: three pages construct offscreen; validation logic holds."""

import os

import pytest

pytest.importorskip("qfluentwidgets", reason="PyQt6-Fluent-Widgets (qfluentwidgets) not installed")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import QSharedMemory
from PyQt5.QtWidgets import QApplication

from fungi import gui
from fungi.gui import FungiGui, valid_host_name


@pytest.fixture(scope="module")
def window(qapp):  # noqa: ARG001 (Qt app fixture)
    win = FungiGui()
    yield win
    win.close()


class FakeRoom:
    def __init__(self):
        self.stopped = False

    def stop(self):
        self.stopped = True


@pytest.fixture(autouse=True)
def _fresh_pages(window):
    """Module-scoped window: reset mutable page state between tests."""
    yield
    window.host_page.room = None
    window.host_page.ip_edit.clear()
    window.host_page.token_edit.clear()
    window.host_page._set_started(False)
    window.join_page.room = None
    window.join_page.join_btn.setEnabled(True)
    window.join_page.leave_btn.setVisible(False)


def test_three_pages_present(window):
    assert window.host_page.objectName() == "hostPage"
    assert window.join_page.objectName() == "joinPage"
    assert window.cfg_page.objectName() == "configPage"
    # status card hidden until the room is launched
    assert not window.host_page.ip_row.isVisibleTo(window.host_page)
    window.host_page._set_started(True)
    assert window.host_page.ip_row.isVisibleTo(window.host_page)
    assert not window.host_page.start_btn.isEnabled()
    window.host_page._set_started(False)


def test_host_page_starts_server_in_process(window, monkeypatch):
    page = window.host_page
    started = []

    def fake_start(host, display, token, port):
        started.append((host, display, token, port))
        return object()

    monkeypatch.setattr(gui, "start_server_room", fake_start)
    page.name_edit.setText("pc-alpha")
    page.nick_edit.setText("🌸花酱")
    page._start()
    host, display, token, port = started[0]
    assert host == "pc-alpha" and display == "🌸花酱"
    assert token and len(token) >= 16
    assert gui.GUI_PORT <= port < gui.GUI_PORT + gui.PORT_SCAN_LIMIT  # upward scan
    assert page.room is not None and not page.start_btn.isEnabled()
    page.room = None  # FungiGui.closeEvent must not stop a fake


def test_host_page_self_heals_pure_cjk_name(window, monkeypatch):
    page = window.host_page
    started = []

    def fake_start(host, display, token, port):  # noqa: ARG001 (fakes ignore token/port)
        started.append((host, display))
        return FakeRoom()

    monkeypatch.setattr(gui, "start_server_room", fake_start)
    page.name_edit.setText("小新")  # pure CJK: nothing sanitizable
    page.nick_edit.setText("")
    page._start()
    wire = page.name_edit.text()
    assert gui.valid_host_name(wire) and wire != "小新"
    assert page.nick_edit.text() == "小新"  # the pretty input became the nickname
    assert started == [(wire, "小新")]
    page.room = None


def test_host_page_sanitizes_mixed_name(window, monkeypatch):
    page = window.host_page
    started = []

    def fake_start(host, display, token, port):  # noqa: ARG001 (fakes ignore token/port)
        started.append((host, display))
        return FakeRoom()

    monkeypatch.setattr(gui, "start_server_room", fake_start)
    page.name_edit.setText("pc 阿新!")
    page.nick_edit.setText("🌸花酱")  # already set: must not be overwritten
    page._start()
    assert started == [("pc", "🌸花酱")]  # sanitized wire, nickname untouched
    assert page.name_edit.text() == "pc"
    page.room = None


def test_host_page_refreshes_ip(window, monkeypatch):
    page = window.host_page
    page.ip_edit.setText("192.168.1.10")
    monkeypatch.setattr(gui, "lan_ip", lambda: "10.0.0.9")
    page._refresh_ip()
    assert page.ip_edit.text() == "10.0.0.9"
    page._refresh_ip()  # unchanged: must not raise
    assert page.ip_edit.text() == "10.0.0.9"


def test_host_page_leave_stops_room(window):
    page = window.host_page
    room = FakeRoom()
    page.room = room
    page._set_started(True)
    page._leave()
    assert room.stopped and page.room is None
    assert page.start_btn.isEnabled() and not page.leave_btn.isVisibleTo(page)
    assert page.ip_edit.text() == ""  # status card reset


def test_host_page_leave_without_room_is_noop(window):
    window.host_page._leave()  # must not raise


def test_join_page_leave_stops_room(window):
    page = window.join_page
    room = FakeRoom()
    page.room = room
    page.leave_btn.setVisible(True)
    page._leave()  # stop() sends leave to the hub
    assert room.stopped and page.room is None
    assert page.join_btn.isEnabled() and not page.leave_btn.isVisibleTo(page)


def test_join_page_requires_token(window):
    page = window.join_page
    page.token_edit.setText("")
    before = page.settings.value("last_token", "")
    page._join()  # empty token: warns and returns without spawning
    assert page.settings.value("last_token", "") == before


def test_join_page_discovers_and_joins_in_process(window, monkeypatch):
    joined = []

    def fake_discover(token):
        assert token == "tok"
        return ("192.168.1.20", gui.GUI_PORT + 3)

    def fake_client(host, display, url, token):
        joined.append((host, display, url, token))
        return FakeRoom()

    monkeypatch.setattr(gui, "discover_room", fake_discover)
    monkeypatch.setattr(gui, "start_client_room", fake_client)
    page = window.join_page
    page.ip_edit.clear()  # a real join on this box may have restored last_ip from QSettings
    page.token_edit.setText("tok")  # IP left empty: auto-discovery fills it
    page.nick_edit.setText("🌸花酱")
    page.name_edit.setText("pc-alpha")
    page._join()  # discovery runs on a thread; results arrive via signals
    for _ in range(200):
        QApplication.processEvents()
        if joined:
            break
    assert page.ip_edit.text() == "192.168.1.20"  # discovered IP visible in the field
    assert joined == [("pc-alpha", "🌸花酱", f"http://192.168.1.20:{gui.GUI_PORT + 3}", "tok")]
    assert page.settings.value("last_token") == "tok"
    for key in ("last_ip", "last_token", "last_nick"):  # don't leak into user QSettings
        page.settings.remove(key)


def test_join_page_uses_typed_ip(window, monkeypatch):
    joined = []

    def fake_probe(ip, token, start=gui.GUI_PORT, limit=gui.PORT_SCAN_LIMIT):  # noqa: ARG001
        assert ip == "192.168.1.20" and token == "tok"
        return gui.GUI_PORT + 3

    def fake_client(host, display, url, token):
        joined.append((host, display, url, token))
        return FakeRoom()

    monkeypatch.setattr(gui, "probe_room_port", fake_probe)
    monkeypatch.setattr(gui, "start_client_room", fake_client)
    page = window.join_page
    page.ip_edit.setText("192.168.1.20")  # typed/refilled IP: no subnet sweep
    page.token_edit.setText("tok")
    page.nick_edit.setText("🌸花酱")
    page.name_edit.setText("pc-alpha")
    page._join()
    for _ in range(200):
        QApplication.processEvents()
        if joined:
            break
    assert joined == [("pc-alpha", "🌸花酱", "http://192.168.1.20:8902", "tok")]
    for key in ("last_ip", "last_token", "last_nick"):
        page.settings.remove(key)


def test_local_subnet_hosts_covers_self(monkeypatch):
    monkeypatch.setattr(gui, "lan_ip", lambda: "192.168.0.104")
    hosts = gui.local_subnet_hosts()
    assert len(hosts) == 254 and hosts[0] == "192.168.0.1" and "192.168.0.104" in hosts


def test_discover_room_finds_matching_host(monkeypatch):
    monkeypatch.setattr(gui, "local_subnet_hosts", lambda: ["10.0.0.1", "10.0.0.2"])
    monkeypatch.setattr(
        gui,
        "_port_open",
        lambda ip, port, timeout=gui.SWEEP_TIMEOUT: (  # noqa: ARG005
            (ip, port) == ("10.0.0.2", gui.GUI_PORT)
        ),
    )
    monkeypatch.setattr(
        gui,
        "_room_accepts",
        lambda ip, port, token: (ip, port) == ("10.0.0.2", gui.GUI_PORT),  # noqa: ARG005
    )
    assert gui.discover_room("tok") == ("10.0.0.2", gui.GUI_PORT)


def test_discover_room_returns_none_when_absent(monkeypatch):
    monkeypatch.setattr(gui, "local_subnet_hosts", lambda: ["10.0.0.1"])
    monkeypatch.setattr(
        gui,
        "_port_open",
        lambda ip, port, timeout=gui.SWEEP_TIMEOUT: False,  # noqa: ARG005
    )
    assert gui.discover_room("tok") is None


def test_join_page_webui_button_lifecycle(window, monkeypatch):
    page = window.join_page
    rooms = []

    def fake_client(host, display, url, token):  # noqa: ARG001 (fakes ignore args)
        rooms.append(FakeRoom())
        return rooms[-1]

    monkeypatch.setattr(gui, "probe_room_port", lambda *a, **k: gui.GUI_PORT + 3)  # noqa: ARG005
    monkeypatch.setattr(gui, "start_client_room", fake_client)
    page.ip_edit.setText("192.168.1.20")
    page.token_edit.setText("tok")
    page._join()
    for _ in range(200):
        QApplication.processEvents()
        if rooms:
            break
    assert page.webui_row.isVisibleTo(page)  # joined: own WebUI is openable
    page._leave()
    assert rooms[0].stopped
    assert not page.webui_row.isVisibleTo(page)
    for key in ("last_ip", "last_token", "last_nick"):  # don't leak into user QSettings
        page.settings.remove(key)


def test_join_page_reports_scan_miss(window, monkeypatch):
    monkeypatch.setattr(gui, "discover_room", lambda *_a, **_k: None)
    monkeypatch.setattr(gui, "start_client_room", lambda *_: pytest.fail("must not join"))
    page = window.join_page
    page.ip_edit.clear()  # module-scoped window: drop any leftover from other tests
    page.token_edit.setText("tok")  # empty IP: full subnet discovery
    page.name_edit.setText("pc-alpha")
    page._join()
    for _ in range(200):
        QApplication.processEvents()
        if page.join_btn.isEnabled():
            break
    assert page.room is None
    assert page.ip_edit.text() == ""  # nothing discovered, nothing filled
    assert "没有找到" in page.status.text()


def test_join_page_refresh_fills_ip(window, monkeypatch):
    page = window.join_page
    monkeypatch.setattr(gui, "discover_room", lambda *_a, **_k: ("10.0.0.9", gui.GUI_PORT))
    page.token_edit.setText("tok")
    page._refresh_ip()
    for _ in range(200):
        QApplication.processEvents()
        if page.ip_edit.text() == "10.0.0.9":
            break
    assert page.ip_edit.text() == "10.0.0.9"
    assert page.ip_refresh_btn.isEnabled()


def test_close_parks_room_to_tray(window):
    page = window.host_page
    room = FakeRoom()
    page.room = room
    window._tray = None
    window.close()
    assert not room.stopped  # close hides to tray; the room keeps running
    assert not window.isVisible()
    window._tray.hide()
    page.room = None


def test_quit_from_tray_stops_rooms(window):
    page = window.host_page
    room = FakeRoom()
    page.room = room
    window.quit_from_tray()
    assert room.stopped and page.room is None


def test_valid_host_name_contract():
    assert valid_host_name("pc-alpha")
    assert not valid_host_name("不合法/名字")


def test_gui_singleton_guard():
    key = "FungiGuiSingletonTest"

    shared = QSharedMemory(key)
    assert shared.create(1)
    assert gui._singleton_taken(key)
    shared.detach()
    assert not gui._singleton_taken(key)
