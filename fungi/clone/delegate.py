"""Delegate/peers tools for the local clone: the user's bridge to remote hosts."""

from ..agent import BoundTool
from ..pending import PendingAsks
from ..protocol import Envelope, parse_addr


class DelegateTools:
    def __init__(
        self,
        addr: str,
        transport,
        pending: PendingAsks,
        peers_fn,
        timeout_s: float = 1800.0,
    ):
        self.addr = addr
        self.host, self.role, self.peer = parse_addr(addr)
        self.transport = transport
        self.pending = pending
        self.peers_fn = peers_fn
        self.timeout_s = timeout_s

    def delegate(self, args: dict) -> str:
        host = str(args.get("host") or "").strip()
        goal = str(args.get("goal") or "").strip()
        reply_format = str(args.get("reply_format") or "").strip()
        if not host or not goal:
            return "ERROR: Required arguments: host, goal"
        env = Envelope(
            src=self.addr,
            dst=f"{host}:comm-{self.host}",
            type="task",
            body={"goal": goal, "reply_format": reply_format, "context": args.get("context")},
        )
        self.pending.register(env.id)
        self.transport.send(env)
        try:
            answered, body = self.pending.wait(env.id, timeout_s=self.timeout_s)
        finally:
            self.pending.discard(env.id)
        if not answered:
            return "FAIL: no response from remote host (timeout)"
        if not isinstance(body, dict) or not body.get("ok"):
            return f"FAIL: {body}"
        return str(body.get("payload") or "")

    def peers(self, _args: dict) -> str:
        names = list(self.peers_fn() or [])
        return "PEERS: " + ", ".join(names) if names else "(no peers connected)"

    def bound(self) -> dict[str, BoundTool]:
        return {
            "delegate": BoundTool(schema=_SCHEMA_DELEGATE, fn=self.delegate),
            "peers": BoundTool(schema=_SCHEMA_PEERS, fn=self.peers),
        }


_SCHEMA_DELEGATE = {
    "type": "function",
    "function": {
        "name": "delegate",
        "description": (
            "Delegate a cross-host task to the comm Orchestrator of the given host. "
            "Blocks until the result envelope returns. Users only talk to you — "
            "anything touching another host goes through here."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "host": {"type": "string", "description": "target host name"},
                "goal": {"type": "string", "description": "what to do"},
                "reply_format": {"type": "string", "description": "expected reply shape"},
                "context": {"type": "string", "description": "background material"},
            },
            "required": ["host", "goal"],
        },
    },
}
_SCHEMA_PEERS = {
    "type": "function",
    "function": {
        "name": "peers",
        "description": "List currently connected peer hosts.",
        "parameters": {"type": "object", "properties": {}},
    },
}
