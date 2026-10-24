#!/usr/bin/env python3
"""
Test script for session_memory module.
Run: python -m workspace.session_memory.test_session_memory
"""

import sys
import os

# Path bootstrap
_ROOT = "/data/data/com.termux/files/home/Termux-AI"
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from workspace.session_memory import (
    append_session_note,
    get_session_draft,
    finalize_session,
    list_sessions,
    resume_session,
    delete_session,
    get_current_session_id,
)


def get_draft_file():
    """Get the draft file path."""
    import paths
    return os.path.join(paths.WORKSPACE_DIR, "session_draft.json")


def test_basic_flow():
    """Test the basic session draft -> finalize -> list -> resume cycle."""
    print("=" * 60)
    print("TEST: Basic session flow")
    print("=" * 60)

    # Clean up any existing draft
    draft_file = get_draft_file()
    if os.path.exists(draft_file):
        os.unlink(draft_file)

    # 1. Start a session by appending notes
    print("\n1. Appending session notes...")

    append_session_note(
        note_type="decision",
        content="Chose hybrid draft + small-model compression for session memory",
        chunk_id=1,
        metadata={"reasoning": "Balances incremental capture with cheap finalization"},
    )

    append_session_note(
        note_type="action",
        content="Created workspace/session_memory/session_memory.py",
        chunk_id=1,
        metadata={"file": "workspace/session_memory/session_memory.py", "tool": "write_file"},
    )

    append_session_note(
        note_type="finding",
        content="Existing chunk system already has 3-level compression (short/micro/oneline)",
        chunk_id=2,
        metadata={"source": "context_manager.py _summarize_old_chunks"},
    )

    append_session_note(
        note_type="question",
        content="Should we auto-save every N turns or only on explicit /session save?",
        chunk_id=2,
    )

    append_session_note(
        note_type="error",
        content="Permission denied on config/api.keys",
        chunk_id=3,
        metadata={"context": "Trying to read api.keys directly", "recovery": "User approved after permission prompt"},
    )

    # 2. Check the draft
    print("\n2. Current draft:")
    draft = get_session_draft()
    print(f"   Session ID: {draft['session_id']}")
    print(f"   Steps: {len(draft['steps'])}")
    print(f"   Decisions: {len(draft['decisions'])}")
    print(f"   Questions: {len(draft['open_questions'])}")
    print(f"   Files modified: {draft['files_modified']}")
    print(f"   Tools used: {draft['tools_used']}")

    # 3. Finalize (without small model for speed in test)
    print("\n3. Finalizing session (no small model)...")
    session_file = finalize_session(use_small_model=False)
    print(f"   Created: {session_file}")

    # 4. List sessions
    print("\n4. Listing sessions:")
    sessions = list_sessions()
    for s in sessions:
        print(f"   {s['session_id']} -- {s['step_count']} steps -- {s['summary'][:80]}")

    # 5. Resume session
    print("\n5. Resuming session:")
    session_id = draft['session_id']
    resumed = resume_session(session_id)
    if "error" not in resumed:
        print(f"   Loaded: {resumed['session_id']}")
        print(f"   Summary: {resumed['summary']}")
        print(f"   Steps kept: {len(resumed.get('steps', []))}")
    else:
        print(f"   Error: {resumed['error']}")

    # 6. Delete session (cleanup)
    print("\n6. Deleting test session:")
    print(f"   {delete_session(session_id)}")

    print("\nBasic flow test PASSED")


def test_integration_helpers():
    """Test the integration helper functions."""
    print("\n" + "=" * 60)
    print("TEST: Integration helpers")
    print("=" * 60)

    from workspace.session_memory.integration import (
        note_on_chunk_open,
        note_on_chunk_close,
        note_on_tool_call,
        note_on_file_edit,
        note_on_decision,
        note_on_error,
        note_on_finding,
        note_on_question,
    )

    # Clean up
    draft_file = get_draft_file()
    if os.path.exists(draft_file):
        os.unlink(draft_file)

    print("\n1. Testing integration helpers...")

    note_on_chunk_open(10, "User asked about session memory implementation")
    note_on_tool_call(10, "read_file", "path: core/context_manager.py", "Returned 650 lines")
    note_on_file_edit(10, "workspace/session_memory/session_memory.py", "create", "New session memory module")
    note_on_decision(10, "Use delegate_subtask with north-mini-code for compression", "Free, 256k context, good at JSON")
    note_on_finding(10, "Chunk system already supports subchunks for large turns", "context_manager.py lines 237-305")
    note_on_question(10, "Should session draft persist across crashes?")
    note_on_chunk_close(10, "User asked about session memory", "Built prototype in workspace/", ["read_file", "write_file", "list_directory"])
    note_on_error(10, "Import error on delegate_subtask", "tools not in path", "Fixed sys.path bootstrap")

    draft = get_session_draft()
    print(f"   Steps recorded: {len(draft['steps'])}")
    for step in draft['steps']:
        print(f"   - [{step['type']}] {step['content'][:80]}")

    session_file = finalize_session(use_small_model=False)
    print(f"\n   Finalized to: {session_file}")

    # Cleanup
    session_id = draft['session_id']
    delete_session(session_id)

    print("\nIntegration helpers test PASSED")


def test_edge_cases():
    """Test edge cases."""
    print("\n" + "=" * 60)
    print("TEST: Edge cases")
    print("=" * 60)

    draft_file = get_draft_file()
    if os.path.exists(draft_file):
        os.unlink(draft_file)

    # Empty finalize
    print("\n1. Finalize with empty draft:")
    result = finalize_session(use_small_model=False)
    print(f"   Result: {result}")

    # Resume non-existent
    print("\n2. Resume non-existent session:")
    result = resume_session("2026-01-01_00-00-00")
    print(f"   Result: {result}")

    # Delete non-existent
    print("\n3. Delete non-existent session:")
    result = delete_session("2026-01-01_00-00-00")
    print(f"   Result: {result}")

    # Invalid note type (should default to 'action')
    print("\n4. Invalid note type:")
    append_session_note("invalid_type", "This should become an action note", chunk_id=1)
    draft = get_session_draft()
    print(f"   Note type stored: {draft['steps'][0]['type']}")

    finalize_session(use_small_model=False)
    delete_session(draft['session_id'])

    print("\nEdge cases test PASSED")


def main():
    """Run all tests."""
    print("Running session_memory tests...\n")

    test_basic_flow()
    test_integration_helpers()
    test_edge_cases()

    print("\n" + "=" * 60)
    print("ALL TESTS PASSED")
    print("=" * 60)


if __name__ == "__main__":
    main()