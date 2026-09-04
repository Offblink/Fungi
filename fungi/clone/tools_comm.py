"""Comm clone toolset: peer messaging, consent-gated fs, file transfer, asks.

The fs tools are thin wrappers over the transport's fs entry — the path guard
lives on the hub (server-side, authoritative). Non-public writes ride a
consent_id captured by a prior ask_consent. File transfers stage bytes on the
hub (store-and-forward); only metadata rides the envelope, and the receiving
host's user consents before anything touches their local disk.
"""

from pathlib import Path

from ..agent import BoundTool
from ..config import PROJECT_ROOT
from ..hub.app import safe_name
from ..pending import PendingAsks
from ..protocol import Envelope, parse_addr
from ..tools.ask import _format_answer, _normalize_questions


class CommTools:
    def __init__(
        self,
        addr: str,
        transport,
        pending: PendingAsks,
        ask_timeout_s: float = 1800.0,
        inbox_dir: Path | None = None,
    ):
        self.addr = addr
        self.host, self.role, self.peer = parse_addr(addr)
        self.transport = transport
        self.pending = pending
        self.ask_timeout_s = ask_timeout_s
        self.inbox_dir = Path(inbox_dir) if inbox_dir else PROJECT_ROOT / "inbox"
        self.consent_id: str | None = None  # last granted consent (envelope id)
        self.peer_sends = 0  # send_peer calls in the current turn (chat fallback)

    # ── helpers ──

    def _fs(self, op: str, path: str, **kw) -> str:
        out = self.transport.fs(op, path, **kw)
        if out.get("error"):
            return f"ERROR: {out['error']}"
        result = out.get("result")
        if isinstance(result, list):
            return "\n".join(str(item) for item in result) if result else "(empty)"
        return str(result) if result is not None else "(ok)"

    def _blocking_ask(self, dst: str, body: dict) -> tuple[str, str | None]:
        """Send an ask envelope, block for the answer. Returns (text, ask_id)."""
        env = Envelope(src=self.addr, dst=dst, type="ask", body=body)
        self.pending.register(env.id)
        self.transport.send(env)
        try:
            answered, value = self.pending.wait(env.id, timeout_s=self.ask_timeout_s)
        finally:
            self.pending.discard(env.id)
        if not answered:
            return "ERROR: 用户未回答", None
        if value == "no":
            return "DENIED", None
        return _format_answer(value), env.id

    # ── tools ──

    def send_peer(self, args: dict) -> str:
        text = str(args.get("text") or "").strip()
        if not text:
            return "ERROR: Missing required argument: text"
        env = Envelope(
            src=self.addr, dst=f"{self.peer}:comm-{self.host}", type="chat", body={"text": text}
        )
        out = self.transport.send(env)
        self.peer_sends += 1
        return "SENT" if out.get("ok", True) else f"ERROR: {out.get('status', 'send failed')}"

    def send_file(self, args: dict) -> str:
        """Transfer a server-stored file to the peer host's local inbox.

        Bytes are staged on the hub (copied out of the store server-side); the
        receiver's user consents before the file lands on their disk.
        """
        host = str(args.get("host") or "").strip()
        path = str(args.get("path") or "").strip()
        name = str(args.get("name") or "").strip() or safe_name(path)
        reason = str(args.get("reason") or "").strip()
        if not host or not path:
            return "ERROR: Required arguments: host, path"
        if host == self.host:
            return "ERROR: host must be your peer, not yourself"
        staged = self.transport.transfer(path, name, host)
        if staged.get("error"):
            return f"ERROR: {staged['error']}"
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
            answered, value = self.pending.wait(env.id, timeout_s=self.ask_timeout_s)
        finally:
            self.pending.discard(env.id)
        if not answered:
            return "ERROR: 用户未回答"
        if not isinstance(value, dict):
            return "ERROR: malformed transfer result"
        if value.get("ok"):
            return f"DELIVERED: saved on {host} as {value.get('saved', '(unknown path)')}"
        return f"REJECTED: {value.get('error', 'declined')}"

    def receive_transfer(self, env: Envelope) -> dict:
        """Handle an incoming transfer envelope: consent, download, land locally."""
        body = env.body
        name = safe_name(str(body.get("name") or "file"))
        size = body.get("size")
        reason = str(body.get("reason") or "")
        src_host, _role, _peer = parse_addr(str(body.get("from") or env.src))
        text, _ask_id = self._blocking_ask(
            f"{self.host}:local",
            {
                "from": env.src,
                "action": "receive file",
                "path": name,
                "reason": reason,
                "question": (
                    f"{src_host} wants to send you a file: {name} "
                    f"({size} bytes). Accept?\nReason: {reason}"
                ),
            },
        )
        if text == "DENIED":
            return {"ok": False, "error": "declined by the receiving user"}
        if text.startswith("ERROR"):
            return {"ok": False, "error": text}
        dest_dir = self.inbox_dir / src_host
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / name
        stem, suffix = dest.stem, dest.suffix
        n = 1
        while dest.exists():
            dest = dest_dir / f"{stem}-{n}{suffix}"
            n += 1
        self.transport.download_transfer(str(body.get("id")), dest)
        return {"ok": True, "saved": str(dest)}

    def ask_consent(self, args: dict) -> str:
        host = str(args.get("host") or "").strip()
        action = str(args.get("action") or "").strip()
        path = str(args.get("path") or "").strip()
        reason = str(args.get("reason") or "").strip()
        if not host or not action or not path:
            return "ERROR: Required arguments: host, action, path"
        text, ask_id = self._blocking_ask(
            f"{host}:local",
            {
                "from": self.addr,
                "action": action,
                "path": path,
                "reason": reason,
                "question": f"Allow {action} on {path}?\nReason: {reason}",
            },
        )
        if ask_id is not None:
            self.consent_id = ask_id
        return text

    def confirm(self, args: dict) -> str:
        questions = _normalize_questions(args)
        if not questions:
            return "ERROR: Missing required argument: question"
        text, _ask_id = self._blocking_ask(
            f"{self.host}:local", {"from": self.addr, "questions": questions}
        )
        return text

    # ── guarded fs tools ──

    def read_file(self, args: dict) -> str:
        return self._fs("read", str(args.get("path") or ""), consent_id=self.consent_id)

    def write_file(self, args: dict) -> str:
        return self._fs(
            "write",
            str(args.get("path") or ""),
            content=str(args.get("content") or ""),
            consent_id=self.consent_id,
        )

    def edit_file(self, args: dict) -> str:
        return self._fs(
            "edit",
            str(args.get("path") or ""),
            old_string=str(args.get("old_string") or ""),
            new_string=str(args.get("new_string") or ""),
            consent_id=self.consent_id,
        )

    def glob_files(self, args: dict) -> str:
        return self._fs(
            "glob", str(args.get("path") or ""), pattern=str(args.get("pattern") or "**/*")
        )

    def grep_files(self, args: dict) -> str:
        return self._fs("grep", str(args.get("path") or ""), pattern=str(args.get("pattern") or ""))

    def fs_bound(self) -> dict[str, BoundTool]:
        """Guarded fs tools only — the surface spawned workers inherit."""
        return {
            name: bound
            for name, bound in self.bound().items()
            if name in {"read_file", "write_file", "edit_file", "glob_files", "grep_files"}
        }

    # ── registration ──

    def bound(self) -> dict[str, BoundTool]:
        return {
            "send_peer": BoundTool(schema=_SCHEMA_SEND_PEER, fn=self.send_peer),
            "send_file": BoundTool(schema=_SCHEMA_SEND_FILE, fn=self.send_file),
            "ask_consent": BoundTool(schema=_SCHEMA_ASK_CONSENT, fn=self.ask_consent),
            "confirm": BoundTool(schema=_SCHEMA_CONFIRM, fn=self.confirm),
            "read_file": BoundTool(schema=_SCHEMA_READ, fn=self.read_file),
            "write_file": BoundTool(schema=_SCHEMA_WRITE, fn=self.write_file),
            "edit_file": BoundTool(schema=_SCHEMA_EDIT, fn=self.edit_file),
            "glob_files": BoundTool(schema=_SCHEMA_GLOB, fn=self.glob_files),
            "grep_files": BoundTool(schema=_SCHEMA_GREP, fn=self.grep_files),
        }


