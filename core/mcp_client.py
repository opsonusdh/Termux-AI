"""
mcp_client.py — MCP (Model Context Protocol) client for Termux-AI.

Orion speaks the *client* side of MCP (spec revision 2025-06-18): it
launches or connects to external MCP servers, discovers their tools via
``tools/list`` and calls them via ``tools/call``, so a whole tool library
(termux-api, filesystem bridges, anything MCP) plugs in without one line
of it landing in core/.

Wire protocol
-------------
Two transports, both JSON-RPC 2.0:

* **stdio** — the server is a subprocess; requests and responses are
  newline-delimited JSON on stdin/stdout, human-facing text on stderr.
* **Streamable HTTP** — JSON-RPC POSTed to a single MCP endpoint; the
  answer comes back as ``application/json`` or an SSE
  (``text/event-stream``) body, and the server may hand out an
  ``Mcp-Session-Id`` that every later request echoes.

Lifecycle: ``initialize`` → (server replies with its capabilities) →
``notifications/initialized`` → then ``tools/list`` / ``tools/call`` /
``ping``.

Tool naming
-----------
A server's tool ``echo`` on server ``termux`` becomes
``mcp__termux__echo`` — the double-underscore form other MCP clients use,
so names never collide with core tools or with each other.  The mapping
from qualified name back to (server, tool) lives in
``MCPManager.tool_index()``.

Configuration
-------------
Servers are stored in ``config/config.json`` under ``"mcp_servers"``:

.. code-block:: json

    {
      "mcp_servers": {
        "termux": {
          "transport": "stdio",
          "command": "node",
          "args": ["~/mcp/termux-server/index.js"],
          "env": {"TERMUX_PREFIX": "/data/data/com.termux"},
          "enabled": true,
          "timeout": 60,
          "description": "Termux:API tools"
        },
        "remote": {
          "transport": "http",
          "url": "http://127.0.0.1:9100/mcp",
          "headers": {"Authorization": "Bearer ..."},
          "enabled": true
        }
      }
    }

Nothing here is credential-shaped by itself; a server that needs a token
carries it in ``headers``/``env``, which the user configures — the same
trust level as a shell alias.

Failure posture
---------------
* A server that will not start or answer is *cached as failed* for
  ``FAILURE_TTL`` seconds: the rest of the tool catalogue keeps working
  and the failure shows in ``/mcp status`` instead of stalling requests.
* Tool schemas are cached per server (``TOOLS_TTL``); a server that
  announces ``notifications/tools/list_changed`` invalidates its cache.
* Every request carries a timeout; a hung transport is closed and
  reported, never waited on forever.
"""

from __future__ import annotations

import atexit
import json
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from typing import Any, Dict, List, Optional, Tuple

import paths

__all__ = [
    "MCPError",
    "MCPServerConfig",
    "MCPConnection",
    "StdioTransport",
    "StreamableHTTPTransport",
    "MCPManager",
    "mcp_manager",
    "PROTOCOL_VERSION",
]

#: The protocol revision this client speaks.  The server may answer with
#: a different (usually older) version; per spec the client then either
#: works with that version or disconnects — we work with it and record it.
PROTOCOL_VERSION = "2025-06-18"

#: Client identity reported during the initialize handshake.
CLIENT_INFO = {"name": "Termux-AI", "version": "1.0.0"}

#: Seconds a ``tools/list`` result stays trusted before it is refreshed.
TOOLS_TTL = 300.0

#: Seconds a failed start/handshake is remembered before the next attempt.
FAILURE_TTL = 60.0

#: Default per-request timeout, overridable per server via ``timeout``.
DEFAULT_TIMEOUT = 60.0

#: Default connection + initialize timeout.
DEFAULT_CONNECT_TIMEOUT = 20.0

#: Longest tool description kept in the model's tool catalogue.  MCP
#: descriptions are written for humans and can be long; past this they
#: stop earning their tokens.
MAX_TOOL_DESCRIPTION = 700

#: How much of a server's stderr is kept in memory for diagnostics.
STDERR_KEEP = 100

_LOG_LOCK = threading.Lock()


def _log(line: str) -> None:
    """Append a line to logs/mcp.log (best effort, never raises)."""
    try:
        with _LOG_LOCK:
            with open(paths.MCP_LOG, "a", encoding="utf-8") as fh:
                fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {line}\n")
    except Exception:
        pass


class MCPError(Exception):
    """A failure inside the MCP client, with an optional wire error code."""

    def __init__(self, message: str, code: Optional[int] = None):
        super().__init__(message)
        self.code = code


# ── Server configuration ─────────────────────────────────────────────────────

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,48}$")


