"""Runtime activation for Termux-AI plugins.

A plugin can be *enabled* — its tools are handed to the model — without
being *usable*, because the service behind it is not running.  The WhatsApp
plugin was exactly that: ``/plugin enable whatsapp`` flipped a flag, the
model was given ``send_whatsapp_message``, and every call died with
"connection refused" because the Termux-WP node gateway (``bot.js``) was
still down.  Starting that gateway by hand in a second terminal was the
undocumented price of the plugin.

This module is the bridge between "the plugin is switched on" and "the
plugin actually works".  It knows how to bring a runtime up, and it is
called from every path that enables a plugin:

  * ``/plugin enable <name>`` in the interface, and
  * the ``manage_plugin`` tool the model itself can call.

A plugin without a runtime (the Google plugins talk to a hosted API) simply
has no handler here — enabling it is enough, because the *account* handshake
is the only extra step they need.
"""
import os
import sys

# Path bootstrap — the flat module name below has to resolve to the very same
# module object that core/tools.py imports.  Two copies of the WhatsApp
# manager would mean two WS listener threads and two runtime states.
_CORE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_CORE)
if _CORE not in sys.path:
    sys.path.insert(0, _CORE)
if _ROOT not in sys.path:
    sys.path.insert(1, _ROOT)

GRAY = "\033[90m"
YELLOW = "\033[93m"
RED = "\033[91m"
RESET = "\033[0m"


def _whatsapp_manager():
    """The WhatsApp manager, or None when Termux-WP is not installed.

    The import raises FileNotFoundError when the Termux-WP checkout is
    missing — that is a normal state, not a crash, so it is reported
    instead of propagated.
    """
    try:
        from whatsapp_manager import whatsapp_manager
        return whatsapp_manager
    except (ImportError, FileNotFoundError) as exc:
        print(f"{YELLOW}[Plugins] The WhatsApp runtime is unavailable: {exc}{RESET}")
        return None


def _whatsapp_activate() -> dict:
    """Bring the Termux-WP gateway and the Python listener up."""
    manager = _whatsapp_manager()
    if manager is None:
        return {
            "ok": False,
            "state": "not_installed",
            "message": ("Termux-WP is not installed on this device — the "
                        "WhatsApp gateway cannot start. Clone it with "
                        "'git clone https://github.com/opsonusdh/Termux-WP' "
                        "and run its setup.sh."),
        }
    ok, message = manager.ensure_running()
    return {
        "ok": bool(ok),
        "state": "running" if ok else "failed",
        "message": message,
        "detail": manager.runtime_status(),
    }


def _whatsapp_deactivate() -> dict:
    """Stop the gateway this session started."""
    manager = _whatsapp_manager()
    if manager is None:
        return {"ok": True, "state": "not_installed",
                "message": "Termux-WP is not installed — nothing to stop."}
    stopped, message = manager.stop_server()
    return {
        "ok": True,
        "state": "stopped" if stopped else "not_started",
        "message": message,
        "detail": manager.runtime_status(),
    }


def _whatsapp_status() -> dict:
    """Is the WhatsApp runtime up right now?"""
    manager = _whatsapp_manager()
    if manager is None:
        return {"ok": False, "state": "not_installed",
                "message": "Termux-WP is not installed."}
    detail = manager.runtime_status()
    running = bool(detail.get("server_running"))
    pairing = "unknown"
    if running:
        pairing = manager.gateway_state().get("state", "UNKNOWN")
    detail["pairing_state"] = pairing
    return {
        "ok": running,
        "state": "running" if running else "stopped",
        "message": ("Termux-WP is running."
                    if running else
                    "Termux-WP is not running (the WhatsApp tools would "
                    "fail until it is started)."),
        "detail": detail,
    }


def _set_plugin_enabled(plugin_name: str, enabled: bool) -> bool:
    """Flip a plugin's enabled flag through the plugin manager.

    The plugin manager is imported lazily and by its package name: the
    module is imported one way everywhere (``core.plugin_manager``), and a
    second copy would keep its own state and write a different answer to
    config.json.
    """
    try:
        from core.plugin_manager import plugin_manager
        if enabled:
            return bool(plugin_manager.enable(plugin_name))
        return bool(plugin_manager.disable(plugin_name))
    except Exception as exc:
        print(f"{YELLOW}[Plugins] Could not update the state of "
              f"'{plugin_name}': {exc}{RESET}")
        return False


