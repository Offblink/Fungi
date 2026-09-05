"""Room mode tests: cards, consent rules, server/client assembly, WebUI routing."""

import json
import time
import urllib.request

import pytest

from fungi.cards import AskCards
from fungi.config import Config
from fungi.consent_rules import ConsentRules
from fungi.events import NullSink
from fungi.hub.app import Hub
from fungi.protocol import Envelope
from fungi.room import RoomClient, RoomRuntime, RoomServer

CFG = Config(api_key="k", endpoint="e", model="m")  # assembly reads max_file_mb/inbox_dir
LLM = object()


def _all_msgs(inbox) -> list:
    """Drain (cursor 0 = everything buffered) without relying on long-poll wait."""
    msgs, _cursor = inbox.after(0, 0.2)
    return msgs


def _wait(predicate, timeout_s: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


# ── units ──


def test_ask_cards_record_take_and_dedup():
    cards = AskCards()
    a = Envelope(src="beta:comm-alpha", dst="alpha:local", type="ask", body={"question": "q"})
    assert cards.record(a) is True
    assert cards.record(a) is False  # duplicate
    assert [c["id"] for c in cards.pending()] == [a.id]
    taken = cards.take(a.id, "yes")
    assert taken is not None and taken.id == a.id
    assert cards.pending() == []
    assert cards.record(a) is False  # answered ids stay known (replay dedup)


def test_consent_modes_persist_and_legacy_migrates(tmp_path):
    path = tmp_path / "rules.json"
    path.write_text(json.dumps({"always_allow": ["beta:comm-alpha"]}), encoding="utf-8")
    rules = ConsentRules(path)
    assert rules.mode_for("beta") == "allow"  # legacy grant migrated to a visible mode
    assert rules.allows("beta:comm-alpha") is True

    rules.set_mode("beta", "ask")
    assert rules.mode_for("beta") == "ask"
    assert rules.allows("beta:comm-alpha") is False
    reloaded = ConsentRules(path)
    assert reloaded.mode_for("beta") == "ask"
    assert "always_allow" not in json.loads(path.read_text(encoding="utf-8"))


# ── server role ──


@pytest.fixture()
def server_room(tmp_path):
    room = RoomServer(
        "alpha",
        CFG,
        NullSink(),
        "tok",
        tmp_path / "data",
        llm=LLM,
        rules_path=tmp_path / "rules.json",
    )
    room.start()
    yield room
    room.stop()


def _send_ask(hub, src, dst, body):
    env = Envelope(src=src, dst=dst, type="ask", body=body)
    hub.send(env)
    return env


def test_server_ask_becomes_card_and_answer_envelope_flows(server_room):
    room = server_room
    requester_inbox = room.hub.relay.register_local("alpha:comm-selftest")
    ask = _send_ask(
        room.hub,
        "alpha:comm-selftest",
        "alpha:local",
        {"question": "Allow write?", "from": "alpha:comm-selftest"},
    )
    assert _wait(room.cards.pending), "ask envelope never became a card"

    runtime: RoomRuntime = room.webui_runtime()
    pending = runtime.pending_asks()
    assert pending[0]["kind"] == "consent"
    assert pending[0]["from"] == "alpha:comm-selftest"
    # conv = the friend conversation the ask belongs to (agent suffix "selftest")
    assert pending[0]["conv"] == "selftest"

    assert runtime.route_answer(ask.id, "yes") is True
    msgs, _cursor = requester_inbox.after(0, 2.0)
    answers = [m for m in msgs if m.type == "answer" and m.reply_to == ask.id]
    assert answers and answers[0].body == {"value": "yes"}
    assert room.hub.asks.get(ask.id)["status"] == "answered"


def test_room_turn_persists_answered_asks(server_room):
    """Room-mode turns must record completed inquire calls on the agent:
    the WebUI turn runner saves that bucket, and sessions replay answered
    cards from it. Room mode lost the on_answer wiring — asks stayed [] on
    every session file."""
    import threading

    from fungi.events import FnSink

    room = server_room
    runtime = room.webui_runtime()
    events: list[tuple] = []
    agent = runtime.build_agent(FnSink(lambda t, c: events.append((t, c))), lambda: False)

    out: dict = {}
    th = threading.Thread(
        target=lambda: out.update(
            reply=agent.extra_tools["inquire"].fn({"question": "proceed?"})
        ),
        daemon=True,
    )
    th.start()

    def ask_id():
        asks = [c for t, c in events if t == "ask"]
        return asks[-1]["id"] if asks else None

    assert _wait(lambda: ask_id() is not None), "ask event never surfaced on the turn sink"
    assert runtime.route_answer(ask_id(), "yes") is True
    th.join(timeout=5.0)
    assert out["reply"].startswith("USER:"), f"ask tool never woke: {out}"
    assert [a["status"] for a in agent.asks] == ["answered"]
    assert agent.asks[0]["answers"] == "yes"


def test_server_allow_mode_silently_answers(server_room):
    room = server_room
    room.rules.set_mode("alpha", "allow")

    requester_inbox = room.hub.relay.register_local("alpha:comm-beta")
    _send_ask(
        room.hub,
        "alpha:comm-beta",
        "alpha:local",
        {
            "from": "alpha:comm-beta",
            "action": "write",
            "path": "homes/alpha/x.md",
            "reason": "r",
            "question": "Allow write?",
        },
    )
    assert room.cards.pending() == [], "always-allowed ask must not become a card"
    assert _wait(
        lambda: any(
            m.type == "answer" and m.body == {"value": "yes"} for m in _all_msgs(requester_inbox)
        )
    ), "auto-allowed ask was never answered yes"


def test_card_answer_is_one_shot_but_slider_mode_persists(server_room):
    room = server_room
    requester_inbox = room.hub.relay.register_local("alpha:comm-gamma")
    ask = _send_ask(
        room.hub,
        "alpha:comm-gamma",
        "alpha:local",
        {
            "from": "alpha:comm-gamma",
            "action": "write",
            "path": "homes/alpha/y.md",
            "reason": "r",
            "question": "Allow?",
        },
    )
    assert _wait(room.cards.pending)
    runtime = room.webui_runtime()
    assert runtime.route_answer(ask.id, "yes") is True
    assert room.rules.mode_for("alpha") == "ask"  # a one-shot card grants nothing

    # same-source ask right after: a new card, not a silent yes
    second = _send_ask(
        room.hub,
        "alpha:comm-gamma",
        "alpha:local",
        {
            "from": "alpha:comm-gamma",
            "action": "write",
            "path": "homes/alpha/z.md",
            "reason": "r",
            "question": "Allow again?",
        },
    )
    assert _wait(lambda: any(c["id"] == second.id for c in room.cards.pending()))

    # slider: allow mode makes future asks silent and reversible
    runtime.set_consent_mode("alpha", "allow")
    third = _send_ask(
        room.hub,
        "alpha:comm-gamma",
        "alpha:local",
        {
            "from": "alpha:comm-gamma",
            "action": "write",
            "path": "homes/alpha/w.md",
            "reason": "r",
            "question": "Allow a third time?",
        },
    )
    assert _wait(
        lambda: any(
            m.type == "answer" and m.reply_to == third.id and m.body == {"value": "yes"}
            for m in _all_msgs(requester_inbox)
        )
    ), "allow-mode ask was not auto-answered"
    assert not [c for c in room.cards.pending() if c["id"] == third.id]

    runtime.set_consent_mode("alpha", "ask")  # reversible: back to cards
    assert room.rules.mode_for("alpha") == "ask"


def test_consent_mode_endpoint_roundtrip(server_room):
    room = server_room
    url = room.open_webui(open_browser=False)
    d = json.loads(urllib.request.urlopen(url + "/consent-mode?host=alpha", timeout=5).read())
    assert d["mode"] == "ask"
    req = urllib.request.Request(
        url + "/consent-mode",
        data=json.dumps({"host": "alpha", "mode": "allow"}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    assert json.loads(urllib.request.urlopen(req, timeout=5).read())["ok"] is True
    assert room.rules.mode_for("alpha") == "allow"
    assert room.webui_runtime().consent_mode("alpha") == "allow"


def test_server_roster_monitor_adds_and_removes_comm_clones(server_room):
    room = server_room
    room.hub.join("beta", "127.0.0.1")
    assert _wait(lambda: "beta" in room._clones, timeout_s=8.0), "monitor never spawned comm clone"

    room.hub.roster.leave("beta")
    assert _wait(lambda: "beta" not in room._clones, timeout_s=8.0), (
        "monitor never removed comm clone"
    )


def test_server_runtime_sessions_backed_by_hub_store(server_room):
    runtime = server_room.webui_runtime()
    sid = runtime.new_session_id()
    runtime.sessions_save(sid, "t", [{"role": "user", "content": "hi"}])
    assert [s["id"] for s in runtime.sessions_list()] == [sid]
    assert runtime.sessions_load(sid)["messages"][0]["content"] == "hi"
    assert runtime.new_session_prompt().startswith("You are the local Orchestrator on host alpha")
    runtime.sessions_delete(sid)
    assert runtime.sessions_list() == []


def test_slider_keys_on_logical_requester_not_envelope_src(server_room):
    """Transfer receipts: the ask envelope's src is the receiving host's own
    comm clone; the friend's slider must still govern (body.from wins)."""
    room = server_room
    room.rules.set_mode("alpha", "allow")
    requester_inbox = room.hub.relay.host_buffer("beta")  # answer dst host = beta
    _send_ask(
        room.hub,
        "beta:comm-alpha",  # envelope src: our OWN comm clone (transfer path)
        "alpha:local",
        {
            "from": "alpha:comm-beta",  # logical requester: the friend
            "action": "receive file",
            "path": "report.txt",
            "reason": "r",
            "question": "Accept file?",
        },
    )
    assert _wait(
        lambda: any(
            m.type == "answer" and m.body == {"value": "yes"} for m in _all_msgs(requester_inbox)
        )
    ), "allow-mode transfer ask was not auto-answered"
    assert room.cards.pending() == []


def test_slider_does_not_auto_allow_generic_confirm(server_room):
    room = server_room
    room.rules.set_mode("alpha", "allow")
    requester_inbox = room.hub.relay.register_local("alpha:comm-beta")
    _send_ask(
        room.hub,
        "alpha:comm-beta",
        "alpha:local",
        {"from": "alpha:comm-beta", "questions": [{"question": "Pick one?", "options": []}]},
    )
    time.sleep(0.3)
    assert not any(m.type == "answer" for m in _all_msgs(requester_inbox))
    assert room.cards.pending(), "generic inquire must still raise a card"


# ── client role ──


@pytest.fixture()
def client_room(tmp_path):
    hub = Hub("alphaserver", "tok", tmp_path / "server-data")
    hub.start()
    room = RoomClient(
        "beta",
        CFG,
        NullSink(),
        f"http://127.0.0.1:{hub.port}",
        "tok",
        llm=LLM,
        rules_path=tmp_path / "rules.json",
        sessions_dir=tmp_path / "client-sessions",
    )
    room.start()
    yield hub, room
    room.stop()
    hub.stop()


def test_client_join_cards_and_heartbeat_replay(client_room):
    hub, room = client_room
    assert hub.roster.known("beta")

    # direct delivery: ask envelope reaches beta:local through the poller fanout
    ask = _send_ask(
        hub, "alpha:comm-beta", "beta:local", {"question": "q?", "from": "alpha:comm-beta"}
    )
    assert _wait(room.cards.pending), "poller fanout never delivered the ask"

    # heartbeat replay: same ask id again (from pending_asks) must dedup
    rec = hub.asks.get(ask.id)
    room._replay_ask({"ask_id": rec["ask_id"], "src": rec["src"], "payload": rec["payload"]})
    assert len(room.cards.pending()) == 1

    # a different pending ask replays as a new card
    second = _send_ask(
        hub, "alpha:comm-beta", "beta:local", {"question": "q2?", "from": "alpha:comm-beta"}
    )
    rec2 = hub.asks.get(second.id)
    room._replay_ask({"ask_id": rec2["ask_id"], "src": rec2["src"], "payload": rec2["payload"]})
    assert _wait(lambda: len(room.cards.pending()) == 2)  # comparison, not a bare call


def test_client_roster_diff_spawns_comm_clone(client_room):
    hub, room = client_room
    hub.join("gamma", "127.0.0.1")  # roster entry only; beta learns via heartbeat
    room._heartbeat_once()
    assert "gamma" in room._peers()
    assert "gamma" in room._clones, "heartbeat diff never spawned comm clone"


def test_client_runtime_answers_via_remote_transport(client_room):
    hub, room = client_room
    ask = _send_ask(
        hub, "alpha:comm-beta", "beta:local", {"question": "q?", "from": "alpha:comm-beta"}
    )
    assert _wait(room.cards.pending)
    runtime = room.webui_runtime()
    assert runtime.route_answer(ask.id, "no") is True
    assert _wait(lambda: (hub.asks.get(ask.id) or {}).get("status") == "denied", timeout_s=5.0), (
        "answer envelope never resolved the hub ask registry"
    )


def test_client_sessions_stay_off_the_hub_disk(client_room):
    """Client conversations must never land in the peer-operated hub store."""

    hub, room = client_room
    backend = room._sessions_backend()
    backend.save("s1", "t", [{"role": "user", "content": "private"}])
    assert backend.load("s1")["messages"][0]["content"] == "private"
    assert hub.store.sessions.list_sessions() == []


def test_comm_turn_transcript_recorded_for_friend_view(server_room):
    """_record_comm_turn persists per-peer transcripts; comm_log returns them
    (session-style payload for the friend view)."""
    room = server_room

    class FakeAgent:
        subagents = {
            "s1": {
                "id": "s1",
                "call_id": "c1",
                "layer": 2,
                "goal": "g",
                "reply_format": "",
                "status": "done",
                "events": [],
            }
        }
        asks = [{"id": "a1", "questions": [], "answers": ["x"], "status": "answered"}]

    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "[beta:comm-alpha] hi"},
        {"role": "assistant", "content": "hello", "reasoning": "r"},
    ]
    room._record_comm_turn("beta", "chat", msgs, FakeAgent)
    runtime = room.webui_runtime()
    d = runtime.comm_log("beta")
    assert d["messages"] == msgs
    assert d["subagents"][0]["id"] == "s1"
    assert d["asks"][0]["id"] == "a1"
    assert isinstance(d["events"], list)


def test_comm_log_http_route_returns_full_payload(server_room):
    """Regression: the /comm-log route re-wrapped the already-dict payload as
    {"messages": {messages, events, ...}} — the frontend iterated an object
    as an array, threw into its swallowed catch, and the friend view stayed
    blank forever. The route must return comm_log's payload unchanged."""
    import json
    import threading
    import urllib.request

    from fungi.server import make_webui_server

    room = server_room

    class NoState:
        subagents = {}
        asks: list = []

    room._record_comm_turn(
        "beta",
        "chat",
        [{"role": "assistant", "content": "hello"}],
        NoState,
    )

    server = make_webui_server(0, room.webui_runtime())
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        host, port = server.server_address[:2]
        with urllib.request.urlopen(
            f"http://{host}:{port}/comm-log?host=beta", timeout=5.0
        ) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    finally:
        server.shutdown()
        server.server_close()
    assert isinstance(payload["messages"], list)
    assert payload["messages"][-1]["content"] == "hello"
    assert isinstance(payload["events"], list)


def test_comm_task_turn_appends_after_chat_transcript(server_room):
    room = server_room

    class NoState:
        subagents = {}
        asks: list = []

    chat = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "[beta] hi"},
        {"role": "assistant", "content": "hello"},
    ]
    room._record_comm_turn("beta", "chat", chat, NoState)
    task = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "[TASK from beta]\nGoal: g"},
        {"role": "assistant", "content": "done"},
    ]
    room._record_comm_turn("beta", "task", task, NoState)
    d = server_room.webui_runtime().comm_log("beta")
    roles = [m["role"] for m in d["messages"]]
    assert roles[0] == "system"
    assert d["messages"][3]["content"].startswith("[TASK from beta]")
    assert d["messages"][-1]["content"] == "done"


