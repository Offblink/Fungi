"""Message envelope: clone addressing + JSON wire format.

An address is ``host:local`` or ``host:comm-<peer>`` — e.g. ``alpha:local``,
``alpha:comm-beta``. Wire fields are src/dst (``from`` is a Python keyword).
"""

import time
import uuid
from dataclasses import dataclass

ENVELOPE_VERSION = 1
TYPES = ("chat", "task", "result", "ask", "answer", "err", "transfer")


class ProtocolError(Exception):
    pass


def parse_addr(addr: str) -> tuple[str, str, str | None]:
    """Split ``host:role[-peer]`` into (host, role, peer); role is local|comm."""
    if not isinstance(addr, str) or addr.count(":") != 1:
        raise ProtocolError(f"bad address: {addr!r}")
    host, role = addr.split(":")
    if not host:
        raise ProtocolError(f"bad address: {addr!r}")
    if role == "local":
        return host, "local", None
    if role.startswith("comm-"):
        peer = role[len("comm-") :]
        if not peer:
            raise ProtocolError(f"bad address: {addr!r}")
        return host, "comm", peer
    raise ProtocolError(f"bad address: {addr!r}")


def valid_addr(addr: str) -> bool:
    try:
        parse_addr(addr)
    except ProtocolError:
        return False
    return True


@dataclass(frozen=True)
class Envelope:
    src: str
    dst: str
    type: str
    body: dict
    id: str = ""
    ts: float = 0.0
    reply_to: str | None = None
    v: int = ENVELOPE_VERSION

    def __post_init__(self) -> None:
        if not self.id:
            object.__setattr__(self, "id", new_id())
        if not self.ts:
            object.__setattr__(self, "ts", time.time())

    def serialize(self) -> dict:
        return {
            "v": self.v,
            "id": self.id,
            "src": self.src,
            "dst": self.dst,
            "type": self.type,
            "ts": self.ts,
            "reply_to": self.reply_to,
            "body": self.body,
        }


def new_id() -> str:
    return uuid.uuid4().hex


def deserialize(data: dict) -> Envelope:
    """Validate a wire dict and build an Envelope; raises ProtocolError."""
    if not isinstance(data, dict):
        raise ProtocolError("envelope must be an object")
    if data.get("v") != ENVELOPE_VERSION:
        raise ProtocolError(f"unsupported version: {data.get('v')!r}")
    if not valid_addr(str(data.get("src", ""))):
        raise ProtocolError(f"bad src: {data.get('src')!r}")
    if not valid_addr(str(data.get("dst", ""))):
        raise ProtocolError(f"bad dst: {data.get('dst')!r}")
    if data.get("type") not in TYPES:
        raise ProtocolError(f"bad type: {data.get('type')!r}")
    if not isinstance(data.get("body"), dict):
        raise ProtocolError("body must be an object")
    reply_to = data.get("reply_to")
    if reply_to is not None and not isinstance(reply_to, str):
        raise ProtocolError("reply_to must be a string or null")
    return Envelope(
        src=str(data["src"]),
        dst=str(data["dst"]),
        type=str(data["type"]),
        body=data["body"],
        id=str(data.get("id") or "") or new_id(),
        ts=float(data.get("ts") or 0.0),
        reply_to=reply_to,
    )


def error_envelope(env: Envelope, message: str) -> Envelope:
    """Build an err envelope addressed back to the sender."""
    return Envelope(
        src=env.dst,
        dst=env.src,
        type="err",
        body={"error": message, "undeliverable_to": env.dst},
        reply_to=env.id,
    )
