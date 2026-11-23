"""Tests for the MCP client (core/mcp_client.py) and its integrations.

A real MCP server fixture (tests/fixtures/mcp_echo_server.py) runs as a
subprocess for the stdio tests and as a threading HTTP server for the
streamable-HTTP tests, so the wire protocol is exercised end-to-end —
framing, handshake, session ids, timeouts and failure modes, not mocked
dictionaries.

The registry tests run against an isolated config.json so the user's real
server list is never touched.
"""
import ast
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
if os.path.join(REPO_ROOT, "core") not in sys.path:
    sys.path.insert(0, os.path.join(REPO_ROOT, "core"))

import paths  # noqa: E402
from core import mcp_client as M  # noqa: E402
import core.app_config as app_config  # noqa: E402

SERVER = os.path.join(REPO_ROOT, "tests", "fixtures", "mcp_echo_server.py")


def server_config(mode="normal", name="test", timeout=15):
    return M.MCPServerConfig(
        name=name, transport="stdio", command=sys.executable,
        args=[SERVER, mode], timeout=timeout)


@pytest.fixture()
def isolated_config(monkeypatch, tmp_path):
    """Point the config at a scratch file for the registry tests.

    ``app_config`` reads ``paths.CONFIG_FILE`` at call time through its own
    ``import paths``, so patching the paths module covers every reader.
    """
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(paths, "CONFIG_FILE", str(cfg_file))
    yield str(cfg_file)


@pytest.fixture()
def fresh_manager(isolated_config):
    """The real singleton manager, with empty caches, on an isolated config.

    Resetting the singleton (instead of constructing a second one) keeps
    the dispatch/sub-agent tests honest: they exercise the same manager
    instance the runtime uses.
    """
    mgr = M.mcp_manager
    mgr._connections.clear()
    mgr._failures.clear()
    mgr._last_error.clear()
    mgr._schema_cache.clear()
    yield mgr
    mgr.shutdown()
    mgr._connections.clear()
    mgr._failures.clear()
    mgr._last_error.clear()
    mgr._schema_cache.clear()


@pytest.fixture()
def connection():
    conn = M.MCPConnection(server_config())
    yield conn
    conn.close()


# ── naming ───────────────────────────────────────────────────────────────────

def test_qualified_name_uses_double_underscores():
    assert M.qualified_name("termux", "battery") == "mcp__termux__battery"


def test_qualified_name_sanitizes_spaces():
    assert M.qualified_name("termux api", "battery status") == \
        "mcp__termux_api__battery_status"


def test_sanitize_falls_back_to_x_for_empty():
    assert M._sanitize("") == "x"
    assert M._sanitize("!!!") == "x"


# ── config validation ────────────────────────────────────────────────────────

def test_stdio_config_needs_a_command():
    problem = M.MCPServerConfig("s", transport="stdio", command="").validate()
    assert problem and "command" in problem


def test_http_config_needs_a_url():
    assert M.MCPServerConfig("s", transport="http", url="").validate()
    problem = M.MCPServerConfig("s", transport="http",
                                url="ftp://x").validate()
    assert problem and "http" in problem


def test_config_name_shape_is_enforced():
    assert M.MCPServerConfig("bad name!", command="x").validate()
    assert M.MCPServerConfig("-leading", command="x").validate()
    assert M.MCPServerConfig("ok_name-1", command="x").validate() is None


def test_config_roundtrip_keeps_transport_fields():
    cfg = M.MCPServerConfig("s", transport="stdio", command="node",
                            args=["a.js"], env={"K": "v"})
    restored = M.MCPServerConfig.from_dict("s", cfg.to_dict())
    assert restored.command == "node"
    assert restored.args == ["a.js"]
    assert restored.env == {"K": "v"}


# ── stdio transport over the real fixture server ─────────────────────────────

