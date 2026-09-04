"""Comm clone contract tests: FakeLLM drives task/chat/consent flows over a real hub."""

import json
import threading
import time

from fungi.clone.base import RemoteTransport
from fungi.clone.comm import SILENT_REPLY, build_comm_clone
from fungi.config import Config
from fungi.events import NullSink
from fungi.llm import LLMResult
from fungi.protocol import Envelope, deserialize

CFG = Config(api_key="k", endpoint="e", model="m")


class ScriptedLLM:
    """Pops scripted LLMResults in order; records requested tool names."""

    def __init__(self, results: list[LLMResult]) -> None:
        self.results = list(results)
        self.requested: list[list[str]] = []

    def __call__(self, _messages: list[dict], tool_defs: list[dict]) -> LLMResult:
        self.requested.append([d["function"]["name"] for d in tool_defs])
        return self.results.pop(0)


def tool_call(name: str, args: dict, call_id: str = "t1") -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


def _joined_room(room):
    hub, clients = room
    for name in ("alpha", "beta"):
        clients[name].post("/api/join", {"name": name, "token": "room-token"})
    return hub, clients


def test_comm_clone_tool_surface():
    clone = build_comm_clone("beta", "alpha", transport=None, cfg=CFG, sink=NullSink())
    assert set(clone.tools) == {
        "send_peer",
        "send_file",
        "ask_consent",
        "confirm",
        "read_file",
        "write_file",
        "edit_file",
        "glob_files",
        "grep_files",
    }
    assert "host beta" in clone.system_prompt
    assert "peer" in clone.system_prompt


def test_task_produces_result_envelope(room):
    _hub, clients = _joined_room(room)
    fake = ScriptedLLM([LLMResult(content="done: 42")])
    clone = build_comm_clone(
        "beta", "alpha", RemoteTransport(clients["beta"]), CFG, NullSink(), llm=fake
    )
    task = Envelope(
        src="alpha:comm-beta",
        dst="beta:comm-alpha",
        type="task",
        body={"goal": "compute the answer", "reply_format": "number"},
    )
    clients["alpha"].send(task)
    messages, _cursor = clients["beta"].poll_env("beta")
    assert len(messages) == 1
    clone.run_turn(messages[0])
    replies, _cursor = clients["alpha"].poll_env("alpha")
    assert len(replies) == 1
    out = replies[0]
    assert out.type == "result"
    assert out.src == "beta:comm-alpha"
    assert out.body == {"ok": True, "payload": "done: 42"}
    assert out.reply_to == task.id


def test_render_input_formats_task():
    clone = build_comm_clone("beta", "alpha", transport=None, cfg=CFG, sink=NullSink())
    task = Envelope(
        src="alpha:comm-beta",
        dst="beta:comm-alpha",
        type="task",
        body={"goal": "g", "reply_format": "rf", "context": "ctx"},
    )
    text = clone.render_input(task)
    assert "[TASK from alpha:comm-beta]" in text
    assert "Goal: g" in text and "Reply format: rf" in text and "Context: ctx" in text


def test_send_peer_tool_sends_chat(room):
    _hub, clients = _joined_room(room)
    fake = ScriptedLLM(
        [
            LLMResult(content="", tool_calls=[tool_call("send_peer", {"text": "hi"})]),
            LLMResult(content="sent"),
        ]
    )
    clone = build_comm_clone(
        "beta", "alpha", RemoteTransport(clients["beta"]), CFG, NullSink(), llm=fake
    )
    task = Envelope(
        src="alpha:comm-beta", dst="beta:comm-alpha", type="task", body={"goal": "greet"}
    )
    clients["alpha"].send(task)
    messages, _cursor = clients["beta"].poll_env("beta")
    clone.run_turn(messages[0])
    replies, _cursor = clients["alpha"].poll_env("alpha")
    chats = [e for e in replies if e.type == "chat" and e.src == "beta:comm-alpha"]
    assert chats and chats[0].body == {"text": "hi"}


