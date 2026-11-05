"""
Tests for the browser tools' three promises.

  1. A screenshot is taken *only* when the model asks for one.  An
     unasked-for picture would have to be handed to a model that cannot
     see images, and the turn would then be moved to a different model --
     the conversation would silently change models for a picture nobody
     wanted.
  2. The page is given time to finish filling itself in before it is read.
     A search engine can report a finished load while its background
     script is still filling the result list; reading or photographing it
     at that moment returns an empty page.
  3. The page is read as Markdown built from the DOM, and one element can
     be pulled out by class name, id, tag name or CSS selector together
     with everything inside it.

The DevTools socket is faked, so none of these tests need a Chromium
process, a network connection or an API key.
"""
import inspect
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
from core import html_markdown  # noqa: E402


# ── a fake DevTools socket ───────────────────────────────────────────────────

def _string_value(text):
    """A CDP response carrying a plain string result."""
    return {"result": {"result": {"type": "string", "value": text}}}


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

    def expressions(self):
        """Every JavaScript expression that was sent to the page."""
        return [m["params"].get("expression", "")
                for m in self.methods("Runtime.evaluate")]


class _LiveProc:
    def poll(self):
        return None


WEATHER_BOX = (
    '<div class="weather data">'
    '<div class="temperature">20&deg;C</div>'
    '<div class="sky-state">Clear</div>'
    '<div class="aqi">42</div>'
    '</div>'
)

PAGE_HTML = (
    "<html><body>"
    "<nav class=\"navbar\"><a href=\"/\">Home</a></nav>"
    "<h1>Weather</h1>"
    "<p>It is <strong>warm</strong> today.</p>"
    "<p>See <a href=\"/details\">the details</a>.</p>"
    + WEATHER_BOX +
    "<div class=\"cookie-banner\">We use cookies</div>"
    "<footer>Copyright</footer>"
    "</body></html>"
)


def _page_evaluate(params, html=PAGE_HTML, fingerprint=None):
    """Answer every probe the browser tools send to a page."""
    expr = params.get("expression", "")
    if "location.href" in expr:
        return _string_value(json.dumps(
            {"url": "http://example.test", "title": "Example"}))
    if "classList" in expr:
        # A search: the page reports the elements the query matched.
        return _string_value(json.dumps({
            "total": 1,
            "mode": "class",
            "elements": [{"tag": "div", "id": "", "cls": "weather data",
                          "html": WEATHER_BOX}],
        }))
    if "getElementsByTagName" in expr:
        # The content-settling probe.  It asks for the ready state as well,
        # so it has to be answered before the plain ready-state probe.
        state = fingerprint or {"ready": "complete", "len": 120,
                                "nodes": 12, "html": 900}
        return _string_value(json.dumps(state))
    if "document.readyState" in expr:
        return _string_value("complete")
    if "outerHTML" in expr:
        # The DOM the Markdown is built from.
        return _string_value(json.dumps({"count": 1, "htmls": [html]}))
    return _string_value("page body")


def _session(ws, **extra):
    """A live-looking browser session wired to the fake socket."""
    session = {"id": "b1", "proc": _LiveProc(), "ws": ws, "msg_id": 1,
               "url": "http://example.test", "title": "Example",
               "created": time.time(), "last_used": time.time(),
               "port": 9222, "screenshots": 0}
    session.update(extra)
    return session


# ── 1. a screenshot is a thing you ask for ───────────────────────────────────

