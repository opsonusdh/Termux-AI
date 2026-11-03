"""
Plugin Manager for Termux-AI.

Manages plugin enable/disable state, maps tools to their respective plugins
and knows what each plugin needs before it can actually be used.

Plugins are stored in config.json under the "plugins" key.

Two kinds of plugins exist here:

  * **Local plugins** (``whatsapp``) — they talk to a service that needs no
    account handshake (the WhatsApp session is paired on the device).
  * **Account plugins** (``gmail``, ``google_calendar``, ``google_drive``) —
    they need the user to grant Google access first.  Enabling such a plugin
    is only half the job: until the OAuth consent flow has been completed
    the plugin has no credentials, so its tools are not advertised to the
    model and calling one returns a message that says exactly how to fix it.
"""
import json
import os
import paths

# Default plugin configuration.
#
# ``service`` names the Google service behind a plugin (see
# ``core/google_oauth.py``).  A plugin with a service needs the OAuth consent
# flow before its tools can run; a plugin without one is ready as soon as it
# is enabled.
DEFAULT_PLUGINS = {
    "whatsapp": {
        "enabled": True,
        "description": "WhatsApp messaging, auto-reply, chat management",
        "service": None,
        "setup_hint": "Runs locally through Termux-WP — no account setup needed.",
    },
    "gmail": {
        "enabled": False,
        "description": "Gmail read/write/send (Google OAuth)",
        "service": "gmail",
        "setup_hint": ("Needs a Google OAuth client (application type: "
                       "Desktop app) — run '/plugin setup gmail'."),
    },
    "google_calendar": {
        "enabled": False,
        "description": "Google Calendar events (read/create/update)",
        "service": "google_calendar",
        "setup_hint": ("Needs a Google OAuth client (application type: "
                       "Desktop app) — run '/plugin setup google_calendar'."),
    },
    "google_drive": {
        "enabled": False,
        "description": "Google Drive file operations",
        "service": "google_drive",
        "setup_hint": ("Needs a Google OAuth client (application type: "
                       "Desktop app) — run '/plugin setup google_drive'."),
    },
}

# Tool name -> plugin name mapping
TOOL_PLUGIN_MAP = {
    # WhatsApp tools
    "send_whatsapp_message": "whatsapp",
    "get_whatsapp_status": "whatsapp",
    "get_whatsapp_chats": "whatsapp",
    "silence_whatsapp_contact": "whatsapp",
    "react_to_whatsapp_message": "whatsapp",
    "get_whatsapp_contact_info": "whatsapp",
    "get_whatsapp_group_participants": "whatsapp",
    "download_whatsapp_media": "whatsapp",
    "schedule_whatsapp_message": "whatsapp",
    "search_whatsapp_chat": "whatsapp",
    "archive_whatsapp_chat": "whatsapp",
    "set_whatsapp_seen": "whatsapp",
    "get_pending_whatsapp_messages": "whatsapp",
    "fetch_whatsapp_chat_history": "whatsapp",
    "set_whatsapp_busy_mode": "whatsapp",
    "get_whatsapp_report": "whatsapp",
    "set_whatsapp_user_profile": "whatsapp",
    # Gmail tools
    "send_gmail_message": "gmail",
    "get_gmail_messages": "gmail",
    "search_gmail": "gmail",
    "get_gmail_labels": "gmail",
    "create_gmail_label": "gmail",
    "delete_gmail_message": "gmail",
    "read_gmail_message": "gmail",
    "get_gmail_profile": "gmail",
    # Google Calendar tools
    "get_calendar_events": "google_calendar",
    "create_calendar_event": "google_calendar",
    "update_calendar_event": "google_calendar",
    "delete_calendar_event": "google_calendar",
    "list_calendars": "google_calendar",
    # Google Drive tools
    "list_drive_files": "google_drive",
    "upload_drive_file": "google_drive",
    "download_drive_file": "google_drive",
    "delete_drive_file": "google_drive",
    "create_drive_folder": "google_drive",
    "share_drive_file": "google_drive",
}