def test_silent_marker_turn_delivers_nothing(room):
    """A turn ending with the bare SILENT_REPLY marker is true silence: the
    fallback auto-delivery must not ship prose 'silence declarations' back to
    the peer (the five-round mutual-silence loop of 2026-09-04)."""
    _hub, clients = _joined_room(room)
    fake = ScriptedLLM([LLMResult(content=SILENT_REPLY)])
    clone = build_comm_clone(
        "beta", "alpha", RemoteTransport(clients["beta"]), CFG, NullSink(), llm=fake
    )
    chat = Envelope(
        src="alpha:comm-beta", dst="beta:comm-alpha", type="chat", body={"text": "ping"}
    )
    clients["alpha"].send(chat)
    messages, _cursor = clients["beta"].poll_env("beta")
    clone.run_turn(messages[0])
    replies, _cursor = clients["alpha"].poll_env("alpha")
    chats = [e for e in replies if e.type == "chat" and e.src == "beta:comm-alpha"]
    assert not chats


def test_consent_flow_wakes_blocked_write(room):
    hub, clients = _joined_room(room)
    fake = ScriptedLLM(
        [
            LLMResult(
                content="",
                tool_calls=[
                    tool_call(
                        "ask_consent",
                        {
                            "host": "alpha",
                            "action": "write",
                            "path": "homes/alpha/notes.md",
                            "reason": "share meeting notes",
                        },
                        call_id="t1",
                    )
                ],
            ),
            LLMResult(
                content="",
                tool_calls=[
                    tool_call(
                        "write_file",
                        {"path": "homes/alpha/notes.md", "content": "shared"},
                        call_id="t2",
                    )
                ],
            ),
            LLMResult(content="written"),
        ]
    )
    clone = build_comm_clone(
        "beta",
        "alpha",
        RemoteTransport(clients["beta"]),
        CFG,
        NullSink(),
        llm=fake,
        ask_timeout_s=5,
        poll_timeout=0.1,
    )

    task = Envelope(
        src="alpha:comm-beta", dst="beta:comm-alpha", type="task", body={"goal": "share"}
    )
    clients["alpha"].send(task)

    # alpha's "user" (local clone stand-in): answer every ask with yes
    stop = threading.Event()
    results: list[Envelope] = []

    def responder():
        after = 0
        while not stop.is_set():
            _code, out = clients["alpha"].poll_raw("alpha", after=after, timeout=0.2)
            after = out["cursor"]
            for raw in out["messages"]:
                if raw["type"] == "ask":
                    clients["alpha"].send(
                        Envelope(
                            src=raw["dst"],
                            dst=raw["src"],
                            type="answer",
                            body={"value": "yes"},
                            reply_to=raw["id"],
                        )
                    )
                else:
                    results.append(deserialize(raw))

    watcher = threading.Thread(target=responder)
    watcher.start()
    clone.start()
    try:
        deadline = time.time() + 15
        while time.time() < deadline and not any(r.type == "result" for r in results):
            time.sleep(0.05)
        assert any(r.type == "result" for r in results), "turn never completed"
        assert any("written" in r.body.get("payload", "") for r in results)
        # the consent ask reached the LLM, and the guarded write ran after "yes"
        assert "ask_consent" in fake.requested[0]
        assert "write_file" in fake.requested[1]
    finally:
        clone.stop()
        stop.set()
        watcher.join(timeout=3)

    # the consented write actually landed, and the ask is ANSWERED in the hub registry
    notes = hub.store.root / "homes" / "alpha" / "notes.md"
    assert notes.read_text(encoding="utf-8") == "shared"
    comm_tools = clone.tools["ask_consent"].fn.__self__
    record = hub.asks.get(comm_tools.consent_id)
    assert record is not None and record["status"] == "answered"

def test_turn_end_hook_receives_transcript(room):
    """The room's recorder gets the full turn transcript (system + history +
    new turn) for the friend view."""
    _hub, clients = _joined_room(room)
    fake = ScriptedLLM([LLMResult(content="reply text")])
    seen: list[tuple[str, list[dict]]] = []
    clone = build_comm_clone(
        "beta",
        "alpha",
        RemoteTransport(clients["beta"]),
        CFG,
        NullSink(),
        llm=fake,
        on_turn_end=lambda env_type, messages, agent: seen.append((env_type, list(messages))),
    )
    chat = Envelope(
        src="alpha:comm-beta",
        dst="beta:comm-alpha",
        type="chat",
        body={"text": "hello"},
    )
    clients["alpha"].send(chat)
    messages, _cursor = clients["beta"].poll_env("beta")
    clone.run_turn(messages[0])
    assert seen and seen[0][0] == "chat"
    transcript = seen[0][1]
    assert transcript[0]["role"] == "system"
    assert "[alpha:comm-beta]" in transcript[-2]["content"]
    assert transcript[-1]["content"] == "reply text"
    assert transcript[-1]["role"] == "assistant"
