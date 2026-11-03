import pytest

def test_show_all_plugins_output(capsys):
    from core.interface import _show_all_plugins
    # Ensure no exception
    _show_all_plugins()
    captured = capsys.readouterr()
    # The output should contain the word Plugins and a known plugin name
    assert "Plugins" in captured.out
    assert "whatsapp" in captured.out.lower()

def test_show_help_output(capsys):
    from core.interface import _show_help
    _show_help()
    captured = capsys.readouterr()
    assert "Available commands" in captured.out
    assert "/help" in captured.out
