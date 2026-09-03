"""Minimal MCP stdio server (legacy era) for tests and E2E.

Tools: echo(text), add(a, b), slow(seconds) — sleeps for the timeout path,
fail() — returns an isError result, empty() — no content at all, rich() —
every content type, junk() — noise lines before the real reply, rpcfail() —
a JSON-RPC error object, die() — exits before replying. tools/list serves
PAGE_SIZE tools per page to exercise cursor handling. Speaks the
2025-06-18 handshake.
"""

import json
import sys
import time

PAGE_SIZE = 2

TOOLS = [
    {
        "name": "echo",
        "description": "Echo the given text back.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
    {
        "name": "add",
        "description": "Add two integers.",
        "inputSchema": {
            "type": "object",
            "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
            "required": ["a", "b"],
        },
    },
    {
        "name": "slow",
        "description": "Sleep for the given seconds, then reply.",
        "inputSchema": {
            "type": "object",
            "properties": {"seconds": {"type": "number"}},
            "required": ["seconds"],
        },
    },
    {
        "name": "fail",
        "description": "Always reports a tool error.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "empty",
        "description": "Replies with an empty content list.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "rich",
        "description": "Replies with every content type.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "junk",
        "description": "Writes non-MCP noise to stdout before replying.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "rpcfail",
        "description": "Replies with a JSON-RPC error object.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "die",
        "description": "Exits the process before replying.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def reply(rid, result):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}) + "\n")
    sys.stdout.flush()


def call(name, args):
    if name == "echo":
        return f"echo: {args.get('text', '')}", False
    if name == "add":
        return str(int(args.get("a", 0)) + int(args.get("b", 0))), False
    if name == "slow":
        time.sleep(float(args.get("seconds", 0)))
        return "finally awake", False
    if name == "fail":
        return "the demo tool failed on purpose", True
    return f"unknown tool: {name}", True


def tools_page(params):
    try:
        start = int(str(params.get("cursor") or 0))
    except ValueError:
        start = 0
    page = TOOLS[start : start + PAGE_SIZE]
    result = {"tools": page}
    if start + PAGE_SIZE < len(TOOLS):
        result["nextCursor"] = str(start + PAGE_SIZE)
    return result


def rich_content():
    return [
        {"type": "text", "text": "see below"},
        {"type": "image", "mimeType": "image/png"},
        {"type": "audio", "mimeType": "audio/wav"},
        {"type": "resource_link", "name": "doc", "uri": "file:///tmp/doc"},
        {"type": "resource", "resource": {"uri": "file:///tmp/x", "text": "hello"}},
        {"type": "widget"},
        42,
    ]


def main():
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        method, rid = msg.get("method"), msg.get("id")
        if method == "initialize":
            reply(
                rid,
                {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "fake-mcp", "version": "0.0.0"},
                },
            )
        elif method == "tools/list":
            reply(rid, tools_page(msg.get("params") or {}))
        elif method == "tools/call":
            params = msg.get("params") or {}
            name = str(params.get("name"))
            if name == "empty":
                reply(rid, {"content": [], "isError": False})
            elif name == "rich":
                reply(rid, {"content": rich_content(), "isError": False})
            elif name == "junk":
                sys.stdout.write("this is not json\n")
                sys.stdout.write(
                    json.dumps({"jsonrpc": "2.0", "method": "notifications/noise"}) + "\n"
                )
                sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": 99999, "result": {}}) + "\n")
                sys.stdout.flush()
                reply(
                    rid,
                    {"content": [{"type": "text", "text": "junk survived"}], "isError": False},
                )
            elif name == "rpcfail":
                err = {"code": -32603, "message": "boom"}
                sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid, "error": err}) + "\n")
                sys.stdout.flush()
            elif name == "die":
                sys.exit(0)
            else:
                text, is_error = call(name, params.get("arguments") or {})
                reply(rid, {"content": [{"type": "text", "text": text}], "isError": is_error})
        elif rid is not None:
            reply(rid, {})


if __name__ == "__main__":
    main()
