"""Tests for the agent turn loop, driven by scripted (Fake) LLM completions."""

import fungi.agent as fungi_agent
from fungi.agent import Agent, BoundTool, wrap_reasoning_events
from fungi.config import Config
from fungi.events import FnSink
from fungi.llm import LLMAbortedError, LLMError, LLMResult


class FakeLLM:
    """Returns scripted LLMResults in order; records every request."""

    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def __call__(self, messages, tool_defs):
        self.calls.append((list(messages), list(tool_defs)))
        return self.results.pop(0)


def make_agent(results, extra_tools=None):
    events = []
    sink = FnSink(lambda t, c: events.append((t, c)))
    fake = FakeLLM(results)
    agent = Agent(
        Config(api_key="k", endpoint="e", model="m"), sink, extra_tools=extra_tools, llm=fake
    )
    return agent, fake, events


def tool_call(name, args_json, call_id="t1"):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": args_json}}


def test_plain_text_turn():
    agent, _fake, events = make_agent([LLMResult(content="hello there")])
    messages = [{"role": "user", "content": "hi"}]
    agent.run(messages)

    assert messages[0]["role"] == "system"  # system prompt injected
    assert messages[1]["role"] == "user"
    assert messages[2] == {"role": "assistant", "content": "hello there", "reasoning": ""}
    assert events == []  # no deltas from FakeLLM


def test_tool_round_then_answer(tmp_path):
    target = tmp_path / "f.txt"
    target.write_text("data", encoding="utf-8")
    results = [
        LLMResult(tool_calls=[tool_call("read", f'{{"path": "{target.as_posix()}"}}')]),
        LLMResult(content="the file says data"),
    ]
    agent, _fake, events = make_agent(results)
    messages = [{"role": "user", "content": "read it"}]
    agent.run(messages)

    roles = [m["role"] for m in messages]
    assert roles == ["system", "user", "assistant", "tool", "assistant"]
    assert "1:data" in messages[3]["content"]
    assert [e[0] for e in events] == ["tool", "tool_result"]
    assert messages[4]["content"] == "the file says data"


def test_tool_streaming_deltas_reach_sink():
    results = [
        LLMResult(content="let me check", tool_calls=[tool_call("bash", '{"command": "echo hi"}')]),
        LLMResult(content="done"),
    ]
    agent, _fake, events = make_agent(results)
    agent.run([{"role": "user", "content": "go"}])
    assert events[0][0] == "tool"
    assert events[0][1]["name"] == "bash"
    assert "hi" in events[1][1]["content"]


def test_extra_tool_dispatch():
    calls = []

    def spawn_fn(args):
        calls.append(args)
        return "child finished"

    spawn = BoundTool(
        schema={"type": "function", "function": {"name": "spawn", "parameters": {}}}, fn=spawn_fn
    )
    results = [
        LLMResult(tool_calls=[tool_call("spawn", '{"goal": "x"}')]),
        LLMResult(content="orchestrated"),
    ]
    agent, _fake, _events = make_agent(results, extra_tools={"spawn": spawn})
    messages = [{"role": "user", "content": "dispatch"}]
    agent.run(messages)

    assert calls == [{"goal": "x"}]
    assert messages[3]["content"] == "child finished"
    assert messages[4]["content"] == "orchestrated"


def test_extra_tool_exception_becomes_error_result():
    def bad_fn(_args):
        raise ValueError("boom")

    bad = BoundTool(
        schema={"type": "function", "function": {"name": "bad", "parameters": {}}}, fn=bad_fn
    )
    results = [LLMResult(tool_calls=[tool_call("bad", "{}")]), LLMResult(content="recovered")]
    agent, _fake, _events = make_agent(results, extra_tools={"bad": bad})
    messages = [{"role": "user", "content": "go"}]
    agent.run(messages)

    assert messages[3]["content"].startswith("ERROR: boom")
    assert messages[4]["content"] == "recovered"


def test_invalid_tool_args_json():
    results = [LLMResult(tool_calls=[tool_call("read", "{not json")]), LLMResult(content="ok")]
    agent, _fake, _events = make_agent(results)
    messages = [{"role": "user", "content": "go"}]
    agent.run(messages)
    assert messages[3]["content"].startswith("ERROR: Missing required argument: path")


