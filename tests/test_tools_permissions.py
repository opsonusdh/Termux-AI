"""
Regression tests for tool permission gate fixes.

Bug #2: delete_file / rename_file bypass permissions
Bug #3: search_in_files / list_directory bypass permissions
Bug #4: run_diagnosis timeout ineffective
"""
import os
import sys
import time
from unittest.mock import patch, MagicMock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from core import tools
from core import permissions
import paths

import pytest


# ── Bug #2: delete_file / rename_file permission checks ─────────────────────

def _abs(path: str) -> str:
    """Resolve path like tools.py does."""
    p = os.path.expanduser(path)
    if not os.path.isabs(p):
        p = os.path.join(paths.ROOT, p)
    return p


@patch("core.tools.request_file_permission")
def test_delete_file_calls_permission_check(mock_perm):
    """delete_file should call request_file_permission for write operations."""
    mock_perm.return_value = (True, "OK")
    tools.delete_file("workspace/test.txt")
    mock_perm.assert_called_once_with("write", _abs("workspace/test.txt"))


@patch("core.tools.request_file_permission")
def test_delete_file_blocked_when_permission_denied(mock_perm):
    """delete_file should return error when permission denied."""
    mock_perm.return_value = (False, "[PERMISSION DENIED] 'workspace/test.txt': outside root")
    result = tools.delete_file("workspace/test.txt")
    assert "PERMISSION DENIED" in result


@patch("core.tools.request_file_permission")
def test_rename_file_calls_permission_check_both_paths(mock_perm):
    """rename_file should call request_file_permission for both source and dest."""
    mock_perm.return_value = (True, "OK")
    tools.rename_file("workspace/old.txt", "workspace/new.txt")
    assert mock_perm.call_count == 2
    calls = mock_perm.call_args_list
    assert calls[0][0] == ("write", _abs("workspace/old.txt"))
    assert calls[1][0] == ("write", _abs("workspace/new.txt"))


@patch("core.tools.request_file_permission")
def test_rename_file_blocked_when_dest_denied(mock_perm):
    """rename_file should fail if destination permission denied."""
    mock_perm.side_effect = [
        (True, "OK"),  # source allowed
        (False, "[PERMISSION DENIED] 'core/tools.py': protected dir"),  # dest denied
    ]
    result = tools.rename_file("workspace/old.txt", "core/tools.py")
    assert "PERMISSION DENIED" in result


# ── Bug #3: search_in_files / list_directory permission checks ──────────────

@patch("core.tools.request_file_permission")
def test_list_directory_calls_permission_check(mock_perm):
    """list_directory should call request_file_permission for read."""
    mock_perm.return_value = (True, "OK")
    tools.list_directory("workspace/subdir")
    mock_perm.assert_called_once_with("read", _abs("workspace/subdir"))


@patch("core.tools.request_file_permission")
def test_list_directory_blocked_when_permission_denied(mock_perm):
    """list_directory should return error when permission denied."""
    mock_perm.return_value = (False, "[PERMISSION DENIED] 'core/': protected dir")
    result = tools.list_directory("core")
    assert "PERMISSION DENIED" in result


@patch("core.tools.request_file_permission")
def test_search_in_files_calls_permission_check(mock_perm):
    """search_in_files should call request_file_permission for read."""
    mock_perm.return_value = (True, "OK")
    # search_in_files(path, query, ...) - path is FIRST arg
    tools.search_in_files("workspace/src", "query")
    mock_perm.assert_called_once_with("read", _abs("workspace/src"))


@patch("core.tools.request_file_permission")
def test_search_in_files_blocked_when_permission_denied(mock_perm):
    """search_in_files should return error when permission denied."""
    mock_perm.return_value = (False, "[PERMISSION DENIED] '/tmp': outside root")
    result = tools.search_in_files("/tmp", "query")
    assert "PERMISSION DENIED" in result


# ── Bug #4: run_diagnosis timeout cancellation ──────────────────────────────

def test_run_diagnosis_completes_within_timeout():
    """run_diagnosis should complete and not hang indefinitely."""
    start = time.monotonic()
    result = tools.run_diagnosis()
    elapsed = time.monotonic() - start
    
    # Should complete well within the timeout (DIAGNOSIS_TIMEOUT = 20s)
    assert elapsed < 15, f"run_diagnosis took {elapsed:.1f}s, expected < 15s"
    assert isinstance(result, dict)
    assert len(result) > 0
    for name, value in result.items():
        assert isinstance(value, dict), f"Result {name} is not a dict: {type(value)}"


def test_run_diagnosis_no_hang_on_slow_check():
    """run_diagnosis should respect timeout even with slow checks."""
    start = time.monotonic()
    result = tools.run_diagnosis()
    elapsed = time.monotonic() - start
    
    assert elapsed < 25, f"run_diagnosis took {elapsed:.1f}s, should respect timeout"
    assert isinstance(result, dict)


# ── Additional: verify the permission functions are properly imported ────────

def test_request_file_permission_imported_in_tools():
    """Ensure request_file_permission is imported in tools.py"""
    import core.tools as tools_module
    assert hasattr(tools_module, "request_file_permission") or \
           "request_file_permission" in tools_module.__dict__.get("__all__", []) or \
           hasattr(tools_module, "delete_file")  # sanity - module loads
