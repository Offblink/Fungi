"""Tests for LLM delta assembly (offline)."""

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from fungi.llm import LLMAbortedError, LLMResult, _apply_delta, stream_chat


def test_tool_call_single_complete():
    acc: dict[int, dict] = {}
    _apply_delta(
        acc,
        {
            "tool_calls": [
                {
                    "index": 0,
                    "id": "call1",
                    "type": "function",
                    "function": {"name": "read", "arguments": '{"path"'},
                }
            ]
        },
    )
    _apply_delta(acc, {"tool_calls": [{"index": 0, "function": {"arguments": ': "a.txt"}'}}]})
    assert acc[0]["id"] == "call1"
    assert acc[0]["function"]["name"] == "read"
    assert acc[0]["function"]["arguments"] == '{"path": "a.txt"}'


def test_tool_call_parallel_indexes():
    acc: dict[int, dict] = {}
    _apply_delta(
        acc,
        {
            "tool_calls": [
                {"index": 0, "id": "a", "function": {"name": "read", "arguments": "{}"}},
                {"index": 1, "id": "b", "function": {"name": "glob", "arguments": ""}},
            ]
        },
    )
    _apply_delta(
        acc, {"tool_calls": [{"index": 1, "function": {"arguments": '{"pattern": "*.py"}'}}]}
    )
    assert acc[0]["function"]["name"] == "read"
    assert acc[1]["function"]["arguments"] == '{"pattern": "*.py"}'


def test_tool_call_missing_index_defaults_zero():
    acc: dict[int, dict] = {}
    _apply_delta(acc, {"tool_calls": [{"id": "x", "function": {"name": "bash", "arguments": ""}}]})
    assert acc[0]["function"]["name"] == "bash"


def test_delta_without_tool_calls_is_noop():
    acc: dict[int, dict] = {}
    _apply_delta(acc, {"content": "hello"})
    assert acc == {}


def test_llm_result_defaults():
    result = LLMResult()
    assert result.content == ""
    assert result.tool_calls == []
    assert result.reasoning == ""


def test_stream_chat_abort_mid_stream_carries_partial():
    """LLMAbortedError raised between SSE lines carries the partial content."""
    lines = [
        b'data: {"choices": [{"delta": {"content": "one"}}]}\n\n',
        b'data: {"choices": [{"delta": {"content": "two"}}]}\n\n',
        b"data: [DONE]\n\n",
    ]
    deltas = {"n": 0}

    def on_delta(_kind, _text):
        deltas["n"] += 1

    def should_abort():
        return deltas["n"] >= 1

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            try:
                for line in lines:
                    self.wfile.write(line)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, fmt, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with pytest.raises(LLMAbortedError) as excinfo:
            stream_chat(
                "m",
                f"http://127.0.0.1:{server.server_address[1]}/v1",
                "k",
                [],
                [],
                on_delta=on_delta,
                should_abort=should_abort,
            )
        assert excinfo.value.partial.content == "one"
    finally:
        server.shutdown()