class TestStdioTransport:
    def test_handshake_reports_server_info(self, connection):
        connection.connect()
        assert connection.server_info["name"] == "test-server-normal"
        assert connection.negotiated_version == M.PROTOCOL_VERSION

    def test_tools_list_returns_the_fixture_tools(self, connection):
        connection.connect()
        tools = connection.list_tools()
        assert [t["name"] for t in tools] == ["echo", "add", "boom"]

    def test_second_tools_list_is_cached(self, connection):
        connection.connect()
        connection.list_tools()
        conn = connection
        conn._tools_at = time.monotonic()  # freshly cached
        result = conn.transport.request  # noqa: F841 — just prove it lives
        assert conn.list_tools() == connection.list_tools()

    def test_echo_call_round_trips(self, connection):
        connection.connect()
        result = connection.call_tool("echo", {"text": "hello"})
        text = M.normalize_tool_result(result)
        assert text == "echo: hello"
        assert result.get("isError") is False

    def test_tool_error_comes_back_flagged(self, connection):
        connection.connect()
        text = M.normalize_tool_result(connection.call_tool("boom", {}))
        assert text.startswith("[MCP ERROR]")
        assert "deliberate" in text

    def test_unknown_method_raises_with_wire_code(self, connection):
        connection.connect()
        with pytest.raises(M.MCPError) as exc:
            connection.transport.request("no/such/method", {})
        assert exc.value.code == -32601

    def test_ping_answers(self, connection):
        assert connection.ping() is True

    def test_a_chatty_stdout_does_not_break_framing(self):
        conn = M.MCPConnection(server_config(mode="chatty"))
        try:
            conn.connect()
            assert [t["name"] for t in conn.list_tools()] == ["echo", "add", "boom"]
            # the banner line was parked as a diagnostic, not lost
            assert any("[stdout]" in line
                       for line in conn.transport.recent_stderr(10))
        finally:
            conn.close()

    def test_a_crashing_server_fails_the_handshake(self):
        conn = M.MCPConnection(server_config(mode="crash"))
        try:
            with pytest.raises(M.MCPError):
                conn.connect(timeout=10)
        finally:
            conn.close()

    def test_a_slow_call_times_out_instead_of_hanging(self):
        conn = M.MCPConnection(server_config(mode="slow", timeout=2))
        try:
            conn.connect()
            start = time.monotonic()
            with pytest.raises(M.MCPError, match="timed out"):
                conn.call_tool("echo", {"text": "x"})
            assert time.monotonic() - start < 4
        finally:
            conn.close()

    def test_a_server_initiated_request_is_declined_without_hanging(self):
        conn = M.MCPConnection(server_config(mode="server_req"))
        try:
            conn.connect()
            time.sleep(0.3)  # let the sampling request arrive
            # the session still works after the decline
            assert [t["name"] for t in conn.list_tools()] == ["echo", "add", "boom"]
            text = M.normalize_tool_result(conn.call_tool("echo", {"text": "ok"}))
            assert text == "echo: ok"
        finally:
            conn.close()

    def test_close_terminates_the_process(self, connection):
        connection.connect()
        proc = connection.transport._proc
        connection.close()
        assert proc.poll() is not None
        assert connection.transport.alive is False


# ── result normalization ─────────────────────────────────────────────────────

class TestNormalizeToolResult:
    def test_text_blocks_are_joined(self):
        text = M.normalize_tool_result({
            "content": [{"type": "text", "text": "a"},
                        {"type": "text", "text": "b"}]})
        assert text == "a\nb"

    def test_error_results_are_prefixed(self):
        text = M.normalize_tool_result({
            "content": [{"type": "text", "text": "nope"}],
            "isError": True})
        assert text.startswith("[MCP ERROR] nope")

    def test_structured_content_used_when_no_text(self):
        text = M.normalize_tool_result({
            "content": [], "structuredContent": {"sum": 42}})
        assert '"sum": 42' in text

    def test_image_block_is_described_not_inlined(self):
        text = M.normalize_tool_result({
            "content": [{"type": "image", "mimeType": "image/png",
                         "data": "..."}]})
        assert "image/png" in text

    def test_resource_block_carries_uri_and_text(self):
        text = M.normalize_tool_result({
            "content": [{"type": "resource",
                         "resource": {"uri": "file:///x.txt",
                                      "text": "body"}}]})
        assert "file:///x.txt" in text and "body" in text

    def test_empty_success_becomes_success(self):
        assert M.normalize_tool_result({"content": []}) == "Success"


# ── schema conversion ────────────────────────────────────────────────────────

class TestToolToSchema:
    def test_basic_shape(self):
        schema = M.tool_to_schema("srv", {
            "name": "echo", "description": "Echo.",
            "inputSchema": {"type": "object", "properties": {}}})
        assert schema["type"] == "function"
        assert schema["function"]["name"] == "mcp__srv__echo"
        assert schema["function"]["parameters"]["type"] == "object"
        assert "[MCP tool served by 'srv']" in schema["function"]["description"]

    def test_missing_input_schema_gets_an_object_default(self):
        schema = M.tool_to_schema("srv", {"name": "x"})
        assert schema["function"]["parameters"] == {
            "type": "object", "properties": {}}

    def test_non_object_schema_is_wrapped(self):
        schema = M.tool_to_schema("srv", {
            "name": "x", "inputSchema": {"properties": {"a": {}}}})
        assert schema["function"]["parameters"]["type"] == "object"

    def test_long_descriptions_are_capped(self):
        schema = M.tool_to_schema("srv", {
            "name": "x", "description": "y" * 5000})
        assert len(schema["function"]["description"]) <= \
            M.MAX_TOOL_DESCRIPTION + 50

    def test_entry_without_a_name_is_dropped(self):
        assert M.tool_to_schema("srv", {"description": "?"}) is None


