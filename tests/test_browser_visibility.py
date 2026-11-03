"""
Tests for what the browser tool shows -- and, more importantly, for what it
hides.

Two bugs are covered here, both of which made the tool lie about a page:

  1. The no-JS fallback scraper returned text that a visitor would never
     see.  A page keeps every translation, every closed dialog and every
     collapsed menu in its markup, because the script that hides them never
     ran.  The result was "all messages of all languages at once" for a page
     that shows one language.

  2. Clicking an element that sat below the fold reported a click that
     never happened.  Its rectangle lies outside the viewport, so the mouse
     event landed on empty space while the tool cheerfully said it had
     clicked the button.

The DevTools socket is faked, so the interaction tests need no Chromium.
The fallback tests use a tiny local HTTP server.
"""
import http.server
import json
import os
import socketserver
import sys
import threading

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORE = os.path.join(REPO_ROOT, "core")
for _p in (REPO_ROOT, CORE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core import tools  # noqa: E402


# ── a fake DevTools socket ───────────────────────────────────────────────────

def _string_value(text):
    return {"result": {"result": {"type": "string", "value": text}}}


class FakeWS:
    """Records every command and answers with a configured payload."""

    def __init__(self, handlers=None, default=None):
        self.sent = []
        self._outbox = []
        self.handlers = handlers or {}
        self.default = default if default is not None else _string_value("complete")

    def send(self, raw):
        msg = json.loads(raw)
        self.sent.append(msg)
        handler = self.handlers.get(msg.get("method"))
        if handler is None:
            payload = self.default
        elif callable(handler):
            payload = handler(msg.get("params", {}))
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


def _rect_handler(rect):
    """A Runtime.evaluate handler that answers with a bounding rectangle."""
    def evaluate(params):
        expr = params.get("expression", "")
        if "getBoundingClientRect" in expr:
            return _string_value(json.dumps(rect))
        return _string_value("complete")
    return evaluate


# ── 1. the fallback must not report hidden content ──────────────────────────

class TestFallbackVisibility:
    def _text(self, html):
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        tools._strip_hidden_content(soup)
        return soup.get_text(separator="\n", strip=True)

    def test_css_display_none_is_hidden(self):
        html = "<style>.hide{display:none}</style><p class='hide'>FRANCAIS</p><p>English</p>"
        text = self._text(html)
        assert "FRANCAIS" not in text
        assert "English" in text

    def test_the_hidden_attribute_is_hidden(self):
        html = "<p hidden>DEUTSCH</p><p>English</p>"
        text = self._text(html)
        assert "DEUTSCH" not in text
        assert "English" in text

    def test_visibility_hidden_is_hidden(self):
        html = "<style>.x{visibility:hidden}</style><p class='x'>ITALIANO</p><p>English</p>"
        text = self._text(html)
        assert "ITALIANO" not in text
        assert "English" in text

    def test_inline_display_none_is_hidden(self):
        html = "<p style='display:none'>ESPANOL</p><p>English</p>"
        text = self._text(html)
        assert "ESPANOL" not in text
        assert "English" in text

    def test_noscript_content_is_hidden(self):
        # With scripting on, a browser never renders <noscript>.
        html = "<noscript>NOSCRIPT text</noscript><p>English</p>"
        text = self._text(html)
        assert "NOSCRIPT" not in text
        assert "English" in text

    def test_visible_text_survives(self):
        html = "<h1>Heading</h1><p>Body text</p>"
        text = self._text(html)
        assert "Heading" in text
        assert "Body text" in text

    def test_visible_aria_hidden_text_is_kept(self):
        # aria-hidden does not make text invisible -- it only takes it out of
        # the accessibility tree.  The live browser path shows it, so the
        # fallback has to show it too, or the two disagree.
        html = "<p aria-hidden='true'>ESPANOL</p><p>English</p>"
        text = self._text(html)
        assert "ESPANOL" in text

    def test_the_page_title_is_kept(self):
        html = "<title>Page title</title><p>English</p>"
        text = self._text(html)
        assert "Page title" in text

    def test_a_hidden_element_is_removed_whole(self):
        # The whole subtree goes, not just the tag -- otherwise the text
        # inside a hidden dialog would leak out anyway.
        html = ("<div style='display:none'><p>Hidden paragraph</p>"
                "<span>Hidden span</span></div><p>English</p>")
        text = self._text(html)
        assert "Hidden paragraph" not in text
        assert "Hidden span" not in text

    def test_a_media_query_hiding_rule_is_honoured(self):
        html = ("<style>@media (max-width:600px){.small{display:none}}</style>"
                "<p class='small'>TINY</p><p>English</p>")
        text = self._text(html)
        assert "TINY" not in text

    def test_a_selector_that_cannot_be_parsed_is_skipped(self):
        # A broken selector must not take the whole scrape down.
        html = "<style>.bad{</style><p>English</p>"
        text = self._text(html)
        assert "English" in text


class TestFallbackOverHttp:
    """The same rules, applied through the real fallback entry point."""

    @pytest.fixture
    def server(self):
        pages = {
            "/page.html": (
                b"<html><head><title>T</title>"
                b"<link rel='stylesheet' href='/style.css'></head><body>"
                b"<p class='translated'>FRANCAIS</p>"
                b"<p class='plain'>English</p></body></html>"),
            "/style.css": b".translated{display:none}",
        }

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                body = pages.get(self.path)
                if body is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                ctype = "text/css" if self.path.endswith(".css") else "text/html"
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        httpd = socketserver.TCPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        yield "http://127.0.0.1:%d" % httpd.server_address[1]
        httpd.shutdown()
        httpd.server_close()

    def test_text_hidden_by_a_linked_stylesheet_is_not_reported(self, server):
        # The class that hides the translation lives in the external
        # stylesheet, so the fallback has to fetch it to know.
        out = tools._web_scrape_fallback(server + "/page.html", None, 4000, None)
        assert "FRANCAIS" not in out
        assert "English" in out


# ── 2. clicking has to reach the element ─────────────────────────────────────

class TestElementLocation:
    def test_the_element_is_scrolled_into_view_before_it_is_measured(self):
        ws = FakeWS({"Runtime.evaluate": _rect_handler(
            {"x": 100, "y": 481, "width": 200, "height": 60})})
        tools._locate_element(ws, "#target", 1)
        expression = ws.methods("Runtime.evaluate")[0]["params"]["expression"]
        assert "scrollIntoView" in expression, \
            "an element below the fold has to be scrolled to before measuring"

    def test_the_scrolled_rectangle_is_reported(self):
        ws = FakeWS({"Runtime.evaluate": _rect_handler(
            {"x": 100, "y": 481, "width": 200, "height": 60,
             "vwidth": 1024, "vheight": 681})})
        info = tools._locate_element(ws, "#target", 1)
        assert info["found"] is True
        assert info["hidden"] is False
        assert info["x"] == 100
        assert info["y"] == 481
        assert info["vwidth"] == 1024

    def test_a_hidden_element_is_reported_as_hidden(self):
        ws = FakeWS({"Runtime.evaluate": _rect_handler(
            {"found": True, "hidden": True})})
        info = tools._locate_element(ws, "#gone", 1)
        assert info["found"] is True
        assert info["hidden"] is True

    def test_a_missing_element_is_reported_as_missing(self):
        ws = FakeWS({"Runtime.evaluate": _rect_handler(None)})
        info = tools._locate_element(ws, "#nope", 1)
        assert info["found"] is False

    def test_a_selector_with_a_quote_does_not_break_the_script(self):
        ws = FakeWS({"Runtime.evaluate": _rect_handler(
            {"x": 1, "y": 2, "width": 3, "height": 4})})
        tools._locate_element(ws, "a[title=\"it's\"]", 1)
        expression = ws.methods("Runtime.evaluate")[0]["params"]["expression"]
        # The selector has to survive as one valid JS string literal.
        assert "querySelector(" in expression
        assert "\\'" not in expression


class TestHonestClicks:
    def test_a_click_on_a_hidden_element_is_refused(self):
        # Before the fix this clicked (0, 0) -- the top-left corner of the
        # page -- because a hidden element has a zero-sized rectangle.
        ws = FakeWS({"Runtime.evaluate": _rect_handler(
            {"found": True, "hidden": True})})
        ok, err, _mid, results = tools._perform_actions(
            ws, [{"type": "click", "selector": "#gone", "wait_after": 0}], 1)

        assert not ok
        assert "not visible" in err
        assert ws.methods("Input.dispatchMouseEvent") == [], \
            "no mouse event may be sent for an element that is not there"
        assert results == []

    def test_a_click_outside_the_viewport_is_refused(self):
        ws = FakeWS({"Runtime.evaluate": _rect_handler(
            {"x": 100, "y": 3800, "width": 200, "height": 60,
             "vwidth": 1024, "vheight": 681})})
        ok, err, _mid, _results = tools._perform_actions(
            ws, [{"type": "click", "selector": "#far", "wait_after": 0}], 1)

        assert not ok
        assert "viewport" in err
        assert ws.methods("Input.dispatchMouseEvent") == []

    def test_a_click_inside_the_viewport_reaches_the_element(self):
        ws = FakeWS({"Runtime.evaluate": _rect_handler(
            {"x": 100, "y": 481, "width": 200, "height": 60,
             "vwidth": 1024, "vheight": 681})})
        ok, err, _mid, results = tools._perform_actions(
            ws, [{"type": "click", "selector": "#target", "wait_after": 0}], 1)

        assert ok, err
        clicks = ws.methods("Input.dispatchMouseEvent")
        assert clicks[0]["params"]["x"] == 200
        assert clicks[0]["params"]["y"] == 511
        assert "clicked at (200, 511)" in results[0]

    def test_a_click_on_a_missing_element_still_names_the_selector(self):
        ws = FakeWS({"Runtime.evaluate": _rect_handler(None)})
        ok, err, _mid, _results = tools._perform_actions(
            ws, [{"type": "click", "selector": "#nope", "wait_after": 0}], 1)

        assert not ok
        assert "#nope" in err

    def test_inputting_into_a_hidden_field_is_refused(self):
        ws = FakeWS({"Runtime.evaluate": _rect_handler(
            {"found": True, "hidden": True})})
        ok, err, _mid, _results = tools._perform_actions(
            ws, [{"type": "input", "selector": "#gone", "text": "x",
                  "wait_after": 0}], 1)

        assert not ok
        assert "not visible" in err
        assert ws.methods("Input.insertText") == []

    def test_an_explicit_coordinate_is_not_second_guessed(self):
        # x/y is an explicit instruction, so the viewport rules do not apply
        # to it -- the caller knows where it is pointing.
        ws = FakeWS()
        ok, err, _mid, _results = tools._perform_actions(
            ws, [{"type": "click", "x": 900, "y": 900, "wait_after": 0}], 1)
        assert ok, err
        assert ws.methods("Input.dispatchMouseEvent")[0]["params"]["x"] == 900
