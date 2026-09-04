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


def test_slider_does_not_auto_allow_generic_ask_user(server_room):
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
    assert room.cards.pending(), "generic ask_user must still raise a card"


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
