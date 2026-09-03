"""Card asks: envelope-borne asks awaiting the local user's answer in the WebUI.

The local clone's on_ask records every incoming ask envelope here; the WebUI
lists pending cards (GET /asks) and answers them (POST /answer), which routes
an answer envelope back to the requester. Doubles as the heartbeat-replay
dedup set: replaying a known ask id is a no-op.
"""

import threading

from .protocol import Envelope


class AskCards:
    """Thread-safe registry of pending ask envelopes, keyed by envelope id."""

    def __init__(self) -> None:
        self._asks: dict[str, Envelope] = {}
        self._answered: dict[str, str] = {}  # ask_id -> answer value (for dedup)
        self._lock = threading.Lock()

    def record(self, env: Envelope) -> bool:
        """Register an ask; False if the id is already known (replay/duplicate)."""
        with self._lock:
            if env.id in self._asks or env.id in self._answered:
                return False
            self._asks[env.id] = env
            return True

    def take(self, ask_id: str, value: str) -> Envelope | None:
        """Answer and remove a card; returns the ask envelope or None."""
        with self._lock:
            env = self._asks.pop(ask_id, None)
            if env is not None:
                self._answered[ask_id] = value
            return env

    def pending(self) -> list[dict]:
        """Unanswered cards, oldest first: [{id, src, body}]."""
        with self._lock:
            return [{"id": env.id, "src": env.src, "body": env.body} for env in self._asks.values()]

    def known(self, ask_id: str) -> bool:
        with self._lock:
            return ask_id in self._asks or ask_id in self._answered
