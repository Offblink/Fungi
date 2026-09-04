"""Room mode: one process per host — tray (main thread) + hub/client + clones.

Server role: hosts the hub itself; clones ride LocalTransport (direct relay).
Client role: one HubClient; a single HostPoller drains the host buffer on the
server and fans envelopes out to per-clone inboxes (clones must not share one
cursor). Roster diffs (server monitor thread / client heartbeat) add and
remove comm clones on both ends. The WebUI starts lazily from the tray and
runs the local clone's toolset with per-host sessions (server: hub store;
client: the joining machine's own disk — conversations must not land on the
peer-operated hub).

Consent modes (WebUI friend-view slider): a host set to "allow" gets its
consent-shaped asks (file ops, transfers) auto-answered yes at on_ask — no
notification, no card. Mode "ask" (default) raises a card; modes are visible
and reversible in the friend view.
"""

import contextlib
import json
import threading
import time
import webbrowser
from http.server import ThreadingHTTPServer
from pathlib import Path

from .agent import Agent
from .cards import AskCards
from .clone.base import Clone, LocalTransport, RemoteTransport
from .clone.comm import build_comm_clone
from .clone.local import build_local_clone
from .config import Config
from .consent_rules import ConsentRules
from .events import Sink
from .hub.app import Hub
from .hub.client import HubClient, HubError
from .hub.relay import Inbox
from .protocol import Envelope, parse_addr
from .server import WebUIRuntime, make_webui_server
from .session import SESSIONS_DIR, SessionStore
from .tools.ask import make_ask_tool, resolve_ask
from .trilayer import TriLayer

MONITOR_INTERVAL_S = 2.0
HEARTBEAT_INTERVAL_S = 10.0


def _summary(body: dict) -> str:
    """One-line human summary of an ask body (for the notification)."""
    questions = body.get("questions")
    if isinstance(questions, list) and questions:
        return "; ".join(str(q.get("question", "")) for q in questions)
    if body.get("question"):
        return str(body["question"])
    return json.dumps(body, ensure_ascii=False)


def _is_consent(body: dict) -> bool:
    """Consent-shaped ask: produced by ask_consent (action + path present)."""
    return bool(body.get("action")) and bool(body.get("path"))


class HostPoller:
    """Client-side fanout: one cursor on the shared host buffer, per-clone
    inboxes on this side. Unknown destinations are dropped (clone removed)."""

    def __init__(self, client: HubClient, host: str, poll_timeout: float = 5.0):
        self.client = client
        self.host = host
        self.poll_timeout = poll_timeout
        self._inboxes: dict[str, Inbox] = {}
        self._cursor = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def inbox_for(self, addr: str) -> Inbox:
        inbox = self._inboxes.get(addr)
        if inbox is None:
            inbox = Inbox()
            self._inboxes[addr] = inbox
        return inbox

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name=f"poller-{self.host}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                messages, self._cursor = self.client.poll(self._cursor, self.poll_timeout)
            except Exception:
                self._stop.wait(1.0)  # transient network hiccup: back off, keep looping
                continue
            for env in messages:
                inbox = self._inboxes.get(env.dst)
                if inbox is not None:
                    inbox.push(env)


