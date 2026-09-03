"""Hub HTTP API: join/leave/heartbeat/send/poll + fs + sessions (token auth)."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ..protocol import Envelope, ProtocolError, deserialize, parse_addr
from .asks import Asks
from .relay import Relay
from .roster import Roster
from .store import GuardError, Store

MAX_BODY = 5 * 1024 * 1024
MAX_POLL = 25.0
ASK_TIMEOUT = 600.0
REAP_INTERVAL = 5.0


def fs_via_hub(
    store: Store,
    host: str,
    op: str,
    path: str,
    *,
    content: str | None = None,
    old_string: str | None = None,
    new_string: str | None = None,
    pattern: str | None = None,
    consent_id: str | None = None,
) -> dict:
    """Shared fs dispatch for the HTTP handler and server-local clones."""
    try:
        if op in {"glob", "grep"}:
            root = store.check_search_root(host, path)
            if op == "glob":
                result = store.glob(root, pattern or "*")
            else:
                result = store.grep(root, pattern or "")
        else:
            target = store.resolve(
                host, path, consent_id=consent_id, mutating=op in {"write", "edit"}
            )
            if op == "ls":
                result = store.ls(target)
            elif op == "read":
                result = store.read(target)
            elif op == "write":
                result = store.write(target, content or "")
            elif op == "edit":
                result = store.edit(target, old_string or "", new_string or "")
            else:
                return {"error": f"unknown op: {op}"}
        return {"ok": True, "result": result}
    except GuardError as exc:
        return {"error": str(exc)}


class Hub:
    """Room server: roster + relay + asks + store behind one HTTP API."""

    def __init__(self, name: str, token: str, data_root: Path, heartbeat_timeout: float = 30.0):
        self.name = name
        self.token = token
        self.roster = Roster(heartbeat_timeout)
        self.relay = Relay(name)
        self.asks = Asks()
        self.store = Store(Path(data_root), self.asks)
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._reaper: threading.Thread | None = None
        self._stop = threading.Event()

    @property
    def port(self) -> int:
        if self._server is None:
            raise RuntimeError("hub not started")
        return self._server.server_address[1]

    def start(self) -> None:
        handler = type("HubHandler", (_Handler,), {"hub": self})
        self._server = ThreadingHTTPServer(("0.0.0.0", 0), handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="fungi-hub", daemon=True
        )
        self._thread.start()
        self._reaper = threading.Thread(target=self._reap_loop, name="fungi-reaper", daemon=True)
        self._reaper.start()

    def stop(self) -> None:
        self._stop.set()
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def _reap_loop(self) -> None:
        while not self._stop.wait(REAP_INTERVAL):
            for name in self.roster.reap():
                self.relay.drop_host(name)
            self.asks.sweep(ASK_TIMEOUT)

    # ── operations shared by handler, clones, and tests ──

    def join(self, name: str, addr: str) -> dict:
        new = self.roster.join(name, addr)
        self.store.ensure_home(name)
        self.relay.host_buffer(name)
        return {"ok": True, "host": name, "new": new, "peers": self.roster.peers(name)}

    def send(self, env: Envelope) -> dict:
        # ask/answer envelopes maintain the consent registry transparently:
        # ask -> opened with the envelope id (so consent_id == envelope id),
        # answer -> resolves the referenced ask.
        if env.type == "ask":
            host, _role, _peer = parse_addr(env.dst)
            self.asks.open(host, env.body, ask_id=env.id, src=env.src)
        elif env.type == "answer" and env.reply_to:
            self.asks.resolve(env.reply_to, value=env.body.get("value"))
        status = self.relay.deliver(env)
        return {"ok": status != "bounced", "status": status}


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    hub: Hub = None  # type: ignore[assignment]

    def log_message(self, format: str, *args) -> None:
        pass  # quiet

    # ── plumbing ──

    def _reply(self, obj: dict, code: int = 200) -> None:
        data = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise ValueError("body too large")
        raw = self.rfile.read(length) if length else b"{}"
        data = json.loads(raw or b"{}")
        if not isinstance(data, dict):
            raise ValueError("body must be an object")
        return data

    # ── routing ──

    def do_POST(self) -> None:
        try:
            body = self._body()
            if body.get("token") != self.hub.token:
                self._reply({"error": "bad token"}, 403)
                return
            path = urlparse(self.path).path
            if path == "/api/join":
                self._join(body)
            elif path == "/api/leave":
                self._leave(body)
            elif path == "/api/heartbeat":
                self._heartbeat(body)
            elif path == "/api/send":
                self._send(body)
            elif path.startswith("/api/fs/"):
                self._fs(path[len("/api/fs/") :], body)
            elif path == "/api/save":
                self._save(body)
            elif path == "/api/session/delete":
                self._session_delete(body)
            else:
                self._reply({"error": "not found"}, 404)
        except (ValueError, json.JSONDecodeError) as exc:
            self._reply({"error": str(exc)}, 400)

    def do_GET(self) -> None:
        url = urlparse(self.path)
        params = parse_qs(url.query)
        token = (params.get("token") or [""])[0]
        if token != self.hub.token:
            self._reply({"error": "bad token"}, 403)
            return
        if url.path == "/api/poll":
            self._poll(params)
        elif url.path == "/api/sessions":
            self._reply({"sessions": self.hub.store.sessions.list_sessions()})
        elif url.path == "/api/session":
            self._session_load(params)
        else:
            self._reply({"error": "not found"}, 404)

    # ── room ops ──

    def _join(self, body: dict) -> None:
        name = body.get("name")
        if not isinstance(name, str) or not name or "/" in name:
            self._reply({"error": "bad name"}, 400)
            return
        self._reply(self.hub.join(name, self.client_address[0]))

    def _leave(self, body: dict) -> None:
        name = body.get("name")
        if self.hub.roster.leave(str(name)):
            self.hub.relay.drop_host(str(name))
        self._reply({"ok": True})

    def _heartbeat(self, body: dict) -> None:
        name = str(body.get("name"))
        if not self.hub.roster.beat(name):
            self._reply({"error": "unknown host"}, 404)
            return
        self._reply(
            {
                "ok": True,
                "peers": self.hub.roster.peers(name),
                "pending_asks": self.hub.asks.pending_for(name),
            }
        )

    def _send(self, body: dict) -> None:
        try:
            env = deserialize(body.get("envelope") or {})
        except ProtocolError as exc:
            self._reply({"error": str(exc)}, 400)
            return
        self._reply(self.hub.send(env))

    def _poll(self, params: dict) -> None:
        host = (params.get("host") or [""])[0]
        if not self.hub.roster.known(host):
            self._reply({"error": "unknown host"}, 404)
            return
        after = int((params.get("after") or ["0"])[0])
        timeout = min(float((params.get("timeout") or ["0"])[0]), MAX_POLL)
        inbox = self.hub.relay.host_buffer(host)
        messages, cursor = inbox.after(after, timeout)
        self._reply({"messages": [e.serialize() for e in messages], "cursor": cursor})

    # ── fs ops ──

    def _fs(self, op: str, body: dict) -> None:
        host = str(body.get("host") or "")
        if not self.hub.roster.known(host):
            self._reply({"error": "unknown host"}, 403)
            return
        out = fs_via_hub(
            self.hub.store,
            host,
            op,
            str(body.get("path") or ""),
            content=body.get("content"),
            old_string=body.get("old_string"),
            new_string=body.get("new_string"),
            pattern=body.get("pattern"),
            consent_id=body.get("consent_id"),
        )
        self._reply(out, 403 if "error" in out else 200)

    # ── sessions ──

    def _save(self, body: dict) -> None:
        store = self.hub.store.sessions
        store.save(
            str(body.get("id") or ""),
            str(body.get("title") or ""),
            body.get("messages") or [],
            body.get("subagents"),
            body.get("asks"),
        )
        self._reply({"ok": True})

    def _session_load(self, params: dict) -> None:
        sid = (params.get("id") or [""])[0]
        data = self.hub.store.sessions.load(sid)
        if data is None:
            self._reply({"error": "not found"}, 404)
            return
        self._reply(data)

    def _session_delete(self, body: dict) -> None:
        self.hub.store.sessions.delete(str(body.get("id") or ""))
        self._reply({"ok": True})
