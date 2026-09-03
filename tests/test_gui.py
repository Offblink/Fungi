"""GUI launcher smoke: three pages construct offscreen; validation logic holds."""

import os

import pytest

pytest.importorskip("qfluentwidgets", reason="PySide6-Fluent-Widgets not installed")

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


def test_three_pages_present(window):
    assert window.host_page.objectName() == "hostPage"
    assert window.join_page.objectName() == "joinPage"
    assert window.cfg_page.objectName() == "configPage"
    # status card hidden until the room is launched
    assert not window.host_page.ip_row.isVisibleTo(window.host_page)
    window.host_page._set_started(True)
    assert window.host_page.ip_row.isVisibleTo(window.host_page)
    window.host_page._set_started(False)


def test_host_page_rejects_bad_host_name(window):
    page = window.host_page
    page.name_edit.setText("不合法/名字")
    page._start()  # must not spawn anything or raise
    assert page.ip_edit.text() == ""  # status card never populated


def test_join_page_validation(window):
    page = window.join_page
    page.ip_combo.setText("")
    page.token_edit.setText("")
    before = page.settings.value("last_ip", "")
    page._join()  # empty IP: warns and returns without spawning
    assert page.settings.value("last_ip", "") == before


def test_join_page_builds_args_with_nickname(window, monkeypatch):
    spawned: list[list[str]] = []

    def fake_spawn(args):
        spawned.append(args)

    monkeypatch.setattr(gui, "spawn_room", fake_spawn)
    page = window.join_page
    page.ip_combo.setText("192.168.1.20")
    page.token_edit.setText("tok")
    page.nick_edit.setText("🌸花酱")
    page.name_edit.setText("pc-alpha")
    page._join()
    assert spawned == [
        [
            "--join",
            "http://192.168.1.20:8899",
            "--token",
            "tok",
            "--name",
            "pc-alpha",
            "--display",
            "🌸花酱",
        ]
    ]
    assert page.settings.value("last_ip") == "192.168.1.20"
    for key in ("last_ip", "last_token", "last_nick"):  # don't leak into user QSettings
        page.settings.remove(key)


def test_valid_host_name_contract():
    assert valid_host_name("pc-alpha")
    assert not valid_host_name("不合法/名字")