class RoomBase:
    """Shared assembly: local clone + comm clones + card asks + WebUI runtime."""

    role = "base"

    def __init__(
        self,
        host: str,
        cfg: Config,
        sink: Sink,
        notifier=None,
        llm=None,
        rules_path: Path | None = None,
        display: str = "",
    ):
        self.host = host
        self.display = display
        self.cfg = cfg
        self.sink = sink
        self.notifier = notifier
        self.llm = llm
        self.cards = AskCards()
        self.rules = ConsentRules(rules_path)
        self.local_addr = f"{host}:local"
        self._local: Clone | None = None
        self._clones: dict[str, Clone] = {}
        self._guard = threading.Lock()
        self._stop = threading.Event()
        self._webui: ThreadingHTTPServer | None = None
        self._webui_guard = threading.Lock()

    # ── clones ──

    @property
    def local(self) -> Clone:
        if self._local is None:
            raise RuntimeError("room not started")
        return self._local

    def _make_local(self, transport) -> Clone:
        return build_local_clone(
            self.host,
            transport,
            self.cfg,
            self.sink,
            llm=self.llm,
            peers_fn=self._peers,
            on_ask=self._on_ask,
        )

    def _peers(self) -> list[str]:
        raise NotImplementedError

    def display_of(self, host: str) -> str:
        """Presentation nickname for `host` ("" -> UI falls back to the name)."""
        if getattr(self, "hub", None) is not None:  # server role: direct roster
            return self.hub.roster.display(host)
        for entry in getattr(self, "_entries", None) or []:  # client role: cached
            if entry.get("name") == host:
                return str(entry.get("display") or "")
        return ""

    def add_comm_clone(self, peer: str, transport) -> None:
        with self._guard:
            if peer in self._clones or self._stop.is_set():
                return
            clone = build_comm_clone(
                self.host,
                peer,
                transport,
                self.cfg,
                self.sink,
                llm=self.llm,
                inbox_dir=Path(self.cfg.inbox_dir) if self.cfg.inbox_dir else None,
            )
            self._clones[peer] = clone
        clone.start()

    def remove_comm_clone(self, peer: str) -> None:
        with self._guard:
            clone = self._clones.pop(peer, None)
        if clone is not None:
            clone.stop()

    # ── incoming asks: auto-allow -> cards + notification ──

    def _on_ask(self, env: Envelope) -> None:
        # The logical requester is body["from"] (ask_consent/transfer both set
        # it). For transfer receipts the envelope src is the receiving host's
        # OWN comm clone — keying the mode on env.src would key the friend's
        # slider to the wrong host.
        src = str(env.body.get("from") or env.src)
        if _is_consent(env.body) and self.rules.allows(src):
            self._send_answer(env, "yes")  # slider says allow: silent grant
            return
        if not self.cards.record(env):
            return  # replay/duplicate
        if self.notifier is not None:
            try:
                src_host, _role, _peer = parse_addr(src)
            except Exception:
                src_host = src
            self.notifier.ask(self.display_of(src_host) or src_host, _summary(env.body))

    def _send_answer(self, ask: Envelope, value) -> None:
        self.local.transport.send(
            Envelope(
                src=self.local_addr,
                dst=ask.src,
                type="answer",
                body={"value": value},
                reply_to=ask.id,
            )
        )

    # ── WebUI ──

    def open_webui(self, open_browser: bool = True) -> str:
        """Start the WebUI lazily; returns its URL."""
        with self._webui_guard:
            if self._webui is None:
                self._webui = make_webui_server(None, self.webui_runtime())
                threading.Thread(
                    target=self._webui.serve_forever, name="webui", daemon=True
                ).start()
        url = f"http://localhost:{self._webui.server_address[1]}"
        if open_browser:
            threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()
        return url

    def webui_runtime(self) -> WebUIRuntime:
        return RoomRuntime(self, self._sessions_backend())

    def _sessions_backend(self):
        raise NotImplementedError

    # ── lifecycle ──

    def stop(self) -> None:
        self._stop.set()
        with self._guard:
            peers = list(self._clones)
            self._clones.clear()
        for peer in peers:
            self.remove_comm_clone(peer)
        if self._local is not None:
            self._local.stop()
            self._local = None
        if self._webui is not None:
            self._webui.shutdown()
            self._webui.server_close()
            self._webui = None


class StoreSessions:
    """Server-role session backend: the hub's own SessionStore."""

    def __init__(self, hub: Hub):
        self.hub = hub

    def list(self) -> list[dict]:
        return self.hub.store.sessions.list_sessions()

    def load(self, session_id: str) -> dict | None:
        return self.hub.store.sessions.load(session_id)

    def save(self, session_id, title, messages, subagents=None, asks=None) -> None:
        self.hub.store.sessions.save(session_id, title, messages, subagents, asks)

    def delete(self, session_id: str) -> None:
        self.hub.store.sessions.delete(session_id)