class TestScreenshotsAreOptIn:
    def test_neither_browser_tool_takes_a_picture_by_default(self):
        assert inspect.signature(tools.browser).parameters["screenshot"].default is False
        assert inspect.signature(tools.browse_web).parameters["screenshot"].default is False

    def test_the_schema_tells_the_model_the_picture_is_opt_in(self):
        for name in ("browser", "browse_web"):
            entry = next(t for t in tools.TOOLS_DESCRIPTION
                         if t["function"]["name"] == name)
            props = entry["function"]["parameters"]["properties"]
            assert props["screenshot"]["default"] is False, name
            assert "ONLY" in props["screenshot"]["description"], name

    def test_the_dispatch_route_does_not_ask_for_a_picture(self, monkeypatch):
        seen = {}

        def fake_browse_web(**kwargs):
            seen.update(kwargs)
            return "ok"

        monkeypatch.setattr(llm_client, "browse_web", fake_browse_web)
        llm_client._dispatch_tool({"function": {
            "name": "browse_web",
            "arguments": json.dumps({"url": "http://example.test"})}})

        assert seen["screenshot"] is False

    def test_the_dispatch_route_passes_the_search_through(self, monkeypatch):
        seen = {}

        def fake_browse_web(**kwargs):
            seen.update(kwargs)
            return "ok"

        monkeypatch.setattr(llm_client, "browse_web", fake_browse_web)
        llm_client._dispatch_tool({"function": {
            "name": "browse_web",
            "arguments": json.dumps({"url": "http://example.test",
                                     "search": "weather data"})}})

        assert seen["search"] == "weather data"

    def test_no_picture_is_taken_when_none_was_asked_for(self, monkeypatch):
        taken = []
        monkeypatch.setattr(tools, "_browser_screenshot",
                            lambda *a, **k: taken.append(1))
        ws = FakeWS({"Runtime.evaluate": _page_evaluate})
        monkeypatch.setattr(tools, "_browser_get", lambda bid: _session(ws))

        tools.browser(action="state", browser_id="b1", screenshot=False)

        assert taken == [], "the page was not photographed"

    def test_a_picture_is_taken_when_it_was_asked_for(self, monkeypatch):
        taken = []

        def fake_screenshot(session, label="screenshot"):
            taken.append(label)
            return None

        monkeypatch.setattr(tools, "_browser_screenshot", fake_screenshot)
        ws = FakeWS({"Runtime.evaluate": _page_evaluate})
        monkeypatch.setattr(tools, "_browser_get", lambda bid: _session(ws))

        tools.browser(action="state", browser_id="b1", screenshot=True)

        assert taken == ["b1"], "the page was photographed because it was asked for"

    def test_an_unasked_for_picture_never_changes_the_model(self):
        # The rule that protects the conversation: only a picture the model
        # asked for may move the turn to a vision-capable model.
        assert llm_client._wants_vision_switch(
            [{"path": "/tmp/x.png", "explicit": False}],
            "openrouter", "some/text-model", False) is False

    def test_an_asked_for_picture_does_move_the_turn(self):
        assert llm_client._wants_vision_switch(
            [{"path": "/tmp/x.png", "explicit": True}],
            "openrouter", "some/text-model", False) is True

    def test_the_switch_happens_at_most_once_per_turn(self):
        assert llm_client._wants_vision_switch(
            [{"path": "/tmp/x.png", "explicit": True}],
            "openrouter", "some/text-model", True) is False

    def test_an_unasked_for_picture_is_not_handed_to_the_model(self):
        messages = []
        llm_client._append_image_message(
            messages, [{"path": "/tmp/x.png", "explicit": False}])
        assert messages == [], "nobody asked for that picture"

    def test_an_asked_for_picture_is_handed_to_the_model(self):
        messages = []
        llm_client._append_image_message(
            messages, [{"path": "/tmp/x.png", "explicit": True}])
        assert len(messages) == 1
        assert messages[0]["content"][1]["path"] == "/tmp/x.png"


# ── 2. the page is given time to fill itself in ─────────────────────────────