# ── streamable HTTP transport ────────────────────────────────────────────────

SESSION_ID = "sess-mcp-test-1"


class _HTTPHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep the test output clean
        pass

    def _json(self, payload, status=200, extra=None):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            message = json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            self._json({"jsonrpc": "2.0", "id": None, "error": {
                "code": -32700, "message": "parse error"}}, status=400)
            return
        method = message.get("method", "")
        rid = message.get("id")

        if method != "initialize" and \
                self.headers.get("Mcp-Session-Id") != SESSION_ID:
            self._json({"jsonrpc": "2.0", "id": rid, "error": {
                "code": -32000, "message": "bad session"}}, status=400)
            return

        if method == "initialize":
            self._json(
                {"jsonrpc": "2.0", "id": rid, "result": {
                    "protocolVersion": message["params"]["protocolVersion"],
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "http-test", "version": "0.1.0"}}},
                extra={"Mcp-Session-Id": SESSION_ID})
        elif method == "tools/list":
            # answered as SSE to exercise the stream path
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Mcp-Session-Id", SESSION_ID)
            self.end_headers()
            payload = json.dumps({"jsonrpc": "2.0", "id": rid, "result": {
                "tools": [{"name": "remote_echo", "description": "Echo.",
                           "inputSchema": {"type": "object",
                                           "properties": {}}}]}})
            self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
        elif method == "tools/call":
            args = (message.get("params") or {}).get("arguments") or {}
            self._json({"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text",
                             "text": "http:" + str(args.get("t", ""))}],
                "isError": False}})
        elif method == "ping":
            self._json({"jsonrpc": "2.0", "id": rid, "result": {}})
        else:
            self._json({"jsonrpc": "2.0", "id": rid, "error": {
                "code": -32601, "message": "method not found"}})

    def do_DELETE(self):
        self.send_response(200)
        self.end_headers()


@pytest.fixture()
def http_server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _HTTPHandler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/mcp"
    srv.shutdown()


class TestHTTPTransport:
    def test_handshake_records_the_session_id(self, http_server):
        conn = M.MCPConnection(M.MCPServerConfig(
            "remote", transport="http", url=http_server, timeout=10))
        try:
            conn.connect()
            assert conn.server_info["name"] == "http-test"
            assert conn.transport._session_id == SESSION_ID
        finally:
            conn.close()

    def test_sse_answer_is_parsed(self, http_server):
        conn = M.MCPConnection(M.MCPServerConfig(
            "remote", transport="http", url=http_server, timeout=10))
        try:
            conn.connect()
            tools = conn.list_tools()
            assert [t["name"] for t in tools] == ["remote_echo"]
        finally:
            conn.close()

    def test_calls_reach_the_server_after_initialize(self, http_server):
        conn = M.MCPConnection(M.MCPServerConfig(
            "remote", transport="http", url=http_server, timeout=10))
        try:
            conn.connect()
            text = M.normalize_tool_result(
                conn.call_tool("remote_echo", {"t": "hi"}))
            assert text == "http:hi"
        finally:
            conn.close()

    def test_session_enforcement_rejects_an_early_call(self, http_server):
        # A bare transport, no initialize: the server answers 400 to any
        # method that needs a session, and that surfaces as an MCPError.
        transport = M.StreamableHTTPTransport(M.MCPServerConfig(
            "remote", transport="http", url=http_server, timeout=10))
        with pytest.raises(M.MCPError, match="400"):
            transport.request("tools/list", {})


# ── the manager and its registry ─────────────────────────────────────────────