class MCPServerConfig:
    """One entry of the ``mcp_servers`` registry in config.json."""

    def __init__(self, name: str, transport: str = "stdio",
                 command: str = "", args: Optional[List[str]] = None,
                 env: Optional[Dict[str, str]] = None, cwd: str = "",
                 url: str = "", headers: Optional[Dict[str, str]] = None,
                 enabled: bool = True, timeout: float = DEFAULT_TIMEOUT,
                 description: str = ""):
        self.name = str(name)
        self.transport = str(transport or "stdio").strip().lower()
        self.command = str(command or "")
        self.args = [str(a) for a in (args or [])]
        self.env = {str(k): str(v) for k, v in (env or {}).items()}
        self.cwd = str(cwd or "")
        self.url = str(url or "")
        self.headers = {str(k): str(v) for k, v in (headers or {}).items()}
        self.enabled = bool(enabled)
        self.timeout = float(timeout or DEFAULT_TIMEOUT)
        self.description = str(description or "")

    # ── validation ───────────────────────────────────────────────────────
    def validate(self) -> Optional[str]:
        """None when the entry is usable, otherwise the reason it is not."""
        if not _NAME_RE.match(self.name):
            return ("server name must match [A-Za-z0-9][A-Za-z0-9_-]{0,48} "
                    f"(got {self.name!r})")
        if self.transport not in ("stdio", "http"):
            return f"unknown transport {self.transport!r} (stdio or http)"
        if self.transport == "stdio":
            if not self.command:
                return "stdio servers need a command"
        else:
            if not re.match(r"^https?://", self.url):
                return "http servers need an http(s) URL"
        if self.timeout <= 0:
            return "timeout must be positive"
        return None

    # ── (de)serialization ────────────────────────────────────────────────
    def to_dict(self) -> dict:
        d: Dict[str, Any] = {
            "transport": self.transport,
            "enabled": self.enabled,
            "timeout": self.timeout,
        }
        if self.description:
            d["description"] = self.description
        if self.transport == "stdio":
            d["command"] = self.command
            if self.args:
                d["args"] = self.args
            if self.env:
                d["env"] = self.env
            if self.cwd:
                d["cwd"] = self.cwd
        else:
            d["url"] = self.url
            if self.headers:
                d["headers"] = self.headers
        return d

    @classmethod
    def from_dict(cls, name: str, data: dict) -> "MCPServerConfig":
        data = data if isinstance(data, dict) else {}
        return cls(
            name=name,
            transport=data.get("transport", "stdio"),
            command=data.get("command", ""),
            args=data.get("args") or [],
            env=data.get("env") or {},
            cwd=data.get("cwd", ""),
            url=data.get("url", ""),
            headers=data.get("headers") or {},
            enabled=data.get("enabled", True),
            timeout=data.get("timeout", DEFAULT_TIMEOUT),
            description=data.get("description", ""),
        )


def _sanitize(part: str) -> str:
    """Reduce a server/tool name to [A-Za-z0-9_-] for a qualified name."""
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "_", str(part or "")).strip("_")
    return cleaned or "x"


def qualified_name(server: str, tool: str) -> str:
    """``echo`` on server ``termux`` → ``mcp__termux__echo``."""
    return f"mcp__{_sanitize(server)}__{_sanitize(tool)}"


# ── Transports ───────────────────────────────────────────────────────────────