def _pong_llm(_messages, _tool_defs):
    from fungi.llm import LLMResult

    return LLMResult(content="pong")


def test_delegate_roundtrip_between_server_and_client(tmp_path):
    """REAL cross-host delegate: alpha:local -> hub -> beta comm clone turn ->
    result envelope back to alpha:local -> pending resolved. Guards against
    the 'delegate with correct args hangs forever' class of failure."""
    import threading

    from fungi.llm import LLMResult  # noqa: F401

    server = RoomServer(
        "alpha", CFG, NullSink(), "tok", tmp_path / "d1",
        llm=_pong_llm, rules_path=tmp_path / "r1.json",
    )
    server.start()
    try:
        client = RoomClient(
            "beta", CFG, NullSink(),
            f"http://127.0.0.1:{server.hub.port}", "tok",
            llm=_pong_llm, sessions_dir=tmp_path / "cs",
            rules_path=tmp_path / "r2.json",
        )
        client.start()
        try:
            assert _wait(
                lambda: "beta" in (server.local.delegate_tools.peers_fn() or []), timeout_s=10
            ), "beta never appeared in alpha's roster"
            out: list[str] = []

            def run():
                out.append(
                    server.local.delegate_tools.delegate({"host": "beta", "goal": "ping test"})
                )

            thread = threading.Thread(target=run, daemon=True)
            thread.start()
            thread.join(timeout=25)
            assert not thread.is_alive(), "delegate never returned"
            assert out and "pong" in out[0], out
        finally:
            client.stop()
    finally:
        server.stop()


