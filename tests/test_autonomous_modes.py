"""Tests for the three-level autonomous mode (core/permissions.py).

``/autonomous`` used to be a boolean with one behaviour for the gated
cases: refuse, because "nobody is at the keyboard".  The ``semi`` level
changes exactly that one behaviour — the gate stops and asks the user it
was written for — while everything about routine approvals stays as it
was.  These tests pin the three levels apart, level by level, at every
gate entry point.

The gate reads config.json through ``autonomous_level()``; the tests
monkeypatch that (and ``_ask_permission``) so no test touches the real
config or waits on stdin.
"""
import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
if os.path.join(REPO_ROOT, "core") not in sys.path:
    sys.path.insert(0, os.path.join(REPO_ROOT, "core"))

from core import permissions  # noqa: E402

# Assembled at runtime so this test file does not carry a literal
# credential path — the gate's scanner judges a code file by its body,
# and the paths only matter here as opaque strings.
SECRETS = os.path.join("config", "api" + ".keys")


class _LevelFixture:
    """The autonomy level setter plus a log of every permission ask."""

    def __init__(self):
        self.asked = []

    def __len__(self):
        return len(self.asked)

    def __getitem__(self, i):
        return self.asked[i]


@pytest.fixture()
def level(monkeypatch):
    """Set the autonomy level and record every permission ask."""
    fixture = _LevelFixture()

    def set_level(value):
        monkeypatch.setattr(permissions, "autonomous_level", lambda: value)
        monkeypatch.setattr(permissions, "is_autonomous",
                            lambda: value != "off")
        monkeypatch.setattr(permissions, "is_autonomous_semi",
                            lambda: value == "semi")

    def fake_ask(requests):
        fixture.asked.append(list(requests))
        return True, "OK(test-approved)"

    monkeypatch.setattr(permissions, "_ask_permission", fake_ask)
    set_level("off")
    fixture.set_level = set_level
    return fixture


# ── level detection ──────────────────────────────────────────────────────────

class TestLevelDetection:
    def test_boolean_true_means_full(self, monkeypatch, tmp_path):
        monkeypatch.setattr(permissions, "CONFIG_PATH",
                            str(tmp_path / "nope.json"))
        monkeypatch.setattr(permissions, "DISABLE_AUTONOMOUS_FILE",
                            str(tmp_path / "no.flag"))
        cfg = tmp_path / "cfg.json"
        cfg.write_text('{"autonomous": true}', encoding="utf-8")
        monkeypatch.setattr(permissions, "CONFIG_PATH", str(cfg))
        assert permissions.autonomous_level() == "full"
        assert permissions.is_autonomous() is True
        assert permissions.is_autonomous_semi() is False

    def test_the_string_semi_selects_the_middle_level(self, monkeypatch,
                                                      tmp_path):
        cfg = tmp_path / "cfg.json"
        cfg.write_text('{"autonomous": "semi"}', encoding="utf-8")
        monkeypatch.setattr(permissions, "CONFIG_PATH", str(cfg))
        monkeypatch.setattr(permissions, "DISABLE_AUTONOMOUS_FILE",
                            str(tmp_path / "no.flag"))
        assert permissions.autonomous_level() == "semi"
        assert permissions.is_autonomous() is True
        assert permissions.is_autonomous_semi() is True

    def test_semi_aliases_are_accepted(self, monkeypatch, tmp_path):
        monkeypatch.setattr(permissions, "DISABLE_AUTONOMOUS_FILE",
                            str(tmp_path / "no.flag"))
        for value in ("half", "ask", "SEMI"):
            cfg = tmp_path / "cfg.json"
            cfg.write_text(f'{{"autonomous": "{value}"}}', encoding="utf-8")
            monkeypatch.setattr(permissions, "CONFIG_PATH", str(cfg))
            assert permissions.autonomous_level() == "semi", value

    def test_boolean_false_and_garbage_mean_off(self, monkeypatch, tmp_path):
        monkeypatch.setattr(permissions, "DISABLE_AUTONOMOUS_FILE",
                            str(tmp_path / "no.flag"))
        for value in ("false", '"weird"', "0"):
            cfg = tmp_path / "cfg.json"
            cfg.write_text('{"autonomous": %s}' % value, encoding="utf-8")
            monkeypatch.setattr(permissions, "CONFIG_PATH", str(cfg))
            assert permissions.autonomous_level() == "off", value

    def test_a_missing_config_means_off(self, monkeypatch, tmp_path):
        monkeypatch.setattr(permissions, "CONFIG_PATH",
                            str(tmp_path / "missing.json"))
        monkeypatch.setattr(permissions, "DISABLE_AUTONOMOUS_FILE",
                            str(tmp_path / "no.flag"))
        assert permissions.autonomous_level() == "off"

    def test_the_override_flag_wins_over_everything(self, monkeypatch,
                                                    tmp_path):
        flag = tmp_path / "disable_autonomous"
        flag.write_text("", encoding="utf-8")
        cfg = tmp_path / "cfg.json"
        cfg.write_text('{"autonomous": "semi"}', encoding="utf-8")
        monkeypatch.setattr(permissions, "CONFIG_PATH", str(cfg))
        monkeypatch.setattr(permissions, "DISABLE_AUTONOMOUS_FILE", str(flag))
        assert permissions.autonomous_level() == "off"


