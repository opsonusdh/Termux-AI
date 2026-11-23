"""Tests for the credential-exfiltration remediation.

Three layers are pinned:

1. ``core.confidential.redact_credentials`` — every credential-shaped value
   in a tool result is replaced by a redaction tag, and benign text is left
   alone.
2. ``core.permissions.validate_command`` / ``command_needs_permission`` —
   inline code that references a credential path constant
   (``paths.API_KEYS_FILE``, ``paths.GOOGLE_OAUTH_FILE`` …) is gated, in
   interactive *and* autonomous mode.
3. ``_dispatch_tool`` / ``_dispatch_sub_tool`` — a tool result that contains
   a credential is redacted before it reaches the log, the screen or the
   model.
"""
import builtins
import json
import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from core import confidential  # noqa: E402
from core import permissions  # noqa: E402


# ── redact_credentials ────────────────────────────────────────────────────────


def test_redacts_google_api_key():
    text = 'key = "AIzaSyDMJBcIk7Zjy3er3eEHWcZbZFQehvwi2OE"'
    out = confidential.redact_credentials(text)
    assert "AIzaSyDMJBcIk7Zjy3er3eEHWcZbZFQehvwi2OE" not in out
    assert "[REDACTED:Google API key]" in out


def test_redacts_openai_key():
    text = "sk-proj-abcdefghijklmnopqrstuvwxyz1234567890"
    out = confidential.redact_credentials(text)
    assert "sk-proj-abcdefghijklmnopqrstuvwxyz1234567890" not in out
    assert "[REDACTED:OpenAI/Anthropic key]" in out


def test_redacts_google_api_key():
    # AIza + exactly 35 chars = 39 total (the pattern uses {35})
    # Use a clearly-fake key that cannot match any real configured key.
    text = 'key = "AIzaFAKEKEYFAKEKEYFAKEKEYFAKEKEY1234567"'
    out = confidential.redact_credentials(text)
    assert "AIzaFAKEKEYFAKEKEYFAKEKEYFAKEKEY1234567" not in out
    assert "[REDACTED:Google API key]" in out


def test_redacts_pem_private_key():
    body1 = "aB3xK9mP2vQ5wR8tY1uN4jH6gF3dS7aW2eR5tY8uI9oP0lK"
    body2 = "X7mN2pQ9vR4tY6uI8oP0lKjH3gF5dS1aW4eR7tY9uI2oP6q"
    body = body1 + body2
    text = f"""-----BEGIN EC PRIVATE KEY-----
{body}
-----END EC PRIVATE KEY-----"""
    out = confidential.redact_credentials(text)
    assert body not in out
    assert "-----BEGIN EC PRIVATE KEY-----" not in out
    assert "[REDACTED:PEM private key]" in out


def test_redacts_jwt():
    text = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0In0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    out = confidential.redact_credentials(text)
    assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in out
    assert "[REDACTED:JWT]" in out


def test_redacts_bearer_token():
    text = 'Authorization: Bearer hf_abcdefghijklmnopqrstuvwxyz1234567890AB'
    out = confidential.redact_credentials(text)
    assert "hf_abcdefghijklmnopqrstuvwxyz1234567890AB" not in out
    # The bearer pattern matches the whole header, so it wins the overlap.
    assert "[REDACTED:bearer token]" in out


def test_redacts_assignment_value():
    # Use a fake key that's not a real configured key
    fake = 'AIzaFAKEKEYFAKEKEYFAKEKEYFAKEKEY1234567'
    text = f'GOOGLE_API_KEY = "{fake}"'
    out = confidential.redact_credentials(text)
    assert fake not in out
    # The name stays, the provider key is redacted.
    assert "GOOGLE_API_KEY" in out
    assert "[REDACTED:Google API key]" in out


def test_redacts_unlabelled_key_material_blob():
    # A file that is mostly a long random-looking run of characters.
    text = "k8sClusterConfig: " + "aB3xK9mP2vQ5wR8tY1uN4jH6gF3dS7aW2eR5tY8uI9oP0lK" * 3
    out = confidential.redact_credentials(text)
    assert "[REDACTED:key material]" in out


def test_leaves_benign_text_alone():
    text = "The quick brown fox jumps over the lazy door. Nothing secret here."
    out = confidential.redact_credentials(text)
    assert out == text


def test_leaves_placeholders_alone():
    text = 'api_key = "your-api-key-here"'
    out = confidential.redact_credentials(text)
    assert "your-api-key-here" in out


def test_redacts_nested_without_double_redaction():
    # A provider key that is also an assignment value — redacted once.
    text = 'OPENAI_API_KEY="sk-proj-abcdefghijklmnopqrstuvwxyz1234567890"'
    out = confidential.redact_credentials(text)
    assert "sk-proj-abcdefghijklmnopqrstuvwxyz1234567890" not in out
    assert "[REDACTED]" in out or "[REDACTED:OpenAI/Anthropic key]" in out
    # Only one redaction tag for this value.
    assert out.count("[REDACTED") == 1


# ── permission gate: credential constants ───────────────────────────────────


def test_segment_references_credential_constants():
    assert permissions._segment_references_credential_constants(
        'print(open(paths.API_KEYS_FILE).read())'
    )
    assert permissions._segment_references_credential_constants(
        "json.load(open(paths.GOOGLE_OAUTH_FILE))"
    )
    assert permissions._segment_references_credential_constants(
        "paths.GOOGLE_TOKENS_DIR"
    )
    # Bare words that are not credential constants.
    assert not permissions._segment_references_credential_constants(
        "print('hello world')"
    )
    assert not permissions._segment_references_credential_constants(
        "API_KEYS = 1"
    )