def _str_schema(name: str, description: str, required: bool = True) -> dict:
    return {
        "type": "string",
        "description": description,
        **({"required": [name]} if required else {}),
    }


def _obj_schema(properties: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": properties.pop("__name"),
            "description": properties.pop("__desc"),
            "parameters": {"type": "object", "properties": properties, "required": required},
        },
    }


_SCHEMA_SEND_PEER = _obj_schema(
    {
        "__name": "send_peer",
        "__desc": "Send a chat message to your counterpart comm Orchestrator on the peer host.",
        "text": _str_schema("text", "message body"),
    },
    ["text"],
)
_SCHEMA_SEND_FILE = _obj_schema(
    {
        "__name": "send_file",
        "__desc": (
            "Transfer a server-stored file to the peer host's local inbox. "
            "The receiving user must accept before it lands on their disk."
        ),
        "host": _str_schema("host", "destination host name"),
        "path": _str_schema("path", "source path relative to data/, e.g. public/x.pdf"),
        "name": _str_schema("name", "file name to save as (default: source name)", required=False),
        "reason": _str_schema("reason", "why the peer needs this file", required=False),
    },
    ["host", "path"],
)
_SCHEMA_ASK_CONSENT = _obj_schema(
    {
        "__name": "ask_consent",
        "__desc": "Ask the owning host's user for consent before touching non-public files. Blocks until answered.",
        "host": _str_schema("host", "host that owns the target directory"),
        "action": _str_schema("action", "what to do: read/write/edit"),
        "path": _str_schema("path", "relative path under data/, e.g. homes/<host>/x.md"),
        "reason": _str_schema("reason", "why this is needed", required=False),
    },
    ["host", "action", "path"],
)
_SCHEMA_CONFIRM = _obj_schema(
    {
        "__name": "confirm",
        "__desc": "Ask your own host's user a question via system notification + WebUI card. Blocks until answered.",
        "question": _str_schema("question", "the question", required=False),
        "options": {"type": "array", "description": "optional answer options"},
        "allow_custom": {
            "type": "boolean",
            "description": "allow a free-text answer (default true)",
        },
        "questions": {"type": "array", "description": "multiple questions at once"},
    },
    [],
)
_SCHEMA_READ = _obj_schema(
    {
        "__name": "read_file",
        "__desc": "Read a server-stored file (path relative to data/).",
        "path": _str_schema("path", "relative path"),
    },
    ["path"],
)
_SCHEMA_WRITE = _obj_schema(
    {
        "__name": "write_file",
        "__desc": "Write a server-stored file (path relative to data/).",
        "path": _str_schema("path", "relative path"),
        "content": _str_schema("content", "full file content"),
    },
    ["path", "content"],
)
_SCHEMA_EDIT = _obj_schema(
    {
        "__name": "edit_file",
        "__desc": "Unique-match replace in a server-stored file.",
        "path": _str_schema("path", "relative path"),
        "old_string": _str_schema("old_string", "exact text to replace (must be unique)"),
        "new_string": _str_schema("new_string", "replacement text"),
    },
    ["path", "old_string", "new_string"],
)
_SCHEMA_GLOB = _obj_schema(
    {
        "__name": "glob_files",
        "__desc": "Glob under public/ or your own home only.",
        "path": _str_schema("path", "search root: public/ or homes/<own>/"),
        "pattern": _str_schema("pattern", "glob pattern", required=False),
    },
    ["path"],
)
_SCHEMA_GREP = _obj_schema(
    {
        "__name": "grep_files",
        "__desc": "Regex search under public/ or your own home only.",
        "path": _str_schema("path", "search root: public/ or homes/<own>/"),
        "pattern": _str_schema("pattern", "regex"),
    },
    ["path", "pattern"],
)