class StdioTransport:
    """JSON-RPC over a subprocess's stdin/stdout (newline-delimited JSON).

    A reader thread owns stdout: it frames lines, routes responses to the
    waiter registered for their id, parks notifications, and answers
    server-initiated *requests* with method-not-found so a server that
    tries to sample from us never deadlocks on a silent client.
    """

    def __init__(self, config: MCPServerConfig):
        self.config = config
        self._proc: Optional[subprocess.Popen] = None
        self._write_lock = threading.Lock()
        self._pending: Dict[int, threading.Event] = {}
        self._responses: Dict[int, dict] = {}
        self._notifications: deque = deque(maxlen=64)
        self._stderr: deque = deque(maxlen=STDERR_KEEP)
        self._next_id = 1
        self._id_lock = threading.Lock()
        self._reader: Optional[threading.Thread] = None
        self._stderr_thread: Optional[threading.Thread] = None
        self._closed = False

    # ── lifecycle ────────────────────────────────────────────────────────
    def start(self, timeout: float = DEFAULT_CONNECT_TIMEOUT) -> None:
        if self._proc is not None:
            return
        env = dict(os.environ)
        env.update(self.config.env)
        cwd = os.path.expanduser(self.config.cwd) if self.config.cwd else None
        cmd = [os.path.expanduser(self.config.command)] + [
            os.path.expanduser(a) for a in self.config.args
        ]
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                cwd=cwd,
            )
        except OSError as exc:
            raise MCPError(f"could not start {self.config.command!r}: {exc}")

        self._reader = threading.Thread(
            target=self._read_loop, name=f"mcp-stdio-read-{self.config.name}",
            daemon=True)
        self._reader.start()
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr, name=f"mcp-stderr-{self.config.name}",
            daemon=True)
        self._stderr_thread.start()

    def close(self) -> None:
        """Terminate the server and release every waiter."""
        self._closed = True
        proc, self._proc = self._proc, None
        if proc is not None:
            try:
                if proc.stdin:
                    try:
                        proc.stdin.close()
                    except Exception:
                        pass
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
            except Exception:
                pass
        # Every pending request must wake up; the process is gone.
        for event in list(self._pending.values()):
            event.set()
        self._pending.clear()

    @property
    def alive(self) -> bool:
        return (self._proc is not None
                and self._proc.poll() is None)

    # ── wire I/O ─────────────────────────────────────────────────────────
    def request(self, method: str, params: Optional[dict] = None,
                timeout: float = DEFAULT_TIMEOUT) -> Any:
        """Send a JSON-RPC request and return the result.

        Raises :class:`MCPError` on a wire error, a timeout, or a dead
        process — a server that dies mid-call is an error, not an
        empty result.
        """
        if self._proc is None:
            raise MCPError("transport is not running")
        with self._id_lock:
            req_id = self._next_id
            self._next_id += 1
        message = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            message["params"] = params

        event = threading.Event()
        self._pending[req_id] = event
        try:
            self._send(message)
            if not event.wait(timeout):
                raise MCPError(f"{method} timed out after {timeout:.0f}s")
        finally:
            self._pending.pop(req_id, None)

        if not self.alive and req_id not in self._responses:
            raise MCPError(f"server exited during {method}")

        response = self._responses.pop(req_id, None)
        if response is None:
            raise MCPError(f"no response for {method} (server exited?)")
        if "error" in response and response["error"] is not None:
            err = response["error"] or {}
            raise MCPError(
                f"{method} failed: {err.get('message', 'unknown error')}",
                code=err.get("code"))
        return response.get("result")

    def notify(self, method: str, params: Optional[dict] = None) -> None:
        """Send a notification (no id, no answer expected)."""
        message: Dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self._send(message)

    def _send(self, message: dict) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise MCPError("transport is not running")
        try:
            line = json.dumps(message, ensure_ascii=False) + "\n"
            with self._write_lock:
                proc.stdin.write(line.encode("utf-8"))
                proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as exc:
            raise MCPError(f"failed to write to the server: {exc}")

    # ── reader thread ────────────────────────────────────────────────────
    def _read_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        while not self._closed:
            try:
                raw = proc.stdout.readline()
            except Exception:
                break
            if not raw:
                break  # EOF — the server is gone
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                # Not JSON on the JSON stream: most likely a chatty server
                # printing a banner.  Keep it for diagnostics, keep reading.
                self._stderr.append(f"[stdout] {line[:500]}")
                continue
            if not isinstance(message, dict):
                continue
            self._route(message)
        # EOF: wake everyone so no request waits out its full timeout.
        for ev in list(self._pending.values()):
            ev.set()

    def _route(self, message: dict) -> None:
        if message.get("id") is not None and (
                "result" in message or "error" in message):
            # A response to one of ours.
            try:
                req_id = int(message["id"])
            except (TypeError, ValueError):
                return
            self._responses[req_id] = message
            event = self._pending.get(req_id)
            if event is not None:
                event.set()
            return
        method = message.get("method", "")
        if "id" in message:
            # A server-initiated request.  We implement nothing the server
            # may call back for, so decline politely instead of ignoring
            # it — an ignored request leaves the server waiting.
            self._send({"jsonrpc": "2.0", "id": message.get("id"),
                        "error": {"code": -32601,
                                  "message": "method not found"}})
            return
        self._notifications.append(message)
        _log(f"[{self.config.name}] notification {method}")

    def _drain_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        while not self._closed:
            try:
                raw = proc.stderr.readline()
            except Exception:
                break
            if not raw:
                break
            line = raw.decode("utf-8", errors="replace").rstrip()
            if line:
                self._stderr.append(line)

    def recent_stderr(self, limit: int = 8) -> List[str]:
        """The last stderr lines the server produced (for /mcp status)."""
        return list(self._stderr)[-limit:]


