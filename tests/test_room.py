"""Room mode tests: cards, consent rules, server/client assembly, WebUI routing."""

import json
import time

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


def test_consent_rules_persist(tmp_path):
    path = tmp_path / "rules.json"
    rules = ConsentRules(path)
    assert rules.allows("beta:comm-alpha") is False
    rules.allow("beta:comm-alpha")
    assert rules.allows("beta:comm-alpha") is True
    reloaded = ConsentRules(path)
    assert reloaded.allows("beta:comm-alpha") is True
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data == {"always_allow": ["beta:comm-alpha"]}


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

    assert runtime.route_answer(ask.id, "yes") is True
    msgs, _cursor = requester_inbox.after(0, 2.0)
    answers = [m for m in msgs if m.type == "answer" and m.reply_to == ask.id]
    assert answers and answers[0].body == {"value": "yes"}
    assert room.hub.asks.get(ask.id)["status"] == "answered"


def test_server_always_allow_silently_answers(server_room):
    room = server_room
    room.rules.allow("alpha:comm-beta")

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


def test_route_answer_always_grants_future_asks(server_room):
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
    assert runtime.route_answer(ask.id, "always") is True
    assert room.rules.allows("alpha:comm-gamma")

    # next consent ask from the same orchestrator: no card, silent yes
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
    assert _wait(
        lambda: any(
            m.type == "answer" and m.reply_to == second.id and m.body == {"value": "yes"}
            for m in _all_msgs(requester_inbox)
        )
    ), "second ask was not auto-answered"
    assert not [c for c in room.cards.pending() if c["id"] == second.id]


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
