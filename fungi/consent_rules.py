"""Per-friend consent mode: who gets asked, who is silently allowed.

Host-local policy, persisted at ~/.fungi/consent_rules.json (path injectable
for tests). The local clone checks the rules in on_ask: a consent-shaped ask
from a host whose mode is "allow" is answered with an answer envelope
(value=yes) immediately — no notification, no card. Mode "ask" (the default)
raises a card. Generic ask_user never auto-allows.

Modes are visible and reversible from the WebUI friend view (slider). Legacy
"always_allow" address lists from earlier versions migrate to host modes so
nothing granted before stays invisible or permanent.
"""

import json
import threading
from pathlib import Path

from .protocol import parse_addr

DEFAULT_PATH = Path.home() / ".fungi" / "consent_rules.json"
MODES = ("allow", "ask")


def host_of(src: str) -> str:
    """Host part of an orchestrator address; falls back to the raw string."""
    try:
        host, _role, _peer = parse_addr(src)
        return host
    except Exception:
        return src


class ConsentRules:
    """Thread-safe per-host consent modes ("allow" | "ask")."""

    def __init__(self, path: Path | None = None):
        self.path = path or DEFAULT_PATH
        self._modes: dict[str, str] = {}
        self._lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(data, dict):
            return
        if isinstance(data.get("modes"), dict):
            self._modes = {str(h): m for h, m in data["modes"].items() if m in MODES}
        # Legacy migration: always_allow address list -> per-host allow mode,
        # so pre-slider grants become visible and reversible in the WebUI.
        if isinstance(data.get("always_allow"), list):
            for addr in data["always_allow"]:
                self._modes.setdefault(host_of(str(addr)), "allow")
            self._save()  # rewrite in the new shape; legacy keys are gone

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps({"modes": self._modes}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def mode_for(self, host: str) -> str:
        """Consent mode for a host; "ask" unless explicitly allowed."""
        with self._lock:
            return self._modes.get(host, "ask")

    def set_mode(self, host: str, mode: str) -> None:
        if mode not in MODES:
            raise ValueError(f"unknown consent mode: {mode!r}")
        with self._lock:
            if self._modes.get(host) == mode:
                return
            if mode == "ask":
                self._modes.pop(host, None)  # keep the file minimal
            else:
                self._modes[host] = mode
            self._save()

    def allows(self, src: str) -> bool:
        """True when consent-shaped asks from this orchestrator auto-grant."""
        return self.mode_for(host_of(src)) == "allow"
