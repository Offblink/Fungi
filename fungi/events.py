"""Event sinks: where agent turns emit their stream (console, web NDJSON, or any callable)."""

import sys
from collections.abc import Callable
from typing import Any, ClassVar, Protocol

EmitFn = Callable[[str, Any], None]


class Sink(Protocol):
    def emit(self, event_type: str, content: Any) -> None: ...


class FnSink:
    """Adapts a plain callable into a Sink."""

    def __init__(self, fn: EmitFn) -> None:
        self._fn = fn

    def emit(self, event_type: str, content: Any = None) -> None:
        self._fn(event_type, content)


class NullSink:
    def emit(self, event_type: str, content: Any = None) -> None:
        pass


class ConsoleSink:
    """Terminal output, mirroring the PowerShell original's formatting."""

    _COLORS: ClassVar[dict] = {
        "reasoning": "\033[90m",
        "tool": "\033[33m",
        "tool_args": "\033[93m",
        "result": "\033[90m",
        "error": "\033[31m",
    }
    _RESET = "\033[0m"

    def emit(self, event_type: str, content: Any = None) -> None:
        if event_type == "text":
            sys.stdout.write(str(content))
            sys.stdout.flush()
        elif event_type == "tool":
            args = content.get("args", "")
            sys.stdout.write(f"  [{content['name']}] {args}")
            sys.stdout.flush()
        elif event_type == "tool_result":
            print(f"\n{content['content']}")
        elif event_type == "reasoning":
            sys.stdout.write(f"{self._COLORS['reasoning']}{content}{self._RESET}")
            sys.stdout.flush()
        elif event_type == "agent_spawn":
            layer = content.get("layer", 2)
            goal = str(content.get("goal", ""))[:100]
            print(f"\n\033[35m  \U0001f9e9 spawn L{layer}: {goal}\033[0m")
        elif event_type == "agent_status":
            marks = {"running": "\u23f3", "done": "\u2705", "failed": "\u274c"}
            mark = marks.get(content.get("status"), "")
            print(f"\033[35m  {mark} L-agent {content.get('id')} {content.get('status')}\033[0m")
        elif event_type == "ask":
            for q in content.get("questions", []):
                opts = "/".join(o.get("label", "") for o in q.get("options", []))
                suffix = f" [{opts}]" if opts else ""
                print(f"\n\033[36m  \U0001f4dd {q.get('question', '')}{suffix}\033[0m")
        elif event_type == "agent_event":
            inner = content.get("event") or {}
            kind = inner.get("type")
            if kind == "tool":
                print(f"\033[90m      \u21b3 [{inner['content'].get('name')}]\033[0m")
            elif kind == "error":
                print(f"\033[31m      \u21b3 error: {inner.get('content')}\033[0m")
        elif event_type == "error":
            print(f"\n[ERROR: {content}]")
        # newline / reasoning_start / reasoning_end: console-only cosmetics, ignored here