class StreamableHTTPTransport:
    """JSON-RPC over Streamable HTTP (the 2025-03-26+ HTTP transport).

    Every message is a POST to the MCP endpoint.  The answer is either a
    plain JSON body or an SSE stream; the session id the server hands out
    at initialize is echoed back on every later request, as is the
    negotiated protocol version.
    """

    def __init__(self, config: MCPServerConfig):
        self.config = config
        self._session_id: Optional[str] = None
        self._protocol_version: Optional[str] = None
        self._next_id = 1
        self._id_lock = threading.Lock()
        self._notifications: deque = deque(maxlen=64)
        self._closed = False

    @property
    def alive(self) -> bool:
        # A sessionless transport is always "open": each POST stands alone.
        return not self._closed

    def start(self, timeout: float = DEFAULT_CONNECT_TIMEOUT) -> None:
        pass  # nothing to spawn; the first POST does the talking

    def close(self) -> None:
        if self._closed or not self._session_id:
            self._closed = True
            return
        # Politely end the session (best effort, no response expected).
        self._closed = True
        try:
            req = urllib.request.Request(
                self.config.url, data=b"", method="DELETE",
                headers=self._headers())
            urllib.request.urlopen(req, timeout=5).close()
        except Exception:
            pass

    def _headers(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            **self.config.headers,
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        if self._protocol_version:
            headers["MCP-Protocol-Version"] = self._protocol_version
        if extra:
            headers.update(extra)
        return headers

    def request(self, method: str, params: Optional[dict] = None,
                timeout: float = DEFAULT_TIMEOUT) -> Any:
        with self._id_lock:
            req_id = self._next_id
            self._next_id += 1
        message: Dict[str, Any] = {"jsonrpc": "2.0", "id": req_id,
                                   "method": method}
        if params is not None:
            message["params"] = params
        result, headers = self._post(message, timeout)
        sid = headers.get("Mcp-Session-Id") or headers.get("mcp-session-id")
        if sid:
            self._session_id = sid
        return result

    def notify(self, method: str, params: Optional[dict] = None) -> None:
        message: Dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        try:
            self._post(message, 10.0, expect_response=False)
        except MCPError:
            pass  # a notification's HTTP failure is not fatal to the session

    def _post(self, message: dict, timeout: float,
              expect_response: bool = True) -> Tuple[Any, Dict[str, str]]:
        body = json.dumps(message, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.config.url, data=body, method="POST",
            headers=self._headers())
        try:
            resp = urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:300]
            except Exception:
                pass
            raise MCPError(f"HTTP {exc.code} from {self.config.url}: {detail}")
        except (urllib.error.URLError, OSError) as exc:
            raise MCPError(f"cannot reach {self.config.url}: {exc}")

        try:
            headers = dict(resp.headers.items())
            ctype = (headers.get("Content-Type")
                     or headers.get("content-type") or "").lower()
            sid = (headers.get("Mcp-Session-Id")
                   or headers.get("mcp-session-id"))
            if sid:
                self._session_id = sid

            if "text/event-stream" in ctype:
                return self._consume_sse(resp, message.get("id")), headers
            raw = resp.read().decode("utf-8", errors="replace")
        finally:
            try:
                resp.close()
            except Exception:
                pass

        if not raw.strip():
            if expect_response:
                raise MCPError(f"empty response for {message.get('method')}")
            return None, headers
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise MCPError(f"response is not JSON: {exc}")
        return self._extract(parsed, expect_response), headers

    def _extract(self, parsed: Any, expect_response: bool) -> Any:
        if isinstance(parsed, list):
            # A batch: take the response matching our expectation (the
            # 2025-06-18 spec removed batching, but tolerate a server that
            # still sends one).
            for item in parsed:
                if isinstance(item, dict) and "result" in item:
                    return item.get("result")
            for item in parsed:
                if isinstance(item, dict) and "error" in item:
                    err = item["error"] or {}
                    raise MCPError(err.get("message", "unknown error"),
                                   code=err.get("code"))
            return None
        if not isinstance(parsed, dict):
            raise MCPError("unexpected response shape")
        if "error" in parsed and parsed["error"] is not None:
            err = parsed["error"] or {}
            raise MCPError(err.get("message", "unknown error"),
                           code=err.get("code"))
        if "result" in parsed:
            return parsed.get("result")
        if "method" in parsed:
            self._notifications.append(parsed)
            if expect_response:
                raise MCPError(
                    f"got a notification ({parsed.get('method')}) where a "
                    f"response was expected")
            return None
        if expect_response:
            raise MCPError("response carried neither result nor error")
        return None

    def _consume_sse(self, resp, wanted_id) -> Any:
        """Read an SSE stream until the response with *wanted_id* arrives."""
        result: Any = None
        error: Optional[dict] = None
        data_lines: List[str] = []
        while True:
            try:
                raw = resp.readline()
            except Exception:
                break
            if not raw:
                break
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if line == "":  # event boundary
                if data_lines:
                    blob = "\n".join(data_lines)
                    data_lines = []
                    try:
                        parsed = json.loads(blob)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(parsed, dict):
                        if parsed.get("id") == wanted_id and (
                                "result" in parsed or "error" in parsed):
                            if "error" in parsed and parsed["error"]:
                                error = parsed["error"]
                            else:
                                result = parsed.get("result")
                            break  # got our answer; drop the stream
                        if "method" in parsed:
                            self._notifications.append(parsed)
                continue
            if line.startswith("data:"):
                data_lines.append(line[5:].strip())
            # event:/id:/retry: lines are not needed for request/response
        if error is not None:
            raise MCPError(error.get("message", "unknown error"),
                           code=error.get("code"))
        if result is None:
            raise MCPError("SSE stream ended without a response")
        return result


