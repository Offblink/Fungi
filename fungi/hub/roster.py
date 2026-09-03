"""Roster: joined hosts + heartbeat reaping (Face Roster 同构)."""

import threading
import time


class Member:
    __slots__ = ("addr", "display", "last_seen", "name")

    def __init__(self, name: str, addr: str, display: str = ""):
        self.name = name
        self.addr = addr
        self.display = display
        self.last_seen = time.time()


class Roster:
    """Host 名册 + heartbeat 时间戳, 全部操作锁保护

    `name` is the wire identity (envelope addresses, URLs, file names);
    `display` is the presentation nickname, surfaced to the WebUI only.
    """

    def __init__(self, heartbeat_timeout: float = 30.0):
        self._members: dict[str, Member] = {}
        self._guard = threading.Lock()
        self.heartbeat_timeout = heartbeat_timeout

    def join(self, name: str, addr: str, display: str = "") -> bool:
        """Add or re-attach a host (reconnect). Returns True if new.

        Re-joining refreshes the display name, so a UI-side rename needs no
        restart: the next join/heartbeat carries the new nickname.
        """
        with self._guard:
            member = self._members.get(name)
            new = member is None
            if member is None:
                member = Member(name, addr, display)
                self._members[name] = member
            member.addr = addr
            member.display = display
            member.last_seen = time.time()
            return new

    def display(self, name: str) -> str:
        with self._guard:
            member = self._members.get(name)
            return member.display if member is not None else ""

    def entries(self, name: str) -> list[dict]:
        """Peers as display records ``[{"name", "display"}]`` (self excluded)."""
        with self._guard:
            return sorted(
                (
                    {"name": m.name, "display": m.display}
                    for m in self._members.values()
                    if m.name != name
                ),
                key=lambda e: e["name"],
            )

    def leave(self, name: str) -> bool:
        with self._guard:
            return self._members.pop(name, None) is not None

    def beat(self, name: str) -> bool:
        with self._guard:
            member = self._members.get(name)
            if member is None:
                return False
            member.last_seen = time.time()
            return True

    def known(self, name: str) -> bool:
        with self._guard:
            return name in self._members

    def peers(self, name: str) -> list[str]:
        with self._guard:
            return sorted(m.name for m in self._members.values() if m.name != name)

    def reap(self) -> list[str]:
        """Remove hosts whose heartbeat timed out; returns removed names."""
        deadline = time.time() - self.heartbeat_timeout
        with self._guard:
            dead = [n for n, m in self._members.items() if m.last_seen < deadline]
            for n in dead:
                del self._members[n]
        return dead
