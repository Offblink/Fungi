"""Relay: the single delivery function — local direct drop or client forwarding.

Every destination (a server-local clone, or a remote host's message buffer) is
an Inbox with a monotonic cursor. Clients pull their host buffer via long-poll;
local clones get their Inbox handed to them at registration.
"""

import threading
from collections import OrderedDict

from ..protocol import Envelope, ProtocolError, error_envelope, parse_addr

LOCAL = "local"
QUEUED = "queued"
DUPLICATE = "duplicate"
BOUNCED = "bounced"


class Inbox:
    """FIFO of envelopes with monotonic seq; long-poll via after(cursor)."""

    def __init__(self, capacity: int = 1000):
        self._cond = threading.Condition()
        self._items: list[tuple[int, Envelope]] = []
        self._seq = 0
        self._capacity = capacity
        self._seen: OrderedDict[str, None] = OrderedDict()  # id dedup outlives the buffer

    def push(self, env: Envelope) -> str:
        """Append; duplicate ids are dropped. Returns QUEUED or DUPLICATE."""
        with self._cond:
            if env.id in self._seen:
                return DUPLICATE
            self._seen[env.id] = None
            while len(self._seen) > self._capacity * 2:
                self._seen.popitem(last=False)
            self._seq += 1
            self._items.append((self._seq, env))
            if len(self._items) > self._capacity:
                self._items = self._items[-self._capacity // 2 :]
            self._cond.notify_all()
            return QUEUED

    def after(self, cursor: int, timeout: float) -> tuple[list[Envelope], int]:
        """Wait up to timeout for messages newer than cursor; drains them."""
        with self._cond:
            if not any(seq > cursor for seq, _e in self._items) and timeout > 0:
                self._cond.wait(timeout)
            out = [(seq, e) for seq, e in self._items if seq > cursor]
            self._items = [(seq, e) for seq, e in self._items if seq <= cursor]
            new_cursor = out[-1][0] if out else cursor
            return [e for _seq, e in out], new_cursor


class Relay:
    """Star-topology delivery. One method, two paths, same function."""

    def __init__(self, self_name: str):
        self.self_name = self_name
        self._locals: dict[str, Inbox] = {}
        self._hosts: dict[str, Inbox] = {}
        self._guard = threading.Lock()

    def register_local(self, addr: str) -> Inbox:
        with self._guard:
            inbox = self._locals.get(addr)
            if inbox is None:
                inbox = Inbox()
                self._locals[addr] = inbox
            return inbox

    def unregister_local(self, addr: str) -> None:
        with self._guard:
            self._locals.pop(addr, None)

    def host_buffer(self, name: str) -> Inbox:
        with self._guard:
            inbox = self._hosts.get(name)
            if inbox is None:
                inbox = Inbox()
                self._hosts[name] = inbox
            return inbox

    def drop_host(self, name: str) -> None:
        with self._guard:
            self._hosts.pop(name, None)

    def deliver(self, env: Envelope) -> str:
        """Route one envelope; returns the original message's delivery status.

        Unreachable dst bounces an err back to src (best effort) and returns BOUNCED.
        """
        try:
            host, _role, _peer = parse_addr(env.dst)
            with self._guard:
                inbox = (
                    self._locals.get(env.dst) if host == self.self_name else self._hosts.get(host)
                )
            if inbox is not None:
                return inbox.push(env)
            self._bounce(env)
            return BOUNCED
        except ProtocolError:
            return BOUNCED

    def _bounce(self, env: Envelope) -> str:
        if env.type == "err":
            return BOUNCED  # never bounce an err; drop silently
        try:
            host, _role, _peer = parse_addr(env.src)
        except ProtocolError:
            return BOUNCED
        with self._guard:
            inbox = self._locals.get(env.src) if host == self.self_name else self._hosts.get(host)
        if inbox is None:
            return BOUNCED
        return inbox.push(error_envelope(env, "unreachable destination"))
