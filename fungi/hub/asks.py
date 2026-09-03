"""Pending-ask registry: consent/request records addressed to a host's user.

Lifecycle: pending -> answered | denied | timeout | error. The registry only
stores state; the blocking PendingAsk machinery lives with the clones (Phase 2).
"""

import threading
import time

from ..protocol import new_id

PENDING = "pending"
ANSWERED = "answered"
DENIED = "denied"
TIMEOUT = "timeout"
ERROR = "error"


class Asks:
    def __init__(self):
        self._asks: dict[str, dict] = {}
        self._guard = threading.Lock()

    def open(self, dst_host: str, payload: dict, ask_id: str | None = None) -> dict:
        record = {
            "ask_id": ask_id or new_id(),
            "dst_host": dst_host,
            "payload": payload,
            "status": PENDING,
            "value": None,
            "ts": time.time(),
            "resolved_ts": None,
        }
        with self._guard:
            self._asks[record["ask_id"]] = record
        return dict(record)

    def resolve(
        self, ask_id: str, value: str | None = None, error: str | None = None
    ) -> dict | None:
        """Settle an ask: answered (any value except "no"), denied ("no"), timeout/error."""
        with self._guard:
            record = self._asks.get(ask_id)
            if record is None or record["status"] != PENDING:
                return None
            if error is not None:
                record["status"] = ERROR
                record["value"] = error
            elif value == "no":
                record["status"] = DENIED
                record["value"] = "no"
            else:
                record["status"] = ANSWERED
                record["value"] = value
            record["resolved_ts"] = time.time()
            return dict(record)

    def get(self, ask_id: str) -> dict | None:
        with self._guard:
            record = self._asks.get(ask_id)
            return dict(record) if record else None

    def pending_for(self, host: str) -> list[dict]:
        """Unresolved asks addressed to this host (heartbeat replay)."""
        with self._guard:
            return [
                dict(r)
                for r in self._asks.values()
                if r["dst_host"] == host and r["status"] == PENDING
            ]

    def sweep(self, max_age: float) -> list[str]:
        """Timeout pending asks older than max_age; returns swept ask ids."""
        deadline = time.time() - max_age
        swept = []
        with self._guard:
            for record in self._asks.values():
                if record["status"] == PENDING and record["ts"] < deadline:
                    record["status"] = TIMEOUT
                    record["resolved_ts"] = time.time()
                    swept.append(record["ask_id"])
        return swept