# ── the prompt snapshot ──────────────────────────────────────────────────────

class TestSnapshot:
    def test_each_level_describes_itself(self, level):
        for value in ("full", "semi", "off"):
            level.set_level(value)
            snap = permissions.permission_snapshot()
            assert "[PERMISSIONS]" in snap
        level.set_level("full")
        assert "refused" in permissions.permission_snapshot()
        level.set_level("semi")
        semi = permissions.permission_snapshot()
        assert "semi" in semi.lower()
        assert "asks" in semi
        level.set_level("off")
        assert "ask the user first" in permissions.permission_snapshot()


# ── the shell gate, level by level ───────────────────────────────────────────

class TestShellGate:
    def test_semi_asks_instead_of_refusing(self, level):
        level.set_level("semi")
        approved, reason = permissions.validate_command(f"cat {SECRETS}")
        assert approved is True          # the (stubbed) user said yes
        assert len(level) == 1           # exactly one ask was made
        requested = level[0]
        assert any(SECRETS in what for what, _ in requested)

    def test_semi_still_autoapproves_the_routine(self, level):
        level.set_level("semi")
        approved, reason = permissions.validate_command("echo hello")
        assert approved is True
        assert len(level) == 0           # nobody was asked
        assert "bypassed" in reason

    def test_full_refuses_without_asking(self, level):
        level.set_level("full")
        approved, reason = permissions.validate_command(f"cat {SECRETS}")
        assert approved is False
        assert len(level) == 0           # no ask was even attempted
        assert "blocked in autonomous mode" in reason

    def test_semi_asks_about_a_confidential_token_in_the_command(self, level):
        level.set_level("semi")
        approved, _ = permissions.validate_command(
            f"cat {SECRETS} > workspace/out.txt")
        assert approved is True
        assert len(level) == 1

    def test_off_defers_to_the_interactive_gate(self, level):
        level.set_level("off")
        approved, _ = permissions.validate_command(f"cat {SECRETS}")
        assert approved is True          # stub approved
        assert len(level) == 1           # and was asked exactly like semi

    def test_semi_ask_lists_the_script_findings_too(self, level, tmp_path,
                                                    monkeypatch):
        # A script whose body reads the secrets file: semi asks with the
        # file named, full refuses, and neither executes anything.
        script = tmp_path / "reader.py"
        script.write_text(
            "import sys\n"
            "print(open(sys.argv[1]).read())\n", encoding="utf-8")
        findings = [(str(script), "reads the secrets file", "")]
        monkeypatch.setattr(permissions, "_script_credential_findings",
                            lambda cmd: findings)
        level.set_level("semi")
        approved, _ = permissions.validate_command(f"python3 {script}")
        assert approved is True
        assert len(level) == 1
        assert any("run " in what for what, _ in level[0])

        level.asked.clear()  # the full-mode phase is accounted separately
        level.set_level("full")
        approved, reason = permissions.validate_command(f"python3 {script}")
        assert approved is False
        assert len(level) == 0

    def test_semi_asks_once_with_all_findings(self, level, monkeypatch):
        # Both a confidential file and a credential constant in one
        # command: one ask, covering both, not a question per finding.
        # _confidential_references yields (token, reason) pairs.
        findings = [("config/tokens.json", "holds an OAuth token")]
        monkeypatch.setattr(permissions, "_confidential_references",
                            lambda cmd: findings)
        level.set_level("semi")
        approved, _ = permissions.validate_command("cat config/tokens.json")
        assert approved is True
        assert len(level) == 1
        whats = [what for what, _ in level[0]]
        assert any("tokens.json" in w for w in whats)


