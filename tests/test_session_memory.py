"""The session-memory module: the draft, the saved sessions, the round trip.

This replaces the stray ``core/session_memory/test_session_memory.py`` that
was committed into the package itself.  That script imported
``workspace.session_memory`` — a path that does not exist in a fresh clone —
hard-coded this device's absolute home directory, and wrote to the **real**
``workspace/session_draft.json`` whenever it ran.  The module it exercised is
real and worth testing; a print-script parked in the shipped package is not
how to test it.

Every test here writes into ``tmp_path``: the module keeps its draft in
``paths.WORKSPACE_DIR`` and its saved sessions in ``paths.LOGS_DIR``, so both
are redirected, and the in-memory session id is reset between tests.
"""
import json
import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import paths
from core.session_memory import integration
from core.session_memory import session_memory


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Point the module at a temporary workspace and logs directory."""
    workspace = tmp_path / "workspace"
    logs = tmp_path / "logs"

    monkeypatch.setattr(paths, "WORKSPACE_DIR", str(workspace))
    monkeypatch.setattr(paths, "LOGS_DIR", str(logs))
    monkeypatch.setattr(session_memory, "DRAFT_FILE",
                        str(workspace / "session_draft.json"))
    monkeypatch.setattr(session_memory, "SESSIONS_DIR", str(logs))
    monkeypatch.setattr(session_memory, "_current_session_id", None)

    return {"workspace": workspace, "logs": logs,
            "draft": str(workspace / "session_draft.json")}


# ── The draft ────────────────────────────────────────────────────────────────

def test_a_note_lands_in_the_draft(store):
    """The draft collects the steps, the decisions and the questions."""
    session_memory.append_session_note(
        "decision", "Chose the draft", chunk_id=1,
        metadata={"reasoning": "It is cheap"},
    )
    session_memory.append_session_note(
        "action", "Created a.py", chunk_id=1,
        metadata={"file": "a.py", "tool": "write_file"},
    )
    session_memory.append_session_note("question", "Ship it?", chunk_id=2)

    draft = session_memory.get_session_draft()

    assert len(draft["steps"]) == 3
    assert draft["decisions"] == ["Chose the draft"]
    assert draft["open_questions"] == ["Ship it?"]
    assert draft["files_modified"] == ["a.py"]
    assert draft["tools_used"] == ["write_file"]
    assert draft["chunk_range"] == [1, 2]
    assert os.path.exists(store["draft"]), "the draft is written to disk"


def test_an_unknown_note_type_falls_back_to_action(store):
    """A note type the module does not know becomes an action, not a crash."""
    session_memory.append_session_note("not_a_type", "Something happened")

    draft = session_memory.get_session_draft()

    assert draft["steps"][0]["type"] == "action"


def test_the_current_session_id_matches_the_draft(store):
    """While a draft exists, the reported id is that draft's id."""
    session_memory.append_session_note("action", "Something happened", chunk_id=1)

    draft = session_memory.get_session_draft()

    assert session_memory.get_current_session_id() == draft["session_id"]


# ── Finalising ───────────────────────────────────────────────────────────────

def test_finalize_writes_the_session_and_clears_the_draft(store):
    """A finalized session is a file on disk; the draft is gone afterwards."""
    session_memory.append_session_note("decision", "Chose the draft", chunk_id=1)
    session_id = session_memory.get_session_draft()["session_id"]

    path = session_memory.finalize_session(use_small_model=False)

    assert os.path.exists(path)
    assert path.endswith(f"session_{session_id}.json")
    assert not os.path.exists(store["draft"]), "the draft is cleared"

    with open(path, encoding="utf-8") as fh:
        saved = json.load(fh)
    assert saved["session_id"] == session_id
    assert saved["decisions"] == ["Chose the draft"]
    assert "Decisions:" in saved["summary"]
    assert saved["ended"], "the session carries an end time"


def test_finalize_without_a_draft_says_so(store):
    """Finalizing an empty draft reports the problem instead of writing."""
    result = session_memory.finalize_session(use_small_model=False)

    assert result.startswith("[ERROR]")
    assert session_memory.list_sessions() == []


