"""Regression tests for :mod:`core.shell_runner`.

The shell tool used to apply a single Python-level watchdog around the whole
command and only killed the immediate ``bash`` process, which left orphaned
children running after a timeout.  ``shell_runner`` now gives every stage of a
chain its own GNU ``timeout`` budget and guarantees that the whole process
tree is torn down.

These tests cover the behaviour that matters:

* per-stage timeouts (``a && b`` no longer shares one budget),
* complete process-tree termination (no orphans),
* preservation of shell semantics (builtins, quoting, heredocs, pipelines).
"""
import os
import signal
import sys
import time

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from core import shell_runner as sr  # noqa: E402

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="shell_runner drives bash"
)

MARKER = "shellrunnermark"
#: A command whose process would outlive the shell if the tree were not killed.
LEAKY = f'python3 -c "import time; time.sleep(300)  # {MARKER}"'


def _alive_leaked():
    """PIDs of surviving marker processes (the sleeper, not the shell wrapper)."""
    pids = []
    try:
        entries = os.listdir("/proc")
    except OSError:
        return pids
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/cmdline", "rb") as fh:
                cmd = fh.read().decode("utf-8", "replace").replace("\x00", " ").strip()
        except OSError:
            continue
        if MARKER not in cmd:
            continue
        first = cmd.split()[0] if cmd.split() else ""
        if os.path.basename(first) in ("bash", "sh", "timeout"):
            continue
        pids.append(int(entry))
    return pids


def _kill_leaked():
    for pid in _alive_leaked():
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


@pytest.fixture(autouse=True)
def _no_leftovers():
    _kill_leaked()
    yield
    _kill_leaked()


# ── basic execution ─────────────────────────────────────────────────────────

def test_simple_command_runs():
    res = sr.run("echo hello-shell-runner", timeout=10)
    assert res.returncode == 0
    assert "hello-shell-runner" in res.stdout
    assert not res.timed_out
    assert not res.stage_timed_out
    assert not res.error


def test_exit_code_is_reported():
    res = sr.run("exit 7", timeout=10)
    assert res.returncode == 7
    assert "[EXIT CODE 7]" in sr.describe(res, 10)


def test_stderr_is_captured():
    res = sr.run("echo oops >&2", timeout=10)
    assert "oops" in res.stderr
    assert "[STDERR]" in sr.describe(res, 10)


# ── per-stage timeouts ──────────────────────────────────────────────────────

def test_hanging_stage_times_out():
    t0 = time.monotonic()
    res = sr.run("echo stage1 && sleep 300", timeout=1)
    dur = time.monotonic() - t0
    assert res.stage_timed_out
    assert "[TIMEOUT]" in sr.describe(res, 1)
    assert "stage1" in res.stdout
    assert dur < 45, f"took {dur:.1f}s"


@pytest.mark.parametrize("chain", [
    "sleep 300 || echo recovered",
    "sleep 300 ; echo after",
    "sleep 300 | cat",
    "sleep 300 | cat | wc -l",
    "echo before && sleep 300",
])
def test_a_masked_timeout_is_still_reported(chain):
    """``a || b`` / ``a ; b`` / ``a | b`` must not swallow the timeout."""
    res = sr.run(chain, timeout=1)
    assert res.stage_timed_out, "the timeout was masked by the chain operator"
    assert res.returncode == sr.TIMEOUT_EXIT_CODE
    assert "[TIMEOUT]" in sr.describe(res, 1)


def test_each_stage_gets_its_own_budget():
    """``a && b`` must not share a single budget: the first stage may take time."""
    res = sr.run("sleep 2 && echo second-stage-ran", timeout=5)
    assert not res.timed_out
    assert "second-stage-ran" in res.stdout


def test_timeout_kills_the_whole_process_tree():
    """The sleeper must not survive the timeout (the original orphan bug)."""
    res = sr.run(LEAKY, timeout=1)
    assert res.stage_timed_out
    time.sleep(0.5)
    assert _alive_leaked() == [], "a grandchild survived the timeout"


