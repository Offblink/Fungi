"""Delegate/peers/send_file tools for the local clone: the user's bridge to
remote hosts."""

import tempfile
import uuid
import zipfile
from pathlib import Path

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

    def send_file(self, args: dict) -> str:
        """Send a real file from this machine to a peer: upload bytes to the
        hub staging area, then the peer's comm clone asks its user and lands
        the file in their inbox. The result envelope comes back here."""
        host = str(args.get("host") or "").strip()
        path = str(args.get("path") or "").strip()
        name = str(args.get("name") or "").strip()
        reason = str(args.get("reason") or "").strip()
        if not host or not path:
            return "ERROR: Required arguments: host, path"
        if host == self.host:
            return "ERROR: host must be a peer, not yourself"
        if host not in (self.peers_fn() or []):
            return f"ERROR: unknown or offline peer: {host}"
        src = Path(path)
        tmp_zip: Path | None = None
        try:
            if src.is_dir():
                # Folders cannot ride the transfer as-is: zip them. The
                # receiver gets one archive named after the folder.
                tmp_zip = Path(tempfile.gettempdir()) / f"fungi-zip-{uuid.uuid4().hex[:8]}.zip"
                default_name = f"{src.name or 'folder'}.zip"
                with zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_DEFLATED) as zf:
                    for f in sorted(src.rglob("*")):
                        if f.is_file():
                            zf.write(f, f.relative_to(src.parent))
                upload_path, name = tmp_zip, name or default_name
            elif src.is_file():
                upload_path = src
                name = name or src.name
            else:
                return f"ERROR: no such file or folder: {path}"
            staged = self.transport.upload_transfer(str(upload_path), name, host)
            if staged.get("error"):
                return f"ERROR: {staged['error']}"
        finally:
            if tmp_zip is not None:
                tmp_zip.unlink(missing_ok=True)
        env = Envelope(
            src=self.addr,
            dst=f"{host}:comm-{self.host}",
            type="transfer",
            body={
                "id": staged["id"],
                "name": staged["name"],
                "size": staged["size"],
                "reason": reason,
                "from": self.addr,
            },
        )
        self.pending.register(env.id)
        self.transport.send(env)
        try:
            answered, value = self.pending.wait(env.id, timeout_s=self.timeout_s)
        finally:
            self.pending.discard(env.id)
        if not answered:
            return "FAIL: no response from remote host (timeout)"
        if not isinstance(value, dict):
            return "ERROR: malformed transfer result"
        if value.get("ok"):
            return f"DELIVERED: saved on {host} as {value.get('saved', '(unknown path)')}"
        return f"REJECTED: {value.get('error', 'declined')}"

    def bound(self) -> dict[str, BoundTool]:
        return {
            "delegate": BoundTool(schema=_SCHEMA_DELEGATE, fn=self.delegate),
            "peers": BoundTool(schema=_SCHEMA_PEERS, fn=self.peers),
            "send_file": BoundTool(schema=_SCHEMA_SEND_FILE, fn=self.send_file),
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
_SCHEMA_SEND_FILE = {
    "type": "function",
    "function": {
        "name": "send_file",
        "description": (
            "Send a file from THIS machine to a peer host's user. The receiving "
            "user must accept before it lands on their disk. Blocks until they "
            "answer. path may be a file or a FOLDER (folders are zipped "
            "automatically). Use this whenever the user asks to give/send files "
            "to a friend host — never ask the peer's clone how to transfer."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "host": {"type": "string", "description": "destination host name"},
                "path": {
                    "type": "string",
                    "description": "local file OR folder path (a folder is zipped automatically)",
                },
                "name": {
                    "type": "string",
                    "description": "file name as the receiver sees it (default: basename of path)",
                },
                "reason": {"type": "string", "description": "why you are sending it"},
            },
            "required": ["host", "path"],
        },
    },
}