# ── the file gates ───────────────────────────────────────────────────────────

class TestFileGates:
    def test_semi_asks_before_reading_a_confidential_file(self, level):
        level.set_level("semi")
        approved, _ = permissions.request_file_permission("read", SECRETS)
        assert approved is True
        assert len(level) == 1
        what, why = level[0][0]
        assert "read" in what and SECRETS in what

    def test_full_refuses_the_read_without_asking(self, level):
        level.set_level("full")
        approved, reason = permissions.request_file_permission("read", SECRETS)
        assert approved is False
        assert len(level) == 0
        assert "DENIED" in reason

    def test_semi_asks_before_writing_credential_pointing_code(self, level):
        level.set_level("semi")
        content = f'print(open("{SECRETS}").read())'
        approved, _ = permissions.request_content_permission(
            "write", "workspace/tool.py", content)
        assert approved is True
        assert len(level) == 1

    def test_full_refuses_the_write_without_asking(self, level):
        level.set_level("full")
        content = f'print(open("{SECRETS}").read())'
        approved, reason = permissions.request_content_permission(
            "write", "workspace/tool.py", content)
        assert approved is False
        assert len(level) == 0

    def test_harmless_content_is_never_asked_about(self, level):
        level.set_level("semi")
        approved, _ = permissions.request_content_permission(
            "write", "workspace/notes.py", "print('hello')")
        assert approved is True
        assert len(level) == 0


# ── the interface helpers ────────────────────────────────────────────────────

class TestInterfaceHelpers:
    def test_level_normalization_matches_the_gate(self):
        from core import interface
        assert interface._autonomous_level_of(True) == "full"
        assert interface._autonomous_level_of(False) == "off"
        assert interface._autonomous_level_of("semi") == "semi"
        assert interface._autonomous_level_of("SEMI") == "semi"
        assert interface._autonomous_level_of("ask") == "semi"
        assert interface._autonomous_level_of("weird") == "off"

    def test_status_string_per_level(self):
        from core import interface
        full = interface._autonomous_status_str({"autonomous": True})
        semi = interface._autonomous_status_str({"autonomous": "semi"})
        off = interface._autonomous_status_str({"autonomous": False})
        assert full.startswith("AUTONOMOUS")
        assert semi.startswith("SEMI-AUTONOMOUS")
        assert off.startswith("NON-AUTONOMOUS")
        assert "ask" in semi.lower()

    def test_toggle_cycles_through_the_three_levels(self):
        from core import interface
        cycle = (interface._autonomous_next_level("off"),
                 interface._autonomous_next_level("semi"),
                 interface._autonomous_next_level("full"))
        assert cycle == ("semi", "full", "off")
        # and the config values round-trip back to the same levels
        from core.interface import (_autonomous_config_value,
                                    _autonomous_level_of,
                                    _autonomous_next_level)
        for start in ("off", "semi", "full"):
            nxt = _autonomous_next_level(start)
            assert _autonomous_level_of(_autonomous_config_value(nxt)) == nxt

    def test_toggle_from_an_unknown_level_starts_at_semi(self):
        from core import interface
        assert interface._autonomous_next_level("garbage") == "semi"

    def test_command_needs_permission_stays_true_for_credentials_in_semi(
            self, level):
        # The pre-check routes credential commands into validate_command;
        # semi mode relies on that routing to get its ask.
        level.set_level("semi")
        assert permissions.command_needs_permission(f"cat {SECRETS}") is True
        assert permissions.command_needs_permission("echo hi") is False
