"""
Tests for element discovery -- how the browser tool lets a model aim a click
without guessing pixels.

The problem this solves: a model that can only click by coordinates has to
invent them, and a model that can only click by CSS selector has to invent
*that*.  Either way the first few clicks are spent guessing, and a page with
a dozen buttons turns "click Submit" into a guessing game.

The tool therefore asks the page what it actually has.  Every visible button,
link and field comes back with the label a human would read, a unique CSS
selector and its live rectangle, so a click can be aimed by name ("the button
labelled Submit") or by the index from that listing -- and the position is
resolved against the live DOM at the moment of the click.

The DevTools socket is faked, so none of these tests need a Chromium process,
a network connection or an API key.
"""
import json
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORE = os.path.join(REPO_ROOT, "core")
for _p in (REPO_ROOT, CORE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core import tools  # noqa: E402


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


def _element(name, selector, tag="button", **extra):
    """One element of a page listing."""
    element = {
        "tag": tag,
        "type": "",
        "role": "",
        "name": name,
        "selector": selector,
        "x": 500,
        "y": 380,
        "width": 80,
        "height": 40,
        "cx": 540,
        "cy": 400,
        "in_viewport": True,
    }
    element.update(extra)
    return element


def _page(elements, rect=None):
    """A Runtime.evaluate handler serving a fixed element listing.

    The listing and the live rectangle are separate on purpose: a click has
    to use the rectangle measured *now*, not the one that was listed a moment
    ago.
    """
    listing = {"elements": elements}
    live = rect if rect is not None else {
        "x": 100, "y": 200, "width": 60, "height": 40,
        "vwidth": 1024, "vheight": 681,
    }

    def evaluate(params):
        expr = params.get("expression", "")
        # The enumeration script also mentions getBoundingClientRect, so the
        # listing marker has to be checked first.
        if "querySelectorAll" in expr:
            return _string_value(json.dumps(listing))
        if "getBoundingClientRect" in expr:
            return _string_value(json.dumps(live))
        return _string_value("complete")

    return evaluate


def _clicks(ws):
    return ws.methods("Input.dispatchMouseEvent")


# ── 1. listing what the page can be clicked ──────────────────────────────────

class TestFindAction:
    def test_find_lists_the_labels_and_selectors_of_the_page(self):
        ws = FakeWS({"Runtime.evaluate": _page([
            _element("Submit", "#submit"),
            _element("Home", "nav > a:nth-of-type(1)", tag="a"),
        ])})
        ok, err, _mid, results = tools._perform_actions(
            ws, [{"type": "find", "wait_after": 0}], 1)

        assert ok, err
        listing = results[0]
        assert "Submit" in listing
        assert "#submit" in listing
        assert "Home" in listing
        assert "(540, 400)" in listing, "the listing reports where the element is"

    def test_find_can_be_narrowed_to_one_word(self):
        ws = FakeWS({"Runtime.evaluate": _page([
            _element("Submit", "#submit"),
            _element("Cancel", "#cancel"),
        ])})
        ok, err, _mid, results = tools._perform_actions(
            ws, [{"type": "find", "query": "submit", "wait_after": 0}], 1)

        assert ok, err
        listing = results[0]
        assert "Submit" in listing
        assert "Cancel" not in listing

    def test_find_says_so_when_the_page_has_nothing_to_click(self):
        ws = FakeWS({"Runtime.evaluate": _page([])})
        ok, err, _mid, results = tools._perform_actions(
            ws, [{"type": "find", "wait_after": 0}], 1)

        assert ok, err
        assert "no interactive elements" in results[0]

    def test_the_listing_tells_the_model_how_to_use_it(self):
        ws = FakeWS({"Runtime.evaluate": _page([_element("Submit", "#submit")])})
        _ok, _err, _mid, results = tools._perform_actions(
            ws, [{"type": "find", "wait_after": 0}], 1)

        assert "index" in results[0]
        assert "text" in results[0]


# ── 2. clicking by the label a human would read ─────────────────────────────

class TestClickByLabel:
    def test_a_click_by_label_hits_the_element(self):
        ws = FakeWS({"Runtime.evaluate": _page([
            _element("Submit", "#submit"),
            _element("Cancel", "#cancel"),
        ])})
        ok, err, _mid, results = tools._perform_actions(
            ws, [{"type": "click", "text": "Cancel", "wait_after": 0}], 1)

        assert ok, err
        clicks = _clicks(ws)
        assert len(clicks) == 2, "a click is a press plus a release"
        assert "#cancel" in results[0]

    def test_the_click_uses_the_live_rectangle_not_the_listed_one(self):
        # The listing says (540, 400); the element has since moved.  A click
        # that used the stale number would land on empty space.
        ws = FakeWS({"Runtime.evaluate": _page(
            [_element("Submit", "#submit")],
            rect={"x": 10, "y": 20, "width": 30, "height": 40,
                  "vwidth": 1024, "vheight": 681})})
        ok, err, _mid, _results = tools._perform_actions(
            ws, [{"type": "click", "text": "Submit", "wait_after": 0}], 1)

        assert ok, err
        assert _clicks(ws)[0]["params"]["x"] == 25
        assert _clicks(ws)[0]["params"]["y"] == 40

    def test_an_element_below_the_fold_is_scrolled_to_before_the_click(self):
        ws = FakeWS({"Runtime.evaluate": _page([
            _element("Submit", "#submit", in_viewport=False)])})
        ok, err, _mid, _results = tools._perform_actions(
            ws, [{"type": "click", "text": "Submit", "wait_after": 0}], 1)

        assert ok, err
        expressions = [m["params"]["expression"]
                       for m in ws.methods("Runtime.evaluate")]
        locate = [e for e in expressions if "getBoundingClientRect" in e
                  and "querySelectorAll" not in e]
        assert locate, "the element has to be located in the live DOM"
        assert "scrollIntoView" in locate[-1]

    def test_one_partial_match_is_good_enough(self):
        ws = FakeWS({"Runtime.evaluate": _page([
            _element("Sign in with Google", "#google"),
            _element("Cancel", "#cancel"),
        ])})
        ok, err, _mid, _results = tools._perform_actions(
            ws, [{"type": "click", "text": "sign in", "wait_after": 0}], 1)

        assert ok, err
        assert _clicks(ws), "the matching button has to be clicked"

    def test_an_ambiguous_label_is_refused_rather_than_guessed(self):
        # Two buttons called "Save".  Clicking the wrong one is worse than
        # asking once more.
        ws = FakeWS({"Runtime.evaluate": _page([
            _element("Save", "#save-top"),
            _element("Save", "#save-bottom"),
        ])})
        ok, err, _mid, results = tools._perform_actions(
            ws, [{"type": "click", "text": "Save", "wait_after": 0}], 1)

        assert not ok
        assert "several elements" in err
        assert "#save-top" in err and "#save-bottom" in err, \
            "the error has to say what is available"
        assert _clicks(ws) == [], "nothing may be clicked when the target is unclear"
        assert results == []

    def test_an_unknown_label_lists_what_is_there(self):
        ws = FakeWS({"Runtime.evaluate": _page([
            _element("Submit", "#submit"),
        ])})
        ok, err, _mid, _results = tools._perform_actions(
            ws, [{"type": "click", "text": "Purchase", "wait_after": 0}], 1)

        assert not ok
        assert "Purchase" in err
        assert "#submit" in err, "the error has to name the elements on the page"

    def test_a_label_that_matches_nothing_on_an_empty_page_says_so(self):
        ws = FakeWS({"Runtime.evaluate": _page([])})
        ok, err, _mid, _results = tools._perform_actions(
            ws, [{"type": "click", "text": "Submit", "wait_after": 0}], 1)

        assert not ok
        assert "no interactive elements" in err
        assert _clicks(ws) == []


# ── 3. clicking by the index from a listing ──────────────────────────────────

class TestClickByIndex:
    def test_the_nth_element_of_the_listing_is_clicked(self):
        ws = FakeWS({"Runtime.evaluate": _page([
            _element("One", "#one"),
            _element("Two", "#two"),
            _element("Three", "#three"),
        ])})
        ok, err, _mid, _results = tools._perform_actions(
            ws, [{"type": "click", "index": 2, "wait_after": 0}], 1)

        assert ok, err
        locate = [m["params"]["expression"]
                  for m in ws.methods("Runtime.evaluate")]
        assert any("#two" in e for e in locate), \
            "the second element of the listing has to be the one located"

    def test_an_index_out_of_range_is_refused(self):
        ws = FakeWS({"Runtime.evaluate": _page([_element("One", "#one")])})
        ok, err, _mid, _results = tools._perform_actions(
            ws, [{"type": "click", "index": 9, "wait_after": 0}], 1)

        assert not ok
        assert "out of range" in err
        assert _clicks(ws) == []

    def test_index_zero_explains_that_the_listing_is_one_based(self):
        ws = FakeWS({"Runtime.evaluate": _page([_element("One", "#one")])})
        ok, err, _mid, _results = tools._perform_actions(
            ws, [{"type": "click", "index": 0, "wait_after": 0}], 1)

        assert not ok
        assert "1-based" in err
        assert _clicks(ws) == []


# ── 4. a failed lookup has to be worth the round it costs ────────────────────

class TestRecoveryHints:
    def test_a_missing_selector_names_the_elements_on_the_page(self):
        # The model guessed a selector.  The answer has to tell it what is
        # actually there, or the next guess is just as blind as this one.
        def evaluate(params):
            expr = params.get("expression", "")
            if "querySelectorAll" in expr:
                return _string_value(json.dumps({"elements": [
                    _element("Submit", "#submit"),
                    _element("Cancel", "#cancel"),
                ]}))
            if "getBoundingClientRect" in expr:
                return _string_value("null")   # the guessed selector misses
            return _string_value("complete")

        ws = FakeWS({"Runtime.evaluate": evaluate})
        ok, err, _mid, _results = tools._perform_actions(
            ws, [{"type": "click", "selector": "#submmit", "wait_after": 0}], 1)

        assert not ok
        assert "#submmit" in err
        assert "#submit" in err, "the error has to name the elements on the page"
        assert "#cancel" in err


# ── 5. typing into a field by its label ──────────────────────────────────────

class TestInputByLabel:
    def test_a_field_can_be_typed_into_by_its_label(self):
        ws = FakeWS({"Runtime.evaluate": _page([
            _element("Email", "input[name=email]", tag="input", type="email"),
        ])})
        ok, err, _mid, results = tools._perform_actions(
            ws, [{"type": "input", "label": "Email", "text": "a@b.com",
                  "wait_after": 0}], 1)

        assert ok, err
        assert "a@b.com" in results[0]

    def test_an_unknown_field_label_is_refused(self):
        ws = FakeWS({"Runtime.evaluate": _page([
            _element("Email", "input[name=email]", tag="input"),
        ])})
        ok, err, _mid, _results = tools._perform_actions(
            ws, [{"type": "input", "label": "Phone", "text": "123",
                  "wait_after": 0}], 1)

        assert not ok
        assert "Phone" in err
        assert "Email" in err, "the error has to name the fields on the page"


# ── 6. the schema has to offer the new ways to aim a click ───────────────────

class TestSchema:
    def _action_props(self, tool_name):
        for tool in tools.TOOLS_DESCRIPTION:
            if tool.get("function", {}).get("name") == tool_name:
                return tool["function"]["parameters"]["properties"]["actions"]
        raise AssertionError(tool_name + " is not advertised")

    def test_both_browser_tools_offer_the_find_action(self):
        for name in ("browse_web", "browser"):
            actions = self._action_props(name)
            assert "find" in actions["items"]["properties"]["type"]["enum"]

    def test_both_browser_tools_offer_the_new_fields(self):
        for name in ("browse_web", "browser"):
            props = self._action_props(name)["items"]["properties"]
            for field in ("index", "query", "label", "text"):
                assert field in props, f"{name} does not offer {field}"

    def test_the_tool_description_tells_the_model_not_to_guess(self):
        for name in ("browse_web", "browser"):
            for tool in tools.TOOLS_DESCRIPTION:
                if tool.get("function", {}).get("name") == name:
                    description = tool["function"]["description"]
                    assert "guess" in description.lower()
                    assert "find" in description.lower()
                    break