# ── Connection (transport + handshake + tool cache) ─────────────────────────

class MCPConnection:
    """One server: transport, initialize handshake, tool cache, calls."""

    def __init__(self, config: MCPServerConfig):
        self.config = config
        transport_cls = (StdioTransport if config.transport == "stdio"
                         else StreamableHTTPTransport)
        self.transport = transport_cls(config)
        self.server_info: Dict[str, Any] = {}
        self.capabilities: Dict[str, Any] = {}
        self.negotiated_version: Optional[str] = None
        self._tools: List[dict] = []
        self._tools_at = 0.0
        self._lock = threading.RLock()

    # ── lifecycle ────────────────────────────────────────────────────────
    def connect(self, timeout: float = DEFAULT_CONNECT_TIMEOUT) -> None:
        """Start the transport and run the initialize handshake."""
        with self._lock:
            if self.transport.alive and self.server_info:
                return
            self.transport.start(timeout)
            result = self.transport.request(
                "initialize",
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": CLIENT_INFO,
                },
                timeout=timeout,
            ) or {}
            self.negotiated_version = result.get("protocolVersion")
            self.server_info = result.get("serverInfo") or {}
            self.capabilities = result.get("capabilities") or {}
            # The handshake completes with this notification; a server is
            # allowed to reject further requests until it arrives.
            self.transport.notify("notifications/initialized", {})
            name = self.server_info.get("name", self.config.name)
            version = self.server_info.get("version", "?")
            _log(f"[{self.config.name}] connected: {name} {version} "
                 f"(protocol {self.negotiated_version})")

    def close(self) -> None:
        with self._lock:
            self.transport.close()
            self.server_info = {}
            self.capabilities = {}
            self._tools = []
            self._tools_at = 0.0

    @property
    def connected(self) -> bool:
        return bool(self.transport.alive and self.server_info)

    # ── tools ────────────────────────────────────────────────────────────
    def list_tools(self, refresh: bool = False) -> List[dict]:
        """The server's tool list, cached for TOOLS_TTL seconds."""
        with self._lock:
            fresh = (time.monotonic() - self._tools_at) < TOOLS_TTL
            if self._tools and fresh and not refresh:
                return self._tools
            self.connect()
            result = self.transport.request("tools/list", {},
                                            timeout=self.config.timeout) or {}
            tools = result.get("tools") or []
            self._tools = [t for t in tools if isinstance(t, dict)]
            self._tools_at = time.monotonic()
            return self._tools

    def invalidate_tools(self) -> None:
        with self._lock:
            self._tools_at = 0.0

    def pop_notifications(self) -> List[dict]:
        """Drain notifications the transport received since last check."""
        out = []
        while True:
            try:
                out.append(self.transport._notifications.popleft())
            except IndexError:
                break
        return out

    def call_tool(self, tool: str, arguments: Optional[dict] = None) -> dict:
        """Call one tool and return the raw CallToolResult dict."""
        self.connect()
        return self.transport.request(
            "tools/call",
            {"name": tool, "arguments": dict(arguments or {})},
            timeout=self.config.timeout,
        ) or {}

    def ping(self, timeout: float = 10.0) -> bool:
        try:
            self.connect(timeout)
            self.transport.request("ping", None, timeout=timeout)
            return True
        except Exception:
            return False


# ── Result normalization ─────────────────────────────────────────────────────

def normalize_tool_result(result: dict, server: str = "",
                          tool: str = "") -> str:
    """Flatten a CallToolResult into the text the model should see.

    Text blocks are joined; resource blocks carry their URI (and text when
    embedded); image blocks are described, not inlined — a picture arriving
    as a tool result is noted so the model can ask for it explicitly.
    ``structuredContent`` is used verbatim when the server sent no text.
    """
    if not isinstance(result, dict):
        return str(result)
    parts: List[str] = []
    for block in (result.get("content") or []):
        if not isinstance(block, dict):
            parts.append(str(block))
            continue
        btype = block.get("type")
        if btype == "text":
            text = block.get("text", "")
            if text:
                parts.append(str(text))
        elif btype == "resource":
            res = block.get("resource") or {}
            uri = res.get("uri", "")
            text = res.get("text")
            if text:
                parts.append(f"[resource {uri}]\n{text}")
            else:
                parts.append(f"[resource {uri} — non-text content, "
                             f"{len(str(res.get('blob') or ''))} base64 chars]")
        elif btype == "image":
            parts.append(f"[image block: {block.get('mimeType', 'unknown')} "
                         f"mime — use an image-capable path to view it]")
        elif btype == "audio":
            parts.append(f"[audio block: {block.get('mimeType', 'unknown')}]")
        else:
            parts.append(f"[{btype or 'unknown'} block]")

    text = "\n".join(p for p in parts if p).strip()
    if not text and result.get("structuredContent") is not None:
        try:
            text = json.dumps(result["structuredContent"],
                              ensure_ascii=False, indent=2)
        except (TypeError, ValueError):
            text = str(result["structuredContent"])

    if not text:
        text = "Success" if not result.get("isError") else "Error (no details)"
    if result.get("isError"):
        text = f"[MCP ERROR] {text}"
    return text


