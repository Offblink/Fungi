"""OpenAI-compatible SSE streaming client, stdlib only.

Compatible with DeepSeek's `reasoning_content` deltas and incremental
tool_call argument assembly (same protocol as the PowerShell original).
"""

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field

READ_TIMEOUT = 600  # socket inactivity timeout per read, seconds

DeltaCallback = Callable[[str, str], None]  # (kind, text) with kind in {"text", "reasoning"}


@dataclass
class LLMResult:
    content: str = ""
    reasoning: str = ""
    tool_calls: list[dict] = field(default_factory=list)


class LLMError(Exception):
    """Raised for transport or non-200 responses; message is user-displayable."""


class LLMAbortedError(Exception):
    """Raised when the abort predicate fires mid-stream; carries the partial result."""

    def __init__(self, partial: LLMResult) -> None:
        super().__init__("Aborted by user")
        self.partial = partial


def _apply_delta(tool_acc: dict[int, dict], delta: dict) -> None:
    """Accumulate one streamed delta into tool_call slots, keyed by index."""
    for tc in delta.get("tool_calls") or []:
        idx = int(tc.get("index", 0))
        slot = tool_acc.setdefault(
            idx, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
        )
        if tc.get("id"):
            slot["id"] = tc["id"]
        if tc.get("type"):
            slot["type"] = tc["type"]
        fn = tc.get("function") or {}
        if fn.get("name"):
            slot["function"]["name"] += fn["name"]
        if fn.get("arguments"):
            slot["function"]["arguments"] += fn["arguments"]


def stream_chat(
    model: str,
    endpoint: str,
    api_key: str,
    messages: list[dict],
    tools: list[dict],
    on_delta: DeltaCallback | None = None,
    should_abort: Callable[[], bool] | None = None,
) -> LLMResult:
    """One streaming completion; raises LLMError on failure, LLMAbortedError
    (carrying the partial result) when `should_abort` fires mid-stream."""
    body = json.dumps(
        {"model": model, "messages": messages, "tools": tools, "stream": True}
    ).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=body,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(request, timeout=READ_TIMEOUT)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise LLMError(f"HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise LLMError(f"Connection failed: {getattr(exc, 'reason', exc)}") from exc

    result = LLMResult()
    tool_acc: dict[int, dict] = {}

    def _aborted() -> bool:
        return should_abort is not None and should_abort()

    if _aborted():
        raise LLMAbortedError(result)
    try:
        with resp:
            for raw_line in resp:
                if _aborted():
                    raise LLMAbortedError(result)
                line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                if not line.startswith("data: "):
                    continue
                payload = line[len("data: ") :]
                if payload == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                reasoning = delta.get("reasoning_content")
                if reasoning:
                    result.reasoning += reasoning
                    if on_delta:
                        on_delta("reasoning", reasoning)
                content = delta.get("content")
                if content:
                    result.content += content
                    if on_delta:
                        on_delta("text", content)
                if delta.get("tool_calls"):
                    _apply_delta(tool_acc, delta)
    except OSError as exc:
        raise LLMError(f"Stream interrupted: {exc}") from exc

    result.tool_calls = [tool_acc[i] for i in sorted(tool_acc)]
    return result