def test_command_needs_permission_blocks_inline_credential_constants(monkeypatch):
    # Non-autonomous: the segment-level check catches it.
    monkeypatch.setattr(permissions, "is_autonomous", lambda: False)
    assert permissions.command_needs_permission(
        'python -c "open(paths.API_KEYS_FILE).read()"'
    )

    # Autonomous: the whole-command check must catch it too.
    monkeypatch.setattr(permissions, "is_autonomous", lambda: True)
    assert permissions.command_needs_permission(
        'python -c "open(paths.API_KEYS_FILE).read()"'
    )
    assert permissions.command_needs_permission(
        'python -c "json.load(open(paths.GOOGLE_OAUTH_FILE))"'
    )
    # Benign inline code is still allowed in autonomous mode.
    assert not permissions.command_needs_permission('python -c "print(1 + 1)"')


def test_validate_command_blocks_inline_credential_constants(monkeypatch, capsys):
    # Never block on input() during tests.
    monkeypatch.setattr(builtins, "input", lambda prompt: "n")

    # Interactive mode: the user is asked (and denies).  The prompt must
    # name the credential constant.
    monkeypatch.setattr(permissions, "is_autonomous", lambda: False)
    approved, _ = permissions.validate_command(
        'python -c "open(paths.API_KEYS_FILE).read()"'
    )
    assert not approved
    captured = capsys.readouterr().out
    assert "credential path constant" in captured

    # Autonomous mode: refused outright, not asked.
    monkeypatch.setattr(permissions, "is_autonomous", lambda: True)
    approved, reason = permissions.validate_command(
        'python -c "open(paths.API_KEYS_FILE).read()"'
    )
    assert not approved
    assert "blocked in autonomous mode" in reason


# ── end-to-end: tool result redaction ───────────────────────────────────────


def test_dispatch_sub_tool_redacts_credential_result(monkeypatch):
    from core import tools

    captured = {}

    def fake_tool(**kwargs):
        captured["called"] = True
        return 'The key is AIzaFAKEKEYFAKEKEYFAKEKEYFAKEKEY1234567'

    monkeypatch.setitem(tools._SUB_TOOL_FUNCS, "fake_tool", fake_tool)
    try:
        result = tools._dispatch_sub_tool(
            "fake_tool", json.dumps({})
        )
        assert "AIzaFAKEKEYFAKEKEYFAKEKEYFAKEKEY1234567" not in result
        assert "[REDACTED:Google API key]" in result
    finally:
        del tools._SUB_TOOL_FUNCS["fake_tool"]


# ── addresses are not secrets (regression: workspace/problem.txt, 2026-11-16) ─
#
# The blob rules swallowed the devtools URL the browser tooling needs and
# every screenshot file name the browser tool prints.  A redaction that
# destroys an address protects nothing and breaks the workflow that produced
# the output, so the four address shapes are now exempt — and these tests
# pin them, together with the shapes that must KEEP being redacted.

def test_loopback_devtools_url_survives_redaction():
    text = ('"webSocketDebuggerUrl": '
            '"ws://127.0.0.1:36893/devtools/browser/3f9a2c64-8e41-4b7d-9c3a-1f2e5d6b7a08"')
    assert confidential.redact_credentials(text) == text


def test_loopback_devtools_page_url_survives_redaction():
    text = ('"webSocketDebuggerUrl": '
            '"ws://127.0.0.1:51095/devtools/page/8A1B2C3D4E5F6A7B8C9D0E1F2A3B4C5D"')
    assert confidential.redact_credentials(text) == text


def test_screenshot_file_name_survives_redaction():
    text = ('[Screenshot saved to /data/data/com.termux/files/home/Termux-AI/'
            'workspace/screenshots/b3_20261116_014057.jpg — attached]')
    assert confidential.redact_credentials(text) == text


def test_flag_assigned_absolute_path_survives_redaction():
    text = ('--user-data-dir=/data/data/com.termux/files/home/Termux-AI/'
            'workspace/.browser/session_36893_30544')
    assert confidential.redact_credentials(text) == text


def test_rooted_path_listing_survives_redaction():
    text = ('/data/data/com.termux/files/home/Termux-AI/workspace/'
            '.browser/session_51095_123/Default/Cookies')
    assert confidential.redact_credentials(text) == text


def test_remote_devtools_url_is_still_redacted():
    """Only loopback endpoints are addresses of this device; a devtools
    endpoint on another host is a remote capability and stays redacted."""
    text = 'ws://10.0.0.5:9222/devtools/browser/3f9a2c64-8e41-4b7d-9c3a-1f2e5d6b7a08'
    out = confidential.redact_credentials(text)
    assert "[REDACTED:key material]" in out
    assert "3f9a2c64" not in out


def test_key_inside_a_remote_url_is_still_redacted():
    text = ('https://api.example.com/v1?'
            'key=AbCdEfGhIjKlMnOpQrStUvWxYz0123456789abcd+/EfGhIjKlMnOp')
    out = confidential.redact_credentials(text)
    assert "[REDACTED" in out
    assert "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789abcd" not in out
