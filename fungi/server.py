"""WebUI server: ThreadingHTTPServer + NDJSON streaming + static files from web/.

Single-host mode keeps the original YESIR behavior (TriLayer in the request
thread, local session files). Room mode (fungi/room.py) injects a WebUIRuntime
that runs the local clone's toolset, backs sessions per host (server: hub
store; client: its own disk — never the peer-operated hub), and routes
card answers back out as answer envelopes.
"""

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from fungi import session
from fungi.agent import SYSTEM_PROMPT, Agent
from fungi.config import RESOURCE_ROOT, load_config, save_config
from fungi.events import Sink
from fungi.llm import stream_chat
from fungi.tools.ask import resolve_ask
from fungi.tools.mcp import mcp_extra_tools
from fungi.trilayer import TriLayer

WEB_DIR = RESOURCE_ROOT / "web"

_mime = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
}


RETRY_STRIP_PREFIXES = ("(LLM error:", "(Hit max tool rounds", "(Aborted")


def sanitize_for_retry(messages: list[dict]) -> list[dict]:
    """Drop the synthetic tail a failed turn left behind, so Alt+R continues
    from real context. Marker lines are recognized anywhere in the tail block;
    everything from the first marker on is discarded (tool calls without their
    results would poison the next completion)."""
    out = list(messages)
    while out:
        last = out[-1]
        content = last.get("content")
        if isinstance(content, str) and content.startswith(RETRY_STRIP_PREFIXES):
            out.pop()
            continue
        if last.get("role") == "assistant" and last.get("content") is None:
            out.pop()  # dangling tool_calls
            continue
        break
    return out


def repair_tool_gaps(messages: list[dict]) -> list[dict]:
    """Ensure every assistant tool_call is followed by a tool result.

    A turn that died between appending tool_calls and their results used to
    poison the saved session: the next completion request fails with HTTP 400
    (tool_calls must be answered), and the conversation is bricked from there
    on. Synthesize an explicit failure result for any unanswered call.
    """
    out: list[dict] = []
    unanswered: dict[str, str] = {}  # tool_call_id -> tool name
    for m in messages:
        role = m.get("role")
        if role == "assistant" and m.get("tool_calls"):
            out.append(m)
            for tc in m["tool_calls"]:
                unanswered[str(tc.get("id"))] = str(
                    (tc.get("function") or {}).get("name") or "tool"
                )
            continue
        if role == "tool":
            unanswered.pop(str(m.get("tool_call_id")), None)
            out.append(m)
            continue
        if unanswered and role in ("user", "assistant"):
            # History gap: answer the dangling calls before moving on.
            for call_id, name in unanswered.items():
                out.append(
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": f"ERROR: turn was interrupted before {name} could run.",
                    }
                )
            unanswered = {}
        out.append(m)
    for call_id, name in unanswered.items():
        out.append(
            {
                "role": "tool",
                "tool_call_id": call_id,
                "content": f"ERROR: turn was interrupted before {name} could run.",
            }
        )
    return out


