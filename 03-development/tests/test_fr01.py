"""FR-01: 任務提交與驗證 — Task submission and validation.

Test cases correspond 1:1 to TEST_SPEC.md §FR-01 (rows 1–7). The function
names below are the canonical names `spec-coverage-check` looks up — do
NOT rename. Validation rules per SPEC.md §3 FR-01:

    non-empty           empty / whitespace-only command -> reject
    length              command > 1000 chars           -> reject
    injection chars     any of ; | & $ > < `            -> reject
    name uniqueness     --name collides with pending    -> reject
    dependency exists   --after id unknown              -> reject

On success a uuid4 (first 8 hex chars) is emitted to stdout and the task
is persisted atomically to `$TASKQ_HOME/tasks.json`.

In-process and subprocess tests coexist: subprocess tests exercise the
real CLI entry point (`python -m taskq_plus`); in-process tests import
`taskq_plus.cli.commands` so coverage tooling can measure the validation
paths directly.
"""
from __future__ import annotations

import contextlib
import io
import re
import subprocess
import sys

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# 8-hex-char id pattern (uuid4 without dashes, first 8 chars).
TASK_ID_RE = re.compile(r"^[0-9a-f]{8}$")


def _run_submit_subprocess(args: list, env: dict) -> subprocess.CompletedProcess:
    """Invoke `python -m taskq_plus submit ...` in a child process.

    Decision: out-of-process for AC-01-1/2/3/4/5 — the canonical SPEC.md §8
    rows spell out `python -m taskq_plus` literally, so the test must
    reproduce the user-facing entry point to be valid. The in-process
    counterparts below add coverage of the same validation paths against
    the declared SAB module so pytest-cov can measure them.
    """
    return subprocess.run(
        [sys.executable, "-m", "taskq_plus", "submit", *args],
        capture_output=True,
        text=True,
        env=env,
    )


def _run_submit_inprocess(args: list) -> tuple:
    """Invoke the `submit` command in-process via the declared SAB module.

    Returns (exit_code, stdout). Stderr is captured so the test can
    inspect the validation message; the GREEN agent must expose
    `taskq_plus.cli.commands.submit(argv: list[str]) -> int` (or equivalent)
    that prints the new id on stdout and returns 0 on success / 2 on
    validation failure. Side-effects: writes to $TASKQ_HOME/tasks.json.
    """
    # GREEN TODO: taskq_plus.cli.commands must have a callable that
    # accepts argv (excluding the leading "submit" token) and returns
    # an integer exit code — for example:
    #     def submit(argv: list[str]) -> int: ...
    # Validation failures must print to stderr and return 2.
    from taskq_plus.cli import commands

    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    with contextlib.redirect_stdout(stdout_buf), contextlib.redirect_stderr(stderr_buf):
        exit_code = commands.submit(list(args))
    return exit_code, stdout_buf.getvalue(), stderr_buf.getvalue()


# ---------------------------------------------------------------------------
# Case 1 — AC-01-1: valid command -> exit 0, stdout is 8-hex id
# ---------------------------------------------------------------------------


def test_fr01_submit_valid_command(taskq_home, child_env):
    """AC-01-1: `python -m taskq_plus submit "echo hi"` -> exit 0;
    stdout is an 8-hex task id. *(SPEC §8 #4)*
    """
    # Subprocess: real entry point.
    proc = _run_submit_subprocess(["echo hi"], child_env)
    assert proc.returncode == 0, (
        f"expected exit 0, got {proc.returncode}; stderr={proc.stderr!r}"
    )
    assert TASK_ID_RE.match(proc.stdout.strip()), (
        f"stdout {proc.stdout!r} is not an 8-hex id"
    )

    # In-process: same validation path through the declared SAB module
    # (necessary for pytest-cov to report coverage of the validation
    # function — subprocess code shows 0% coverage by design).
    exit_code, stdout_value, stderr_value = _run_submit_inprocess(["echo hi"])
    assert exit_code == 0, (
        f"in-process exit was {exit_code}; stderr={stderr_value!r}"
    )
    assert TASK_ID_RE.match(stdout_value.strip()), (
        f"in-process stdout {stdout_value!r} is not an 8-hex id"
    )


# ---------------------------------------------------------------------------
# Case 2 — AC-01-2: empty command -> exit 2
# ---------------------------------------------------------------------------


def test_fr01_submit_empty_command_rejected(taskq_home, child_env):
    """AC-01-2: `python -m taskq_plus submit ""` -> exit 2. *(SPEC §8 #5)*
    """
    proc = _run_submit_subprocess([""], child_env)
    assert proc.returncode == 2, (
        f"expected exit 2, got {proc.returncode}; stderr={proc.stderr!r}"
    )

    # In-process: same empty-rejection path.
    exit_code, _stdout_value, stderr_value = _run_submit_inprocess([""])
    assert exit_code == 2, (
        f"in-process exit was {exit_code}; stderr={stderr_value!r}"
    )


# ---------------------------------------------------------------------------
# Case 3 — AC-01-3: injection character -> exit 2
# ---------------------------------------------------------------------------


def test_fr01_submit_injection_command_rejected(taskq_home, child_env):
    """AC-01-3: `python -m taskq_plus submit "echo hi; rm x"` -> exit 2
    (injection character rejected). *(SPEC §8 #6)*
    """
    proc = _run_submit_subprocess(["echo hi; rm x"], child_env)
    assert proc.returncode == 2, (
        f"expected exit 2, got {proc.returncode}; stderr={proc.stderr!r}"
    )

    # In-process: the same semicolon-rejection path through the
    # declared SAB module.
    exit_code, _stdout_value, stderr_value = _run_submit_inprocess(
        ["echo hi; rm x"]
    )
    assert exit_code == 2, (
        f"in-process exit was {exit_code}; stderr={stderr_value!r}"
    )


