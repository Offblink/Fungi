"""Tray controller tests: activation reasons and toast-click signal (regression:
ActivationReason has no Message member in PyQt6 — toast clicks are a signal)."""

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtWidgets import QApplication, QSystemTrayIcon

from fungi.tray import TrayController


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_activation_and_message_click_open_webui(qapp):  # noqa: ARG001 (Qt app fixture)
    clicks = []
    tray = TrayController(on_open_webui=lambda: clicks.append(1))
    tray._on_activated(QSystemTrayIcon.ActivationReason.Trigger)
    tray._on_activated(QSystemTrayIcon.ActivationReason.DoubleClick)
    tray.messageClicked.emit()  # the toast-click path
    assert len(clicks) == 3
    tray.hide()


def test_context_menu_click_does_not_open_webui(qapp):  # noqa: ARG001 (Qt app fixture)
    clicks = []
    tray = TrayController(on_open_webui=lambda: clicks.append(1))
    tray._on_activated(QSystemTrayIcon.ActivationReason.Context)
    assert clicks == []
    tray.hide()
