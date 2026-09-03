"""Roster: joined hosts + heartbeat reaping (Face Roster 同构)."""

import threading
import time


class Member:
    __slots__ = ("addr", "last_seen", "name")

    def __init__(self, name: str, addr: str):
        self.name = name
        self.addr = addr
        self.last_seen = time.time()


class Roster:
    """Host 名册 + heartbeat 时间戳, 全部操作锁保护"""

    def __init__(self, heartbeat_timeout: float = 30.0):
        self._members: dict[str, Member] = {}
        self._guard = threading.Lock()
        self.heartbeat_timeout = heartbeat_timeout

    def join(self, name: str, addr: str) -> bool:
        """Add or re-attach a host (reconnect). Returns True if new."""
        with self._guard:
            member = self._members.get(name)
            new = member is None
            if member is None:
                member = Member(name, addr)
                self._members[name] = member
            member.addr = addr
            member.last_seen = time.time()
            return new

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
