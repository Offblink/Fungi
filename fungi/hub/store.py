"""Storage: data/ layout + server-enforced path guard + per-path write locks.

Layout: ``public/`` (free for all clones), ``homes/<host>/`` (owner free,
others need a resolved consent), ``sessions/`` (only via the session API,
never the fs API). The guard is authoritative — clones cannot bypass it.
"""

import re
import threading
from pathlib import Path, PurePosixPath

from ..session import SessionStore
from .asks import ANSWERED, Asks

MAX_READ = 100_000
MAX_MATCHES = 200
GUARD_TOPS = ("public", "homes")


class GuardError(Exception):
    pass


class Store:
    def __init__(self, root: Path, asks: Asks):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "public").mkdir(exist_ok=True)
        (self.root / "homes").mkdir(exist_ok=True)
        (self.root / "sessions").mkdir(exist_ok=True)
        self.asks = asks
        self.sessions = SessionStore(self.root / "sessions")
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    # ── guard ──

    def _parse_rel(self, rel: str) -> PurePosixPath:
        if not rel or "\\" in rel:
            raise GuardError(f"bad path: {rel!r}")
        pure = PurePosixPath(rel)
        if pure.is_absolute() or any(part in ("..", ".") for part in pure.parts):
            raise GuardError(f"bad path: {rel!r}")
        if len(pure.parts) < 1 or pure.parts[0] not in GUARD_TOPS:
            raise GuardError(f"path must start with public/ or homes/: {rel!r}")
        if pure.parts[0] == "homes" and len(pure.parts) < 2:
            raise GuardError(f"homes path needs an owner: {rel!r}")
        return pure

    def resolve(
        self, host: str, rel: str, consent_id: str | None = None, *, mutating: bool = False
    ) -> Path:
        """Guard a relative path for a host; returns the resolved absolute path.

        Non-owner homes/ access always needs consent (read and write). Own-home
        WRITES need the own user's consent too (spec 6.1); own-home reads stay
        free — the clone belongs to that host."""
        pure = self._parse_rel(rel)
        owner = pure.parts[1] if pure.parts[0] == "homes" else None
        if owner is not None and owner != host and not self._consent_ok(consent_id):
            raise GuardError(f"consent required: {host} -> homes/{owner}/")
        if owner == host and mutating and not self._consent_ok(consent_id):
            raise GuardError(f"consent required for own-home write: {host} -> homes/{host}/")
        resolved = (self.root / pure).resolve()
        if resolved != self.root and self.root not in resolved.parents:
            raise GuardError(f"path escapes data root: {rel!r}")
        return resolved

    def _consent_ok(self, consent_id: str | None) -> bool:
        if not consent_id:
            return False
        record = self.asks.get(consent_id)
        return record is not None and record["status"] == ANSWERED

    def check_search_root(self, host: str, rel: str) -> Path:
        """glob/grep roots: public/ or the caller's own home only."""
        pure = self._parse_rel(rel)
        if pure.parts[0] == "homes" and pure.parts[1] != host:
            raise GuardError(f"search root must be public/ or own home: {rel!r}")
        resolved = (self.root / pure).resolve()
        if self.root not in resolved.parents and resolved != self.root:
            raise GuardError(f"path escapes data root: {rel!r}")
        return resolved

    def ensure_home(self, name: str) -> None:
        (self.root / "homes" / name).mkdir(parents=True, exist_ok=True)

    # ── locks ──

    def _lock_for(self, key: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(key, threading.Lock())

    # ── fs ops (all take already-guarded paths) ──

    def ls(self, path: Path) -> list[dict]:
        if not path.is_dir():
            raise GuardError(f"not a directory: {path.name}")
        return [
            {"name": p.name, "dir": p.is_dir(), "size": p.stat().st_size if p.is_file() else 0}
            for p in sorted(path.iterdir(), key=lambda p: p.name)
        ]

    def read(self, path: Path) -> str:
        if not path.is_file():
            raise GuardError(f"not a file: {path.name}")
        return path.read_text(encoding="utf-8-sig", errors="replace")[:MAX_READ]

    def write(self, path: Path, content: str) -> str:
        with self._lock_for(str(path)):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8", newline="")
        return f"Wrote {path.name} ({len(content)} chars)"

    def edit(self, path: Path, old_string: str, new_string: str) -> str:
        with self._lock_for(str(path)):
            if not path.is_file():
                raise GuardError(f"not a file: {path.name}")
            content = path.read_text(encoding="utf-8-sig", errors="replace")
            count = content.count(old_string)
            if count == 0:
                raise GuardError("old_string not found")
            if count > 1:
                raise GuardError(f"old_string matches {count} times — must be unique")
            path.write_text(
                content.replace(old_string, new_string, 1), encoding="utf-8", newline=""
            )
        return f"Edited {path.name} (1 replacement)"

    def glob(self, root: Path, pattern: str) -> list[str]:
        base = root
        out = [str(p.relative_to(self.root).as_posix()) for p in base.rglob(pattern)]
        return sorted(out)[:MAX_MATCHES]

    def grep(self, root: Path, pattern: str) -> list[str]:
        rx = re.compile(pattern)
        out = []
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            try:
                for i, line in enumerate(
                    path.read_text(encoding="utf-8-sig", errors="replace").splitlines(), 1
                ):
                    if rx.search(line):
                        out.append(f"{path.relative_to(self.root).as_posix()}:{i}:{line.strip()}")
                        if len(out) >= MAX_MATCHES:
                            return out
            except OSError:
                continue
        return out
