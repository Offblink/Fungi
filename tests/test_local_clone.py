"""Local clone contract tests: delegate rides the full clone loop; asks reach on_ask."""

import json
import threading
import time

from fungi.clone.base import RemoteTransport
from fungi.clone.local import build_local_clone
from fungi.config import Config
from fungi.events import NullSink
from fungi.llm import LLMResult
from fungi.protocol import Envelope

CFG = Config(api_key="k", endpoint="e", model="m")


class RecordingLLM:
    """Pops scripted LLMResults in order; records every call's messages."""

    def __init__(self, results: list[LLMResult]) -> None:
        self.results = list(results)
        self.calls: list[list[dict]] = []

    def __call__(self, messages: list[dict], _tool_defs: list[dict]) -> LLMResult:
        self.calls.append(list(messages))
        return self.results.pop(0)


def tool_call(name: str, args: dict, call_id: str = "t1") -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


def _comm_stub(clients, stop: threading.Event, payload: str) -> None:
    """Stand-in for the remote comm clone: task envelope -> result envelope."""

    def loop():
        after = 0
        while not stop.is_set():
            messages, after = clients["beta"].poll_env("beta", after, 0.2)
            for env in messages:
                if env.type == "task":
                    clients["beta"].send(
                        Envelope(
                            src=env.dst,
                            dst=env.src,
                            type="result",
                            body={"ok": True, "payload": payload},
                            reply_to=env.id,
                        )
                    )

    threading.Thread(target=loop, daemon=True).start()


def _joined_room(room):
    hub, clients = room
    for name in ("alpha", "beta"):
        clients[name].post("/api/join", {"name": name, "token": "room-token"})
    return hub, clients


def _wait(predicate, timeout_s: float, message: str):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(message)


def test_local_clone_tool_surface():
    clone = build_local_clone(
        "alpha", transport=None, cfg=CFG, sink=NullSink(), peers_fn=lambda: ["beta"]
    )
    assert set(clone.tools) == {"ask_user", "delegate", "peers"}
    assert "local Orchestrator" in clone.system_prompt
    assert "host alpha" in clone.system_prompt


def test_local_clone_dispatches_incoming_ask_to_callback():
    seen: list[Envelope] = []
    clone = build_local_clone("alpha", transport=None, cfg=CFG, sink=NullSink(), on_ask=seen.append)
    ask = Envelope(
        src="beta:comm-alpha",
        dst="alpha:local",
        type="ask",
        body={"question": "Allow write?", "from": "beta:comm-alpha"},
    )
    clone.dispatch(ask)
    assert seen == [ask]


def test_delegate_roundtrip_via_full_loop(room):
    hub, clients = _joined_room(room)
    stop = threading.Event()
    _comm_stub(clients, stop, payload="42 items synced")
    fake = RecordingLLM(
        [
            LLMResult(
                content="",
                tool_calls=[
                    tool_call("delegate", {"host": "beta", "goal": "sync", "reply_format": "count"})
                ],
            ),
            LLMResult(content="done"),
        ]
    )
    # capture the local clone's chat reply on the hub-side local clone address
    reply_inbox = hub.relay.register_local("srv:local")
    local = build_local_clone(
        "alpha",
        RemoteTransport(clients["alpha"]),
        CFG,
        NullSink(),
        llm=fake,
        peers_fn=lambda: ["beta"],
        poll_timeout=0.1,
        ask_timeout_s=5,
    )
    local.start()
    try:
        hub.send(
            Envelope(src="srv:local", dst="alpha:local", type="chat", body={"text": "sync please"})
        )
        replies, _cursor = reply_inbox.after(0, 10.0)
        assert replies and replies[0].type == "chat"
        assert replies[0].body == {"text": "done"}
        # the delegate tool result (the remote payload) reached the agent conversation
        tool_results = [
            m.get("content", "") for call in fake.calls for m in call if m.get("role") == "tool"
        ]
        assert any("42 items synced" in c for c in tool_results), fake.calls
    finally:
        local.stop()
        stop.set()


def test_delegate_timeout_via_full_loop(room):
    hub, clients = _joined_room(room)
    fake = RecordingLLM(
        [
            LLMResult(
                content="",
                tool_calls=[tool_call("delegate", {"host": "beta", "goal": "nobody home"})],
            ),
            LLMResult(content="gave up"),
        ]
    )
    local = build_local_clone(
        "alpha",
        RemoteTransport(clients["alpha"]),
        CFG,
        NullSink(),
        llm=fake,
        peers_fn=lambda: ["beta"],
        poll_timeout=0.1,
        ask_timeout_s=5,
    )
    local.start()
    try:
        hub.send(
            Envelope(src="srv:local", dst="alpha:local", type="chat", body={"text": "try beta"})
        )
        _wait(lambda: len(fake.calls) >= 2, 10.0, "second LLM call never happened")
        tool_results = [
            m.get("content", "") for call in fake.calls for m in call if m.get("role") == "tool"
        ]
        assert any(c.startswith("FAIL:") for c in tool_results)
    finally:
        local.stop()