class TestThePageGetsTimeToSettle:
    def test_the_page_gets_ten_seconds(self):
        assert tools._BROWSER_CONTENT_TIMEOUT == 10.0

    def test_the_content_is_probed_before_the_page_is_read(self, monkeypatch):
        # The order matters: reading the page first and waiting afterwards
        # would still read an empty page.
        ws = FakeWS({"Runtime.evaluate": _page_evaluate})
        monkeypatch.setattr(tools, "_browser_get", lambda bid: _session(ws))

        tools.browser(action="state", browser_id="b1", screenshot=False)

        expressions = ws.expressions()
        probe = next(i for i, e in enumerate(expressions)
                     if "getElementsByTagName" in e)
        read = next(i for i, e in enumerate(expressions) if "outerHTML" in e)
        assert probe < read, "the page has to settle before it is read"

    def test_a_stable_page_is_reported_as_settled(self):
        ws = FakeWS({"Runtime.evaluate": _page_evaluate})
        report = tools._browser_wait_for_content(_session(ws))

        assert report["settled"] is True
        assert report["info"]["ready"] == "complete"

    def test_the_page_has_to_stay_quiet_for_a_moment(self):
        # Two identical readings a third of a second apart prove nothing:
        # a page can be quiet for a moment in the middle of filling itself
        # in.  The quiet window is what catches that.
        #
        # The shipped value is read from the source: the test fixture
        # shortens the window so the suite stays quick.
        src = open(os.path.join(CORE, "tools.py"), encoding="utf-8").read()
        assert "_BROWSER_CONTENT_SETTLE = 1.5" in src

    def test_a_page_that_is_quiet_for_a_moment_is_not_yet_settled(self, monkeypatch):
        monkeypatch.setattr(tools, "_BROWSER_CONTENT_TIMEOUT", 0.4)
        monkeypatch.setattr(tools, "_BROWSER_CONTENT_SETTLE", 1.0)
        monkeypatch.setattr(tools, "_BROWSER_CONTENT_POLL", 0.05)

        samples = []

        def changing(params):
            expr = params.get("expression", "")
            if "getElementsByTagName" in expr:
                samples.append(1)
                # Quiet for the first two readings, then the page changes.
                size = 10 if len(samples) <= 2 else 20
                return _string_value(json.dumps(
                    {"ready": "complete", "len": size, "nodes": size,
                     "html": size * 10, "res": size}))
            return _page_evaluate(params)

        ws = FakeWS({"Runtime.evaluate": changing})
        report = tools._browser_wait_for_content(_session(ws))

        assert len(samples) >= 3, "the page was looked at again"
        assert report["settled"] is False, "a page that changed is not settled"

    def test_a_settled_page_is_not_waited_for_a_second_time(self, monkeypatch):
        monkeypatch.setattr(tools, "_BROWSER_CONTENT_SETTLE", 0.05)
        monkeypatch.setattr(tools, "_BROWSER_CONTENT_POLL", 0.01)
        ws = FakeWS({"Runtime.evaluate": _page_evaluate})
        session = _session(ws)

        tools._browser_wait_for_content(session)
        started = time.monotonic()
        report = tools._browser_wait_for_content(session)

        assert report["settled"] is True
        assert time.monotonic() - started < 0.5, \
            "the page was already quiet, the clock does not start over"

    def test_a_page_that_keeps_changing_gives_up(self, monkeypatch):
        monkeypatch.setattr(tools, "_BROWSER_CONTENT_TIMEOUT", 0.2)
        monkeypatch.setattr(tools, "_BROWSER_CONTENT_POLL", 0.01)

        counter = {"n": 0}

        def changing(params):
            expr = params.get("expression", "")
            if "getElementsByTagName" in expr:
                counter["n"] += 1
                return _string_value(json.dumps(
                    {"ready": "complete", "len": counter["n"] * 10,
                     "nodes": counter["n"], "html": counter["n"] * 100}))
            return _page_evaluate(params)

        ws = FakeWS({"Runtime.evaluate": changing})
        report = tools._browser_wait_for_content(_session(ws))

        assert report["settled"] is False, "a page that never settles is not settled"
        assert report["waited"] >= 0.2

    def test_a_page_that_never_answers_does_not_stall_the_call(self, monkeypatch):
        monkeypatch.setattr(tools, "_BROWSER_CONTENT_POLL", 0.01)

        ws = FakeWS({"Runtime.evaluate": lambda p: _string_value("nonsense")})
        report = tools._browser_wait_for_content(_session(ws))

        assert report["settled"] is False
        assert report["waited"] < tools._BROWSER_CONTENT_TIMEOUT, \
            "a page that will not answer must not burn the whole timeout"

    def test_an_unsettled_page_says_so_in_the_answer(self):
        note = tools._browser_content_note({"settled": False, "waited": 10.0})
        assert "still filling" in note
        assert tools._browser_content_note({"settled": True}) == ""