def test_detached_background_job_is_killed():
    res = sr.run(f"nohup {LEAKY} >/dev/null 2>&1 & sleep 300", timeout=1)
    assert res.stage_timed_out
    time.sleep(0.6)
    assert _alive_leaked() == [], "a detached background job survived the timeout"


def test_a_hanging_grandchild_cannot_block_the_call():
    res = sr.run(
        "python3 -c \"import subprocess,time; "
        f"subprocess.Popen(['python3','-c','import time; time.sleep(300)  # {MARKER}']); "
        'time.sleep(300)\"',
        timeout=1,
    )
    assert res.stage_timed_out
    time.sleep(0.5)
    assert _alive_leaked() == []


# ── shell semantics must survive the rewrite ────────────────────────────────

def test_builtin_cd_still_affects_later_stages():
    """``cd`` is a builtin: it must not be wrapped in ``timeout``."""
    res = sr.run("cd /tmp && pwd", timeout=10)
    assert not res.timed_out
    assert res.stdout.strip().splitlines()[-1] == "/tmp"


def test_pipeline_is_preserved():
    res = sr.run("printf 'a\\nb\\nc\\n' | grep -c b", timeout=10)
    assert res.returncode == 0
    assert res.stdout.strip() == "1"


def test_heredoc_is_not_torn_apart():
    res = sr.run("cat <<'EOF'\nline-one\nline-two\nEOF", timeout=10)
    assert "line-one" in res.stdout and "line-two" in res.stdout


def test_quoted_operators_do_not_split():
    res = sr.run("echo 'a && b'", timeout=10)
    assert "a && b" in res.stdout


def test_function_definition_is_left_alone():
    res = sr.run("greet() { echo hi-from-function; }; greet", timeout=10)
    assert "hi-from-function" in res.stdout


# ── unit level: the splitter and the rewriter ──────────────────────────────

def test_split_chain_respects_quotes():
    stages = sr.split_chain("echo 'a && b' && echo c")
    assert [text for text, _ in stages] == ["echo 'a && b'", "echo c"]


def test_split_chain_keeps_compound_blocks_intact():
    stages = sr.split_chain("for i in 1 2; do echo $i; done && echo done")
    assert len(stages) == 2
    assert "for i in 1 2; do echo $i; done" == stages[0][0]


def test_split_chain_keeps_heredocs_intact():
    cmd = "cat <<'EOF'\nhello && world\nEOF"
    stages = sr.split_chain(cmd)
    assert len(stages) == 1
    assert stages[0][0] == cmd


def test_split_chain_keeps_background_job_with_its_stage():
    """``&`` terminates a command, so the backgrounded job is its own stage."""
    stages = sr.split_chain("sleep 1 & echo after")
    assert [text for text, _ in stages] == ["sleep 1", "echo after"]
    assert stages[0][1] == "&"


@pytest.mark.skipif(sr.timeout_binary() is None, reason="GNU timeout not available")
def test_build_timed_command_wraps_external_commands_only():
    effective, count, wrapped = sr.build_timed_command("cd /tmp && /bin/cat /etc/hostname", 5)
    assert count == 2
    assert wrapped == 1, "the `cd` builtin must not be wrapped"
    assert "timeout" in effective
    assert "cd /tmp" in effective


@pytest.mark.skipif(sr.timeout_binary() is None, reason="GNU timeout not available")
def test_build_timed_command_leaves_function_definitions_alone():
    effective, count, wrapped = sr.build_timed_command("f() { echo x; }; f", 5)
    assert wrapped == 0
    assert effective == "f() { echo x; }; f"


def test_describe_reports_the_timeout():
    res = sr.run("sleep 300", timeout=1)
    text = sr.describe(res, 1)
    assert "[TIMEOUT]" in text
    assert "[EXIT CODE 124]" in text
