"""Reusable blocking ask with dual wake sources.

A clone registers an ask id and blocks in wait(); the wake arrives either from
an in-process resolve (local clone's inquire via HTTP /answer) or from an
answer envelope pulled off the message plane (comm clones' ask/consent).
Timeout and heartbeat are per-wait parameters so callers keep control.
"""

import threading
import time
from collections.abc import Callable
from typing import Any

DEFAULT_TIMEOUT_S = 1800.0
DEFAULT_HEARTBEAT_S = 15.0


class PendingAsks:
    """Registry of blocking asks; thread-safe, value-agnostic."""

    def __init__(self) -> None:
        self._entries: dict[str, dict] = {}
        self._lock = threading.Lock()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def register(self, ask_id: str) -> None:
        with self._lock:
            self._entries[ask_id] = {"event": threading.Event(), "value": None}

    def resolve(self, ask_id: str, value: Any) -> bool:
        """Wake a registered ask with a value; False for unknown/expired ids."""
        with self._lock:
            entry = self._entries.get(ask_id)
        if entry is None:
            return False
        entry["value"] = value
        entry["event"].set()
        return True

    def wait(
        self,
        ask_id: str,
        timeout_s: float | None = None,
        heartbeat_s: float = DEFAULT_HEARTBEAT_S,
        on_heartbeat: Callable[[], None] | None = None,
    ) -> tuple[bool, Any]:
        """Block until resolved or timed out; returns (answered, value)."""
        with self._lock:
            entry = self._entries.get(ask_id)
        if entry is None:
            return False, None
        deadline = time.monotonic() + (timeout_s if timeout_s is not None else DEFAULT_TIMEOUT_S)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or entry["event"].wait(min(heartbeat_s, remaining)):
                return entry["event"].is_set(), entry["value"]
            if on_heartbeat is not None:
                on_heartbeat()

    def discard(self, ask_id: str) -> None:
        with self._lock:
            self._entries.pop(ask_id, None)
