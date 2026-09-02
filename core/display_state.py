"""Shared terminal display state for Termux-AI.

The state is intentionally tiny and dependency-light so it can be used by
both the interactive input handler and the model/tool rendering code without
creating circular imports.
"""
from __future__ import annotations

import json
import os
import threading
from typing import Any

_LOCK = threading.RLock()
_DETAILS_EXPANDED = False
_CONFIG_PATH: str | None = None


def configure(config_path: str | None = None, expanded: bool | None = None) -> None:
    """Configure persistence path and optionally set initial state."""
    global _CONFIG_PATH, _DETAILS_EXPANDED
    with _LOCK:
        if config_path:
            _CONFIG_PATH = os.path.abspath(config_path)
        if expanded is not None:
            _DETAILS_EXPANDED = bool(expanded)


def load_from_config(config_path: str) -> bool:
    """Load the persisted display preference. Returns the loaded state."""
    global _CONFIG_PATH, _DETAILS_EXPANDED
    with _LOCK:
        _CONFIG_PATH = os.path.abspath(config_path)
        value = False
        try:
            with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            value = bool(data.get("show_details", False))
        except Exception:
            value = False
        _DETAILS_EXPANDED = value
        return value


def is_expanded() -> bool:
    """Return the current display preference, synchronized with config when known."""
    global _DETAILS_EXPANDED
    with _LOCK:
        if _CONFIG_PATH:
            try:
                with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict) and "show_details" in data:
                    _DETAILS_EXPANDED = bool(data["show_details"])
            except Exception:
                pass
        return _DETAILS_EXPANDED


def toggle() -> bool:
    """Toggle and persist the expanded/collapsed display state."""
    global _DETAILS_EXPANDED
    with _LOCK:
        _DETAILS_EXPANDED = not _DETAILS_EXPANDED
        _persist_locked()
        return _DETAILS_EXPANDED


def set_expanded(expanded: bool, persist: bool = True) -> None:
    global _DETAILS_EXPANDED
    with _LOCK:
        _DETAILS_EXPANDED = bool(expanded)
        if persist:
            _persist_locked()


def _persist_locked() -> None:
    if not _CONFIG_PATH:
        return
    try:
        os.makedirs(os.path.dirname(_CONFIG_PATH), exist_ok=True)
        data: dict[str, Any] = {}
        if os.path.exists(_CONFIG_PATH):
            with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                data = loaded
        data["show_details"] = _DETAILS_EXPANDED
        tmp = _CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4)
        os.replace(tmp, _CONFIG_PATH)
    except Exception:
        # Display state must never break the agent.
        pass
