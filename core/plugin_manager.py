"""
Plugin Manager for Termux-AI.

Manages plugin enable/disable state and maps tools to their respective plugins.
Plugins are stored in config.json under the "plugins" key.
"""
import json
import os
import paths

# Default plugin configuration
DEFAULT_PLUGINS = {
    "whatsapp": {
        "enabled": True,
        "description": "WhatsApp messaging, auto-reply, chat management",
    },
    "gmail": {
        "enabled": False,
        "description": "Gmail read/write/send (Google OAuth)",
    },
    "google_calendar": {
        "enabled": False,
        "description": "Google Calendar events (read/create/update)",
    },
    "google_drive": {
        "enabled": False,
        "description": "Google Drive file operations",
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
    # Gmail tools (placeholder - not implemented yet)
    "send_gmail_message": "gmail",
    "get_gmail_messages": "gmail",
    "search_gmail": "gmail",
    "get_gmail_labels": "gmail",
    "create_gmail_label": "gmail",
    "delete_gmail_message": "gmail",
    # Google Calendar tools (placeholder)
    "get_calendar_events": "google_calendar",
    "create_calendar_event": "google_calendar",
    "update_calendar_event": "google_calendar",
    "delete_calendar_event": "google_calendar",
    "list_calendars": "google_calendar",
    # Google Drive tools (placeholder)
    "list_drive_files": "google_drive",
    "upload_drive_file": "google_drive",
    "download_drive_file": "google_drive",
    "delete_drive_file": "google_drive",
    "create_drive_folder": "google_drive",
    "share_drive_file": "google_drive",
}


class PluginManager:
    """Manages plugin enable/disable state and tool filtering."""

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
            merged[name] = {
                "enabled": user_cfg.get("enabled", default["enabled"]),
                "description": default["description"],
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
        for name, data in self._plugins.items():
            if name not in existing:
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

    def is_tool_allowed(self, tool_name: str) -> bool:
        """Check if a tool is allowed (its plugin is enabled or it has no plugin)."""
        plugin = self.get_tool_plugin(tool_name)
        if plugin is None:
            return True  # Tools without a plugin are always allowed (core tools)
        return self.is_enabled(plugin)

    def filter_tools_description(self, tools_desc: list) -> list:
        """Filter TOOLS_DESCRIPTION to only include tools from enabled plugins."""
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


# Global instance
plugin_manager = PluginManager()