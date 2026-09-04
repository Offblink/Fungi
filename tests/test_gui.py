"""GUI launcher smoke: three pages construct offscreen; validation logic holds."""

import os

import pytest

pytest.importorskip("qfluentwidgets", reason="PyQt6-Fluent-Widgets (qfluentwidgets) not installed")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtWidgets import QApplication

from fungi import gui
from fungi.gui import FungiGui, valid_host_name


@pytest.fixture(scope="module")
def window():
    app = QApplication.instance() or QApplication([])
    win = FungiGui()
    yield win
    win.close()
    del app


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


def test_host_page_rejects_bad_host_name(window, monkeypatch):
    page = window.host_page
    monkeypatch.setattr(gui, "start_server_room", lambda *_: pytest.fail("must not start"))
    page.name_edit.setText("不合法/名字")
    page._start()
    assert page.ip_edit.text() == ""  # status card never populated


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


def test_join_page_validation(window):
    page = window.join_page
    page.ip_combo.setText("")
    page.token_edit.setText("")
    before = page.settings.value("last_ip", "")
    page._join()  # empty IP: warns and returns without spawning
    assert page.settings.value("last_ip", "") == before


def test_join_page_scans_and_joins_in_process(window, monkeypatch):
    joined = []

    def fake_probe(ip, token, start=gui.GUI_PORT, limit=gui.PORT_SCAN_LIMIT):  # noqa: ARG001
        assert ip == "192.168.1.20" and token == "tok"
        return gui.GUI_PORT + 3  # found on an upward-scanned port

    def fake_client(host, display, url, token):
        joined.append((host, display, url, token))
        return object()

    monkeypatch.setattr(gui, "probe_room_port", fake_probe)
    monkeypatch.setattr(gui, "start_client_room", fake_client)
    page = window.join_page
    page.ip_combo.setText("192.168.1.20")
    page.token_edit.setText("tok")
    page.nick_edit.setText("🌸花酱")
    page.name_edit.setText("pc-alpha")
    page._join()  # scan runs on a thread; result arrives via join_done signal
    for _ in range(200):
        QApplication.processEvents()
        if joined:
            break
    assert joined == [("pc-alpha", "🌸花酱", "http://192.168.1.20:8902", "tok")]
    assert page.settings.value("last_ip") == "192.168.1.20"
    for key in ("last_ip", "last_token", "last_nick"):  # don't leak into user QSettings
        page.settings.remove(key)


def test_join_page_reports_scan_miss(window, monkeypatch):
    monkeypatch.setattr(gui, "probe_room_port", lambda *_a, **_k: None)
    monkeypatch.setattr(gui, "start_client_room", lambda *_: pytest.fail("must not join"))
    page = window.join_page
    page.ip_combo.setText("10.9.9.9")
    page.token_edit.setText("tok")
    page.name_edit.setText("pc-alpha")
    page._join()
    for _ in range(200):
        QApplication.processEvents()
        if page.join_btn.isEnabled():
            break
    assert page.room is None
    assert "未找到" in page.status.text()


def test_valid_host_name_contract():
    assert valid_host_name("pc-alpha")
    assert not valid_host_name("不合法/名字")
