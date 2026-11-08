"""Make the repository root importable so ``import core.shell_runner`` works."""
import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


# ── the expanded / collapsed display ─────────────────────────────────────────
#
# ``display_state.is_expanded()`` re-reads ``config/config.json`` on every
# call, so an in-memory flag is overwritten by whatever the file happens to
# say — and the file is the *user's*, not the test's.  Setting the flag
# alone therefore does nothing: on a fresh clone (``setup.sh`` writes
# ``show_details: false``) the flag is clobbered the moment it is read, and
# the tests that need the expanded view silently run against the collapsed
# one.
#
# The tests have to pin the *reader*, not the flag.  That is what these two
# fixtures do.

@pytest.fixture
def display_expanded(monkeypatch):
    """Pin the tool-call display to the expanded view.

    The preference is global state backed by the user's config file, so it
    is pinned here — otherwise a test depends on what the config happens to
    contain, and on the order the suite runs.
    """
    from core import display_state

    monkeypatch.setattr(display_state, "is_expanded", lambda: True)


@pytest.fixture
def display_collapsed(monkeypatch):
    """Pin the tool-call display to the collapsed view (see above)."""
    from core import display_state

    monkeypatch.setattr(display_state, "is_expanded", lambda: False)


@pytest.fixture(autouse=True)
def _stub_plugin_runtime(monkeypatch):
    """Unit tests must never start a plugin runtime.

    Activating the WhatsApp plugin starts the real Termux-WP gateway — a
    node process with a Chromium behind it.  A test suite that does that by
    accident leaves processes behind and makes the suite depend on the
    device's WhatsApp state.  The runtime is stubbed here; a test that
    exercises it patches ``core.plugin_runtime`` itself.

    ``setup`` is stubbed for the same reason, and with more force: the
    WhatsApp pairing path *kills* the gateway when the session is already
    linked.  A unit test that reached the real gateway took the device's
    running WhatsApp session down with it.
    """
    from core import plugin_runtime

    stub = {
        "ok": True,
        "state": "stubbed",
        "message": "runtime stubbed during tests",
    }
    monkeypatch.setattr(plugin_runtime, "activate", lambda name: dict(stub))
    monkeypatch.setattr(plugin_runtime, "deactivate", lambda name: dict(stub))
    monkeypatch.setattr(plugin_runtime, "status", lambda name: dict(stub))
    monkeypatch.setattr(plugin_runtime, "setup", lambda name: dict(stub))


@pytest.fixture(autouse=True)
def _fast_browser_settle(monkeypatch):
    """Keep the browser tests quick.

    Before a page is read, the browser tools wait for it to stop changing:
    a search engine fires its load event while the results are still on
    their way, so "the page has loaded" and "the page is ready" are two
    different moments.  The wait is a moment long in the tests; the tests
    that are about the wait itself set their own numbers.
    """
    from core import tools

    monkeypatch.setattr(tools, "_BROWSER_CONTENT_SETTLE", 0.05)
    monkeypatch.setattr(tools, "_BROWSER_CONTENT_POLL", 0.01)