class TestManagerRegistry:
    def test_add_and_list(self, fresh_manager):
        result = fresh_manager.add_server(server_config(name="demo"))
        assert result["ok"] is True
        assert "demo" in fresh_manager.configs()

    def test_add_rejects_an_invalid_name(self, fresh_manager):
        result = fresh_manager.add_server(
            M.MCPServerConfig("bad name!", command="x"))
        assert result["ok"] is False

    def test_add_rejects_a_duplicate(self, fresh_manager):
        fresh_manager.add_server(server_config(name="demo"))
        again = fresh_manager.add_server(server_config(name="demo"))
        assert again["ok"] is False and "already exists" in again["message"]

    def test_remove_forgets_the_server(self, fresh_manager):
        fresh_manager.add_server(server_config(name="demo"))
        assert fresh_manager.remove_server("demo")["ok"] is True
        assert "demo" not in fresh_manager.configs()
        assert fresh_manager.remove_server("demo")["ok"] is False

    def test_enable_disable_round_trip(self, fresh_manager):
        fresh_manager.add_server(server_config(name="demo"))
        assert fresh_manager.set_enabled("demo", False)["ok"] is True
        assert fresh_manager.configs()["demo"].enabled is False
        assert fresh_manager.set_enabled("demo", True)["ok"] is True
        assert fresh_manager.configs()["demo"].enabled is True
        assert fresh_manager.set_enabled("ghost", True)["ok"] is False

    def test_registry_persists_to_the_isolated_config(self, fresh_manager,
                                                      isolated_config):
        fresh_manager.add_server(server_config(name="demo"))
        stored = json.loads(open(isolated_config, encoding="utf-8").read())
        assert stored["mcp_servers"]["demo"]["command"] == sys.executable


class TestManagerConnections:
    def test_a_dead_server_is_cached_as_failed(self, fresh_manager):
        fresh_manager.add_server(server_config(mode="crash", name="dead"))
        with pytest.raises(M.MCPError):
            fresh_manager.get_connection("dead")
        # the second attempt short-circuits to the same error without
        # spawning the process again
        start = time.monotonic()
        with pytest.raises(M.MCPError):
            fresh_manager.get_connection("dead")
        assert time.monotonic() - start < 0.5

    def test_tool_schemas_skip_a_dead_server(self, fresh_manager):
        fresh_manager.add_server(server_config(mode="crash", name="dead"))
        fresh_manager.add_server(server_config(mode="normal", name="live"))
        names = {s["function"]["name"] for s in fresh_manager.tool_schemas()}
        assert "mcp__live__echo" in names
        assert not any(n.startswith("mcp__dead__") for n in names)

    def test_disabled_servers_contribute_nothing(self, fresh_manager):
        fresh_manager.add_server(server_config(name="demo"))
        fresh_manager.set_enabled("demo", False)
        assert fresh_manager.tool_schemas() == []
        with pytest.raises(M.MCPError, match="disabled"):
            fresh_manager.get_connection("demo")

    def test_call_qualified_runs_the_right_tool(self, fresh_manager):
        fresh_manager.add_server(server_config(name="demo"))
        text = fresh_manager.call_qualified("mcp__demo__add", {"a": 1, "b": 2})
        assert text == "3"

    def test_call_qualified_reports_an_unknown_name(self, fresh_manager):
        text = fresh_manager.call_qualified("mcp__ghost__x", {})
        assert text.startswith("[ERROR] Unknown MCP tool")

    def test_call_failures_are_reported_not_raised_through(self,
                                                           fresh_manager):
        fresh_manager.add_server(server_config(mode="crash", name="dead"))
        text = fresh_manager.call_qualified("mcp__dead__echo", {})
        # the outage is named, not disguised as an unknown tool
        assert text.startswith("[MCP ERROR] server 'dead'")

    def test_status_reports_states(self, fresh_manager):
        fresh_manager.add_server(server_config(mode="crash", name="dead"))
        fresh_manager.add_server(server_config(mode="normal", name="live"))
        report = fresh_manager.status()
        by_name = {s["name"]: s for s in report["servers"]}
        assert by_name["dead"]["state"] in ("failed", "not connected")
        assert by_name["live"]["state"] in ("connected", "not connected")
        # connecting promotes the state
        fresh_manager.get_connection("live")
        assert fresh_manager.status("live")["state"] == "connected"

    def test_status_of_an_unknown_server_is_an_error(self, fresh_manager):
        assert fresh_manager.status("ghost")["ok"] is False

    def test_refresh_clears_the_schema_cache(self, fresh_manager):
        fresh_manager.add_server(server_config(name="demo"))
        fresh_manager.get_connection("demo")
        fresh_manager.tool_schemas()
        assert "demo" in fresh_manager._schema_cache
        fresh_manager.refresh("demo")
        assert "demo" not in fresh_manager._schema_cache

    def test_is_mcp_tool_matches_advertised_names(self, fresh_manager):
        fresh_manager.add_server(server_config(name="demo"))
        fresh_manager.tool_schemas()
        assert fresh_manager.is_mcp_tool("mcp__demo__echo") is True
        assert fresh_manager.is_mcp_tool("mcp__demo__nope") is False
        assert fresh_manager.is_mcp_tool("shell") is False


# ── runtime integration (dispatch + sub-agents) ──────────────────────────────

