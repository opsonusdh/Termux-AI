"""
Tests for the browser tool's interaction layer.

Covers the four things that turn the browser tool into something you can
drive like a human rather than a scraper:

  1. Clicking at arbitrary viewport coordinates (not just element centres).
  2. Running JavaScript in the live page context.
  3. Pulling the raw DOM out of a live page.
  4. Watching a session's health from the outside.

The DevTools socket is faked, so none of these tests need a Chromium process,
a network connection or an API key.
"""
import json
import os
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORE = os.path.join(REPO_ROOT, "core")
for _p in (REPO_ROOT, CORE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core import tools  # noqa: E402
from core import llm_client  # noqa: E402


# ── a fake DevTools socket ───────────────────────────────────────────────────

def _string_value(text):
    """A CDP response carrying a plain string result."""
    return {"result": {"result": {"type": "string", "value": text}}}


def _object_value(obj):
    """A CDP response carrying an object result."""
    return {"result": {"result": {"type": "object", "value": obj}}}


class FakeWS:
    """A stand-in for the DevTools WebSocket.

    Records every command that was sent and answers with whatever the test
    configured for that CDP method.
    """

    def __init__(self, handlers=None, default=None):
        self.sent = []
        self._outbox = []
        self.handlers = handlers or {}
        self.default = default if default is not None else _string_value("complete")

    def send(self, raw):
        msg = json.loads(raw)
        self.sent.append(msg)
        method = msg.get("method")
        params = msg.get("params", {})
        handler = self.handlers.get(method)
        if handler is None:
            payload = self.default
        elif callable(handler):
            payload = handler(params)
        else:
            payload = handler
        reply = {"id": msg.get("id")}
        reply.update(payload or {})
        self._outbox.append(json.dumps(reply))

    def recv(self):
        if self._outbox:
            return self._outbox.pop(0)
        return json.dumps({"id": -1})

    def methods(self, name):
        return [m for m in self.sent if m.get("method") == name]


class _LiveProc:
    def poll(self):
        return None


class _DeadProc:
    def poll(self):
        return 1


# ── 1. clicking anywhere on the page ─────────────────────────────────────────

class TestCoordinateClicks:
    def test_a_click_can_target_arbitrary_coordinates(self):
        ws = FakeWS()
        ok, err, _mid, results = tools._perform_actions(
            ws, [{"type": "click", "x": 421, "y": 137, "wait_after": 0}], 10)

        assert ok, err
        clicks = ws.methods("Input.dispatchMouseEvent")
        assert len(clicks) == 2, "a click is a press plus a release"
        for message in clicks:
            assert message["params"]["x"] == 421
            assert message["params"]["y"] == 137
            assert message["params"]["button"] == "left"
        assert results[0] == "clicked at (421, 137)"

    def test_a_coordinate_click_needs_no_element_lookup(self):
        # Without x/y the tool has to ask the page where the element is.
        # With x/y there is nothing to look up.
        ws = FakeWS()
        tools._perform_actions(
            ws, [{"type": "click", "x": 10, "y": 20, "wait_after": 0}], 1)
        assert ws.methods("Runtime.evaluate") == []

    def test_coordinates_win_over_a_selector(self):
        # x/y is an explicit instruction ("click *there*"), so it must not be
        # silently overridden by a selector that also happens to be present.
        ws = FakeWS()
        ok, err, _mid, _results = tools._perform_actions(
            ws, [{"type": "click", "selector": "#btn", "x": 7, "y": 9,
                  "wait_after": 0}], 1)

        assert ok, err
        clicks = ws.methods("Input.dispatchMouseEvent")
        assert clicks[0]["params"]["x"] == 7
        assert clicks[0]["params"]["y"] == 9

    def test_a_click_needs_either_a_selector_or_coordinates(self):
        ws = FakeWS()
        ok, err, _mid, results = tools._perform_actions(
            ws, [{"type": "click", "wait_after": 0}], 1)

        assert not ok
        assert "x/y" in err
        assert results == []

    def test_a_selector_click_targets_the_centre_of_the_element(self):
        def evaluate(params):
            expr = params.get("expression", "")
            if "getBoundingClientRect" in expr:
                return _string_value(json.dumps(
                    {"x": 100, "y": 200, "width": 60, "height": 40}))
            return _string_value("complete")

        ws = FakeWS({"Runtime.evaluate": evaluate})
        ok, err, _mid, results = tools._perform_actions(
            ws, [{"type": "click", "selector": "#btn", "wait_after": 0}], 1)

        assert ok, err
        clicks = ws.methods("Input.dispatchMouseEvent")
        assert clicks[0]["params"]["x"] == 130
        assert clicks[0]["params"]["y"] == 220
        assert "#btn" in results[0]

    def test_a_click_on_a_missing_element_reports_the_selector(self):
        def evaluate(params):
            expr = params.get("expression", "")
            if "getBoundingClientRect" in expr:
                return _string_value("null")
            return _string_value("complete")

        ws = FakeWS({"Runtime.evaluate": evaluate})
        ok, err, _mid, _results = tools._perform_actions(
            ws, [{"type": "click", "selector": "#nope", "wait_after": 0}], 1)

        assert not ok
        assert "#nope" in err

    def test_every_command_in_a_batch_gets_its_own_message_id(self):
        # The DevTools protocol matches a response to its command by id, so
        # an id that is used twice can hand a command somebody else's
        # answer.
        def evaluate(params):
            expr = params.get("expression", "")
            if "getBoundingClientRect" in expr:
                return _string_value(json.dumps(
                    {"x": 10, "y": 20, "width": 30, "height": 40}))
            return _string_value("complete")

        ws = FakeWS({"Runtime.evaluate": evaluate})
        ok, err, mid, _results = tools._perform_actions(
            ws, [{"type": "click", "selector": "#btn", "wait_after": 0},
                 {"type": "input", "selector": "#field", "text": "abc",
                  "wait_after": 0},
                 {"type": "evaluate", "expression": "1", "wait_after": 0}], 10)

        assert ok, err
        ids = [m["id"] for m in ws.sent]
        assert len(ids) == len(set(ids)), f"a message id was reused: {ids}"
        assert mid > max(ids), "the next id has to be past the ones used"


# ── 2. running JavaScript in the live page ───────────────────────────────────

class TestEvaluateAction:
    def test_javascript_runs_in_the_page_and_the_value_comes_back(self):
        ws = FakeWS({"Runtime.evaluate": lambda p: _string_value("42")})
        ok, err, _mid, results = tools._perform_actions(
            ws, [{"type": "evaluate", "expression": "6 * 7", "wait_after": 0}], 3)

        assert ok, err
        sent = ws.methods("Runtime.evaluate")
        assert sent[0]["params"]["expression"] == "6 * 7"
        # A promise has to be awaited, otherwise an async page answer arrives
        # as a pending promise object instead of its value.
        assert sent[0]["params"]["awaitPromise"] is True
        assert results[0] == "evaluate: 42"

    def test_an_object_result_comes_back_as_json(self):
        ws = FakeWS({"Runtime.evaluate": lambda p: _object_value({"ok": True})})
        ok, err, _mid, results = tools._perform_actions(
            ws, [{"type": "evaluate", "expression": "({ok:true})",
                  "wait_after": 0}], 3)

        assert ok, err
        assert '"ok": true' in results[0]

    def test_a_long_result_is_truncated(self):
        ws = FakeWS({"Runtime.evaluate": lambda p: _string_value("x" * 900)})
        ok, err, _mid, results = tools._perform_actions(
            ws, [{"type": "evaluate", "expression": "big()", "wait_after": 0}], 3)

        assert ok, err
        assert "truncated" in results[0]
        assert len(results[0]) < 600

    def test_a_javascript_exception_is_reported_not_raised(self):
        def evaluate(params):
            return {"result": {
                "result": {"type": "object", "subtype": "error"},
                "exceptionDetails": {"text": "Uncaught ReferenceError: nope"},
            }}

        ws = FakeWS({"Runtime.evaluate": evaluate})
        ok, err, _mid, results = tools._perform_actions(
            ws, [{"type": "evaluate", "expression": "nope()", "wait_after": 0}], 3)

        assert ok, err
        assert results[0].startswith("evaluate error:")
        assert "ReferenceError" in results[0]

    def test_evaluate_without_an_expression_is_rejected(self):
        ws = FakeWS()
        ok, err, _mid, results = tools._perform_actions(
            ws, [{"type": "evaluate", "wait_after": 0}], 1)

        assert not ok
        assert "expression" in err
        assert results == []

    def test_an_unknown_action_type_is_rejected(self):
        ws = FakeWS()
        ok, err, _mid, _results = tools._perform_actions(
            ws, [{"type": "teleport", "wait_after": 0}], 1)

        assert not ok
        assert "teleport" in err

    def test_a_javascript_exception_reports_the_real_reason(self):
        # "Uncaught" alone is useless to whoever has to fix the expression.
        def evaluate(params):
            return {"result": {
                "result": {"type": "object", "subtype": "error"},
                "exceptionDetails": {
                    "text": "Uncaught",
                    "exception": {
                        "description": "Uncaught SyntaxError: Unexpected token",
                    },
                },
            }}

        ws = FakeWS({"Runtime.evaluate": evaluate})
        _value, err = tools._evaluate_js(ws, "this is not javascript", msg_id=1)

        assert err is not None
        assert "SyntaxError" in err
        assert "Unexpected token" in err


# ── 3. reading the raw DOM out of a live page ────────────────────────────────

class TestDomExtraction:
    def test_the_raw_html_of_a_selector_is_returned(self):
        ws = FakeWS({"Runtime.evaluate": lambda p: _string_value(
            "<button id='btn' data-role='primary'>Press</button>")})
        session = {"ws": ws, "msg_id": 5}

        html = tools._browser_extract_html(session, "#btn")

        assert "data-role='primary'" in html
        expr = ws.methods("Runtime.evaluate")[0]["params"]["expression"]
        assert "outerHTML" in expr
        assert "querySelectorAll" in expr

    def test_without_a_selector_the_whole_document_is_returned(self):
        ws = FakeWS({"Runtime.evaluate": lambda p: _string_value("<html></html>")})
        session = {"ws": ws, "msg_id": 5}

        html = tools._browser_extract_html(session)

        assert "<html>" in html
        expr = ws.methods("Runtime.evaluate")[0]["params"]["expression"]
        assert "documentElement.outerHTML" in expr

    def test_the_generated_javascript_is_valid(self):
        # A raw newline inside a JS string literal is a syntax error, and the
        # browser then answers "Uncaught" without saying what went wrong.
        ws = FakeWS({"Runtime.evaluate": lambda p: _string_value("")})
        session = {"ws": ws, "msg_id": 5}

        tools._browser_extract_html(session, "#btn")

        expr = ws.methods("Runtime.evaluate")[0]["params"]["expression"]
        assert "\n" not in expr, "the JS must stay on one line"
        assert "\\n" in expr, "the newline has to be escaped for the browser"

    def test_the_readable_text_is_returned_too(self):
        ws = FakeWS({"Runtime.evaluate": lambda p: _string_value("Hello Orion")})
        session = {"ws": ws, "msg_id": 5}

        text = tools._browser_extract_text(session, "#heading")

        assert text == "Hello Orion"

    def test_a_broken_socket_is_reported_instead_of_crashing(self):
        session = {"ws": None, "msg_id": 1}
        assert tools._browser_extract_html(session).startswith("[ERROR]")
        assert tools._browser_extract_text(session).startswith("[ERROR]")


# ── 4. watching the sessions ─────────────────────────────────────────────────

class TestSessionHealth:
    def test_a_live_process_is_reported_as_alive(self):
        session = {"proc": _LiveProc(), "created": 1.0, "last_used": 2.0,
                   "url": "http://example.test", "title": "Example",
                   "port": 9222, "screenshots": 3}
        health = tools._browser_session_health(session)

        assert health["process_alive"] is True
        assert health["url"] == "http://example.test"
        assert health["title"] == "Example"
        assert health["screenshots"] == 3

    def test_a_crashed_process_is_reported_as_dead(self):
        session = {"proc": _DeadProc(), "created": 1.0, "last_used": 2.0,
                   "url": "", "title": "", "port": 0, "screenshots": 0}
        health = tools._browser_session_health(session)

        assert health["process_alive"] is False

    def test_the_state_action_reports_the_health(self, monkeypatch):
        ws = FakeWS({"Runtime.evaluate": lambda p: _string_value("page body")})
        session = {"id": "b9", "proc": _LiveProc(), "ws": ws, "msg_id": 1,
                   "url": "http://example.test", "title": "Example",
                   "created": time.time(), "last_used": time.time(),
                   "port": 9222, "screenshots": 2}
        monkeypatch.setattr(tools, "_browser_get", lambda bid: session)

        out = tools.browser(action="state", browser_id="b9", screenshot=False)

        assert "process: alive" in out
        assert "url: http://example.test" in out
        assert "title: Example" in out
        assert "screenshots taken: 2" in out
        assert "page body" in out

    def test_the_state_action_reports_a_dead_browser(self, monkeypatch):
        ws = FakeWS({"Runtime.evaluate": lambda p: _string_value("")})
        session = {"id": "b9", "proc": _DeadProc(), "ws": ws, "msg_id": 1,
                   "url": "", "title": "", "created": 0.0, "last_used": 0.0,
                   "port": 0, "screenshots": 0}
        monkeypatch.setattr(tools, "_browser_get", lambda bid: session)

        out = tools.browser(action="state", browser_id="b9", screenshot=False)

        assert "process: DEAD" in out

    def test_the_session_list_names_every_open_session(self):
        tools._browser_close_all()
        session = {"id": "b7", "proc": _LiveProc(), "ws": None, "msg_id": 1,
                   "url": "http://example.test", "title": "Example",
                   "created": time.time(), "last_used": time.time(),
                   "port": 9222, "screenshots": 1}
        with tools._BROWSER_LOCK:
            tools._BROWSER_SESSIONS["b7"] = session
        try:
            out = tools.browser(action="list")
            assert "b7" in out
            assert "http://example.test" in out
        finally:
            tools._browser_close_all()


# ── 5. the actions actually report back ──────────────────────────────────────

class TestActionResults:
    """The 'act' and 'navigate' actions have to end with the page state.

    An earlier version ran the actions and then fell out of the dispatch
    chain, so both of them answered "Unknown browser action" *after* the
    work had already been done -- the model was told the click failed while
    the click had in fact landed.
    """

    def _session(self, ws):
        return {"id": "b9", "proc": _LiveProc(), "ws": ws, "msg_id": 1,
                "url": "http://example.test", "title": "Example",
                "created": time.time(), "last_used": time.time(),
                "port": 9222, "screenshots": 0}

    def _evaluate(self, params):
        expr = params.get("expression", "")
        if "location.href" in expr:
            return _string_value(json.dumps(
                {"url": "http://example.test", "title": "Example"}))
        if "document.readyState" in expr:
            return _string_value("complete")
        if "getBoundingClientRect" in expr:
            return _string_value(json.dumps(
                {"x": 10, "y": 20, "width": 30, "height": 40}))
        return _string_value("page body")

    def test_the_act_action_returns_the_page_state(self, monkeypatch):
        ws = FakeWS({"Runtime.evaluate": self._evaluate})
        monkeypatch.setattr(tools, "_browser_get", lambda bid: self._session(ws))

        out = tools.browser(action="act", browser_id="b9", screenshot=False,
                            actions=[{"type": "click", "x": 5, "y": 6,
                                      "wait_after": 0}])

        assert "Unknown browser action" not in out
        assert "page body" in out
        assert "clicked at (5, 6)" in out

    def test_the_act_action_carries_the_evaluate_result(self, monkeypatch):
        ws = FakeWS({"Runtime.evaluate": lambda p: _string_value("42")})
        monkeypatch.setattr(tools, "_browser_get", lambda bid: self._session(ws))

        out = tools.browser(action="act", browser_id="b9", screenshot=False,
                            actions=[{"type": "evaluate", "expression": "1+41",
                                      "wait_after": 0}])

        assert "Unknown browser action" not in out
        assert "evaluate: 42" in out

    def test_the_act_action_without_actions_is_rejected(self, monkeypatch):
        ws = FakeWS({"Runtime.evaluate": self._evaluate})
        monkeypatch.setattr(tools, "_browser_get", lambda bid: self._session(ws))

        out = tools.browser(action="act", browser_id="b9", screenshot=False)

        assert out.startswith("[ERROR]")
        assert "actions list" in out

    def test_the_navigate_action_returns_the_page_state(self, monkeypatch):
        ws = FakeWS({"Runtime.evaluate": self._evaluate,
                     "Page.navigate": {"result": {}}})
        monkeypatch.setattr(tools, "_browser_get", lambda bid: self._session(ws))

        out = tools.browser(action="navigate", browser_id="b9",
                            url="http://example.test/next", screenshot=False)

        assert "Unknown browser action" not in out
        assert "page body" in out
        assert ws.methods("Page.navigate")

    def test_the_navigate_action_without_a_url_is_rejected(self, monkeypatch):
        ws = FakeWS({"Runtime.evaluate": self._evaluate})
        monkeypatch.setattr(tools, "_browser_get", lambda bid: self._session(ws))

        out = tools.browser(action="navigate", browser_id="b9", url="",
                            screenshot=False)

        assert out.startswith("[ERROR]")
        assert "requires a url" in out

    def test_the_interact_alias_works_too(self, monkeypatch):
        ws = FakeWS({"Runtime.evaluate": self._evaluate})
        monkeypatch.setattr(tools, "_browser_get", lambda bid: self._session(ws))

        out = tools.browser(action="interact", browser_id="b9", screenshot=False,
                            actions=[{"type": "wait", "wait_after": 0}])

        assert "Unknown browser action" not in out
        assert "page body" in out


# ── 6. the tool advertises what it can do ────────────────────────────────────

class TestBrowserSchema:
    def _browser_entry(self):
        return next(t for t in tools.TOOLS_DESCRIPTION
                    if t["function"]["name"] == "browser")

    def test_the_schema_advertises_coordinate_clicks(self):
        items = self._browser_entry()["function"]["parameters"]["properties"][
            "actions"]["items"]["properties"]
        assert "x" in items
        assert "y" in items

    def test_the_schema_advertises_javascript_evaluation(self):
        items = self._browser_entry()["function"]["parameters"]["properties"][
            "actions"]["items"]["properties"]
        assert "evaluate" in items["type"]["enum"]
        assert "expression" in items

    def test_the_schema_advertises_raw_dom_extraction(self):
        props = self._browser_entry()["function"]["parameters"]["properties"]
        assert "extract_html" in props

    def test_the_one_shot_browser_advertises_raw_dom_extraction(self):
        entry = next(t for t in tools.TOOLS_DESCRIPTION
                     if t["function"]["name"] == "browse_web")
        assert "extract_html" in entry["function"]["parameters"]["properties"]

    def test_the_dispatch_route_forwards_the_new_arguments(self, monkeypatch):
        captured = {}

        def fake_browser(**kwargs):
            captured.update(kwargs)
            return "ok"

        monkeypatch.setattr(llm_client, "browser", fake_browser)
        out = llm_client._dispatch_tool({
            "function": {"name": "browser", "arguments": json.dumps({
                "action": "state", "browser_id": "b3",
                "extract_html": True, "max_chars": 5000,
            })}})

        assert out == "ok"
        assert captured["action"] == "state"
        assert captured["browser_id"] == "b3"
        assert captured["extract_html"] is True
        assert captured["max_chars"] == 5000
