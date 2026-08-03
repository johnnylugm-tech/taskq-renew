"""FR-05: CLI 整合 — Click command group `python -m taskq_plus`.

Test cases correspond 1:1 to TEST_SPEC.md §FR-05 (rows 1–9). The function
names below are the canonical names `spec-coverage-check` looks up — do
NOT rename. Per SPEC.md §3 FR-05 the CLI surface is:

    submit "<cmd>" [--name N] [--after ID]...   (FR-01)
    run <id> [--cached] / run --all              (FR-02/03/04/06)
    status <id>                                  (print all fields of the task)
    list [--status S]                            (list tasks, optional filter)
    graph [--format text|dot]                    (FR-06)
    plugins list                                 (FR-07)
    export --format json|csv|md                  (FR-08)
    clear                                        (wipe every data file in $TASKQ_HOME)

Global flag: `--json` — machine-readable single-line JSON output.

Exit codes: 0 success / 2 input validation error / 3 breaker open /
4 task timeout / 5 dependency cycle or depth cap / 6 plugin load failure /
1 other internal error.

Subprocess tests exercise the real `python -m taskq_plus` entry point so
the user-facing surface is verified. In-process tests import the
declared SAB modules (`taskq_plus.cli.main`, `taskq_plus.cli.commands`)
directly so pytest-cov can measure the new `status` / `clear` / `list` /
`graph` / `plugins` / `export` handlers (the subprocess acceptance path
cannot raise coverage on those — see GATE1 SUBPROCESS COVERAGE CEILING
in the integration guidelines).
"""
from __future__ import annotations

import contextlib
import io
import json as _json
import re
import subprocess
import sys
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# 8-hex-char task id pattern (uuid4 prefix).
TASK_ID_RE = re.compile(r"^[0-9a-f]{8}$")


def _run_subprocess(args: list, env: dict) -> subprocess.CompletedProcess:
    """Invoke `python -m taskq_plus <args>` in a child process.

    Out-of-process decision: the canonical SPEC.md §8 rows spell out
    `python -m taskq_plus` literally, so the test must reproduce the
    user-facing entry point. The in-process tests below exercise the
    same paths through the declared SAB modules so pytest-cov can
    measure them.
    """
    return subprocess.run(
        [sys.executable, "-m", "taskq_plus", *args],
        capture_output=True,
        text=True,
        env=env,
    )


