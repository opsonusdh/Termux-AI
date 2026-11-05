"""Tests for the ``/session`` command family.

Two properties are pinned here:

* ``/session list`` is *readable* — a heading and one aligned block per
  session, not a dump of Python dicts.
* ``/session new`` wipes the **conversation** and nothing else.  The mode
  (agent or chat) survives it, the preferences survive it, the startup
  chrome is not reprinted, and the chunks that were closed stay retrievable
  instead of being deleted.
"""
import inspect
import json
import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
if os.path.join(REPO_ROOT, "core") not in sys.path:
    sys.path.insert(0, os.path.join(REPO_ROOT, "core"))

import paths
from core import runtime_log


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def chunk_store(tmp_path, monkeypatch):
    """A private chunk store, with the live one restored afterwards.

    The real store *is* the conversation history.  A test that reset it
    would throw away the thing the runtime exists to keep.
    """
    from core import context_manager as cm

    monkeypatch.setattr(cm, "CHUNKS_FILE", str(tmp_path / "chunks.jsonl"))
    monkeypatch.setattr(cm, "SUMMARIES_FILE", str(tmp_path / "chunk_summaries.json"))
    monkeypatch.setattr(cm, "CONTEXT_SESSION_FILE",
                        str(tmp_path / "context_session.json"))
    # The session-memory hooks write the real draft — keep them out of it.
    monkeypatch.setattr(cm, "note_on_chunk_open", lambda *a, **k: None)
    monkeypatch.setattr(cm, "note_on_chunk_close", lambda *a, **k: None)

    saved = (list(cm._parent_ids), dict(cm._chunk_index), dict(cm._summaries),
             cm._next_id, cm._active_from, cm._current_chunk,
             list(cm._pending_context))
    cm._parent_ids = []
    cm._chunk_index = {}
    cm._summaries = {}
    cm._next_id = 1
    cm._active_from = 1
    cm._current_chunk = None
    cm._pending_context = []
    cm._load()

    yield cm

    (cm._parent_ids, cm._chunk_index, cm._summaries, cm._next_id,
     cm._active_from, cm._current_chunk, cm._pending_context) = saved


@pytest.fixture
def quiet_log(tmp_path, monkeypatch):
    """Keep the test out of the live session log."""
    monkeypatch.setattr(paths, "LOG_FILE", str(tmp_path / "log.txt"))
    runtime_log.reset_state()
    yield
    runtime_log.reset_state()


@pytest.fixture
def no_draft_archive(monkeypatch):
    """Never touch the user's real session draft."""
    from core import interface
    monkeypatch.setattr(interface, "archive_current_draft", lambda: None)


@pytest.fixture
def restore_interface_config():
    """The chat loop reloads ``interface.config`` from disk — put it back."""
    from core import interface
    saved = interface.config
    yield
    interface.config = saved


