"""Regression tests for /plugin enable|disable|status|list dispatch.

These guard against the silent regression where the ``_show_all_plugins``
refactor dropped the ``enable``/``disable`` branches (and the unknown-subcommand
fallback) from ``_handle_plugin_command`` in ``core/interface.py``.

The tests inject a fake plugin manager + a throwaway config path so the real
on-disk ``config.json`` is never mutated.
"""
import json

import core.interface as iface


class _FakePluginManager:
    """A minimal stand-in for PluginManager that operates in memory."""

    def __init__(self, plugins):
        # deep-ish copy so tests don't bleed state
        self._plugins = {n: dict(d) for n, d in plugins.items()}

    def list_plugins(self):
        return {n: dict(d) for n, d in self._plugins.items()}

    def get_plugin_info(self, name):
        return self._plugins.get(name)

    def is_enabled(self, name):
        p = self._plugins.get(name)
        return p.get("enabled", False) if p else False

    def enable(self, name):
        if name not in self._plugins:
            return False
        self._plugins[name]["enabled"] = True
        return True

    def disable(self, name):
        if name not in self._plugins:
            return False
        self._plugins[name]["enabled"] = False
        return True

    def get_enabled_plugin_tools(self, name):
        return []

    def is_tool_allowed(self, tool_name):
        return True


def _install_fake(monkeypatch, tmp_path):
    plugins = {
        "whatsapp": {"enabled": True, "description": "WhatsApp messaging"},
        "gmail": {"enabled": False, "description": "Gmail read/write/send"},
    }
    pm = _FakePluginManager(plugins)
    monkeypatch.setattr(iface, "plugin_manager", pm)

    # Isolate the post-mutation config reload from the real config.json.
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"plugins": {"whatsapp": {"enabled": True}}}))
    monkeypatch.setattr(iface, "CONFIG_PATH", str(cfg))
    return pm


def test_plugin_enable_existing(capsys, monkeypatch, tmp_path):
    pm = _install_fake(monkeypatch, tmp_path)
    iface._handle_plugin_command("/plugin enable gmail")
    out = capsys.readouterr().out
    assert "enabled" in out
    assert "gmail" in out
    assert pm.is_enabled("gmail") is True


def test_plugin_disable_existing(capsys, monkeypatch, tmp_path):
    pm = _install_fake(monkeypatch, tmp_path)
    iface._handle_plugin_command("/plugin disable whatsapp")
    out = capsys.readouterr().out
    assert "disabled" in out
    assert "whatsapp" in out
    assert pm.is_enabled("whatsapp") is False


def test_plugin_enable_unknown_reports_error(capsys, monkeypatch, tmp_path):
    _install_fake(monkeypatch, tmp_path)
    iface._handle_plugin_command("/plugin enable bogus_plugin")
    out = capsys.readouterr().out
    assert "Unknown plugin" in out
    assert "Available" in out


def test_plugin_disable_unknown_reports_error(capsys, monkeypatch, tmp_path):
    _install_fake(monkeypatch, tmp_path)
    iface._handle_plugin_command("/plugin disable bogus_plugin")
    out = capsys.readouterr().out
    assert "Unknown plugin" in out
    assert "Available" in out


def test_plugin_enable_requires_name(capsys, monkeypatch, tmp_path):
    _install_fake(monkeypatch, tmp_path)
    iface._handle_plugin_command("/plugin enable")
    out = capsys.readouterr().out
    assert "Plugin name required" in out


def test_plugin_disable_requires_name(capsys, monkeypatch, tmp_path):
    _install_fake(monkeypatch, tmp_path)
    iface._handle_plugin_command("/plugin disable")
    out = capsys.readouterr().out
    assert "Plugin name required" in out


def test_plugin_unknown_subcommand_fallback(capsys, monkeypatch, tmp_path):
    _install_fake(monkeypatch, tmp_path)
    iface._handle_plugin_command("/plugin frobnicate")
    out = capsys.readouterr().out
    assert "Unknown subcommand" in out
    assert "Available" in out


def test_plugin_no_subcommand_shows_all(capsys, monkeypatch, tmp_path):
    _install_fake(monkeypatch, tmp_path)
    iface._handle_plugin_command("/plugin")
    out = capsys.readouterr().out
    assert "Plugins" in out
    assert "whatsapp" in out


def test_plugin_list_shows_all(capsys, monkeypatch, tmp_path):
    _install_fake(monkeypatch, tmp_path)
    iface._handle_plugin_command("/plugin list")
    out = capsys.readouterr().out
    assert "Plugins" in out
    assert "whatsapp" in out
    assert "gmail" in out


def test_plugin_status_existing(capsys, monkeypatch, tmp_path):
    _install_fake(monkeypatch, tmp_path)
    iface._handle_plugin_command("/plugin status whatsapp")
    out = capsys.readouterr().out
    assert "whatsapp" in out
    assert "ENABLED" in out


def test_plugin_status_unknown(capsys, monkeypatch, tmp_path):
    _install_fake(monkeypatch, tmp_path)
    iface._handle_plugin_command("/plugin status bogus_plugin")
    out = capsys.readouterr().out
    assert "Unknown plugin" in out
