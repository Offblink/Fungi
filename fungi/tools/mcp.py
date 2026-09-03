"""MCP client: Model Context Protocol over the stdio transport.

Launches configured MCP servers as subprocesses and speaks newline-delimited
JSON-RPC 2.0 (https://modelcontextprotocol.io/specification). Uses the legacy
`initialize` handshake (revision 2025-06-18), which every deployed server
speaks; the 2026-07-28 spec explicitly keeps servers backward compatible with
it. Each server tool is exposed to the agent as a `mcp__<server>__<tool>`
BoundTool. Server processes are cached module-wide and reused across turns;
a dead process is reconnected (fresh handshake) on next use.
"""

import atexit
import itertools
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field

from fungi.agent import BoundTool

PROTOCOL_VERSION = "2025-06-18"  # latest initialize-handshake revision
HANDSHAKE_TIMEOUT_S = 20.0
TOOL_TIMEOUT_S = 120.0
SHUTDOWN_WAIT_S = 3.0

CLIENT_INFO = {"name": "fungi", "version": "0.1.0"}


class McpError(Exception):
    """MCP transport or protocol failure."""


class McpTimeoutError(McpError):
    """Request timed out; a notifications/cancelled was sent."""


@dataclass
class _Conn:
    proc: subprocess.Popen
    lines: queue.Queue = field(default_factory=queue.Queue)
    reader: threading.Thread | None = None
    eof: bool = False  # set by the reader thread at stdout EOF


def _sanitize(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", name)


def tool_name(server: str, tool: str) -> str:
    return f"mcp__{_sanitize(server)}__{_sanitize(tool)}"


def _content_to_text(content: list) -> str:
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            parts.append(str(item))
            continue
        kind = item.get("type")
        if kind == "text":
            parts.append(str(item.get("text") or ""))
        elif kind == "image":
            parts.append(f"[image: {item.get('mimeType', 'unknown')}]")
        elif kind == "audio":
            parts.append(f"[audio: {item.get('mimeType', 'unknown')}]")
        elif kind == "resource_link":
            parts.append(f"[link: {item.get('name') or item.get('uri', '')}] {item.get('uri', '')}")
        elif kind == "resource":
            res = item.get("resource") or {}
            parts.append(f"[resource: {res.get('uri', '')}] {res.get('text', '')}".rstrip())
        else:
            parts.append(f"[unsupported content type: {kind}]")
    return "\n".join(parts)


class McpServer:
    """One MCP server subprocess with a serialized request/response channel."""

    def __init__(self, name: str, spec: dict) -> None:
        self.name = name
        self.spec = spec
        self._lock = threading.Lock()
        self._conn: _Conn | None = None
        self._ids = itertools.count(1)
        self.protocol_version = PROTOCOL_VERSION

    # ---- lifecycle ---------------------------------------------------------

    def _start(self) -> _Conn:
        command = str(self.spec.get("command") or "").strip()
        if not command:
            raise McpError("missing 'command' in server config")
        args = [str(a) for a in (self.spec.get("args") or [])]
        env = os.environ | {str(k): str(v) for k, v in (self.spec.get("env") or {}).items()}
        try:
            proc = subprocess.Popen(
                [command, *args],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                env=env,
            )
        except OSError as exc:
            raise McpError(f"cannot launch {command!r}: {exc}") from exc
        conn = _Conn(proc=proc)
        conn.reader = threading.Thread(target=self._read_loop, args=(conn,), daemon=True)
        conn.reader.start()
        return conn

    def _read_loop(self, conn: _Conn) -> None:
        assert conn.proc.stdout is not None
        for line in conn.proc.stdout:
            conn.lines.put(line)
        conn.eof = True
        conn.lines.put(None)

    def _ensure_started(self) -> _Conn:
        # poll() alone races a just-exited child on Windows: treat observed
        # stdout EOF as death too.
        if self._conn is not None and self._conn.proc.poll() is None and not self._conn.eof:
            return self._conn
        conn = self._start()
        self._conn = conn
        self._handshake()
        return conn

    def _handshake(self) -> None:
        result = self._request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": CLIENT_INFO,
            },
            timeout=HANDSHAKE_TIMEOUT_S,
        )
        # Subsequent messages use the version the server answered with.
        self.protocol_version = str(result.get("protocolVersion") or PROTOCOL_VERSION)
        self._notify("notifications/initialized")

    def close(self) -> None:
        with self._lock:
            conn, self._conn = self._conn, None
            if conn is None:
                return
            try:
                if conn.proc.stdin is not None:
                    conn.proc.stdin.close()
                conn.proc.wait(timeout=SHUTDOWN_WAIT_S)
            except Exception:
                conn.proc.kill()

    # ---- wire --------------------------------------------------------------

    def _send(self, msg: dict) -> None:
        conn = self._conn
        if conn is None or conn.proc.stdin is None:
            raise McpError("server not running")
        try:
            conn.proc.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
            conn.proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise McpError(f"server pipe closed: {exc}") from exc

    def _notify(self, method: str, params: dict | None = None) -> None:
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self._send(msg)

    def _request(self, method: str, params: dict | None, timeout: float) -> dict:
        conn = self._conn
        if conn is None:
            raise McpError("server not running")
        rid = next(self._ids)
        request: dict = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            request["params"] = params
        try:
            self._send(request)
        except McpError as exc:
            # A dead child usually surfaces as a broken write on Windows.
            raise McpError(f"server exited during {method} (write failed: {exc})") from exc
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._notify(
                    "notifications/cancelled", {"requestId": rid, "reason": "client timeout"}
                )
                raise McpTimeoutError(f"{method} timed out after {timeout:g}s")
            try:
                line = conn.lines.get(timeout=remaining)
            except queue.Empty:
                continue
            if line is None:
                raise McpError(f"server exited during {method}")
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue  # spec: server MUST NOT write non-MCP to stdout; skip anyway
            if msg.get("id") != rid or "method" in msg:
                continue  # notification or response to another request
            if "error" in msg:
                raise McpError(f"{method} failed: {msg['error']}")
            return msg.get("result") or {}

    # ---- public API (thread-safe: one request at a time per server) --------

    def list_tools(self) -> list[dict]:
        """All server tools, following the pagination cursor."""
        with self._lock:
            self._ensure_started()
            out: list[dict] = []
            cursor: str | None = None
            for _ in range(100):  # pagination hard cap
                params = {"cursor": cursor} if cursor else None
                result = self._request("tools/list", params, timeout=HANDSHAKE_TIMEOUT_S)
                out.extend(t for t in result.get("tools") or [] if isinstance(t, dict))
                cursor = result.get("nextCursor")
                if not cursor:
                    return out
            return out

    def call_tool(self, name: str, arguments: dict, timeout: float = TOOL_TIMEOUT_S) -> str:
        """Invoke a tool; returns its text content (prefixed 'ERROR:' on isError)."""
        with self._lock:
            self._ensure_started()
            try:
                result = self._request(
                    "tools/call", {"name": name, "arguments": arguments}, timeout=timeout
                )
            except McpError as exc:
                detail = str(exc)
                prefix = "tools/call failed:"
                if detail.startswith(prefix):
                    detail = f"{name} failed:{detail[len(prefix) :]}"
                raise type(exc)(detail) from exc
            text = _content_to_text(result.get("content") or [])
            if result.get("isError"):
                return f"ERROR: {text}" if text else "ERROR: tool reported failure"
            return text or "(no content)"