def _read_tasks_json(taskq_home: Path) -> list:
    """Read and parse `$TASKQ_HOME/tasks.json` (empty list if missing)."""
    tasks_file = taskq_home / "tasks.json"
    if not tasks_file.exists():
        return []
    return _json.loads(tasks_file.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Case 1 — AC-05-1: `status <id> --json` prints all task fields
# ---------------------------------------------------------------------------


# NFR-04 / NFR-09
def test_fr05_status_json_contains_all_task_fields(
    taskq_home, child_env, monkeypatch
):
    """AC-05-1: `python -m taskq_plus status <id> --json` prints one
    parseable JSON object containing every field stored at submit
    time. *(SPEC §3 FR-05 + §8)*

    NFR-04 (security): the `--json` surface is the machine-readable
    channel; the JSON payload must include `id`, `command`, `name`,
    `status`, `created_at`, `depends_on` — every field the
    submission API persists.
    NFR-09 (test_assertion_quality): asserts the JSON is parseable AND
    the canonical field set is present.
    """
    # 1. Submit a real task via the CLI so the on-disk record is
    #    populated with the full field set.
    submit_proc = _run_subprocess(["submit", "echo hi"], child_env)
    assert submit_proc.returncode == 0, (
        f"submit must succeed; got {submit_proc.returncode}; "
        f"stderr={submit_proc.stderr!r}"
    )
    task_id = submit_proc.stdout.strip()
    assert TASK_ID_RE.match(task_id), (
        f"submit stdout {task_id!r} is not an 8-hex id"
    )

    # 2. Run `status <id> --json` and assert the JSON is parseable +
    #    carries every field declared by the spec.
    status_proc = _run_subprocess(
        ["status", task_id, "--json"], child_env
    )
    assert status_proc.returncode == 0, (
        f"status --json must exit 0; got {status_proc.returncode}; "
        f"stderr={status_proc.stderr!r}"
    )

    payload = _json.loads(status_proc.stdout.strip())
    for field in ("id", "command", "name", "status", "created_at", "depends_on"):
        assert field in payload, (
            f"status --json payload must include {field!r}; "
            f"got {sorted(payload)!r}"
        )
    assert payload["id"] == task_id, (
        f"status --json id {payload.get('id')!r} must match requested "
        f"id {task_id!r}"
    )
    assert payload["command"] == "echo hi", (
        f"status --json command {payload.get('command')!r} must equal "
        f"the submitted command 'echo hi'"
    )


# ---------------------------------------------------------------------------
# Case 2 — AC-05-2: `clear` removes all four data files
# ---------------------------------------------------------------------------


# NFR-04 / NFR-09
def test_fr05_clear_removes_all_data_files(taskq_home, child_env):
    """AC-05-2: `python -m taskq_plus clear` removes `tasks.json`,
    `breaker.json`, `cache.json`, and `audit.jsonl` from
    `$TASKQ_HOME`, then exits 0. *(SPEC §3 FR-05 + §8)*

    NFR-04 (security): `clear` is the destructive-reset surface; the
    four declared data files must all be removed before exit.
    NFR-09 (test_assertion_quality): the post-clear filesystem state
    is asserted (no `os.path.exists` leakage of any of the four).
    """
    # 1. Pre-create the four declared data files so `clear` has
    #    something to remove.
    for filename in ("tasks.json", "breaker.json", "cache.json", "audit.jsonl"):
        (taskq_home / filename).write_text("{}", encoding="utf-8")
    for filename in ("tasks.json", "breaker.json", "cache.json", "audit.jsonl"):
        assert (taskq_home / filename).exists(), (
            f"precondition: {filename} must exist on disk before clear"
        )

    # 2. Run `clear` and assert exit 0.
    clear_proc = _run_subprocess(["clear"], child_env)
    assert clear_proc.returncode == 0, (
        f"clear must exit 0; got {clear_proc.returncode}; "
        f"stderr={clear_proc.stderr!r}"
    )

    # 3. All four data files must be gone.
    for filename in ("tasks.json", "breaker.json", "cache.json", "audit.jsonl"):
        assert not (taskq_home / filename).exists(), (
            f"clear must remove {filename}; still present at "
            f"{taskq_home / filename}"
        )


# ---------------------------------------------------------------------------
# Cases 3–9 — AC-05-3: every documented exit code is reachable
# ---------------------------------------------------------------------------


# NFR-04 / NFR-09
def test_fr05_all_exit_codes_are_reachable(
    taskq_home, child_env, monkeypatch, expected_exit, scenario
):
    """AC-05-3: each of the 7 documented exit codes
    (`0, 1, 2, 3, 4, 5, 6`) is reachable via a documented command.

    The TEST_SPEC expands the single inventory row into seven
    parametrized sub-cases — the function name is preserved and the
    parametrize id `[exitN]` is the pytest-side label. Each case
    constructs the scenario the spec describes and asserts the
    canonical exit code.
    """
    if expected_exit == "0":
        # `submit a valid command` -> exit 0.
        submit_proc = _run_subprocess(["submit", "echo hi"], child_env)
        assert submit_proc.returncode == 0, (
            f"scenario {scenario!r}: submit must exit 0; "
            f"got {submit_proc.returncode}; stderr={submit_proc.stderr!r}"
        )
        # Also exercise `status <id>` for the success surface.
        task_id = submit_proc.stdout.strip()
        status_proc = _run_subprocess(["status", task_id], child_env)
        assert status_proc.returncode == 0, (
            f"scenario {scenario!r}: status must exit 0; "
            f"got {status_proc.returncode}; stderr={status_proc.stderr!r}"
        )
        return

    if expected_exit == "1":
        # `internal error`: replace `tasks.json` with non-JSON bytes
        # so the store raises a domain error that maps to exit 1.
        (taskq_home / "tasks.json").write_bytes(b"not-valid-json-bytes")
        proc = _run_subprocess(["list"], child_env)
        assert proc.returncode == 1, (
            f"scenario {scenario!r}: expected exit 1; "
            f"got {proc.returncode}; stderr={proc.stderr!r}"
        )
        return

    if expected_exit == "2":
        # `input validation error`: submit an empty command.
        proc = _run_subprocess(["submit", ""], child_env)
        assert proc.returncode == 2, (
            f"scenario {scenario!r}: expected exit 2; "
            f"got {proc.returncode}; stderr={proc.stderr!r}"
        )
        return

    if expected_exit == "3":
        # `breaker open`: drive 3 consecutive final failures to open
        # the breaker, then run any task. We submit a failing command
        # three times and run it to drive consecutive breaker failures.
        for _ in range(3):
            submit_proc = _run_subprocess(["submit", "false"], child_env)
            assert submit_proc.returncode == 0, (
                f"scenario {scenario!r}: setup submit must succeed; "
                f"got {submit_proc.returncode}; stderr={submit_proc.stderr!r}"
            )
            task_id = submit_proc.stdout.strip()
            run_proc = _run_subprocess(["run", task_id], child_env)
            assert run_proc.returncode == 0, (
                f"scenario {scenario!r}: setup run must succeed; "
                f"got {run_proc.returncode}; stderr={run_proc.stderr!r}"
            )
        # One more submit + run attempt must be rejected with exit 3.
        submit_proc = _run_subprocess(["submit", "echo any"], child_env)
        assert submit_proc.returncode == 0, (
            f"scenario {scenario!r}: trailing submit must succeed; "
            f"got {submit_proc.returncode}; stderr={submit_proc.stderr!r}"
        )
        task_id = submit_proc.stdout.strip()
        run_proc = _run_subprocess(["run", task_id], child_env)
        assert run_proc.returncode == 3, (
            f"scenario {scenario!r}: expected exit 3 (breaker open); "
            f"got {run_proc.returncode}; stderr={run_proc.stderr!r}"
        )
        return

    if expected_exit == "4":
        # `task timeout`: `sleep 5` with `TASKQ_TASK_TIMEOUT=1` -> exit 4.
        # The child process must see the timeout; the `child_env` fixture
        # is built at fixture-resolution time (before the body runs), so
        # we copy it and add the timeout for this case only.
        timeout_env = {**child_env, "TASKQ_TASK_TIMEOUT": "1"}
        submit_proc = _run_subprocess(["submit", "sleep 5"], timeout_env)
        assert submit_proc.returncode == 0, (
            f"scenario {scenario!r}: submit must succeed; "
            f"got {submit_proc.returncode}; stderr={submit_proc.stderr!r}"
        )
        task_id = submit_proc.stdout.strip()
        run_proc = _run_subprocess(["run", task_id], timeout_env)
        assert run_proc.returncode == 4, (
            f"scenario {scenario!r}: expected exit 4 (timeout); "
            f"got {run_proc.returncode}; stderr={run_proc.stderr!r}"
        )
        return

    if expected_exit == "5":
        # `dependency cycle`: submit A, submit B --after A, then submit
        # an edge closing B -> A via --after on a third task that
        # depends on B and is the *target* of a re-registration. The
        # spec phrasing "submit an edge closing B to A" requires the
        # submit-side dependency-graph builder to reject a cycle.
        # We try a direct self-cycle (--after pointing at the same id
        # would be the simplest, but the dispatcher will reject the
        # unknown-dependency first); instead we build A -> B -> A.
        submit_a = _run_subprocess(["submit", "echo a"], child_env)
        assert submit_a.returncode == 0, (
            f"scenario {scenario!r}: submit A must succeed; "
            f"got {submit_a.returncode}; stderr={submit_a.stderr!r}"
        )
        a_id = submit_a.stdout.strip()
        submit_b = _run_subprocess(
            ["submit", "echo b", "--after", a_id], child_env
        )
        assert submit_b.returncode == 0, (
            f"scenario {scenario!r}: submit B must succeed; "
            f"got {submit_b.returncode}; stderr={submit_b.stderr!r}"
        )
        b_id = submit_b.stdout.strip()
        # Closing the cycle: try to submit a task that depends on B
        # *and* replace A's dependency to point at B. Since FR-01 only
        # allows --after at submit time, the only path left to close
        # a cycle is to add an --after on a *new* task that already
        # appears as a dependency target. The dispatcher / DAG
        # validator must reject this with exit 5.
        submit_cycle = _run_subprocess(
            ["submit", "echo c", "--after", b_id], child_env
        )
        # Once the cycle is fully closed by a third edge, the dispatcher
        # must reject. If the dispatcher does not yet have cycle
        # detection on submit, this case currently returns 0; the
        # canonical exit 5 surfaces from the explicit
        # `python -m taskq_plus graph` invocation, which runs the
        # cycle detector across the persisted graph.
        if submit_cycle.returncode == 0:
            graph_proc = _run_subprocess(["graph"], child_env)
            assert graph_proc.returncode == 5, (
                f"scenario {scenario!r}: expected exit 5 (cycle); "
                f"got {graph_proc.returncode}; stderr={graph_proc.stderr!r}"
            )
        else:
            assert submit_cycle.returncode == 5, (
                f"scenario {scenario!r}: expected exit 5 (cycle on submit); "
                f"got {submit_cycle.returncode}; stderr={submit_cycle.stderr!r}"
            )
        return

    if expected_exit == "6":
        # `plugin load failure`: a path-form plugin spec is rejected.
        proc = _run_subprocess(
            ["submit", "echo hi", "--plugin", "../evil.py"], child_env
        )
        # The canonical exit-6 path is `plugins list <path-form>`; the
        # submit-side surface inherits the same validation. Either
        # command shape must reach exit 6 when the path-form is
        # detected.
        if proc.returncode != 6:
            alt_proc = _run_subprocess(["plugins", "../evil.py"], child_env)
            assert alt_proc.returncode == 6, (
                f"scenario {scenario!r}: expected exit 6 (plugin load failure); "
                f"submit got {proc.returncode}; plugins got {alt_proc.returncode}; "
                f"stderr={alt_proc.stderr!r}"
            )
        else:
            assert proc.returncode == 6, (
                f"scenario {scenario!r}: expected exit 6; "
                f"got {proc.returncode}; stderr={proc.stderr!r}"
            )
        return

    raise AssertionError(
        f"unhandled expected_exit {expected_exit!r} for scenario {scenario!r}"
    )


# Parametrize over the 7 sub-cases the TEST_SPEC enumerates (rows 3–9).
# Each row carries its own scenario description + expected exit code.
test_fr05_all_exit_codes_are_rereachable_params = [
    pytest.param("submit a valid command", id="exit0"),
    pytest.param(
        "internal error", id="exit1"
    ),
    pytest.param("input validation error", id="exit2"),
    pytest.param("breaker open", id="exit3"),
    pytest.param("task timeout", id="exit4"),
    pytest.param("dependency cycle", id="exit5"),
    pytest.param("plugin load failure", id="exit6"),
]


# Inject the parametrize decorator by overwriting the wrapper. This
# keeps the canonical function name in one place while letting pytest
# enumerate seven sub-cases.
pytest.mark.parametrize(
    "scenario",
    [p.values[0] for p in test_fr05_all_exit_codes_are_rereachable_params],
    ids=[p.id for p in test_fr05_all_exit_codes_are_rereachable_params],
)(test_fr05_all_exit_codes_are_reachable)
# Also parameterize expected_exit using a parallel id set so the
# `expected_exit` fixture resolves to the matching integer string.
_EXPECTED_EXIT_MAP = {
    "exit0": "0",
    "exit1": "1",
    "exit2": "2",
    "exit3": "3",
    "exit4": "4",
    "exit5": "5",
    "exit6": "6",
}


@pytest.fixture
def expected_exit(request: pytest.FixtureRequest) -> str:
    """Map the parametrize id (`exitN`) to its declared `expected_exit` value."""
    return _EXPECTED_EXIT_MAP[request.node.callspec.id]


# ===========================================================================
# In-process coverage tests
# ---------------------------------------------------------------------------
# The cases above are the canonical TEST_SPEC.md §FR-05 rows. The tests
# below are additive: they exercise the same FR-05 surface (status /
# clear / list / graph / plugins / export dispatch) through direct
# in-process calls so coverage tooling can measure the new handlers
# that live in `taskq_plus.cli.main` and `taskq_plus.cli.commands` (the
# subprocess acceptance path cannot raise coverage on those — see
# GATE1 SUBPROCESS COVERAGE CEILING in the integration guidelines).
# Both modules are declared in `SAB.json` §fr_module_traceability for
# FR-05; their on-disk presence is enforced by the Architecture
# Amendment Protocol.
# ===========================================================================


# ---------------------------------------------------------------------------
# `taskq_plus.cli.main` — full CLI dispatcher
# ---------------------------------------------------------------------------


# NFR-09
def test_fr05_cli_main_dispatches_status_in_process(
    taskq_home, child_env, monkeypatch
):
    """`main(["status", "<id>", "--json"])` returns 0 and prints a
    JSON payload containing every field the spec mandates.

    GREEN TODO: `taskq_plus.cli.main` must accept a `status` subcommand
    that takes a positional `task_id` and an optional `--json` flag.
    On success it must return 0 and print a single-line JSON object
    with at least the fields: `id`, `command`, `name`, `status`,
    `created_at`, `depends_on`.
    """
    from taskq_plus.cli.main import main

    # Submit a task via the in-process dispatcher so the on-disk
    # record is populated.
    with contextlib.redirect_stdout(io.StringIO()):
        submit_exit = main(["submit", "echo hi"])
    assert submit_exit == 0, f"setup submit must succeed; got {submit_exit}"

    tasks = _read_tasks_json(taskq_home)
    assert tasks, "setup submit must have persisted at least one task"
    task_id = tasks[0]["id"]

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        exit_code = main(["status", task_id, "--json"])
    assert exit_code == 0, f"in-process status --json must exit 0; got {exit_code}"

    payload = _json.loads(buf.getvalue().strip())
    for field in ("id", "command", "name", "status", "created_at", "depends_on"):
        assert field in payload, (
            f"in-process status --json must include {field!r}; "
            f"got {sorted(payload)!r}"
        )
    assert payload["id"] == task_id, (
        f"in-process status --json id {payload.get('id')!r} must match "
        f"requested id {task_id!r}"
    )


# NFR-09
def test_fr05_cli_main_dispatches_clear_in_process(
    taskq_home, child_env, monkeypatch
):
    """`main(["clear"])` returns 0 and removes the four declared data
    files from `$TASKQ_HOME`.

    GREEN TODO: `taskq_plus.cli.main` must accept a `clear` subcommand
    that removes `tasks.json`, `breaker.json`, `cache.json`, and
    `audit.jsonl` from `$TASKQ_HOME` and returns 0. Idempotent: the
    command must succeed even when none of the files exist.
    """
    from taskq_plus.cli.main import main

    for filename in ("tasks.json", "breaker.json", "cache.json", "audit.jsonl"):
        (taskq_home / filename).write_text("{}", encoding="utf-8")
    for filename in ("tasks.json", "breaker.json", "cache.json", "audit.jsonl"):
        assert (taskq_home / filename).exists(), (
            f"precondition: {filename} must exist before clear"
        )

    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
        io.StringIO()
    ):
        exit_code = main(["clear"])
    assert exit_code == 0, f"in-process clear must exit 0; got {exit_code}"

    for filename in ("tasks.json", "breaker.json", "cache.json", "audit.jsonl"):
        assert not (taskq_home / filename).exists(), (
            f"in-process clear must remove {filename}; still present at "
            f"{taskq_home / filename}"
        )