# ── 3. the page comes back as Markdown ───────────────────────────────────────

class TestThePageIsReadAsMarkdown:
    def _read(self, monkeypatch, html=PAGE_HTML):
        ws = FakeWS({"Runtime.evaluate": _page_evaluate})
        monkeypatch.setattr(tools, "_browser_get", lambda bid: _session(ws))
        return tools.browser(action="state", browser_id="b1", screenshot=False)

    def test_the_answer_is_markdown(self, monkeypatch):
        out = self._read(monkeypatch)

        assert "--- page (markdown) ---" in out
        assert "# Weather" in out, "the heading survived"
        assert "**warm**" in out, "the emphasis survived"

    def test_the_furniture_is_dropped(self, monkeypatch):
        out = self._read(monkeypatch)

        assert "Home" not in out, "the navigation bar is not content"
        assert "Copyright" not in out, "the footer is not content"
        assert "We use cookies" not in out, "the cookie banner is not content"

    def test_the_links_keep_their_target(self, monkeypatch):
        out = self._read(monkeypatch)

        # The link is resolved against the page it came from.
        assert "[the details](http://example.test/details)" in out


# ── 4. finding one element on the page ───────────────────────────────────────

class TestFindingAnElement:
    def test_the_query_kinds_are_recognised(self):
        assert tools._classify_search_query("weather data") == "class"
        assert tools._classify_search_query("div") == "tag"
        assert tools._classify_search_query("a") == "tag"
        assert tools._classify_search_query("#results") == "selector"
        assert tools._classify_search_query(".row") == "selector"
        assert tools._classify_search_query("searchResults") == "id"

    def test_a_selector_with_a_class_inside_is_a_selector(self):
        # "div.weather" is a selector, not the id "div.weather" -- a dot
        # cannot occur inside an id.
        assert tools._classify_search_query("div.weather") == "selector"
        assert tools._classify_search_query("ul#main") == "selector"
        assert tools._classify_search_query("#results .row") == "selector"

    def test_a_class_name_finds_the_box(self, monkeypatch):
        ws = FakeWS({"Runtime.evaluate": _page_evaluate})
        monkeypatch.setattr(tools, "_browser_get", lambda bid: _session(ws))

        out = tools.browser(action="state", browser_id="b1", screenshot=False,
                            search="weather data")

        assert "--- search: weather data ---" in out
        assert "div.weather.data" in out, "the element names itself"

    def test_the_whole_box_comes_back_as_markdown(self, monkeypatch):
        ws = FakeWS({"Runtime.evaluate": _page_evaluate})
        monkeypatch.setattr(tools, "_browser_get", lambda bid: _session(ws))

        out = tools.browser(action="state", browser_id="b1", screenshot=False,
                            search="weather data")

        assert "**temperature**: 20°C" in out
        assert "**sky state**: Clear" in out
        assert "**aqi**: 42" in out

    def test_a_query_that_matches_nothing_says_so(self, monkeypatch):
        def nothing(params):
            expr = params.get("expression", "")
            if "classList" in expr:
                return _string_value(json.dumps(
                    {"total": 0, "elements": [], "mode": "class"}))
            return _page_evaluate(params)

        ws = FakeWS({"Runtime.evaluate": nothing})
        monkeypatch.setattr(tools, "_browser_get", lambda bid: _session(ws))

        out = tools.browser(action="state", browser_id="b1", screenshot=False,
                            search="nothing-here")

        assert "matched 'nothing-here'" in out