# ── turn persistence vs refresh/delete races ──


@pytest.fixture()
def gated_room(tmp_path):
    """Room whose WebUI turns block inside the LLM until the test releases them."""
    import threading

    from fungi.llm import LLMResult

    gate = threading.Event()
    calls: list[list] = []

    def slow_llm(messages, _tools):
        calls.append(list(messages))
        gate.wait(timeout=10)
        return LLMResult(content=f"reply-{len(calls)}")

    room = RoomServer(
        "alpha", CFG, NullSink(), "tok", tmp_path / "data",
        llm=slow_llm, rules_path=tmp_path / "rules.json",
    )
    room.start()
    yield room, gate, calls
    room.stop()


def _webui_server(room):
    import threading

    from fungi.server import make_webui_server

    server = make_webui_server(0, room.webui_runtime())
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _post(port, path, payload):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    return urllib.request.urlopen(req, timeout=10)


def test_turn_persists_user_message_while_streaming(gated_room):
    """Regression: mid-turn the session existed only in the client's memory —
    a refresh showed an empty list entry (or nothing) while the turn ran on.
    The user message must be on disk from the moment the turn starts."""
    import threading

    room, gate, _calls = gated_room
    server = _webui_server(room)
    try:
        port = server.server_address[1]
        thread = threading.Thread(
            target=lambda: _post(port, "/chat", {"message": "hello there", "sessionId": None}).read(),
            daemon=True,
        )
        thread.start()
        assert _wait(
            lambda: any(s["title"] == "hello there" for s in room.webui_runtime().sessions_list())
        ), "session never appeared on disk while the turn was running"
        sid = room.webui_runtime().sessions_list()[0]["id"]
        stored = room.webui_runtime().sessions_load(sid)
        assert stored["messages"][-1] == {"role": "user", "content": "hello there"}
    finally:
        gate.set()
        server.shutdown()
        server.server_close()