class ClientSessions:
    """Client-role session backend: a local store on the joining machine.

    Conversations must not be proxied into the hub store: the peer operates
    that machine, and a single shared sessions/ directory made every host's
    WebUI list every other host's chats (2026-09-04 real-machine finding).
    """

    def __init__(self, directory: Path):
        self.store = SessionStore(directory)

    def list(self) -> list[dict]:
        return self.store.list_sessions()

    def load(self, session_id: str) -> dict | None:
        return self.store.load(session_id)

    def save(self, session_id, title, messages, subagents=None, asks=None) -> None:
        self.store.save(session_id, title, messages, subagents, asks)

    def delete(self, session_id: str) -> None:
        self.store.delete(session_id)


class RoomServer(RoomBase):
    """Server role: hosts the hub; roster diff runs in a monitor thread."""

    role = "server"

    def __init__(
        self,
        host,
        cfg,
        sink,
        token,
        data_root: Path,
        notifier=None,
        llm=None,
        rules_path=None,
        display="",
        port: int = 0,
    ):
        super().__init__(
            host, cfg, sink, notifier=notifier, llm=llm, rules_path=rules_path, display=display
        )
        self.hub = Hub(host, token, data_root, max_file_mb=cfg.max_file_mb, port=port)
        self._monitor: threading.Thread | None = None
        # selftest state (FUNGI_SELFTEST=1)
        self.selftest_answered: str | None = None
        self.selftest_inbox = None
        self.selftest_ask_id: str | None = None

    def start(self) -> None:
        self.hub.start()
        # Own roster entry: peers must see this host; the monitor keeps it alive.
        self.hub.join(self.host, "127.0.0.1", self.display)
        self._local = self._make_local(
            LocalTransport(self.hub.relay, self.local_addr, hub=self.hub)
        )
        self._local.start()
        self._monitor = threading.Thread(
            target=self._monitor_loop, name="roster-monitor", daemon=True
        )
        self._monitor.start()

    def _peers(self) -> list[str]:
        return self.hub.roster.peers(self.host)

    def _monitor_loop(self) -> None:
        while not self._stop.wait(MONITOR_INTERVAL_S):
            self.hub.roster.beat(self.host)  # nobody else beats the server's own entry
            peers = set(self._peers())
            with self._guard:
                current = set(self._clones)
            for peer in peers - current:
                transport = LocalTransport(self.hub.relay, f"{self.host}:comm-{peer}", hub=self.hub)
                self.add_comm_clone(peer, transport)
            for peer in current - peers:
                self.remove_comm_clone(peer)

    def _sessions_backend(self) -> StoreSessions:
        return StoreSessions(self.hub)

    def stop(self) -> None:
        super().stop()
        self.hub.stop()


