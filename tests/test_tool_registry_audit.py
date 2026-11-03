"""The tool registry has to stay honest.

Three invariants, all of them the kind that break *silently* — the tool is
advertised to the model, the model calls it, and the failure only shows up
as a failed turn:

1. every advertised schema has a dispatch route,
2. every dispatch route resolves to something callable — including the routes
   that reach through a module (``state_manager.initialize_project``),
3. the set of tools a delegated worker may run is exactly the advertised set
   minus the documented exclusions.

These used to live in throwaway scripts under ``workspace/audit/``.  They
reported false alarms (a route that reached through a module looked
unresolved; the deliberate worker exclusions looked like gaps), which is
worse than no audit at all: a check that always cries wolf is a check
nobody reads.  The invariants now live in the shipped suite.
"""
import ast
import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
if os.path.join(REPO_ROOT, "core") not in sys.path:
    sys.path.insert(0, os.path.join(REPO_ROOT, "core"))

from core import llm_client, tools as tools_mod  # noqa: E402

LLM_PATH = os.path.join(REPO_ROOT, "core", "llm_client.py")


# ── Static reading of the dispatch table ─────────────────────────────────────

def _route_nodes() -> dict:
    """{tool_name: ast node of the route} from ``_dispatch_tool``'s table."""
    with open(LLM_PATH, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=LLM_PATH)

    routes: dict = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef)
                and node.name == "_dispatch_tool"):
            continue
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Assign):
                continue
            targets = [t.id for t in sub.targets if isinstance(t, ast.Name)]
            if "routes" not in targets or not isinstance(sub.value, ast.Dict):
                continue
            for key, value in zip(sub.value.keys, sub.value.values):
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    routes[key.value] = value
    return routes


def _dotted_path(node) -> list:
    """``a.b.c`` as ``['a', 'b', 'c']``; None when the chain is not a name."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return []
    parts.append(node.id)
    parts.reverse()
    return parts


def _resolve(path: list):
    """Resolve a dotted path against the llm_client module; None if broken."""
    if not path:
        return None
    obj = getattr(llm_client, path[0], None)
    if obj is None:
        return None
    for attr in path[1:]:
        obj = getattr(obj, attr, None)
        if obj is None:
            return None
    return obj


def _route_targets(node) -> list:
    """Every callable a route reaches for."""
    targets = []
    for call in ast.walk(node):
        if not isinstance(call, ast.Call):
            continue
        func = call.func
        if isinstance(func, ast.Name):
            resolved = _resolve([func.id])
        elif isinstance(func, ast.Attribute):
            resolved = _resolve(_dotted_path(func))
        else:
            resolved = None
        if callable(resolved):
            targets.append(resolved)
    return targets


# ── The invariants ───────────────────────────────────────────────────────────

def test_every_schema_has_a_dispatch_route():
    schemas = {e["function"]["name"] for e in tools_mod.TOOLS_DESCRIPTION}
    routes = set(_route_nodes())

    missing = sorted(schemas - routes)
    orphan = sorted(routes - schemas)

    assert not missing, f"schema without a dispatch route: {missing}"
    assert not orphan, f"dispatch route without a schema: {orphan}"


def test_every_route_resolves_to_a_callable():
    """A route that reaches through a module counts as resolved.

    ``state_manager.initialize_project`` and ``_cm.retrieve_chunk`` are
    ordinary routes; the old audit only looked for a bare global with the
    tool's name and reported all of them as broken.
    """
    unresolved = []
    for name, node in sorted(_route_nodes().items()):
        if not _route_targets(node):
            unresolved.append(name)

    assert not unresolved, f"routes that resolve to nothing: {unresolved}"


def test_the_worker_registry_matches_the_documented_exclusions():
    """The sub-AI registry and the list of deliberate exclusions agree."""
    schemas = {e["function"]["name"] for e in tools_mod.TOOLS_DESCRIPTION}
    registry = set(tools_mod._SUB_TOOL_FUNCS)
    excluded = set(tools_mod._SUB_AI_EXCLUDED_TOOLS)

    undocumented = sorted((schemas - registry) - excluded)
    stale = sorted(excluded & registry)

    assert not undocumented, (
        "tools a delegated worker cannot run without a documented reason: "
        f"{undocumented}"
    )
    assert not stale, (
        f"tools listed as excluded but actually available to a worker: {stale}"
    )


def test_every_worker_tool_is_advertised():
    """A worker must never be offered a tool the model cannot see."""
    schemas = {e["function"]["name"] for e in tools_mod.TOOLS_DESCRIPTION}
    registry = set(tools_mod._SUB_TOOL_FUNCS)

    assert not (registry - schemas)


def test_the_worker_registry_entries_are_callable():
    for name, func in tools_mod._SUB_TOOL_FUNCS.items():
        assert callable(func), f"{name} is in the worker registry but not callable"


# ── The two behaviours that were silent ─────────────────────────────────────

def test_a_requested_tool_that_cannot_be_granted_is_reported():
    """A worker must not silently lose a tool the caller asked for."""
    note = tools_mod._dropped_tool_note(["manage_plugin"], ["shell"])
    assert "manage_plugin" in note
    assert "shell" in note

    text_only = tools_mod._dropped_tool_note(["manage_plugin"], [])
    assert "manage_plugin" in text_only
    assert "text-only" in text_only


def test_the_selection_reports_what_it_cannot_grant():
    """The selection itself must name the tools it drops."""
    _, granted, dropped = tools_mod._select_sub_tools(
        ["shell", "manage_plugin"], 5
    )
    assert granted == ["shell"]
    assert dropped == ["manage_plugin"]

    # Every requested name unavailable: the worker runs text-only, and that
    # is said out loud rather than passed off as a normal delegation.
    schemas, granted, dropped = tools_mod._select_sub_tools(
        ["manage_plugin"], 5
    )
    assert schemas is None
    assert granted == []
    assert dropped == ["manage_plugin"]


def test_an_explicitly_empty_tool_list_means_text_only():
    """An empty list is a deliberate choice, not a loss."""
    schemas, granted, dropped = tools_mod._select_sub_tools([], 5)
    assert schemas is None
    assert granted == []
    assert dropped == []


def test_no_tool_rounds_means_no_tools():
    schemas, granted, dropped = tools_mod._select_sub_tools(None, 0)
    assert schemas is None
    assert granted == []
    assert dropped == []


def test_the_default_toolkit_is_fully_grantable():
    """The built-in toolkit must not contain a name the registry lacks."""
    _, granted, dropped = tools_mod._select_sub_tools(None, 5)
    assert dropped == []
    assert set(granted) == set(tools_mod._SUBAGENT_DEFAULT_TOOLS)


def test_the_archive_tools_are_available_to_a_worker():
    """A delegated worker can read back what was already done.

    The archive holds the raw text of every compressed context segment, so it
    is exactly what a worker needs for "check how much of this was already
    done and continue".  It used to be missing from the registry while the
    comment next to it still claimed to list every exclusion.
    """
    for name in ("retrieve_archive", "list_archives"):
        assert name in tools_mod._SUB_TOOL_FUNCS
        assert name not in tools_mod._SUB_AI_EXCLUDED_TOOLS