def test_queued_turn_gets_status_line_and_keeps_prior_reply(gated_room):
    """Regression: a second /chat into a busy session silently blocked on the
    session lock (looked dead) and its pre-lock context snapshot dropped the
    running turn's reply on save. It must announce the wait, then continue
    from the full prior context."""
    import threading

    room, gate, calls = gated_room
    server = _webui_server(room)
    try:
        port = server.server_address[1]
        with _post(port, "/new", {}) as resp:
            sid = json.loads(resp.read())["id"]
        turn1 = threading.Thread(
            target=lambda: _post(port, "/chat", {"message": "first", "sessionId": sid}).read(),
            daemon=True,
        )
        turn1.start()
        assert _wait(lambda: len(calls) == 1), "first turn never reached the LLM"

        resp2 = _post(port, "/chat", {"message": "second", "sessionId": sid})
        first_line = json.loads(resp2.readline().decode("utf-8"))
        assert first_line["type"] == "status", f"no queue feedback, got {first_line}"
        gate.set()
        turn1.join(timeout=10)
        rest = resp2.read().decode("utf-8")
        assert '"done"' in rest, "queued turn never finished"

        messages = room.webui_runtime().sessions_load(sid)["messages"]
        roles = [m["role"] for m in messages]
        assert roles == ["system", "user", "assistant", "user", "assistant"]
        assert messages[2]["content"] == "reply-1", "prior turn's reply lost from context"
        assert messages[4]["content"] == "reply-2"
    finally:
        gate.set()
        server.shutdown()
        server.server_close()


