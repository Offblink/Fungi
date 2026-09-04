"""Clone tool-surface tests: whitelists, spawn inheritance, chat no-auto-reply."""

import json

from fungi import tools
from fungi.clone.base import Clone
from fungi.clone.comm import build_comm_clone
from fungi.clone.local import build_local_clone
from fungi.config import Config
from fungi.events import NullSink
from fungi.llm import LLMResult
from fungi.protocol import Envelope
from fungi.trilayer import TriLayer

CFG = Config(api_key="k", endpoint="e", model="m")


class SpyLLM:
    """Records (tool names offered, messages seen); replies with no tool calls."""

    def __init__(self, content: str = "done"):
        self.content = content
        self.offered: list[list[str]] = []
        self.seen: list[list[dict]] = []

    def __call__(self, messages: list[dict], tool_defs: list[dict]) -> LLMResult:
        self.offered.append([d["function"]["name"] for d in tool_defs])
        self.seen.append(list(messages))
        return LLMResult(content=self.content)


class SpyTransport:
    """Captures outbound envelopes; poll returns nothing."""

    def __init__(self):
        self.sent: list[Envelope] = []

    def send(self, env: Envelope) -> dict:
        self.sent.append(env)
        return {"ok": True}

    def poll(self, after: int, timeout: float) -> tuple[list[Envelope], int]:  # noqa: ARG002
        return [], after

    def fs(self, op: str, path: str, **kw) -> dict:  # noqa: ARG002
        return {"error": "no hub attached"}


def agent_names(agent) -> set[str]:
    return {d["function"]["name"] for d in agent.tool_defs}


def test_comm_clone_surface_is_guarded_only():
    clone = build_comm_clone("beta", "alpha", transport=None, cfg=CFG, sink=NullSink())
    names = agent_names(clone.build_agent())
    assert {"send_peer", "ask_consent", "confirm", "spawn", *clone.tools} <= names
    # no unguarded native tool may leak onto a comm clone (spec 6.1)
    assert names.isdisjoint(tools.BASE_TOOL_NAMES)


def test_local_clone_surface_has_native_tools_and_spawn():
    clone = build_local_clone(
        "alpha", transport=None, cfg=CFG, sink=NullSink(), peers_fn=lambda: []
    )
    names = agent_names(clone.build_agent())
    assert names >= tools.BASE_TOOL_NAMES  # YESIR native full set (spec 6.2)
    assert {"delegate", "peers", "confirm", "spawn"} <= names


def test_spawn_children_inherit_clone_file_surface():
    """A comm clone's spawned workers see only the guarded fs tools, never the
    native (unguarded) ones."""
    clone = build_comm_clone("beta", "alpha", transport=None, cfg=CFG, sink=NullSink())
    spy = SpyLLM()
    trilayer = TriLayer(
        CFG,
        NullSink(),
        llm=spy,
        child_tool_names=clone.child_tool_names,
        child_extra_tools=clone.child_extra_tools,
    )
    trilayer._spawn({"goal": "write a file", "reply_format": "text"}, 1)
    offered = spy.offered[0]
    assert "write_file" in offered  # guarded fs surface inherited
    assert "spawn" in offered  # child is L2: it may still spawn L3
    assert set(offered).isdisjoint(tools.BASE_TOOL_NAMES)


def test_local_spawn_children_keep_native_tools():
    clone = build_local_clone("alpha", transport=None, cfg=CFG, sink=NullSink())
    spy = SpyLLM()
    trilayer = TriLayer(
        CFG,
        NullSink(),
        llm=spy,
        child_tool_names=clone.child_tool_names,
        child_extra_tools=clone.child_extra_tools,
    )
    trilayer._spawn({"goal": "g", "reply_format": "text"}, 1)
    assert set(spy.offered[0]) >= tools.BASE_TOOL_NAMES


def test_chat_turn_does_not_auto_reply_and_keeps_history():
    transport = SpyTransport()
    clone = Clone("beta:comm-alpha", transport, CFG, NullSink(), llm=SpyLLM("noted"))
    clone.dispatch(
        Envelope(
            src="alpha:comm-beta", dst="beta:comm-alpha", type="chat", body={"text": "hello there"}
        )
    )
    clone.run_turn(clone._work.get())  # worker path: chat envelope queued
    assert transport.sent == [], "chat turns must not auto-reply"
    assert [m["role"] for m in clone.history] == ["user", "assistant"]
    assert clone.history[0]["content"] == "[alpha:comm-beta] hello there"
    assert clone.history[1]["content"] == "noted"

    # the next chat turn rides the accumulated history
    clone.dispatch(
        Envelope(src="alpha:comm-beta", dst="beta:comm-alpha", type="chat", body={"text": "again"})
    )
    spy2 = SpyLLM("ok")
    clone.llm = spy2
    clone.run_turn(clone._work.get())
    seen = spy2.seen[0]
    assert any(m.get("content") == "[alpha:comm-beta] hello there" for m in seen), seen
    assert seen[-1]["content"] == "[alpha:comm-beta] again"


def test_chat_reply_goes_through_send_peer_only():
    """Explicit send_peer still delivers to the counterpart."""
    transport = SpyTransport()

    class SendPeerLLM:
        def __call__(self, messages: list[dict], tool_defs: list[dict]) -> LLMResult:  # noqa: ARG002
            return LLMResult(
                content="",
                tool_calls=[
                    {
                        "id": "t1",
                        "type": "function",
                        "function": {
                            "name": "send_peer",
                            "arguments": json.dumps({"text": "hi back"}),
                        },
                    }
                ],
            )

    clone = build_comm_clone(
        "beta", "alpha", transport=transport, cfg=CFG, sink=NullSink(), llm=SendPeerLLM()
    )
    env = Envelope(src="alpha:comm-beta", dst="beta:comm-alpha", type="chat", body={"text": "hi"})
    clone.run_turn(env)
    chats = [e for e in transport.sent if e.type == "chat"]
    assert chats and chats[0].dst == "alpha:comm-beta" and chats[0].body == {"text": "hi back"}