def tool_to_schema(server: str, tool: dict) -> Optional[dict]:
    """One MCP ``tools/list`` entry → one OpenAI-format function schema."""
    name = tool.get("name")
    if not name:
        return None
    qname = qualified_name(server, name)
    schema_in = tool.get("inputSchema")
    if not isinstance(schema_in, dict):
        schema_in = {"type": "object", "properties": {}}
    # Every provider we talk to expects an object schema at the top.
    if schema_in.get("type") != "object":
        schema_in = {"type": "object",
                     "properties": schema_in.get("properties", {}),
                     "required": schema_in.get("required", [])}
    description = str(tool.get("description") or "").strip()
    origin = f"[MCP tool served by '{server}']"
    if len(description) > MAX_TOOL_DESCRIPTION:
        description = description[:MAX_TOOL_DESCRIPTION - 3] + "..."
    full_desc = f"{description}\n{origin}" if description else origin
    return {
        "type": "function",
        "function": {
            "name": qname,
            "description": full_desc,
            "parameters": schema_in,
        },
    }


# ── Manager (registry + connections + schemas) ───────────────────────────────

class MCPManager:
    """The registry of MCP servers and the entry point for everything else.

    Configuration lives in ``config/config.json`` under ``mcp_servers``;
    edits follow the read-fresh-merge-write pattern so concurrent writers
    (display state, plugin flags) are never clobbered.
    """

    def __init__(self):
        self._connections: Dict[str, MCPConnection] = {}
        self._failures: Dict[str, float] = {}   # name -> monotonic time
        self._last_error: Dict[str, str] = {}
        self._schema_cache: Dict[str, Tuple[float, List[dict]]] = {}
        self._lock = threading.RLock()
        atexit.register(self.shutdown)

    # ── registry ─────────────────────────────────────────────────────────
    def _read_registry(self) -> Dict[str, dict]:
        try:
            import app_config
            cfg = app_config.load_config()
        except Exception:
            cfg = {}
        servers = cfg.get("mcp_servers", {})
        return servers if isinstance(servers, dict) else {}

    def _write_registry(self, servers: Dict[str, dict]) -> None:
        """Merge ``mcp_servers`` into config.json without clobbering."""
        try:
            import app_config
            app_config.save_config(mcp_servers=servers)
        except Exception as exc:
            raise MCPError(f"could not save the server registry: {exc}")

    def configs(self) -> Dict[str, MCPServerConfig]:
        with self._lock:
            return {name: MCPServerConfig.from_dict(name, data)
                    for name, data in self._read_registry().items()}

    def config(self, name: str) -> Optional[MCPServerConfig]:
        data = self._read_registry().get(name)
        return MCPServerConfig.from_dict(name, data) if data else None

    def add_server(self, cfg: MCPServerConfig) -> dict:
        problem = cfg.validate()
        if problem:
            return {"ok": False, "message": problem}
        with self._lock:
            servers = self._read_registry()
            if cfg.name in servers:
                return {"ok": False,
                        "message": f"server '{cfg.name}' already exists — "
                                   f"remove it first or pick another name"}
            servers[cfg.name] = cfg.to_dict()
            self._write_registry(servers)
        _log(f"registry: added {cfg.name} ({cfg.transport})")
        return {"ok": True,
                "message": f"server '{cfg.name}' added ({cfg.transport})",
                "config": cfg.to_dict()}

    def remove_server(self, name: str) -> dict:
        with self._lock:
            servers = self._read_registry()
            if name not in servers:
                return {"ok": False,
                        "message": f"unknown server '{name}'"}
            del servers[name]
            self._write_registry(servers)
            conn = self._connections.pop(name, None)
            if conn:
                conn.close()
            self._failures.pop(name, None)
            self._last_error.pop(name, None)
            self._schema_cache.pop(name, None)
        _log(f"registry: removed {name}")
        return {"ok": True, "message": f"server '{name}' removed"}

    def set_enabled(self, name: str, enabled: bool) -> dict:
        with self._lock:
            servers = self._read_registry()
            if name not in servers:
                return {"ok": False, "message": f"unknown server '{name}'"}
            servers[name]["enabled"] = bool(enabled)
            self._write_registry(servers)
            if not enabled:
                conn = self._connections.pop(name, None)
                if conn:
                    conn.close()
            self._schema_cache.pop(name, None)
            self._failures.pop(name, None)
        state = "enabled" if enabled else "disabled"
        _log(f"registry: {state} {name}")
        return {"ok": True, "message": f"server '{name}' {state}"}

    # ── connections ──────────────────────────────────────────────────────
    def get_connection(self, name: str,
                       force: bool = False) -> MCPConnection:
        """The live connection for *name*, connecting on first need.

        A recent failure short-circuits to the same error without another
        start attempt (FAILURE_TTL), so a broken server costs one attempt
        per minute, not one per request.
        """
        cfg = self.config(name)
        if cfg is None:
            raise MCPError(f"unknown server '{name}'")
        if not cfg.enabled:
            raise MCPError(f"server '{name}' is disabled")
        with self._lock:
            failed_at = self._failures.get(name)
            if (not force and failed_at is not None
                    and (time.monotonic() - failed_at) < FAILURE_TTL):
                raise MCPError(self._last_error.get(
                    name, f"server '{name}' failed recently"))
            conn = self._connections.get(name)
            if conn is None:
                conn = MCPConnection(cfg)
                self._connections[name] = conn
            try:
                if force and conn.connected:
                    conn.close()
                conn.connect()
                self._failures.pop(name, None)
                self._last_error.pop(name, None)
                return conn
            except Exception as exc:
                self._failures[name] = time.monotonic()
                self._last_error[name] = str(exc)
                self._schema_cache.pop(name, None)
                try:
                    conn.close()
                except Exception:
                    pass
                raise

    def disconnect(self, name: str) -> dict:
        with self._lock:
            conn = self._connections.pop(name, None)
            if conn:
                conn.close()
            self._failures.pop(name, None)
            self._last_error.pop(name, None)
            self._schema_cache.pop(name, None)
        return {"ok": True,
                "message": (f"server '{name}' disconnected"
                            if conn else f"server '{name}' was not connected")}

    def shutdown(self) -> None:
        with self._lock:
            for conn in self._connections.values():
                try:
                    conn.close()
                except Exception:
                    pass
            self._connections.clear()

    # ── tools ────────────────────────────────────────────────────────────
    def tool_schemas(self) -> List[dict]:
        """OpenAI-format schemas for every tool of every healthy server.

        Cached per server (TOOLS_TTL).  A server that fails contributes
        nothing here — the catalogue stays valid, and ``/mcp status``
        explains the gap.
        """
        schemas: List[dict] = []
        for name, cfg in self.configs().items():
            if not cfg.enabled:
                continue
            with self._lock:
                cached = self._schema_cache.get(name)
                if cached and (time.monotonic() - cached[0]) < TOOLS_TTL:
                    schemas.extend(cached[1])
                    continue
            try:
                conn = self.get_connection(name)
                # Honour a tools/list_changed notification from the server.
                for note in conn.pop_notifications():
                    if note.get("method") == "notifications/tools/list_changed":
                        conn.invalidate_tools()
                tools = conn.list_tools()
            except Exception as exc:
                _log(f"[{name}] tool listing failed: {exc}")
                continue
            built = []
            seen: Dict[str, int] = {}
            for tool in tools:
                schema = tool_to_schema(name, tool)
                if schema is None:
                    continue
                qname = schema["function"]["name"]
                if qname in seen:  # two tools colliding after sanitization
                    seen[qname] += 1
                    schema["function"]["name"] = f"{qname}_{seen[qname]}"
                else:
                    seen[qname] = 1
                built.append(schema)
            with self._lock:
                self._schema_cache[name] = (time.monotonic(), built)
            schemas.extend(built)
        return schemas

    def tool_index(self) -> Dict[str, Tuple[str, str]]:
        """{qualified_name: (server, tool)} for every advertised tool."""
        index: Dict[str, Tuple[str, str]] = {}
        for name, cfg in self.configs().items():
            if not cfg.enabled:
                continue
            with self._lock:
                cached = self._schema_cache.get(name)
                if cached and (time.monotonic() - cached[0]) < TOOLS_TTL:
                    for schema in cached[1]:
                        qname = schema["function"]["name"]
                        index[qname] = (name, self._unqualify(qname, name))
                    continue
            try:
                conn = self.get_connection(name)
                tools = conn.list_tools()
            except Exception:
                continue
            for tool in tools:
                tool_name = tool.get("name")
                if tool_name:
                    index[qualified_name(name, tool_name)] = (name,
                                                              tool_name)
        return index

    @staticmethod
    def _unqualify(qname: str, server: str) -> str:
        prefix = f"mcp__{_sanitize(server)}__"
        return qname[len(prefix):] if qname.startswith(prefix) else qname

    def is_mcp_tool(self, name: str) -> bool:
        if not str(name).startswith("mcp__"):
            return False
        return name in self.tool_index()

    def mcp_tool_names(self) -> List[str]:
        return sorted(self.tool_index().keys())

    def _resolve_qualified(self, qname: str) -> Optional[Tuple[str, str]]:
        """(server, tool) from a qualified name, against the registry only.

        The live index hides the tools of an unreachable server; a call to
        such a tool should still reach the connection attempt and report
        the real failure, not "unknown tool".
        """
        if not qname.startswith("mcp__"):
            return None
        for name in self.configs():
            prefix = f"mcp__{_sanitize(name)}__"
            if qname.startswith(prefix):
                tool = qname[len(prefix):]
                if tool:
                    return (name, tool)
        return None

    def call_qualified(self, qname: str, arguments: Optional[dict]) -> str:
        """Run ``mcp__<server>__<tool>`` and return normalized text."""
        entry = self.tool_index().get(qname)
        if entry is not None:
            server, tool = entry
        else:
            resolved = self._resolve_qualified(qname)
            if resolved is None:
                known = ", ".join(self.mcp_tool_names()) or "(none)"
                return (f"[ERROR] Unknown MCP tool '{qname}'. Available MCP "
                        f"tools: {known}")
            server, tool = resolved
        try:
            result = self.call(server, tool, arguments)
        except MCPError as exc:
            return f"[MCP ERROR] server '{server}': {exc}"
        except Exception as exc:  # never let a server kill the turn
            return f"[MCP ERROR] server '{server}': unexpected failure: {exc}"
        return result

    def call(self, server: str, tool: str,
             arguments: Optional[dict] = None) -> str:
        conn = self.get_connection(server)
        result = conn.call_tool(tool, arguments)
        return normalize_tool_result(result, server, tool)

    def make_route(self, qname: str, arguments: dict):
        """A zero-arg callable for ``_dispatch_tool``'s route table."""
        return lambda: self.call_qualified(qname, arguments)

    # ── status / refresh ─────────────────────────────────────────────────
    def refresh(self, name: str = "") -> dict:
        """Drop the caches; the next listing reconnects and re-reads."""
        with self._lock:
            names = [name] if name else list(self._read_registry().keys())
            for n in names:
                self._schema_cache.pop(n, None)
                self._failures.pop(n, None)
            if name:
                conn = self._connections.get(name)
                if conn:
                    conn.invalidate_tools()
        return {"ok": True,
                "message": ("tool cache cleared"
                            + (f" for '{name}'" if name else "")
                            + " — the next request re-lists the tools")}

    def status(self, name: str = "") -> dict:
        """A status report for one server (or the summary of all)."""
        configs = self.configs()
        if name:
            cfg = configs.get(name)
            if cfg is None:
                return {"ok": False,
                        "message": f"unknown server '{name}'"}
            return self._server_status(cfg)
        return {"ok": True,
                "servers": [self._server_status(c) for c in
                            configs.values()]}

    def _server_status(self, cfg: MCPServerConfig) -> dict:
        info: Dict[str, Any] = {
            "ok": True,
            "name": cfg.name,
            "transport": cfg.transport,
            "enabled": cfg.enabled,
            "timeout": cfg.timeout,
        }
        if cfg.description:
            info["description"] = cfg.description
        if cfg.transport == "stdio":
            info["command"] = " ".join([cfg.command] + cfg.args)
        else:
            info["url"] = cfg.url
        problem = cfg.validate()
        if problem:
            info["state"] = "invalid"
            info["error"] = problem
            return info
        if not cfg.enabled:
            info["state"] = "disabled"
            return info
        conn = self._connections.get(cfg.name)
        failed_at = self._failures.get(cfg.name)
        recent_failure = (failed_at is not None
                          and (time.monotonic() - failed_at) < FAILURE_TTL)
        if conn is not None and conn.connected:
            info["state"] = "connected"
            info["server_info"] = {
                "name": conn.server_info.get("name", ""),
                "version": conn.server_info.get("version", ""),
                "protocol": conn.negotiated_version,
            }
            try:
                tools = conn.list_tools()
                info["tools"] = [t.get("name", "?") for t in tools]
            except Exception as exc:
                info["state"] = "connected_tools_failed"
                info["error"] = str(exc)
        elif recent_failure:
            info["state"] = "failed"
            info["error"] = self._last_error.get(cfg.name, "unknown error")
            if conn is not None:
                stderr = conn.transport.recent_stderr(5)
                if stderr:
                    info["stderr_tail"] = stderr
        else:
            info["state"] = "not connected"
        return info


#: The process-wide manager.  Import *this*, not a new instance, so the
#: connection pool and caches are shared by every caller.
mcp_manager = MCPManager()