# ── 5. the converter itself ──────────────────────────────────────────────────

class TestTheMarkdownConverter:
    def _md(self, html):
        return html_markdown.html_to_markdown(html)

    def test_headings_become_headings(self):
        assert "# Title" in self._md("<h1>Title</h1><h2>Sub</h2>")

    def test_lists_become_lists(self):
        out = self._md("<ul><li>one</li><li>two</li></ul>")
        assert "- one" in out
        assert "- two" in out

    def test_nested_lists_keep_their_depth(self):
        out = self._md("<ul><li>one<ul><li>deep</li></ul></li></ul>")
        assert "  - deep" in out

    def test_links_keep_their_target(self):
        out = self._md('<a href="https://example.test/x">click</a>')
        assert "[click](https://example.test/x)" in out

    def test_tables_become_tables(self):
        out = self._md("<table><tr><th>Day</th><th>Temp</th></tr>"
                       "<tr><td>Wed</td><td>19</td></tr></table>")
        assert "| Day | Temp |" in out
        assert "| Wed | 19 |" in out

    def test_the_navigation_is_dropped(self):
        out = self._md('<nav><a href="/">Home</a></nav><p>Content</p>')
        assert "Home" not in out
        assert "Content" in out

    def test_hidden_panels_are_dropped(self):
        out = self._md('<div style="display:none">secret</div><p>Shown</p>')
        assert "secret" not in out
        assert "Shown" in out

    def test_a_box_of_values_becomes_a_labelled_list(self):
        out = self._md(WEATHER_BOX)
        assert "- **temperature**: 20°C" in out
        assert "- **sky state**: Clear" in out
        assert "- **aqi**: 42" in out

    def test_a_label_next_to_its_value_is_one_line(self):
        out = self._md('<div class="row"><span class="label">Search</span>'
                       '<input value="rain"></div>')
        assert "**Search**: rain" in out

    def test_a_form_value_is_kept(self):
        out = self._md('<label>Name</label><input value="Orion">')
        assert "Orion" in out


class TestFindingElements:
    def _soup(self, html):
        from bs4 import BeautifulSoup
        return BeautifulSoup(html, "html.parser")

    def test_a_class_name_written_with_spaces_finds_the_element(self):
        soup = self._soup(WEATHER_BOX)
        found = html_markdown.find_elements(soup, "weather data")

        assert len(found) == 1
        assert "temperature" in str(found[0])

    def test_an_id_finds_the_element(self):
        soup = self._soup('<div id="results"><p>hit</p></div>')
        found = html_markdown.find_elements(soup, "results")

        assert len(found) == 1
        assert found[0].get("id") == "results"

    def test_a_tag_name_finds_every_element_of_that_kind(self):
        soup = self._soup("<div><a href='/1'>one</a><a href='/2'>two</a></div>")
        found = html_markdown.find_elements(soup, "a")

        assert len(found) == 2

    def test_a_css_selector_finds_the_element(self):
        soup = self._soup('<div class="row"><p>hit</p></div>')
        found = html_markdown.find_elements(soup, "div.row")

        assert len(found) == 1

    def test_a_selector_matching_one_of_several_classes_finds_the_element(self):
        soup = self._soup('<div class="weather data">x</div>')
        found = html_markdown.find_elements(soup, "div.weather")

        assert len(found) == 1

    def test_a_query_that_matches_nothing_finds_nothing(self):
        soup = self._soup(WEATHER_BOX)
        assert html_markdown.find_elements(soup, "not-here") == []