class TestRuntimeIntegration:
    @pytest.fixture()
    def dispatched_manager(self, fresh_manager):
        fresh_manager.add_server(server_config(name="demo"))
        return fresh_manager

    def test_tool_schemas_feed_the_model_catalogue(self, dispatched_manager):
        import core.llm_client as llm_client
        import core.tools as tools_mod
        static = {t["function"]["name"] for t in tools_mod.TOOLS_DESCRIPTION}
        dynamic = {s["function"]["name"]
                   for s in dispatched_manager.tool_schemas()}
        assert dynamic and not (dynamic & static)  # names never collide
        # the per-request listing is static + dynamic
        assert llm_client.mcp_client.mcp_manager is dispatched_manager

    def test_dispatch_tool_routes_an_mcp_call(self, dispatched_manager):
        import core.llm_client as llm_client
        call = {"function": {"name": "mcp__demo__add",
                             "arguments": json.dumps({"a": 20, "b": 22})}}
        assert llm_client._dispatch_tool(call) == "42"

    def test_dispatch_tool_surfaces_a_tool_error(self, dispatched_manager):
        import core.llm_client as llm_client
        call = {"function": {"name": "mcp__demo__boom",
                             "arguments": "{}"}}
        assert llm_client._dispatch_tool(call).startswith("[MCP ERROR]")

    def test_dispatch_tool_rejects_an_unknown_mcp_name(self,
                                                       dispatched_manager):
        import core.llm_client as llm_client
        call = {"function": {"name": "mcp__demo__nope", "arguments": "{}"}}
        # the server is the authority on its own tools: the unknown name
        # reaches it and comes back flagged as a tool error
        assert llm_client._dispatch_tool(call).startswith("[MCP ERROR]")

    def test_a_call_during_a_server_outage_reports_the_outage(self,
                                                              fresh_manager):
        """A cached-failed server must not turn its calls into 'unknown tool'."""
        fresh_manager.add_server(server_config(mode="crash", name="dead"))
        text = fresh_manager.call_qualified("mcp__dead__echo", {})
        assert text.startswith("[MCP ERROR] server 'dead'")

    def test_a_call_to_an_unconfigured_namespace_is_unknown(self,
                                                            fresh_manager):
        text = fresh_manager.call_qualified("mcp__ghost__echo", {})
        assert text.startswith("[ERROR] Unknown MCP tool")

    def test_sub_agent_grants_include_mcp_tools(self, dispatched_manager):
        import core.tools as tools_mod
        schemas, granted, dropped = tools_mod._select_sub_tools(
            ["mcp__demo__echo", "shell", "manage_mcp"], 5)
        assert granted == ["mcp__demo__echo", "shell"]
        assert dropped == ["manage_mcp"]  # documented exclusion still holds
        names = {s["function"]["name"] for s in schemas}
        assert names == {"mcp__demo__echo", "shell"}

    def test_sub_agent_dispatch_runs_an_mcp_tool(self, dispatched_manager):
        import core.tools as tools_mod
        out = tools_mod._dispatch_sub_tool(
            "mcp__demo__echo", json.dumps({"text": "from worker"}))
        assert out == "echo: from worker"

    def test_manage_mcp_is_excluded_from_workers_with_a_reason(self):
        import core.tools as tools_mod
        assert "manage_mcp" in tools_mod._SUB_AI_EXCLUDED_TOOLS
        assert "manage_mcp" not in tools_mod._SUB_TOOL_FUNCS

    def test_manage_mcp_lists_and_adds(self, fresh_manager):
        import core.tools as tools_mod
        assert "No MCP servers configured" in tools_mod.manage_mcp("list")
        added = tools_mod.manage_mcp(
            "add", server="demo", transport="stdio",
            command=sys.executable, args=[SERVER, "normal"])
        assert "added" in added
        assert "demo" in tools_mod.manage_mcp("list")
        assert tools_mod.manage_mcp("add", server="demo") .startswith(
            "[ERROR]")

    def test_manage_mcp_rejects_unknown_actions(self):
        import core.tools as tools_mod
        assert tools_mod.manage_mcp("teleport").startswith("[ERROR]")

    def test_the_registry_audit_invariants_still_hold(self):
        """Adding a tool must keep the shipped audit green."""
        import core.tools as tools_mod
        schemas = {e["function"]["name"]
                   for e in tools_mod.TOOLS_DESCRIPTION}
        registry = set(tools_mod._SUB_TOOL_FUNCS)
        excluded = set(tools_mod._SUB_AI_EXCLUDED_TOOLS)
        assert not ((schemas - registry) - excluded)
        assert not (excluded & registry)
        assert not (registry - schemas)