def test_delete_running_session_leaves_it_deleted(gated_room):
    """Regression: deleting a mid-turn session unlinked the file, but the
    turn's exit-path save resurrected it — a ghost entry reappeared in the
    list when the run finished. Deleting must abort the turn and stay deleted."""

    import threading

    room, gate, _calls = gated_room
    server = _webui_server(room)
    try:
        port = server.server_address[1]
        with _post(port, "/new", {}) as resp:
            sid = json.loads(resp.read())["id"]
        turn = threading.Thread(
            target=lambda: _post(port, "/chat", {"message": "work", "sessionId": sid}).read(),
            daemon=True,
        )
        turn.start()
        assert _wait(
            lambda: len((room.webui_runtime().sessions_load(sid) or {}).get("messages", [])) >= 2
        ), "user message never reached disk while the turn ran"
        urllib.request.urlopen(
            urllib.request.Request(f"http://127.0.0.1:{port}/session?id={sid}", method="DELETE"),
            timeout=10,
        ).read()
        assert room.webui_runtime().sessions_load(sid) is None
        gate.set()
        turn.join(timeout=10)
        assert not turn.is_alive()
        assert room.webui_runtime().sessions_load(sid) is None, "deleted session resurrected"
    finally:
        gate.set()
        server.shutdown()
        server.server_close()


