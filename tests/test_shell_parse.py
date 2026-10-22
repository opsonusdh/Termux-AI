"""Regression tests for :mod:`core.shell_parse`.

The permission gate used to split a command by cutting it at every ``;``,
``&&``, ``||`` and newline.  A compound block contains semicolons of its own,
so the cut landed inside the block and the segment's command word was a shell
keyword (``do``, ``then``, …) instead of the command that actually runs — the
destructive command inside the block was never inspected.

``core.shell_parse`` parses the command structurally and returns the text of
every simple command, including the ones nested inside compound blocks and
command substitutions.
"""
import os
import sys
import time

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from core import shell_parse  # noqa: E402


def cmds(text):
    return shell_parse.simple_commands(text)


# ── flat chains ──────────────────────────────────────────────────────────────

def test_a_plain_command_is_one_simple_command():
    assert cmds("echo hi") == ["echo hi"]


def test_and_or_chain_is_split():
    assert cmds("echo a && rm -rf /tmp/z") == ["echo a", "rm -rf /tmp/z"]


def test_pipeline_is_split():
    assert cmds("cat f | grep x | wc -l") == ["cat f", "grep x", "wc -l"]


def test_background_operator_splits():
    assert cmds("sleep 1 & rm -rf /tmp/bg") == ["sleep 1", "rm -rf /tmp/bg"]


# ── compound blocks: the actual bug ─────────────────────────────────────────

def test_for_loop_body_is_found():
    """The loop header is not a command, the body is."""
    assert cmds("for i in 1 2 3; do rm -rf /tmp/x; done") == ["rm -rf /tmp/x"]


def test_if_body_is_found():
    found = cmds("if true; then rm -rf /tmp/y; fi")
    assert "rm -rf /tmp/y" in found


def test_elif_and_else_bodies_are_found():
    found = cmds("if a; then echo x; elif b; then rm -rf /tmp/q; else echo z; fi")
    assert "rm -rf /tmp/q" in found
    assert "echo z" in found


def test_while_and_until_bodies_are_found():
    assert "rm -rf /tmp/w" in cmds("while read l; do rm -rf /tmp/w; done < list")
    assert "rm -rf /tmp/u" in cmds("until false; do rm -rf /tmp/u; done")


def test_case_arm_is_found():
    found = cmds("case $x in a) rm -rf /tmp/c ;; b) echo b ;; esac")
    assert found == ["rm -rf /tmp/c", "echo b"]


def test_brace_group_is_found():
    assert "rm -rf /tmp/g" in cmds("{ rm -rf /tmp/g; }")


def test_subshell_is_found():
    assert "rm -rf /tmp/s" in cmds("( rm -rf /tmp/s )")


def test_function_body_is_found():
    assert "echo hi" in cmds("greet() { echo hi; }; greet")


def test_nested_blocks_are_found():
    cmd = "for i in 1 2; do if [ -f x ]; then rm -rf /tmp/n; fi; done"
    assert "rm -rf /tmp/n" in cmds(cmd)


# ── command substitution ─────────────────────────────────────────────────────

def test_command_substitution_is_parsed():
    found = cmds("echo $(rm -rf /tmp/sub)")
    assert "rm -rf /tmp/sub" in found


def test_backtick_substitution_is_parsed():
    found = cmds("echo `rm -rf /tmp/bt`")
    assert "rm -rf /tmp/bt" in found


# ── quoting must not be broken ───────────────────────────────────────────────

def test_quoted_operators_are_not_split():
    assert cmds("echo 'a && b'") == ["echo 'a && b'"]
    assert cmds('echo "a ; b"') == ['echo "a ; b"']


def test_a_keyword_as_an_argument_stays_an_argument():
    """``echo done`` must not lose its argument."""
    assert cmds("echo done") == ["echo done"]


# ── redirections ─────────────────────────────────────────────────────────────

def test_redirection_target_belongs_to_the_command():
    assert cmds("echo hi > out.txt") == ["echo hi > out.txt"]


def test_redirection_operator_with_attached_target():
    assert cmds("echo hi >out.txt") == ["echo hi >out.txt"]


def test_file_descriptor_redirection():
    assert cmds("echo hi 2>&1") == ["echo hi 2>&1"]


def test_a_lone_redirection_target_is_not_a_command():
    """The ``< file`` of a while loop must not look like a command."""
    found = cmds("while read l; do echo x; done < list")
    assert "list" not in found


# ── robustness ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "",
    "   ",
    "echo 'unterminated",
    'echo "unterminated',
    "for i in; do; done",
    "if; then; fi",
    "case x in; esac",
    "$((",
    "`",
    ";;;",
    "&&",
])
def test_pathological_input_does_not_raise(text):
    assert isinstance(cmds(text), list)


def test_the_fallback_is_never_less_strict_than_a_textual_split():
    """Even a construct the parser cannot handle is still split."""
    text = "echo a; rm -rf /tmp/q"
    assert len(cmds(text)) >= 1


# ── the parser must not become a denial-of-service vector ───────────────────

def test_deeply_nested_substitutions_stay_fast():
    """Every nesting level re-parses the text inside it.

    Without a depth limit this was quadratic in the length of the command,
    which would let a single long command stall the permission gate.
    """
    depth = 400
    text = "$( " * depth + "rm -rf /tmp/x" + " )" * depth
    start = time.monotonic()
    out = cmds(text)
    elapsed = time.monotonic() - start
    assert isinstance(out, list)
    assert elapsed < 5.0, f"took {elapsed:.1f}s"


def test_many_substitutions_stay_fast():
    text = "echo $(rm -rf /tmp/y) " * 400
    start = time.monotonic()
    cmds(text)
    assert time.monotonic() - start < 5.0


def test_the_substitution_depth_limit_is_respected():
    assert shell_parse.MAX_SUBSTITUTION_DEPTH > 0
    # Beyond the limit the text is still split, just without the structural
    # analysis — so the gate is never *less* strict.
    depth = shell_parse.MAX_SUBSTITUTION_DEPTH + 5
    text = "$( echo a; rm -rf /tmp/q )" * 1
    text = "$( " * depth + "echo a; rm -rf /tmp/q" + " )" * depth
    out = cmds(text)
    assert any("rm -rf /tmp/q" in seg for seg in out)