class RoomClient(RoomBase):
    """Client role: joins the hub over HTTP; roster diff rides the heartbeat."""

    role = "client"

    def __init__(
        self,
        host,
        cfg,
        sink,
        server_url: str,
        token: str,
        notifier=None,
        llm=None,
        rules_path=None,
        display="",
        sessions_dir: Path | None = None,
    ):
        super().__init__(
            host, cfg, sink, notifier=notifier, llm=llm, rules_path=rules_path, display=display
        )
        self.client = HubClient(server_url, token, host, display)
        self.poller = HostPoller(self.client, host)
        self._peers_known: set[str] = set()
        self._entries: list[dict] = []  # roster display records from join/heartbeat
        self._hb: threading.Thread | None = None
        # Client conversations live on THIS machine only (default: the shared
        # single-host location); never staged on the hub's disk.
        self._sessions = ClientSessions(sessions_dir or SESSIONS_DIR)

    def start(self) -> None:
        out = self.client.join()
        self._peers_known = set(out.get("peers") or [])
        self._entries = list(out.get("roster") or [])
        self.poller.start()
        self._local = self._make_local(
            RemoteTransport(self.client, inbox=self.poller.inbox_for(self.local_addr))
        )
        self._local.start()
        for peer in sorted(self._peers_known):
            self.add_comm_clone(peer, self._comm_transport(peer))
        self._hb = threading.Thread(target=self._heartbeat_loop, name="heartbeat", daemon=True)
        self._hb.start()

    def _peers(self) -> list[str]:
        return sorted(self._peers_known)

    def _comm_transport(self, peer: str) -> RemoteTransport:
        return RemoteTransport(self.client, inbox=self.poller.inbox_for(f"{self.host}:comm-{peer}"))

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(HEARTBEAT_INTERVAL_S):
            try:
                self._heartbeat_once()
            except HubError:
                continue  # transient; next beat retries

    def _heartbeat_once(self) -> None:
        """One beat: refresh peers (add/remove comm clones), replay pending asks."""
        out = self.client.heartbeat()
        self._peers_known = set(out.get("peers") or [])
        self._entries = list(out.get("roster") or [])
        with self._guard:
            current = set(self._clones)
        for peer in self._peers_known - current:
            self.add_comm_clone(peer, self._comm_transport(peer))
        for peer in current - self._peers_known:
            self.remove_comm_clone(peer)
        for rec in out.get("pending_asks") or []:
            self._replay_ask(rec)

    def _replay_ask(self, rec: dict) -> None:
        """Heartbeat replay of unresolved asks: re-record + re-notify (dedup)."""
        src = rec.get("src")
        if not src:
            return
        env = Envelope(
            id=str(rec.get("ask_id") or ""),
            src=str(src),
            dst=self.local_addr,
            type="ask",
            body=rec.get("payload") or {},
        )
        self._on_ask(env)

    def _sessions_backend(self) -> ClientSessions:
        return self._sessions

    def stop(self) -> None:
        super().stop()
        self.poller.stop()
        with contextlib.suppress(HubError):
            self.client.leave()


class RoomRuntime(WebUIRuntime):
    """WebUI wired to the local clone: per-host sessions, local toolset,
    card answers routed back out as answer envelopes."""

    def __init__(self, room: RoomBase, sessions):
        self.room = room
        self._sessions = sessions

    # ── sessions ──
    def sessions_list(self) -> list[dict]:
        return self._sessions.list()

    def sessions_load(self, session_id: str) -> dict | None:
        return self._sessions.load(session_id)

    def sessions_save(self, session_id, title, messages, subagents=None, asks=None) -> None:
        self._sessions.save(session_id, title, messages, subagents, asks)

    def sessions_delete(self, session_id: str) -> None:
        self._sessions.delete(session_id)

    def new_session_id(self) -> str:
        from fungi import session  # noqa: PLC0415 (avoid import weight at module load)

        return session.new_session_id()

    def new_session_prompt(self) -> str:
        return self.room.local.system_prompt

    # ── turns ──
    def build_agent(self, sink, should_abort) -> Agent:
        clone = self.room.local
        tools = dict(clone.tools)
        # ask_user rides the turn sink so its card streams in the NDJSON flow;
        # resolution stays on the module-global registry (/answer in-process).
        tools["ask_user"] = make_ask_tool(sink)
        trilayer = TriLayer(
            self.room.cfg,
            sink,
            llm=self.room.llm,
            should_abort=should_abort,
            child_tool_names=clone.child_tool_names,
            child_extra_tools=clone.child_extra_tools,
            skill_save=clone.skill_save,
        )
        return trilayer.build_clone_agent(
            sink,
            system_prompt=clone.system_prompt,
            extra_tools=tools,
            tool_names=clone.tool_names,
            model=clone.model,
        )

    # ── answers ──
    def route_answer(self, ask_id: str, value) -> bool:
        card = self.room.cards.take(
            ask_id, value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        )
        if card is not None:
            self.room._send_answer(card, value)
            return True
        return resolve_ask(ask_id, value)

    def pending_asks(self) -> list[dict]:
        out = []
        for card in self.room.cards.pending():
            body = card["body"]
            src = body.get("from") or card["src"]
            questions = body.get("questions")
            if isinstance(questions, list) and questions:
                out.append({"id": card["id"], "from": src, "kind": "ask", "questions": questions})
            elif body.get("question"):
                out.append(
                    {
                        "id": card["id"],
                        "from": src,
                        "kind": "consent",
                        "questions": [
                            {"question": body["question"], "options": [], "allow_custom": True}
                        ],
                    }
                )
            else:
                out.append(
                    {
                        "id": card["id"],
                        "from": card["src"],
                        "kind": "ask",
                        "questions": [
                            {"question": "(no question text)", "options": [], "allow_custom": True}
                        ],
                    }
                )
        return out

    # ── consent slider (per-friend mode, visible + reversible) ──
    def consent_mode(self, host: str) -> str:
        return self.room.rules.mode_for(host)

    def set_consent_mode(self, host: str, mode: str) -> None:
        self.room.rules.set_mode(host, mode)

    # ── friends / comm conversations ──
    def peers(self) -> list[dict]:
        if getattr(self.room, "hub", None) is not None:  # server role: direct
            return self.room.hub.roster.entries(self.room.host)
        return list(self.room._entries)  # client role: cached roster

    def comm_log(self, host: str) -> list[dict]:
        if getattr(self.room, "hub", None) is not None:  # server role: direct
            return self.room.hub.commlog.read(self.room.host, host)
        return self.room.client.comm_log(host)  # client role: hub API