class WebUIRuntime:
    """Turn/sessions/answer wiring for the WebUI. Default = single-host mode."""

    # Monotonic timestamp of the last WebUI HTTP request: the "is anyone
    # actually looking" signal. ask notifications fire only when this is
    # stale (nobody has the page open). Class attribute because RoomRuntime
    # does not chain __init__; touch() shadows it per instance.
    last_seen: float = 0.0

    def touch(self) -> None:
        self.last_seen = time.monotonic()

    def sessions_list(self) -> list[dict]:
        return session.list_sessions()

    def sessions_load(self, session_id: str) -> dict | None:
        return session.load_session(session_id)

    def sessions_save(
        self,
        session_id: str,
        title: str,
        messages: list[dict],
        subagents: list | None = None,
        asks: list | None = None,
    ) -> None:
        session.save_session(session_id, title, messages, subagents=subagents, asks=asks)

    def sessions_delete(self, session_id: str) -> None:
        session.delete_session(session_id)

    def new_session_id(self) -> str:
        return session.new_session_id()

    def new_session_prompt(self) -> str:
        return SYSTEM_PROMPT

    def build_agent(self, sink: Sink, should_abort) -> Agent:
        return TriLayer(load_config(), sink, should_abort=should_abort).build_orchestrator(sink)

    def kaomoji_reply(self, user_msg: str, last_reply: str | None) -> str | None:
        """Default: the configured model, tiny call. Room mode injects its llm."""
        cfg = load_config()
        if not cfg.kaomoji:
            return None
        return _kaomoji_call(cfg, None, user_msg, last_reply)

    def route_answer(self, ask_id: str, value: str | list[str]) -> bool:
        """Resolve an /answer submission. Default: in-process inquire only."""
        return resolve_ask(ask_id, value)

    def pending_asks(self) -> list[dict]:
        """Out-of-band asks awaiting a card answer (room mode: envelope asks)."""
        return []

    def peers(self) -> list[str]:
        """Other hosts currently in the room (room mode)."""
        return []

    def comm_log(self, host: str) -> dict:  # noqa: ARG002 (room mode overrides)
        """Friend view payload (room mode returns the real transcript)."""
        return {"messages": [], "subagents": [], "asks": [], "events": []}

    def consent_mode(self, host: str) -> str:  # noqa: ARG002 (room mode overrides)
        """Per-friend consent mode: "allow" or "ask" (room mode)."""
        return "ask"

    def set_consent_mode(self, host: str, mode: str) -> None:
        pass

    def mcp_tools(self) -> dict:
        return mcp_extra_tools(load_config().mcp_servers)


# Interrupt support: one Event per running turn, keyed by session id. /stop
# sets them; the agent checks between rounds and on every SSE line read.
_ACTIVE_TURNS: dict[str, set[threading.Event]] = {}
_TURNS_LOCK = threading.Lock()

# One writer per session: a same-session turn started while another is still
# finishing would otherwise overwrite its saved context (last-writer-wins).
_SESSION_LOCKS: dict[str, threading.Lock] = {}
_SESSION_LOCKS_GUARD = threading.Lock()

# Tombstones for sessions deleted while a turn was still running: the turn's
# exit-path save must not resurrect the file the user just deleted.
_TURN_DELETED: set[str] = set()

# Per-turn event tapes: the WebSink records every event it emits so a client
# that reloads mid-turn can reattach via /events and replay what it missed.
# A tape lives until its turn's done marker is consumed / grace-popped.
_TURN_TAPES: dict[str, list[dict]] = {}


def _session_lock(session_id: str) -> threading.Lock:
    with _SESSION_LOCKS_GUARD:
        return _SESSION_LOCKS.setdefault(session_id, threading.Lock())


KAOMOJI_PROMPT = (
    "Reply with exactly ONE kaomoji (a Japanese-style text emoticon built from "
    "punctuation marks and symbols) that expresses your immediate gut reaction "
    "to what the user just said — the face you make when you hear it. Invent "
    "it yourself; do not pick from a list. No words, no explanation, 2-12 "
    "characters, single line."
)
# The mood call runs in parallel and often finishes after the /chat stream
# closed; a done client polls /kaomoji with this wait budget instead of
# losing the decoration on every short turn.
KAOMOJI_FETCH_WAIT_S = 10


def _kaomoji_call(cfg, llm, user_msg: str, last_reply: str | None) -> str | None:
    """One tiny completion: the mood kaomoji, or None if the reply is unusable."""
    messages: list[dict] = [{"role": "system", "content": KAOMOJI_PROMPT}]
    if last_reply:
        messages.append({"role": "assistant", "content": str(last_reply)})
    messages.append({"role": "user", "content": str(user_msg)})
    if llm is not None:
        result = llm(messages, [])
    else:
        # No max_tokens cap: the prompt alone constrains the reply to one
        # short kaomoji, and reasoning models spend budget on thinking first
        # — a tight cap trips finish_reason=length with empty content.
        result = stream_chat(cfg.model, cfg.endpoint, cfg.api_key, messages, [])
    text = (result.content or "").strip()
    return text or None