def _write_chunk(cm, cid, user_text, reply_text):
    """Write one chunk into the (temporary) store."""
    entry = {
        "id": cid,
        "ts": "2026-11-03T10:00:00",
        "messages": [
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": reply_text},
        ],
    }
    with open(cm.CHUNKS_FILE, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


# ── /session list ────────────────────────────────────────────────────────────

SAMPLE_SESSIONS = [
    {
        "session_id": "2026-11-03_14-32-07",
        "file_path": "/logs/session_2026-11-03_14-32-07.json",
        "started": "2026-11-03T14:32:07.123456",
        "ended": "2026-11-03T15:10:00.000000",
        "summary": "Key outcomes: the ask_user prompt now says what it accepts",
        "step_count": 12,
        "token_estimate": 1234,
    },
]


def test_session_list_is_rendered_not_dumped(capsys, monkeypatch):
    from core import interface

    monkeypatch.setattr(interface, "list_sessions", lambda: SAMPLE_SESSIONS)
    interface._handle_session_command("/session list")

    out = capsys.readouterr().out
    assert "Sessions" in out
    assert "2026-11-03_14-32-07" in out
    assert "the ask_user prompt now says what it accepts" in out
    assert "14:32:07" in out
    assert "12" in out
    # The old output was the raw Python list.
    assert "[{" not in out
    assert "'session_id'" not in out


def test_session_list_says_when_there_is_nothing(capsys, monkeypatch):
    from core import interface

    monkeypatch.setattr(interface, "list_sessions", lambda: [])
    interface._handle_session_command("/session list")

    out = capsys.readouterr().out
    assert "Sessions" in out
    assert "none saved yet" in out
    assert "[]" not in out


def test_session_list_survives_a_broken_entry(capsys, monkeypatch):
    """One unreadable record must not take the whole listing down."""
    from core import interface

    monkeypatch.setattr(
        interface, "list_sessions",
        lambda: [SAMPLE_SESSIONS[0], "not a dict"],
    )
    interface._handle_session_command("/session list")

    out = capsys.readouterr().out
    assert "2026-11-03_14-32-07" in out


# ── /session new ─────────────────────────────────────────────────────────────

def test_reset_clears_both_histories_and_the_chunk_context(chunk_store):
    from core import interface

    _write_chunk(chunk_store, 1, "old question", "old answer")
    chunk_store._load()

    history = [{"role": "user", "content": "a"},
               {"role": "assistant", "content": "b"}]
    agent_history = [{"role": "user", "content": "c"}]

    report = interface._reset_conversation_context(history, agent_history)

    assert history == []
    assert agent_history == []
    assert report["history"] == 2
    assert report["agent_history"] == 1
    assert report["dropped_chunks"] == 1
    assert chunk_store.build_history() == []


def test_the_reset_cannot_touch_the_mode():
    """The mode is not an input to the reset, so it cannot be reset.

    ``agent_mode`` lives in the chat loop and is deliberately absent from
    the reset's signature: "/session new" has no way to reach it.
    """
    from core import interface

    params = list(
        inspect.signature(interface._reset_conversation_context).parameters
    )
    assert params == ["history", "agent_history"]


def test_session_new_reports_itself(capsys, chunk_store, no_draft_archive,
                                    quiet_log):
    from core import interface

    interface._handle_session_command("/session new",
                                      on_new=lambda: {"dropped_chunks": 3})

    out = capsys.readouterr().out
    assert "New session" in out
    assert "3 earlier chunk" in out
    assert "Mode, preferences and plugins are unchanged." in out


def test_session_new_does_not_reprint_the_startup_ui(capsys, chunk_store,
                                                      no_draft_archive, quiet_log):
    """A context wipe is not a restart: no banner, no status row, no hints."""
    from core import interface

    interface._handle_session_command("/session new", on_new=lambda: {})

    out = capsys.readouterr().out
    assert "New session" in out
    for startup in ("Termux-AI", "Terminal AI ready", "Enter = send",
                    "Ctrl+O", "mode "):
        assert startup not in out


def test_session_new_leaves_the_preferences_alone(capsys, chunk_store,
                                                  no_draft_archive, quiet_log):
    from core import interface

    before = dict(interface.config)
    interface._handle_session_command("/session new", on_new=lambda: {})

    assert interface.config == before


def test_session_new_archives_the_draft_instead_of_dropping_it(capsys,
                                                               chunk_store,
                                                               quiet_log,
                                                               monkeypatch):
    """The draft describes the conversation being closed — it is kept."""
    from core import interface

    archived = []
    monkeypatch.setattr(
        interface, "archive_current_draft",
        lambda: archived.append("/logs/session_x.json") or "/logs/session_x.json",
    )

    interface._handle_session_command("/session new", on_new=lambda: {})

    out = capsys.readouterr().out
    assert archived == ["/logs/session_x.json"]
    assert "session_x.json" in out


def test_session_new_still_works_when_the_draft_cannot_be_archived(capsys,
                                                                   chunk_store,
                                                                   quiet_log,
                                                                   monkeypatch):
    from core import interface

    def broken():
        raise OSError("disk is full")

    monkeypatch.setattr(interface, "archive_current_draft", broken)

    interface._handle_session_command("/session new", on_new=lambda: {})

    out = capsys.readouterr().out
    assert "New session" in out
    assert "could not be archived" in out


# ── The context boundary in the chunk store ──────────────────────────────────

def test_reset_context_clears_the_active_context(chunk_store):
    cm = chunk_store
    _write_chunk(cm, 1, "hello", "hi")
    cm._load()
    assert len(cm.build_history()) == 2

    report = cm.reset_context()

    assert report["dropped_chunks"] == 1
    assert cm.build_history() == []


def test_the_closed_chunks_stay_retrievable(chunk_store):
    """Wiping the context must not delete the conversation — it only stops
    being pushed into every request."""
    cm = chunk_store
    _write_chunk(cm, 1, "the secret plan", "here it is")
    cm._load()

    cm.reset_context()

    found = json.loads(cm.retrieve_chunk(1))
    assert found["id"] == 1
    assert "the secret plan" in json.dumps(found, ensure_ascii=False)


def test_the_boundary_survives_a_restart(chunk_store):
    """A restart must not bring the closed conversation back."""
    cm = chunk_store
    _write_chunk(cm, 1, "hello", "hi")
    cm._load()

    cm.reset_context()

    # A restart: the store is read from disk again.
    cm._load()
    assert cm.build_history() == []
    assert cm._active_from == 2


def test_a_chunk_written_after_the_reset_is_active_again(chunk_store):
    cm = chunk_store
    _write_chunk(cm, 1, "old", "reply")
    cm._load()

    cm.reset_context()
    cm.open_chunk("new question")
    cm.close_chunk("new answer")

    history = cm.build_history()
    assert any(m.get("content") == "new question" for m in history)
    assert all(m.get("content") != "old" for m in history)


def test_a_boundary_past_the_end_of_the_store_is_clamped(chunk_store):
    """A chunk file that was rotated away must not leave the boundary
    pointing past the end of the store."""
    cm = chunk_store
    cm._load()

    cm._persist_boundary(999)
    cm._load()

    assert cm._active_from == cm._next_id
    cm.open_chunk("still works")
    cm.close_chunk("yes")
    assert any(m.get("content") == "still works" for m in cm.build_history())


# ── The real chat loop ───────────────────────────────────────────────────────

def test_session_new_keeps_the_agent_mode_and_clears_the_agent_history(
        capsys, chunk_store, no_draft_archive, quiet_log, tmp_path,
        monkeypatch, restore_interface_config):
    """Enter agent mode, wipe the context, and the next message still goes to
    the agent — with an empty history."""
    from core import interface

    # A config of our own: the real one may have the voice input switched on,
    # and a test must never listen to the microphone.
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({
        "tts_enabled": False,
        "autonomous": False,
        "notify": False,
        "show_details": False,
        "stt_path": "",
    }), encoding="utf-8")
    monkeypatch.setattr(interface, "CONFIG_PATH", str(config_file))
    monkeypatch.setattr(interface, "HAS_STT", False)
    monkeypatch.setattr(interface, "_start_whatsapp_runtime", lambda: False)
    monkeypatch.setattr(interface, "_notify_plugin_setups", lambda: None)

    # The chat loop asks for input until it gets EOF.
    answers = iter(["/agent", "/session new", "what were we doing?"])

    def fake_input(_prompt):
        try:
            return next(answers)
        except StopIteration:
            raise EOFError

    monkeypatch.setattr(interface, "get_interactive_input", fake_input)

    agent_calls = []

    def fake_ask_agent(prompt, history=None, voice=False):
        agent_calls.append({"prompt": prompt, "history": list(history or [])})
        return "A fresh start."

    monkeypatch.setattr(interface, "ask_agent", fake_ask_agent)

    normal_calls = []
    monkeypatch.setattr(
        interface, "ask_ai",
        lambda *a, **k: normal_calls.append(a) or "should not be called",
    )

    interface.chat_loop()

    out = capsys.readouterr().out
    assert "New session" in out

    # The mode survived the wipe: the message after it went to the agent.
    assert len(agent_calls) == 1
    assert agent_calls[0]["prompt"] == "what were we doing?"
    # ... and the agent's history was wiped.
    assert agent_calls[0]["history"] == []
    # The normal chat path did not take over.
    assert normal_calls == []
