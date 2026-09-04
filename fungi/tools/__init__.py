"""Tool registry: OpenAI function-calling schemas, dispatch, and per-layer whitelists.

Layer rules (see docs/spec.md 2.3):
- L1/L2: all base tools; `spawn` and `inquire` are attached by fungi.trilayer / fungi.tools.ask.
- L3:   read/write/edit/glob/grep/bash only (basic worker, no web, no dispatch).
"""

import inspect

from fungi.tools.files import tool_edit, tool_read, tool_write
from fungi.tools.search import tool_glob, tool_grep
from fungi.tools.shell import tool_bash
from fungi.tools.webtools import tool_web, tool_web_search


def _schema(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required},
        },
    }


TOOLS: dict[str, dict] = {
    "read": {
        "schema": _schema(
            "read",
            "Read a file, numbered lines. Append `:N` for one line, `:N-M` for a range.",
            {
                "path": {
                    "type": "string",
                    "description": "File path, optionally with :N or :N-M selector",
                }
            },
            ["path"],
        ),
        "fn": tool_read,
    },
    "write": {
        "schema": _schema(
            "write",
            "Create or overwrite a file.",
            {"path": {"type": "string"}, "content": {"type": "string"}},
            ["path", "content"],
        ),
        "fn": tool_write,
    },
    "edit": {
        "schema": _schema(
            "edit",
            "Replace old_string with new_string. old_string must match exactly and be unique.",
            {
                "path": {"type": "string"},
                "old_string": {"type": "string"},
                "new_string": {"type": "string"},
            },
            ["path", "old_string", "new_string"],
        ),
        "fn": tool_edit,
    },
    "bash": {
        "schema": _schema(
            "bash",
            "Run a command in cmd.exe. Timeout: 120 seconds. Output truncated.",
            {
                "command": {"type": "string"},
                "cwd": {"type": "string", "description": "Working directory (optional)"},
            },
            ["command"],
        ),
        "fn": tool_bash,
    },
    "glob": {
        "schema": _schema(
            "glob",
            "Find files by pattern (e.g. *.py). Returns paths newest-first.",
            {
                "pattern": {"type": "string"},
                "path": {"type": "string", "description": "Base directory (default: current)"},
            },
            ["pattern"],
        ),
        "fn": tool_glob,
    },
    "grep": {
        "schema": _schema(
            "grep",
            "Search files with a regex. Returns file:line:match.",
            {
                "pattern": {"type": "string"},
                "path": {"type": "string", "description": "File or directory (default: current)"},
            },
            ["pattern"],
        ),
        "fn": tool_grep,
    },
    "web": {
        "schema": _schema(
            "web",
            "Fetch a web page and return its text content.",
            {"url": {"type": "string", "description": "Full URL to fetch"}},
            ["url"],
        ),
        "fn": tool_web,
    },
    "web_search": {
        "schema": _schema(
            "web_search",
            "Search the web via Brave Search. Returns titles, URLs, and snippets.",
            {"query": {"type": "string"}},
            ["query"],
        ),
        "fn": tool_web_search,
    },
}

BASE_TOOL_NAMES = frozenset(TOOLS)
L3_TOOL_NAMES = frozenset({"read", "write", "edit", "glob", "grep", "bash"})


def tool_defs(names: frozenset[str] | set[str] | None = None) -> list[dict]:
    """Function-calling defs for the given whitelist (default: all base tools)."""
    selected = names if names is not None else BASE_TOOL_NAMES
    return [TOOLS[name]["schema"] for name in TOOLS if name in selected]


def dispatch(name: str, args: dict) -> str:
    tool = TOOLS.get(name)
    if tool is None:
        return f"ERROR: Unknown tool: {name}"
    for required in tool["schema"]["function"]["parameters"].get("required", []):
        if not args.get(required):
            return f"ERROR: Missing required argument: {required}"
    accepted = inspect.signature(tool["fn"]).parameters
    kwargs = {k: v for k, v in args.items() if k in accepted}
    try:
        result = tool["fn"](**kwargs)
    except (OSError, ValueError, TypeError) as exc:
        return f"ERROR: {exc}"
    return str(result)