_atexit_registered = [False]


# ---- module-wide server cache ----------------------------------------------

_servers: dict[str, McpServer] = {}
_servers_lock = threading.Lock()


def _get_server(name: str, spec: dict) -> McpServer:
    with _servers_lock:
        server = _servers.get(name)
        if not _atexit_registered[0]:
            atexit.register(shutdown_servers)
            _atexit_registered[0] = True
        if server is None or server.spec != spec:
            if server is not None:
                server.close()
            server = McpServer(name, spec)
            _servers[name] = server
        return server


def mcp_extra_tools(mcp_servers: dict[str, dict]) -> dict[str, BoundTool]:
    """BoundTools for every configured MCP server's tools (empty if none).

    A server that fails to launch or enumerate is skipped with a stderr note;
    one broken server must not take the whole turn down.
    """
    out: dict[str, BoundTool] = {}
    for server_name, spec in mcp_servers.items():
        if not isinstance(spec, dict):
            continue
        try:
            server = _get_server(server_name, spec)
            for tool in server.list_tools():
                raw = str(tool.get("name") or "").strip()
                if not raw:
                    continue
                name = tool_name(server_name, raw)
                if name in out:
                    continue  # sanitized-name collision: first server wins
                out[name] = _make_bound(server_name, raw, tool, server)
        except Exception as exc:
            print(f"[mcp] server {server_name!r} unavailable: {exc}", file=sys.stderr)
    return out


def _make_bound(server_name: str, tool_name_raw: str, tool: dict, server: McpServer) -> BoundTool:
    desc = str(tool.get("description") or tool_name_raw).strip()
    schema = {
        "type": "function",
        "function": {
            "name": tool_name(server_name, tool_name_raw),
            "description": f"{desc} (MCP server: {server_name})",
            "parameters": tool.get("inputSchema") or {"type": "object", "properties": {}},
        },
    }

    def fn(args: dict, _server=server, _tool=tool_name_raw) -> str:
        return _server.call_tool(_tool, args if isinstance(args, dict) else {})

    return BoundTool(schema=schema, fn=fn)


def shutdown_servers() -> None:
    """Terminate all cached MCP server processes (called at exit)."""
    with _servers_lock:
        for server in _servers.values():
            server.close()
        _servers.clear()
