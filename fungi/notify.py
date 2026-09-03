"""Notifications: clone threads -> Qt main thread via a queued signal bridge.

The clone loop thread calls Notifier.ask() from on_ask; the bridge queues it
onto the Qt main thread, which lands it as a tray showMessage (source host +
summary in the title).
"""

from PyQt6.QtCore import QObject, pyqtSignal

_MAX_SUMMARY = 120


class AskBridge(QObject):
    """Background thread -> Qt main thread; carries display data only."""

    ask = pyqtSignal(str, str)  # (source host, summary)


class Notifier:
    """Thread-safe notification entry for clone threads."""

    def __init__(self, tray):
        self.tray = tray
        self.shown = 0  # notifications actually delivered to the tray (selftest spy)
        self.bridge = AskBridge()
        self.bridge.ask.connect(self._show)

    # called from any thread
    def ask(self, source: str, summary: str) -> None:
        self.bridge.ask.emit(source, summary)

    # Qt main thread only
    def _show(self, source: str, summary: str) -> None:
        self.shown += 1
        text = " ".join(summary.split())
        if len(text) > _MAX_SUMMARY:
            text = text[: _MAX_SUMMARY - 1] + "…"
        self.tray.notify(f"Fungi · {source}", text)