def test_llm_error_reported_not_raised():
    class FailingLLM:
        def __call__(self, _messages, _tool_defs):
            raise LLMError("HTTP 500: sad")

    events = []
    sink = FnSink(lambda t, c: events.append((t, c)))
    agent = Agent(Config(), sink, llm=FailingLLM())
    messages = [{"role": "user", "content": "hi"}]
    agent.run(messages)

    assert ("error", "HTTP 500: sad") in events
    assert messages[-1]["content"].startswith("(LLM error")


def test_max_rounds_guard():
    endless = LLMResult(tool_calls=[tool_call("bash", '{"command": "echo x"}', call_id="loop")])
    agent, _fake, events = make_agent([endless] * 25)
    messages = [{"role": "user", "content": "go"}]
    agent.run(messages)

    assert any(t == "error" and "Max tool rounds" in str(c) for t, c in events)
    assert messages[-1]["content"].startswith("(Hit max tool rounds")


def test_tool_defs_include_extra_tools():
    spawn = BoundTool(schema={"type": "function", "function": {"name": "spawn"}}, fn=lambda _a: "")
    agent, _fake, _events = make_agent([], extra_tools={"spawn": spawn})
    names = [d["function"]["name"] for d in agent.tool_defs]
    assert "spawn" in names
    assert "read" in names


def test_session_messages_reused():
    """Preexisting system message (loaded session) is not duplicated."""
    agent, _fake, _events = make_agent([LLMResult(content="ok")])
    messages = [{"role": "system", "content": "custom"}, {"role": "user", "content": "hi"}]
    agent.run(messages)
    assert sum(1 for m in messages if m["role"] == "system") == 1
    assert messages[0]["content"] == "custom"


def test_wrap_reasoning_events_brackets_stream():
    events: list = []
    sink = FnSink(lambda t, c: events.append((t, c)))
    on_delta, state = wrap_reasoning_events(sink)
    on_delta("reasoning", "a")
    on_delta("reasoning", "b")
    on_delta("text", "x")
    assert [t for t, _ in events] == [
        "reasoning_start",
        "reasoning",
        "reasoning",
        "reasoning_end",
        "text",
    ]
    assert state == {"started": True, "ended": True}


def test_wrap_reasoning_events_no_reasoning_no_events():
    events: list = []
    sink = FnSink(lambda t, c: events.append((t, c)))
    on_delta, state = wrap_reasoning_events(sink)
    on_delta("text", "x")
    assert events == [("text", "x")]
    assert state == {"started": False, "ended": False}


def test_abort_before_round_appends_marker():
    """Abort flag set before the loop: no LLM call, marker appended."""
    events = []
    sink = FnSink(lambda t, c: events.append((t, c)))
    fake = FakeLLM([LLMResult(content="never reached")])
    agent = Agent(Config(api_key="k"), sink, llm=fake, should_abort=lambda: True)
    messages = [{"role": "user", "content": "hi"}]
    agent.run(messages)

    assert fake.calls == []  # no LLM round ran
    assert ("error", "Aborted by user") in events
    assert messages[-1] == {"role": "assistant", "content": "(Aborted)"}


def test_abort_mid_stream_keeps_partial_reply():
    """LLMAbortedError from the LLM layer: partial content stays, marker appended."""
    partial = LLMResult(content="Roses are red,")

    class InterruptingLLM:
        def __call__(self, _messages, _tool_defs):
            raise LLMAbortedError(partial)

    events = []
    sink = FnSink(lambda t, c: events.append((t, c)))
    agent = Agent(Config(api_key="k"), sink, llm=InterruptingLLM())
    messages = [{"role": "user", "content": "poem"}]
    agent.run(messages)

    assert ("error", "Aborted by user") in events
    assert messages[-2] == {"role": "assistant", "content": "Roses are red,"}
    assert messages[-1] == {"role": "assistant", "content": "(Aborted)"}


def test_should_abort_passed_to_stream_chat(monkeypatch):
    """Default LLM path forwards the abort predicate to stream_chat."""

    def fake_stream(
        _model, _endpoint, _api_key, _messages, _tool_defs, _on_delta=None, should_abort=None
    ):
        seen.append(should_abort)
        return LLMResult(content="ok")

    seen: list = []
    monkeypatch.setattr(fungi_agent, "stream_chat", fake_stream)
    agent = Agent(Config(api_key="k"), FnSink(lambda _t, _c: None), should_abort=lambda: False)
    agent.run([{"role": "user", "content": "hi"}])
    assert len(seen) == 1 and callable(seen[0])
