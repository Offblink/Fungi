"""Tray controller tests: activation reasons and toast-click signal (toast
clicks arrive via the messageClicked signal, never as an activation reason)."""

import os

import pytest

pytest.importorskip("qfluentwidgets", reason="PyQt-Fluent-Widgets (qfluentwidgets) not installed")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtWidgets import QSystemTrayIcon

from fungi.tray import TrayController


def test_activation_and_message_click_open_webui(qapp):  # noqa: ARG001 (Qt app fixture)
    clicks = []
    tray = TrayController(on_open_webui=lambda: clicks.append(1))
    tray._on_activated(QSystemTrayIcon.Trigger)
    tray._on_activated(QSystemTrayIcon.DoubleClick)
    tray.messageClicked.emit()  # the toast-click path
    assert len(clicks) == 3
    tray.hide()


def test_context_menu_click_does_not_open_webui(qapp):  # noqa: ARG001 (Qt app fixture)
    clicks = []
    tray = TrayController(on_open_webui=lambda: clicks.append(1))
    tray._on_activated(QSystemTrayIcon.Context)  # pops the fluent menu instead
    assert clicks == []
    tray.hide()