def test_the_saved_sessions_are_listed(store):
    """``list_sessions`` reports what was saved, with its metadata."""
    session_memory.append_session_note("action", "One step", chunk_id=1)
    session_memory.finalize_session(use_small_model=False)

    sessions = session_memory.list_sessions()

    assert len(sessions) == 1
    assert sessions[0]["step_count"] == 1
    assert sessions[0]["session_id"]
    assert sessions[0]["started"]


# ── Resuming and deleting ────────────────────────────────────────────────────

def test_a_session_can_be_resumed_by_id_and_by_filename(store):
    """Both spellings of the id find the same saved session."""
    session_memory.append_session_note("action", "One step", chunk_id=1)
    session_id = session_memory.get_session_draft()["session_id"]
    session_memory.finalize_session(use_small_model=False)

    by_id = session_memory.resume_session(session_id)
    by_name = session_memory.resume_session(f"session_{session_id}.json")

    assert "error" not in by_id
    assert by_id["session_id"] == session_id
    assert by_name["session_id"] == session_id


def test_resuming_an_unknown_session_reports_the_error(store):
    """A session that is not there comes back as an error, not a crash."""
    result = session_memory.resume_session("2026-01-01_00-00-00")

    assert "error" in result
    assert "not found" in result["error"].lower()


def test_a_session_can_be_deleted(store):
    """Deleting removes the file; deleting it again says so."""
    session_memory.append_session_note("action", "One step", chunk_id=1)
    session_id = session_memory.get_session_draft()["session_id"]
    session_memory.finalize_session(use_small_model=False)

    deleted = session_memory.delete_session(session_id)
    assert deleted.startswith("Deleted session")
    assert session_memory.list_sessions() == []

    again = session_memory.delete_session(session_id)
    assert again.startswith("[ERROR]")


# ── The integration hooks ────────────────────────────────────────────────────

def test_the_integration_helpers_record_the_events(store):
    """The convenience wrappers land in the draft as the right note types."""
    integration.note_on_chunk_open(10, "User asked about session memory")
    integration.note_on_tool_call(10, "read_file", "path: core/x.py", "10 lines")
    integration.note_on_file_edit(10, "core/x.py", "edit", "New module")
    integration.note_on_decision(10, "Use the draft", "Cheap")
    integration.note_on_error(10, "ImportError", "not in path", "Fixed bootstrap")
    integration.note_on_finding(10, "The chunk system compresses", "context_manager.py")
    integration.note_on_question(10, "Persist across crashes?")
    integration.note_on_chunk_close(10, "user message", "assistant reply", ["read_file"])

    draft = session_memory.get_session_draft()
    types = [step["type"] for step in draft["steps"]]

    assert types.count("action") == 3, types  # chunk open, tool call, chunk close
    assert "file_change" in types
    assert "decision" in types
    assert "error" in types
    assert "finding" in types
    assert "question" in types

    assert draft["decisions"] == ["Use the draft"]
    assert draft["open_questions"] == ["Persist across crashes?"]
    assert draft["files_modified"] == ["core/x.py"]
    assert draft["tools_used"] == ["read_file"]


# ── Archiving (the "/session new" path) ──────────────────────────────────────

def test_archiving_keeps_the_session_without_a_model_call(store, monkeypatch):
    """``/session new`` saves the draft — and does not wait for a sub-agent."""
    def _no_model(*args, **kwargs):
        raise AssertionError("archiving must not call a model")

    monkeypatch.setattr(session_memory, "_compress_with_small_model", _no_model)

    assert session_memory.archive_current_draft() is None, "nothing to archive"

    session_memory.append_session_note("action", "Something happened", chunk_id=1)
    session_id = session_memory.get_session_draft()["session_id"]

    path = session_memory.archive_current_draft()

    assert path and os.path.exists(path)
    assert path.endswith(f"session_{session_id}.json")
    assert not os.path.exists(store["draft"]), "the draft is cleared"
    assert len(session_memory.list_sessions()) == 1