def _kaomoji_worker(sink: "WebSink", runtime, messages: list[dict]) -> None:
    """Emit the turn's mood kaomoji; a decoration must never break the turn."""
    try:
        last_user = ""
        last_reply = None
        for m in reversed(messages):
            role = m.get("role")
            if role == "user" and not last_user:
                last_user = m.get("content") or ""
            elif role == "assistant" and last_user:
                last_reply = m.get("content")
                break
        if not last_user:
            return
        text = runtime.kaomoji_reply(last_user, last_reply)
        if text:
            sink.emit("kaomoji", text)
    except Exception:
        pass  # decoration: any failure just leaves the brand name as-is


class WebSink:
    """Thread-safe NDJSON writer over the /chat response stream."""

    def __init__(self, handler: "YesSirHandler", session_id: str | None = None):
        self.handler = handler
        self.session_id = session_id
        self.closed = False

    def emit(self, kind: str, content) -> None:
        if self.session_id is not None and kind != "done":
            # Record for /events reattach; the done marker is appended by the
            # turn's exit path so replay consumers never miss tail events.
            with _TURNS_LOCK:
                tape = _TURN_TAPES.get(self.session_id)
                if tape is not None:
                    tape.append({"type": kind, "content": content})
        if self.closed:
            return
        try:
            data = json.dumps({"type": kind, "content": content}, ensure_ascii=False)
            self.handler.wfile.write((data + "\n").encode("utf-8"))
            self.handler.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            self.closed = True


class YesSirHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    runtime: WebUIRuntime = None  # type: ignore[assignment]

    # ---- plumbing ---------------------------------------------------------
    def log_message(self, fmt, *args):  # quiet
        pass

    def _send_json(self, payload, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    def _send_static(self, filename: str) -> None:
        path = WEB_DIR / filename
        if not path.is_file():
            self._send_json({"error": "not found"}, status=404)
            return
        body = path.read_bytes()
        mime = _mime.get(path.suffix, "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # ---- GET --------------------------------------------------------------
    def do_GET(self):
        self.runtime.touch()  # anyone still polling = someone is looking
        url = urlparse(self.path)
        route = url.path
        if route == "/":
            self._send_static("index.html")
        elif route in ("/app.js", "/style.css", "/motion.js"):
            self._send_static(route.lstrip("/"))
        elif (
            route.startswith("/vendor/") and "/" not in route[8:] and ".." not in route
        ):  # flat vendor dir; no traversal
            self._send_static(route[1:])  # web/vendor/<file> — keep the dir prefix
        elif route == "/model":
            self._send_json({"model": load_config().model})
        elif route == "/config-status":
            self._send_json({"configured": load_config().configured})
        elif route == "/asks":
            self._send_json({"asks": self.runtime.pending_asks()})
        elif route == "/sessions":
            sessions = self.runtime.sessions_list()
            with _TURNS_LOCK:
                for s in sessions:
                    tape = _TURN_TAPES.get(str(s.get("id")))
                    s["running"] = tape is not None and not any(
                        ev.get("type") == "done" for ev in tape
                    )
            self._send_json({"sessions": sessions})
        elif route == "/session":
            session_id = (parse_qs(url.query).get("id") or [None])[0]
            data = self.runtime.sessions_load(session_id) if session_id else None
            if data is None:
                self._send_json({"error": "not found"}, status=404)
            else:
                self._send_json(data)
        elif route == "/peers":
            self._send_json({"peers": self.runtime.peers()})
        elif route == "/consent-mode":
            host = (parse_qs(url.query).get("host") or [None])[0]
            if not host:
                self._send_json({"error": "missing host"}, status=400)
            else:
                self._send_json({"mode": self.runtime.consent_mode(host)})
        elif route == "/comm-log":
            host = (parse_qs(url.query).get("host") or [None])[0]
            if not host:
                self._send_json({"error": "missing host"}, status=400)
            else:
                # comm_log already returns the full friend-view payload
                # {messages, subagents, asks, events}; re-wrapping it under
                # "messages" handed the frontend an object where it expects
                # an array, and the render threw into the swallowed catch —
                # the friend view stayed blank forever.
                self._send_json(self.runtime.comm_log(host))
        elif route == "/events":
            self._handle_events((parse_qs(url.query).get("sessionId") or [None])[0])
        elif route == "/kaomoji":
            self._handle_kaomoji((parse_qs(url.query).get("sessionId") or [None])[0])
        else:
            self._send_json({"error": "not found"}, status=404)

    def _handle_kaomoji(self, session_id: str | None) -> None:
        """Latest mood kaomoji for a session, waiting briefly for a late one."""
        if not session_id:
            self._send_json({"kaomoji": None})
            return
        deadline = time.monotonic() + KAOMOJI_FETCH_WAIT_S
        while True:
            tape = _TURN_TAPES.get(session_id) or []
            for ev in reversed(tape):
                if ev.get("type") == "kaomoji" and ev.get("content"):
                    self._send_json({"kaomoji": ev["content"]})
                    return
            if time.monotonic() >= deadline:
                break
            time.sleep(0.2)
        self._send_json({"kaomoji": None})

    # ---- POST -------------------------------------------------------------
    def do_POST(self):
        self.runtime.touch()
        url = urlparse(self.path)
        if url.path == "/chat":
            self._handle_chat()
        elif url.path == "/retry":
            self._handle_retry()
        elif url.path == "/stop":
            data = self._read_body()
            sid = str(data.get("sessionId") or "")
            with _TURNS_LOCK:
                events = _ACTIVE_TURNS.pop(sid, set())
            for event in events:
                event.set()
            self._send_json({"ok": bool(events)})
        elif url.path == "/answer":
            data = self._read_body()
            value = data.get("value")
            if isinstance(value, list):
                value = [str(v) for v in value]
            else:
                value = str(value or "")
            ok = self.runtime.route_answer(str(data.get("id") or ""), value)
            self._send_json({"ok": ok}, status=200 if ok else 404)
        elif url.path == "/configure":
            data = self._read_body()
            cfg = load_config()
            if data.get("api_key"):
                cfg.api_key = data["api_key"]
            if data.get("endpoint"):
                cfg.endpoint = data["endpoint"]
            if data.get("model"):
                cfg.model = data["model"]
            save_config(cfg)
            self._send_json({"ok": True})
        elif url.path == "/save":
            data = self._read_body()
            existing = self.runtime.sessions_load(data.get("id", ""))
            if existing is None:
                self._send_json({"ok": False}, status=400)
                return
            self.runtime.sessions_save(
                data["id"],
                data.get("title") or existing.get("title") or "",
                existing.get("messages", []),
                subagents=existing.get("subagents", []),
                asks=existing.get("asks", []),
            )
            self._send_json({"ok": True})
        elif url.path == "/new":
            session_id = self.runtime.new_session_id()
            self.runtime.sessions_save(
                session_id,
                "(new session)",
                [{"role": "system", "content": self.runtime.new_session_prompt()}],
            )
            self._send_json({"id": session_id, "title": "(new session)"})
        elif url.path == "/consent-mode":
            data = self._read_body()
            host = str(data.get("host") or "")
            mode = str(data.get("mode") or "")
            if not host or mode not in ("allow", "ask"):
                self._send_json({"error": "need host and mode (allow|ask)"}, status=400)
            else:
                self.runtime.set_consent_mode(host, mode)
                self._send_json({"ok": True, "mode": mode})
        elif url.path == "/pickfile":
            self._handle_pickfile()
        else:
            self._send_json({"error": "not found"}, status=404)

    # ---- DELETE -----------------------------------------------------------
    def do_DELETE(self):
        url = urlparse(self.path)
        if url.path == "/session":
            session_id = (parse_qs(url.query).get("id") or [None])[0]
            if session_id:
                with _TURNS_LOCK:
                    events = _ACTIVE_TURNS.pop(session_id, set())
                    if events:
                        # A turn still runs in this session: abort it and
                        # tombstone the id so its exit-path save cannot
                        # resurrect the file the user just deleted.
                        _TURN_DELETED.add(session_id)
                for event in events:
                    event.set()
                self.runtime.sessions_delete(session_id)
            self._send_json({"ok": True})
        else:
            self._send_json({"error": "not found"}, status=404)

    def _handle_chat(self) -> None:
        data = self._read_body()
        self._run_turn(data.get("sessionId"), user_msg=str(data.get("message") or ""))

    def _handle_retry(self) -> None:
        """Alt+R: rerun the last turn with no new prompt, continuing from real
        context (synthetic error tail is stripped; see sanitize_for_retry)."""
        data = self._read_body()
        session_id = data.get("sessionId")
        stored = self.runtime.sessions_load(session_id) if session_id else None
        if not stored:
            self._send_json({"error": "no session to retry"}, status=400)
            return
        messages = sanitize_for_retry(stored["messages"])
        if not any(m.get("role") != "system" for m in messages):
            self._send_json({"error": "nothing to retry"}, status=400)
            return
        self._run_turn(session_id, user_msg=None, messages=messages)

    def _run_turn(self, session_id: str | None, user_msg: str | None, messages=None) -> None:
        if messages is None and not session_id:
            # Generate before registering: /stop keys on the real session id.
            session_id = self.runtime.new_session_id()
        abort_event = threading.Event()
        with _TURNS_LOCK:
            _ACTIVE_TURNS.setdefault(session_id, set()).add(abort_event)
        sink = WebSink(self, session_id)
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Connection", "close")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.close_connection = True
        try:
            if _session_lock(session_id).locked():
                # A queued turn must not look dead: say why nothing streams yet.
                sink.emit("status", "Waiting for the still-running turn in this session...")
            with _session_lock(session_id):
                # Load context inside the lock: a queued turn must continue
                # from the previous turn's persisted reply, not a snapshot
                # taken before the wait (which dropped that reply on save).
                stored = self.runtime.sessions_load(session_id) if session_id else None
                if messages is None:
                    if stored:
                        messages = list(stored["messages"])
                    else:
                        messages = [{"role": "system", "content": self.runtime.new_session_prompt()}]
                    if user_msg is not None:
                        messages.append({"role": "user", "content": user_msg})
                messages = repair_tool_gaps(messages)

                # Persist at turn start: the user message must be on disk while
                # the turn streams. Otherwise a refresh mid-turn shows an empty
                # session and invites chatting into / deleting one that is busy.
                prior = (stored or {}).get("subagents", []) if isinstance(stored, dict) else []
                prior_asks = (stored or {}).get("asks", []) if isinstance(stored, dict) else []
                self.runtime.sessions_save(
                    session_id,
                    session.get_session_title(messages),
                    messages,
                    subagents=prior,
                    asks=prior_asks,
                )
                # Start recording after the queue wait: a queued turn must not
                # clobber the still-running turn's tape for the same session.
                with _TURNS_LOCK:
                    _TURN_TAPES[session_id] = []
                # Mood kaomoji in parallel: a slow mood call must not delay
                # the reply, and its failure must not touch the turn.
                threading.Thread(
                    target=_kaomoji_worker, args=(sink, self.runtime, messages), daemon=True
                ).start()
                agent = self.runtime.build_agent(sink, abort_event.is_set)
                try:
                    agent.run(messages)
                finally:
                    # Persist on every exit path (success, abort, crash): a
                    # turn that never saves is a turn whose context is lost.
                    subs = getattr(agent, "subagents", None)
                    new_subs = list(subs.values()) if isinstance(subs, dict) else []
                    new_asks = list(getattr(agent, "asks", None) or [])
                    with _TURNS_LOCK:
                        resurrects = session_id in _TURN_DELETED
                        if resurrects:
                            _TURN_DELETED.discard(session_id)
                    if not resurrects:
                        self.runtime.sessions_save(
                            session_id,
                            session.get_session_title(messages),
                            messages,
                            subagents=prior + new_subs,
                            asks=prior_asks + new_asks,
                        )
            sink.emit("sessionId", session_id)
            sink.emit("done", None)
        except Exception as exc:
            sink.emit("error", str(exc))
            sink.emit("done", None)
        finally:
            # Seal the tape with a done marker (replay consumers close on it)
            # and pop it after a grace window so late reattach still sees it.
            with _TURNS_LOCK:
                tape = _TURN_TAPES.get(session_id)
                if tape is not None:
                    tape.append({"type": "done", "content": None})
            seal = threading.Timer(60.0, _TURN_TAPES.pop, args=(session_id, None))
            seal.daemon = True
            seal.start()
            with _TURNS_LOCK:
                events = _ACTIVE_TURNS.get(session_id)
                if events is not None:
                    events.discard(abort_event)
                    if not events:
                        _ACTIVE_TURNS.pop(session_id, None)
            self.wfile.flush()

    def _handle_events(self, session_id: str | None) -> None:
        """Reattach to a (recently) running turn: replay its recorded events,
        then live-stream new ones until the tape's done marker. A missing tape
        means nothing is running — answer with a bare done so the client
        simply reloads from disk."""
        if not session_id:
            self._send_json({"error": "need sessionId"}, status=400)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Connection", "close")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.close_connection = True
        idx = 0
        try:
            while True:
                with _TURNS_LOCK:
                    tape = _TURN_TAPES.get(session_id)
                    fresh = tape[idx:] if tape is not None else []
                    idx += len(fresh)
                try:
                    for ev in fresh:
                        self.wfile.write((json.dumps(ev, ensure_ascii=False) + "\n").encode("utf-8"))
                    if fresh:
                        self.wfile.flush()
                        if any(ev.get("type") == "done" for ev in fresh):
                            return
                    elif tape is None:
                        self.wfile.write(b'{"type": "done", "content": null}\n')
                        self.wfile.flush()
                        return
                except (BrokenPipeError, ConnectionResetError, OSError):
                    return
                time.sleep(0.2)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _handle_pickfile(self) -> None:
        try:
            import tkinter as tk  # noqa: PLC0415 (heavy GUI import, only on demand)
            from tkinter import filedialog  # noqa: PLC0415

            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            path = filedialog.askopenfilename(title="Select a file")
            root.destroy()
            self._send_json({"path": path or None})
        except Exception as exc:
            self._send_json({"path": None, "error": str(exc)})


def make_webui_server(port: int | None, runtime: WebUIRuntime) -> ThreadingHTTPServer:
    """Build (not start) the WebUI server; room mode embeds this in-process."""
    handler = type("BoundHandler", (YesSirHandler,), {"runtime": runtime})
    return ThreadingHTTPServer(("127.0.0.1", _free_port(port)), handler)


def _free_port(preferred: int | None) -> int:
    port = preferred or 0
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", port))
        return sock.getsockname()[1]


def run_server(port: int | None = None, runtime: WebUIRuntime | None = None) -> None:
    import webbrowser  # noqa: PLC0415 (only needed to open the browser)

    rt = runtime or WebUIRuntime()
    server = make_webui_server(port, rt)
    url = f"http://localhost:{server.server_address[1]}"
    print(f"  YESIR web UI: {url}")
    print("  Press Ctrl+C to stop")
    webbrowser.open(url)
    mcp_tools = rt.mcp_tools()
    if mcp_tools:
        print(f"  MCP: {len(mcp_tools)} tools loaded -> {', '.join(sorted(mcp_tools))}")
    elif load_config().mcp_servers:
        print("  MCP: servers configured but none loaded (see stderr)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
