#!/usr/bin/env python3
"""A tiny MCP server for tests — speaks the 2025-06-18 stdio dialect.

Behaviour (selected with argv[1]):

* ``normal``   — full handshake, two tools (``echo`` text, ``add`` with an
                 object schema), a failing tool (``boom``), and a
                 ``tools/list_changed`` notification after ``refresh``.
* ``slow``     — like ``normal`` but every ``tools/call`` sleeps 5 s, for
                 the timeout test.
* ``chatty``   — prints a non-JSON banner line to stdout before answering
                 (servers that log to stdout exist; the client must
                 survive them).
* ``crash``    — exits immediately (a broken install).
* ``server_req``— after initialize, sends the client a ``sampling/createMessage``
                 request and only answers real requests afterwards, so the
                 client's decline path is exercised.
"""
import json
import sys
import time

MODE = sys.argv[1] if len(sys.argv) > 1 else "normal"

TOOLS = [
    {
        "name": "echo",
        "description": "Echo the text back.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
    {
        "name": "add",
        "description": "Add two numbers.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "a": {"type": "number"},
                "b": {"type": "number"},
            },
            "required": ["a", "b"],
        },
    },
    {
        "name": "boom",
        "description": "Always fails with a tool error.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def reply(req_id, result):
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def tool_error(req_id, message):
    return {"jsonrpc": "2.0", "id": req_id, "result": {
        "content": [{"type": "text", "text": message}],
        "isError": True,
    }}


def call_tool(name, args):
    if name == "echo":
        return {"content": [{"type": "text",
                             "text": f"echo: {args.get('text', '')}"}],
                "isError": False}
    if name == "add":
        return {"content": [{"type": "text",
                             "text": str(args.get("a", 0) + args.get("b", 0))}],
                "isError": False}
    if name == "boom":
        return {"content": [{"type": "text", "text": "deliberate failure"}],
                "isError": True}
    return {"content": [{"type": "text", "text": f"unknown tool {name}"}],
            "isError": True}


def emit(message):
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def main():
    if MODE == "crash":
        sys.stderr.write("fatal: broken install\n")
        sys.exit(3)

    if MODE == "chatty":
        sys.stdout.write("hello, I log to stdout, sorry\n")
        sys.stdout.flush()

    refreshed = False
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            continue
        method = message.get("method", "")
        req_id = message.get("id")

        if req_id is not None:
            if method == "initialize":
                emit(reply(req_id, {
                    "protocolVersion": message["params"]["protocolVersion"],
                    "capabilities": {"tools": {"listChanged": True}},
                    "serverInfo": {"name": f"test-server-{MODE}",
                                   "version": "0.1.0"},
                }))
            elif method == "tools/list":
                tools = list(TOOLS)
                if refreshed:
                    tools.append({
                        "name": "extra",
                        "description": "Appears after refresh.",
                        "inputSchema": {"type": "object", "properties": {}},
                    })
                emit(reply(req_id, {"tools": tools}))
            elif method == "tools/call":
                params = message.get("params") or {}
                if MODE == "slow":
                    time.sleep(5)
                emit(reply(req_id, call_tool(params.get("name", ""),
                                             params.get("arguments") or {})))
            elif method == "ping":
                emit(reply(req_id, {}))
            else:
                emit({"jsonrpc": "2.0", "id": req_id, "error": {
                    "code": -32601, "message": "method not found"}})
        elif method == "notifications/initialized":
            if MODE == "server_req":
                # A server-initiated request: the client must decline it
                # without hanging or dying.
                emit({"jsonrpc": "2.0", "id": 500,
                      "method": "sampling/createMessage",
                      "params": {"messages": []}})
            if MODE == "normal" and refreshed is False:
                pass
        elif method == "internal/refresh":
            refreshed = True
            emit({"jsonrpc": "2.0",
                  "method": "notifications/tools/list_changed"})


if __name__ == "__main__":
    main()