def _read_events(port, sid, sink_lines):
    resp = urllib.request.urlopen(
        f"http://127.0.0.1:{port}/events?sessionId={sid}", timeout=15
    )
    for raw in resp:
        line = raw.decode("utf-8").strip()
        if line:
            sink_lines.append(json.loads(line))


def test_events_replays_recorded_events_then_done(server_room):
    """Seed tape: a reattaching client must receive the recorded events in
    order, then the done marker."""
    import threading

    from fungi.server import _TURN_TAPES

    room = server_room
    server = _webui_server(room)
    try:
        port = server.server_address[1]
        _TURN_TAPES["replay-sid"] = [
            {"type": "text", "content": "partial answer"},
            {"type": "sessionId", "content": "replay-sid"},
            {"type": "done", "content": None},
        ]
        lines: list[dict] = []
        reader = threading.Thread(target=_read_events, args=(port, "replay-sid", lines), daemon=True)
        reader.start()
        reader.join(timeout=10)
        assert not reader.is_alive(), "reader never saw the done marker"
        assert [ev["type"] for ev in lines] == ["text", "sessionId", "done"]
        assert lines[0]["content"] == "partial answer"
    finally:
        _TURN_TAPES.pop("replay-sid", None)
        server.shutdown()
        server.server_close()


def test_events_returns_done_when_nothing_runs(server_room):
    room = server_room
    server = _webui_server(room)
    try:
        port = server.server_address[1]
        lines: list[dict] = []
        _read_events(port, "never-existed", lines)
        assert [ev["type"] for ev in lines] == ["done"]
    finally:
        server.shutdown()
        server.server_close()


def test_events_follows_running_turn_and_reports_running_flag(gated_room):
    """Refresh mid-turn: the reattached client must stay connected while the
    turn runs, then receive its tail events + done; /sessions must flag the
    session as running meanwhile."""
    import threading

    room, gate, calls = gated_room
    server = _webui_server(room)
    try:
        port = server.server_address[1]
        with _post(port, "/new", {}) as resp:
            sid = json.loads(resp.read())["id"]
        turn = threading.Thread(
            target=lambda: _post(port, "/chat", {"message": "work", "sessionId": sid}).read(),
            daemon=True,
        )
        turn.start()
        assert _wait(lambda: len(calls) == 1), "turn never reached the LLM"
        sessions = {
            s["id"]: s
            for s in json.loads(
                urllib.request.urlopen(f"http://127.0.0.1:{port}/sessions", timeout=10).read()
            )["sessions"]
        }
        assert sessions[sid]["running"] is True
        lines: list[dict] = []
        reader = threading.Thread(target=_read_events, args=(port, sid, lines), daemon=True)
        reader.start()
        gate.set()
        def flag() -> bool:
            payload = json.loads(
                urllib.request.urlopen(f"http://127.0.0.1:{port}/sessions", timeout=10).read()
            )
            return {s["id"]: s for s in payload["sessions"]}[sid]["running"]
        assert _wait(lambda: not flag()), "running flag stayed set after the turn finished"
    finally:
        gate.set()
        server.shutdown()
        server.server_close()