# ---------------------------------------------------------------------------
# Case 4 — AC-01-4: duplicate --name while first is pending -> second exits 2
# ---------------------------------------------------------------------------


def test_fr01_submit_duplicate_name_rejected(taskq_home, child_env):
    """AC-01-4: two `submit` calls with the same `--name` while the first
    remains `pending` -> the second exits 2.
    *(SPEC §3 FR-01 name-uniqueness rule + §7)*
    """
    first = _run_submit_subprocess(["echo a", "--name", "dup"], child_env)
    assert first.returncode == 0, (
        f"first submit must succeed; got {first.returncode}, "
        f"stderr={first.stderr!r}"
    )

    second = _run_submit_subprocess(["echo b", "--name", "dup"], child_env)
    assert second.returncode == 2, (
        f"second submit (dup name) must exit 2; got {second.returncode}, "
        f"stderr={second.stderr!r}"
    )

    # In-process: same precondition + same rejection.
    exit_code_a, _stdout_a, stderr_a = _run_submit_inprocess(
        ["echo a", "--name", "dup"]
    )
    assert exit_code_a == 0, (
        f"in-process first submit must succeed; stderr={stderr_a!r}"
    )
    exit_code_b, _stdout_b, stderr_b = _run_submit_inprocess(
        ["echo b", "--name", "dup"]
    )
    assert exit_code_b == 2, (
        f"in-process second submit (dup name) must exit 2; "
        f"stderr={stderr_b!r}"
    )


# ---------------------------------------------------------------------------
# Case 5 — AC-01-5: unknown --after id -> exit 2 + stderr marker
# ---------------------------------------------------------------------------


def test_fr01_submit_unknown_dependency_rejected(taskq_home, child_env):
    """AC-01-5: `submit --after <unknown-id>` -> exit 2 with stderr
    `unknown dependency: <id>`. *(SPEC §3 FR-01 + §7)*
    """
    unknown_id = "deadbeef"
    proc = _run_submit_subprocess(
        ["echo b", "--after", unknown_id], child_env
    )
    assert proc.returncode == 2, (
        f"expected exit 2, got {proc.returncode}; stderr={proc.stderr!r}"
    )
    assert "unknown dependency" in proc.stderr, (
        f"stderr must contain 'unknown dependency'; got {proc.stderr!r}"
    )
    assert unknown_id in proc.stderr, (
        f"stderr must echo the unknown id {unknown_id!r}; got {proc.stderr!r}"
    )

    # In-process: same dependency-existence rule.
    exit_code, _stdout_value, stderr_value = _run_submit_inprocess(
        ["echo b", "--after", unknown_id]
    )
    assert exit_code == 2, (
        f"in-process exit was {exit_code}; stderr={stderr_value!r}"
    )
    assert "unknown dependency" in stderr_value, (
        f"in-process stderr must contain 'unknown dependency'; "
        f"got {stderr_value!r}"
    )
    assert unknown_id in stderr_value, (
        f"in-process stderr must echo {unknown_id!r}; "
        f"got {stderr_value!r}"
    )


# ---------------------------------------------------------------------------
# Case 6 — Boundary: command at length cap (1000) -> accepted
# ---------------------------------------------------------------------------


def test_fr01_submit_command_at_length_limit_accepted(taskq_home, child_env):
    """Boundary: command is exactly 1000 chars ("echo " + 995 'x') -> exit 0.

    The length rule is *inclusive* on the cap: `command > 1000` is the
    rejection condition, so 1000 must be accepted. *(SPEC §3 FR-01
    length rule)*
    """
    # "echo " is 5 chars; 995 trailing 'x' makes 1000 total.
    assert len("echo " + ("x" * 995)) == 1000
    cmd = "echo " + ("x" * 995)

    proc = _run_submit_subprocess([cmd], child_env)
    assert proc.returncode == 0, (
        f"expected exit 0 at 1000 chars, got {proc.returncode}; "
        f"stderr={proc.stderr!r}"
    )
    assert TASK_ID_RE.match(proc.stdout.strip()), (
        f"stdout {proc.stdout!r} is not an 8-hex id"
    )

    # In-process: same boundary.
    exit_code, stdout_value, stderr_value = _run_submit_inprocess([cmd])
    assert exit_code == 0, (
        f"in-process exit at 1000 chars was {exit_code}; "
        f"stderr={stderr_value!r}"
    )
    assert TASK_ID_RE.match(stdout_value.strip()), (
        f"in-process stdout {stdout_value!r} is not an 8-hex id"
    )


# ---------------------------------------------------------------------------
# Case 7 — Boundary: command over length cap (1001) -> rejected
# ---------------------------------------------------------------------------


def test_fr01_submit_command_over_length_limit_rejected(taskq_home, child_env):
    """Boundary: command is 1001 chars ("echo " + 996 'x') -> exit 2.

    *(SPEC §3 FR-01 length rule)*
    """
    assert len("echo " + ("x" * 996)) == 1001
    cmd = "echo " + ("x" * 996)

    proc = _run_submit_subprocess([cmd], child_env)
    assert proc.returncode == 2, (
        f"expected exit 2 at 1001 chars, got {proc.returncode}; "
        f"stderr={proc.stderr!r}"
    )

    # In-process: same boundary.
    exit_code, _stdout_value, stderr_value = _run_submit_inprocess([cmd])
    assert exit_code == 2, (
        f"in-process exit at 1001 chars was {exit_code}; "
        f"stderr={stderr_value!r}"
    )
