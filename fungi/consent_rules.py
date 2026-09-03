"""Always-allow consent rules, remembered per remote orchestrator (ask src).

Host-local policy, persisted at ~/.fungi/consent_rules.json (path injectable
for tests). The local clone checks the rules in on_ask: a consent-shaped ask
from an allowed address is answered with an answer envelope (value=yes)
immediately — no notification, no card. Generic ask_user never auto-allows.
"""

import json
import threading
from pathlib import Path

DEFAULT_PATH = Path.home() / ".fungi" / "consent_rules.json"


class ConsentRules:
    """Thread-safe always-allow set of remote orchestrator addresses."""

    def __init__(self, path: Path | None = None):
        self.path = path or DEFAULT_PATH
        self._allowed: set[str] = set()
        self._lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            return
        if isinstance(data, dict) and isinstance(data.get("always_allow"), list):
            self._allowed = {str(a) for a in data["always_allow"]}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps({"always_allow": sorted(self._allowed)}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def allows(self, src: str) -> bool:
        with self._lock:
            return src in self._allowed

    def allow(self, src: str) -> None:
        with self._lock:
            if src in self._allowed:
                return
            self._allowed.add(src)
            self._save()

    def items(self) -> list[str]:
        with self._lock:
            return sorted(self._allowed)