# ── selftest hook (FUNGI_SELFTEST=1, server role) ──


def run_selftest(room: RoomServer, quit_fn, fail_fn) -> None:
    """Acceptance loop, no human input: start WebUI -> simulated comm clone
    sends an ask -> notification fires -> POST /answer yes -> blocker wakes."""
    from PyQt6.QtCore import QTimer  # noqa: PLC0415 (Qt only in tray mode)

    url = room.open_webui(open_browser=False)

    def _step1() -> None:
        threading.Thread(target=_requester, args=(room,), daemon=True).start()

    def _step2() -> None:
        pending = room.cards.pending()
        if not pending:
            fail_fn(f"no card recorded after ask (pending={pending})")
            return
        import urllib.request  # noqa: PLC0415

        req = urllib.request.Request(
            url + "/answer",
            data=json.dumps({"id": pending[0]["id"], "value": "yes"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = json.loads(resp.read())
        if not body.get("ok"):
            fail_fn(f"/answer rejected: {body}")
            return

    def _step3() -> None:
        if room.selftest_answered is None:
            fail_fn("requester was not woken by the answer envelope")
            return
        if room.selftest_answered != "yes":
            fail_fn(f"unexpected answer value: {room.selftest_answered!r}")
            return
        if room.notifier is not None and not room.notifier.shown:
            fail_fn("notification was never shown")
            return
        print("FUNGI SELFTEST OK", flush=True)
        quit_fn(0)

    QTimer.singleShot(500, _step1)
    QTimer.singleShot(2500, _step2)
    QTimer.singleShot(4000, _step3)


def _requester(room: RoomServer) -> None:
    """Simulated comm clone: register blocking ask, send envelope, drain the
    inbox loop (dispatch answer -> pending), record the answer for step 3."""
    from .pending import PendingAsks  # noqa: PLC0415

    addr = f"{room.host}:comm-selftest"
    room.selftest_inbox = room.hub.relay.register_local(addr)
    pending = PendingAsks()
    env = Envelope(
        src=addr,
        dst=room.local_addr,
        type="ask",
        body={"question": "Selftest: allow writes to homes/report.md?", "from": addr},
    )
    room.selftest_ask_id = env.id
    pending.register(env.id)
    room.hub.send(env)
    cursor = 0
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        msgs, cursor = room.selftest_inbox.after(cursor, 0.5)
        for m in msgs:
            if m.type == "answer" and m.reply_to == env.id:
                room.selftest_answered = str(m.body.get("value"))
                pending.resolve(env.id, m.body.get("value"))
                return