# Tools that are advertised to the model no matter which plugins are on.
#
# The plugin manager itself belongs here: it is the only way the model can
# find out that a plugin exists, and the only way it can switch one on.  If
# the manager were hidden behind the same filter as the plugins it manages,
# the model could never enable one — it would only ever see "that capability
# is unavailable".  Keeping the list explicit (instead of relying on
# "manage_plugin happens to have no plugin mapping") means a future entry in
# TOOL_PLUGIN_MAP cannot silently take the manager out of the tool context.
ALWAYS_AVAILABLE_TOOLS = frozenset({"manage_plugin"})


class PluginManager:
    """Manages plugin enable/disable state, setup state and tool filtering."""

    def __init__(self):
        self._plugins = None
        self._load()

    def _load(self):
        """Load plugin config from config.json."""
        try:
            with open(paths.CONFIG_FILE, "r", encoding="utf-8") as f:
                config = json.load(f)
            if not isinstance(config, dict):
                config = {}
        except Exception:
            config = {}

        # Merge with defaults
        plugins = config.get("plugins", {})
        merged = {}
        for name, default in DEFAULT_PLUGINS.items():
            user_cfg = plugins.get(name, {})
            if not isinstance(user_cfg, dict):
                user_cfg = {}
            merged[name] = {
                "enabled": user_cfg.get("enabled", default["enabled"]),
                "description": default["description"],
                "service": default.get("service"),
                "setup_hint": default.get("setup_hint", ""),
            }
        # Keep plugins the user added themselves (unknown to this version).
        for name, user_cfg in plugins.items():
            if name in merged or not isinstance(user_cfg, dict):
                continue
            merged[name] = {
                "enabled": user_cfg.get("enabled", False),
                "description": user_cfg.get("description", ""),
                "service": user_cfg.get("service"),
                "setup_hint": user_cfg.get("setup_hint", ""),
            }
        self._plugins = merged

    def _save(self):
        """Save plugin config to config.json."""
        try:
            with open(paths.CONFIG_FILE, "r", encoding="utf-8") as f:
                config = json.load(f)
            if not isinstance(config, dict):
                config = {}
        except Exception:
            config = {}

        # Preserve existing plugin configs but update enabled state
        existing = config.get("plugins", {})
        if not isinstance(existing, dict):
            existing = {}
        for name, data in self._plugins.items():
            if name not in existing or not isinstance(existing[name], dict):
                existing[name] = {"enabled": data["enabled"]}
            else:
                existing[name]["enabled"] = data["enabled"]

        config["plugins"] = existing

        tmp = paths.CONFIG_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=4)
        os.replace(tmp, paths.CONFIG_FILE)

    def is_enabled(self, plugin_name: str) -> bool:
        """Check if a plugin is enabled."""
        plugin = self._plugins.get(plugin_name)
        return plugin.get("enabled", False) if plugin else False

    def enable(self, plugin_name: str) -> bool:
        """Enable a plugin. Returns True if successful."""
        if plugin_name not in self._plugins:
            return False
        self._plugins[plugin_name]["enabled"] = True
        self._save()
        return True

    def disable(self, plugin_name: str) -> bool:
        """Disable a plugin. Returns True if successful."""
        if plugin_name not in self._plugins:
            return False
        self._plugins[plugin_name]["enabled"] = False
        self._save()
        return True

    def get_plugin_info(self, plugin_name: str) -> dict | None:
        """Get plugin info including enabled state and description."""
        return self._plugins.get(plugin_name)

    def list_plugins(self) -> dict:
        """Return all plugins with their status."""
        return dict(self._plugins)

    def get_tool_plugin(self, tool_name: str) -> str | None:
        """Get the plugin name that owns a tool."""
        return TOOL_PLUGIN_MAP.get(tool_name)

    # ── Setup / authorisation state ───────────────────────────────────────────
    def get_plugin_service(self, plugin_name: str) -> str | None:
        """The Google service behind a plugin (None for local plugins)."""
        plugin = self._plugins.get(plugin_name)
        return plugin.get("service") if plugin else None

    def requires_setup(self, plugin_name: str) -> bool:
        """True when the plugin needs an account handshake before it works."""
        return bool(self.get_plugin_service(plugin_name))

    def is_authorized(self, plugin_name: str) -> bool:
        """True when the plugin's account is connected.

        Local plugins are always "authorized" — they have nothing to grant.
        """
        service = self.get_plugin_service(plugin_name)
        if not service:
            return True
        try:
            from core import google_oauth
            return bool(google_oauth.auth_status(service).get("authorized"))
        except Exception as exc:
            print(f"[Plugins] Could not read the setup state of "
                  f"'{plugin_name}': {exc}")
            return False

    def plugin_state(self, plugin_name: str) -> dict:
        """Everything the UI needs to describe a plugin's state."""
        plugin = self._plugins.get(plugin_name)
        if not plugin:
            return {}

        service = plugin.get("service")
        state = {
            "name": plugin_name,
            "enabled": bool(plugin.get("enabled")),
            "description": plugin.get("description", ""),
            "service": service,
            "setup_hint": plugin.get("setup_hint", ""),
            "requires_setup": bool(service),
            "authorized": True,
            "account": "",
            "client_configured": True,
        }

        if not service:
            return state

        try:
            from core import google_oauth
            status = google_oauth.auth_status(service)
            state["authorized"] = bool(status.get("authorized"))
            state["account"] = status.get("account", "")
            state["client_configured"] = bool(status.get("client_configured"))
            state["scopes"] = status.get("scopes", [])
        except Exception as exc:
            print(f"[Plugins] Could not read the setup state of "
                  f"'{plugin_name}': {exc}")
            state["authorized"] = False
            state["client_configured"] = False
        return state

    def is_tool_allowed(self, tool_name: str) -> bool:
        """Check if a tool is allowed.

        A tool is allowed when its plugin is enabled *and* the plugin is
        actually usable.  For an account plugin that means the OAuth consent
        has been granted — advertising a tool that cannot work would only
        produce confusing failures later.

        The always-available tools (the plugin manager) are allowed even
        when every plugin is off: without them the model could not switch a
        plugin on in the first place.
        """
        if tool_name in ALWAYS_AVAILABLE_TOOLS:
            return True
        plugin = self.get_tool_plugin(tool_name)
        if plugin is None:
            return True  # Tools without a plugin are always allowed (core tools)
        if not self.is_enabled(plugin):
            return False
        return self.is_authorized(plugin)

    def filter_tools_description(self, tools_desc: list) -> list:
        """Filter TOOLS_DESCRIPTION to only include tools from enabled plugins.

        The result is rebuilt for every request, so a plugin that is
        enabled mid-session (by the user or by the model itself) has its
        tools in the very next request — no restart needed.
        """
        filtered = []
        for tool in tools_desc:
            name = tool.get("function", {}).get("name")
            if name and self.is_tool_allowed(name):
                filtered.append(tool)
        return filtered

    def get_enabled_plugin_tools(self, plugin_name: str) -> list:
        """Get list of tool names for an enabled plugin."""
        if not self.is_enabled(plugin_name):
            return []
        return [tool for tool, p in TOOL_PLUGIN_MAP.items() if p == plugin_name]

    # ── Runtime activation ───────────────────────────────────────────────────
    #
    # Enabling a plugin and making it work are two different things.  The
    # WhatsApp plugin needs the Termux-WP node gateway running before any
    # of its tools can do anything; without this step the model was handed
    # a set of tools that failed every single time.
    #
    # ``activate()`` is the one place that does both, so every path that
    # enables a plugin (``/plugin enable`` and the ``manage_plugin`` tool)
    # gets the same behaviour.

    def activate(self, plugin_name: str) -> dict:
        """Enable a plugin and bring its runtime up.

        Returns a dict with ``enabled``, ``state``, ``message`` and — when
        the plugin owns a runtime — ``runtime`` (the runtime report).
        """
        if plugin_name not in self._plugins:
            return {
                "ok": False,
                "enabled": False,
                "state": "unknown",
                "message": f"Unknown plugin '{plugin_name}'.",
            }

        self._plugins[plugin_name]["enabled"] = True
        self._save()

        tools = self.get_enabled_plugin_tools(plugin_name)
        result = {
            "ok": True,
            "plugin": plugin_name,
            "enabled": True,
            "state": "enabled",
            "tools": tools,
            "message": f"Plugin '{plugin_name}' enabled.",
        }

        try:
            from core import plugin_runtime
            runtime = plugin_runtime.activate(plugin_name)
        except Exception as exc:
            runtime = {"ok": False, "state": "failed",
                       "message": f"Could not start the runtime: {exc}"}

        result["runtime"] = runtime
        if runtime.get("ok"):
            result["state"] = "ready"
            result["message"] = (
                f"Plugin '{plugin_name}' enabled and ready. "
                f"{runtime.get('message', '')}"
            ).strip()
        else:
            result["state"] = "enabled_runtime_failed"
            result["message"] = (
                f"Plugin '{plugin_name}' enabled, but its runtime did not "
                f"start: {runtime.get('message', 'unknown error')}"
            )
        return result

    def deactivate(self, plugin_name: str) -> dict:
        """Disable a plugin and stop the runtime this session started."""
        if plugin_name not in self._plugins:
            return {
                "ok": False,
                "enabled": False,
                "state": "unknown",
                "message": f"Unknown plugin '{plugin_name}'.",
            }

        self._plugins[plugin_name]["enabled"] = False
        self._save()

        try:
            from core import plugin_runtime
            runtime = plugin_runtime.deactivate(plugin_name)
        except Exception as exc:
            runtime = {"ok": False, "state": "failed",
                       "message": f"Could not stop the runtime: {exc}"}

        return {
            "ok": True,
            "plugin": plugin_name,
            "enabled": False,
            "state": "disabled",
            "runtime": runtime,
            "message": f"Plugin '{plugin_name}' disabled.",
        }

    def runtime_state(self, plugin_name: str) -> dict:
        """The runtime report of a plugin (empty when it has no runtime)."""
        try:
            from core import plugin_runtime
            return plugin_runtime.status(plugin_name)
        except Exception as exc:
            return {"ok": False, "state": "unknown",
                    "message": f"Could not read the runtime state: {exc}"}

    def setup(self, plugin_name: str) -> dict:
        """Run the explicit setup path of a plugin.

        For a plugin that owns a runtime this is the pairing step — for
        WhatsApp it is the one and only place the pairing code is shown,
        and it is shown only because the user asked for it.  A plugin
        without a runtime has nothing to set up.
        """
        if plugin_name not in self._plugins:
            return {
                "ok": False,
                "enabled": False,
                "state": "unknown",
                "message": f"Unknown plugin '{plugin_name}'.",
            }
        try:
            from core import plugin_runtime
            result = plugin_runtime.setup(plugin_name)
        except Exception as exc:
            result = {"ok": False, "state": "failed",
                      "message": f"Could not run the setup: {exc}"}
        if not isinstance(result, dict):
            result = {"ok": bool(result), "state": "unknown",
                      "message": str(result)}
        result.setdefault("ok", False)
        result.setdefault("state", "unknown")
        result.setdefault("message", "")
        result["enabled"] = self.is_enabled(plugin_name)
        result["plugin"] = plugin_name
        return result


# Global instance
plugin_manager = PluginManager()
