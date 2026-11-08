"""core/app_config.py — the canonical shape of ``config/config.json``.

Three modules used to carry their own copy of the default configuration:

* ``core/interface.py``
* ``core/tools.py`` (``sleep_mode``)
* ``core/permissions.py``

Each copy drifted.  The ones in ``tools.sleep_mode`` and ``permissions.py``
never learned about the ``notify`` flag, so a ``config.json`` created by
either of them was missing a key the interface expected — and the interface
silently re-added it on the next load, which is exactly the kind of
half-working setting that is hard to notice.

One definition, used everywhere, is the fix.  The *shape* of the file lives
here; the *I/O* stays with whoever needs it, because the display-state writer
(``core/display_state.py``) persists ``show_details`` on its own schedule and
must not be folded into a shared writer that would clobber it.
"""
from __future__ import annotations

import json
import os
import sys

# Path bootstrap — same convention as every other cross-package module.
_CORE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_CORE)
if _CORE not in sys.path:
    sys.path.insert(0, _CORE)
if _ROOT not in sys.path:
    sys.path.insert(1, _ROOT)

import paths  # noqa: E402


#: Every key the runtime understands, with the value used when it is absent.
DEFAULT_CONFIG: dict = {
    "stt_path":    os.path.join(paths.ROOT, "Termux-STT"),
    "tts_enabled": False,
    "use_groq":    False,
    "show_details": False,
    "autonomous":  False,
    "notify":      True,
    # Oversized tool results go to disk instead of into the context window.
    # The threshold is in tokens (roughly 4 characters each); 4000 tokens is
    # about 16 KB, so an ordinary command still arrives whole.
    "spill_trigger_tokens": 4000,
    "spill_enabled":        True,
}


def load_config() -> dict:
    """The stored configuration with every default key filled in.

    A missing or corrupt ``config.json`` yields the defaults rather than an
    exception — the runtime has to survive a bad file, not die on it.
    """
    data: dict = {}
    try:
        with open(paths.CONFIG_FILE, "r", encoding="utf-8") as fh:
            loaded = json.load(fh)
        if isinstance(loaded, dict):
            data = loaded
    except Exception:
        data = {}
    for key, value in DEFAULT_CONFIG.items():
        data.setdefault(key, value)
    return data


def ensure_config() -> dict:
    """Create ``config.json`` from the defaults when it does not exist yet."""
    try:
        if not os.path.exists(paths.CONFIG_FILE):
            os.makedirs(paths.CONFIG_DIR, exist_ok=True)
            with open(paths.CONFIG_FILE, "w", encoding="utf-8") as fh:
                json.dump(dict(DEFAULT_CONFIG), fh, indent=4)
    except Exception:
        pass
    return load_config()


def save_config(**updates) -> dict:
    """Merge *updates* into ``config.json`` and return what was stored.

    The file is read fresh immediately before the write, so a change made by
    another writer in the meantime (``show_details`` from the Ctrl+O toggle,
    for instance) is not clobbered.
    """
    data = load_config()
    data.update(updates)
    try:
        os.makedirs(paths.CONFIG_DIR, exist_ok=True)
        tmp = paths.CONFIG_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=4)
        os.replace(tmp, paths.CONFIG_FILE)
    except Exception:
        pass
    return data