def _whatsapp_setup(qr_timeout: float = 40.0) -> dict:
    """The WhatsApp pairing path — the only place a QR is ever shown.

    The user asked for the pairing code, so there are two cases:

    * the session is **not** linked yet — the gateway is started and the
      QR is rendered once, for the user to scan;
    * the session is **already** linked — a QR is only wanted for a
      re-link, so the gateway is killed and the plugin is switched off
      instead of pretending a fresh code is available.
    """
    manager = _whatsapp_manager()
    if manager is None:
        return {
            "ok": False,
            "state": "not_installed",
            "message": ("Termux-WP is not installed on this device — the "
                        "WhatsApp gateway cannot start."),
        }

    # The plugin is on while we pair it.  The "already linked" branch below
    # switches it off again — that is the whole point of that branch.
    _set_plugin_enabled("whatsapp", True)

    ok, message = manager.ensure_running()
    if not ok:
        return {"ok": False, "state": "failed", "message": message}

    # The gateway opens its port before the WhatsApp engine has finished
    # booting.  Asking it now would answer "INITIALIZING", and treating
    # that as "not linked" would hand out a pairing code for a session
    # that is in fact already linked.
    print(f"{GRAY}[WhatsApp] Waiting for the WhatsApp session to load…{RESET}")
    pairing_state = manager.wait_for_pairing_state(timeout=60.0)

    if pairing_state == "READY":
        killed, kill_message = manager.kill_gateway()
        disabled = _set_plugin_enabled("whatsapp", False)
        return {
            "ok": True,
            "state": "reset",
            "killed": killed,
            "disabled": disabled,
            "message": (
                "WhatsApp was already linked, so no pairing code was issued. "
                f"{kill_message} The plugin has been "
                + ("disabled — run '/plugin enable whatsapp' to bring it back."
                   if disabled else
                   "switched off — run '/plugin enable whatsapp' to bring it "
                   "back.")
            ),
        }

    if pairing_state != "QR_REQUIRED":
        return {
            "ok": False,
            "state": "unknown_state",
            "message": (f"The gateway is running but the WhatsApp session is "
                        f"still '{pairing_state}' — the pairing state could "
                        f"not be decided. Check Termux-WP/bot_output.log for "
                        f"the reason."),
        }

    qr = manager.wait_for_qr(timeout=qr_timeout)
    if not qr:
        # The WebSocket event can be missed; the gateway also carries the
        # current code in its status answer.
        qr = manager.gateway_state().get("qr")
    if not qr:
        return {
            "ok": False,
            "state": "no_qr",
            "message": ("The gateway is running but no pairing code arrived. "
                        "Check Termux-WP/bot_output.log for the reason."),
        }

    manager.show_qr(qr)
    return {
        "ok": True,
        "state": "qr_shown",
        "message": ("The pairing code is on screen — scan it in WhatsApp → "
                    "Linked devices to pair this device."),
    }


# Plugin name -> runtime handler.  A plugin without an entry here needs no
# runtime: enabling it is the whole job.
RUNTIME_HANDLERS = {
    "whatsapp": {
        "activate": _whatsapp_activate,
        "deactivate": _whatsapp_deactivate,
        "status": _whatsapp_status,
        "setup": _whatsapp_setup,
    },
}


def has_runtime(plugin_name: str) -> bool:
    """True when this plugin owns a runtime that has to be started."""
    return plugin_name in RUNTIME_HANDLERS


def activate(plugin_name: str) -> dict:
    """Start the runtime of a plugin.

    Returns ``{"ok", "state", "message"}``.  A plugin without a runtime is
    reported as ready — there is nothing to start.
    """
    handler = RUNTIME_HANDLERS.get(plugin_name)
    if not handler:
        return {"ok": True, "state": "no_runtime",
                "message": "No local runtime needed for this plugin."}
    try:
        result = handler["activate"]()
    except Exception as exc:
        return {"ok": False, "state": "failed",
                "message": f"Could not start the {plugin_name} runtime: {exc}"}
    if not isinstance(result, dict):
        result = {"ok": bool(result), "state": "unknown",
                  "message": str(result)}
    result.setdefault("ok", False)
    result.setdefault("state", "unknown")
    result.setdefault("message", "")
    return result


def deactivate(plugin_name: str) -> dict:
    """Stop the runtime of a plugin."""
    handler = RUNTIME_HANDLERS.get(plugin_name)
    if not handler:
        return {"ok": True, "state": "no_runtime",
                "message": "No local runtime to stop for this plugin."}
    try:
        result = handler["deactivate"]()
    except Exception as exc:
        return {"ok": False, "state": "failed",
                "message": f"Could not stop the {plugin_name} runtime: {exc}"}
    if not isinstance(result, dict):
        result = {"ok": bool(result), "state": "unknown",
                  "message": str(result)}
    result.setdefault("ok", False)
    result.setdefault("state", "unknown")
    result.setdefault("message", "")
    return result



def status(plugin_name: str) -> dict:
    """Report the runtime state of a plugin."""
    handler = RUNTIME_HANDLERS.get(plugin_name)
    if not handler:
        return {"ok": True, "state": "no_runtime",
                "message": "No local runtime for this plugin."}
    try:
        result = handler["status"]()
    except Exception as exc:
        return {"ok": False, "state": "unknown",
                "message": f"Could not read the {plugin_name} runtime state: {exc}"}
    if not isinstance(result, dict):
        result = {"ok": bool(result), "state": "unknown",
                  "message": str(result)}
    result.setdefault("ok", False)
    result.setdefault("state", "unknown")
    result.setdefault("message", "")
    return result


def setup(plugin_name: str) -> dict:
    """Run the explicit setup path of a plugin.

    This is the "set this plugin up" action — for WhatsApp it is the
    pairing path, and the only place the pairing code is ever rendered.
    A plugin without a runtime needs no setup step.
    """
    handler = RUNTIME_HANDLERS.get(plugin_name)
    if not handler or "setup" not in handler:
        return {"ok": True, "state": "no_runtime",
                "message": "No local setup needed for this plugin."}
    try:
        result = handler["setup"]()
    except Exception as exc:
        return {"ok": False, "state": "failed",
                "message": f"Could not run the {plugin_name} setup: {exc}"}
    if not isinstance(result, dict):
        result = {"ok": bool(result), "state": "unknown",
                  "message": str(result)}
    result.setdefault("ok", False)
    result.setdefault("state", "unknown")
    result.setdefault("message", "")
    return result
