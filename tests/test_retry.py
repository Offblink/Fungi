"""Tests for the /retry message sanitizer (Alt+R continue-from-context)."""

from fungi.server import sanitize_for_retry


def test_strips_trailing_llm_error_marker() -> None:
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "(LLM error: HTTP 402: Insufficient Balance)"},
    ]
    out = sanitize_for_retry(msgs)
    assert out[-1] == {"role": "user", "content": "hi"}


def test_strips_max_rounds_marker() -> None:
    msgs = [
        {"role": "user", "content": "do it"},
        {"role": "assistant", "content": "(Hit max tool rounds: 25.)"},
    ]
    assert len(sanitize_for_retry(msgs)) == 1


def test_strips_dangling_tool_calls_tail() -> None:
    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "t1"}]},
    ]
    assert len(sanitize_for_retry(msgs)) == 1


def test_stops_at_tool_result_tail() -> None:
    """Died after tool results: continue from them, strip nothing."""
    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "t1"}]},
        {"role": "tool", "tool_call_id": "t1", "content": "result"},
    ]
    assert sanitize_for_retry(msgs) == msgs


def test_keeps_normal_assistant_answer() -> None:
    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "Hello!"},
    ]
    assert sanitize_for_retry(msgs) == msgs


def test_strips_multiple_synthetic_tail() -> None:
    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "t1"}]},
        {"role": "assistant", "content": "(LLM error: Connection failed)"},
        {"role": "assistant", "content": "(LLM error: HTTP 429: rate limited)"},
    ]
    out = sanitize_for_retry(msgs)
    assert len(out) == 1
    assert out[0]["role"] == "user"


def test_does_not_mutate_input() -> None:
    msgs = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "(LLM error: x)"}]
    sanitize_for_retry(msgs)
    assert len(msgs) == 2


def test_strips_abort_marker_keeps_partial_content() -> None:
    """Esc-interrupted turn: marker is stripped, partial reply stays for continuation."""
    msgs = [
        {"role": "user", "content": "write a poem"},
        {"role": "assistant", "content": "Roses are red,"},
        {"role": "assistant", "content": "(Aborted)"},
    ]
    out = sanitize_for_retry(msgs)
    assert out[-1] == {"role": "assistant", "content": "Roses are red,"}


def test_strips_bare_abort_marker() -> None:
    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "(Aborted)"},
    ]
    assert len(sanitize_for_retry(msgs)) == 1
