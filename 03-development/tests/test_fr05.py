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
        # `dependency cycle`: build the chain A <- B <- C via `--after`,
        # then close the cycle with the edge A -> C and assert that the
        # documented `graph` command reports exit 5.
        #
        # Why the closing edge is written to `tasks.json` directly:
        # `--after` can only name ids that already exist and `submit`
        # rejects unknown dependency ids (SPEC §7 line 385), so no
        # sequence of `submit` calls can ever produce a cycle — every
        # new task is a leaf. The persisted graph is therefore the only
        # surface through which a cycle can enter the system, and
        # `graph`'s Kahn-based detector (SPEC §3 FR-06 line 147) is the
        # documented command that must surface it as exit 5.
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
        submit_c = _run_subprocess(
            ["submit", "echo c", "--after", b_id], child_env
        )
        assert submit_c.returncode == 0, (
            f"scenario {scenario!r}: submit C must succeed; "
            f"got {submit_c.returncode}; stderr={submit_c.stderr!r}"
        )
        c_id = submit_c.stdout.strip()

        # Sanity: the acyclic chain A <- B <- C must still be a valid
        # DAG, so `graph` exits 0 *before* the cycle is introduced.
        # This proves the exit 5 below is caused by the cycle and not
        # by unrelated graph breakage.
        dag_proc = _run_subprocess(["graph"], child_env)
        assert dag_proc.returncode == 0, (
            f"scenario {scenario!r}: the acyclic chain must exit 0 before "
            f"the cycle is closed; got {dag_proc.returncode}; "
            f"stderr={dag_proc.stderr!r}"
        )

        # Close the cycle: A now depends on C, giving A -> B -> C -> A.
        records = _read_tasks_json(taskq_home)
        assert {r["id"] for r in records} == {a_id, b_id, c_id}, (
            f"scenario {scenario!r}: tasks.json must hold exactly the "
            f"three submitted ids; got {sorted(r['id'] for r in records)!r}"
        )
        for record in records:
            if record["id"] == a_id:
                record["depends_on"] = [c_id]
        (taskq_home / "tasks.json").write_text(
            _json.dumps(records), encoding="utf-8"
        )

        graph_proc = _run_subprocess(["graph"], child_env)
        assert graph_proc.returncode == 5, (
            f"scenario {scenario!r}: expected exit 5 (cycle); "
            f"got {graph_proc.returncode}; stderr={graph_proc.stderr!r}"
        )
        assert "dependency cycle" in graph_proc.stderr, (
            f"scenario {scenario!r}: exit 5 must report the cycle on "
            f"stderr (SPEC §7 line 388); got {graph_proc.stderr!r}"
        )
        # The reported path must name the members of the real cycle.
        for member in (a_id, b_id, c_id):
            assert member in graph_proc.stderr, (
                f"scenario {scenario!r}: cycle path must include {member!r}; "
                f"got {graph_proc.stderr!r}"
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


# ---------------------------------------------------------------------------
# `taskq_plus.cli.main` — the remaining FR-05 subcommand dispatch shapes
# ---------------------------------------------------------------------------


def _capture_main(argv: list) -> tuple:
    """Run `main(argv)` in-process, returning `(exit_code, stdout, stderr)`."""
    from taskq_plus.cli.main import main

    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        exit_code = main(list(argv))
    return exit_code, out.getvalue(), err.getvalue()


def _submit_in_process(command: str, *flags: str) -> str:
    """Submit one task through the in-process dispatcher; return its id."""
    exit_code, stdout, stderr = _capture_main(["submit", command, *flags])
    assert exit_code == 0, (
        f"setup submit {command!r} {flags!r} must exit 0; got {exit_code}; "
        f"stderr={stderr!r}"
    )
    return stdout.strip()


# NFR-09
def test_fr05_cli_main_submit_forwards_after_flag_in_process(taskq_home):
    """`submit "<cmd>" --after <id>` must persist the dependency edge.

    Regression guard: the dispatcher receives the `submit` tokens as an
    `argparse.REMAINDER` list. Joining *every* token into the command
    string folds `--after <id>` into the command body, so the edge is
    silently dropped and `graph` sees two unrelated roots. The command
    body must be joined only up to the first handler-owned flag.

    *(SPEC §3 FR-01 line 84 — `--after` builds `depends_on`;
    SPEC §3 FR-05 line 131 — `submit "<cmd>" [--name N] [--after ID]`.)*
    """
    a_id = _submit_in_process("echo a")
    b_id = _submit_in_process("echo b", "--after", a_id)

    records = {r["id"]: r for r in _read_tasks_json(taskq_home)}
    assert records[b_id]["depends_on"] == [a_id], (
        f"--after must persist depends_on=[{a_id!r}]; got "
        f"{records[b_id]['depends_on']!r}"
    )
    assert records[b_id]["command"] == "echo b", (
        f"the --after flag must not leak into the command body; got "
        f"{records[b_id]['command']!r}"
    )
    assert records[a_id]["depends_on"] == [], (
        f"a task submitted without --after must have no dependencies; got "
        f"{records[a_id]['depends_on']!r}"
    )


# NFR-09
def test_fr05_cli_main_submit_forwards_name_flag_in_process(taskq_home):
    """`submit "<cmd>" --name <n>` must persist `name`, not fold the
    flag into the command body. *(SPEC §3 FR-05 line 131)*"""
    task_id = _submit_in_process("echo hi", "--name", "nightly")

    records = {r["id"]: r for r in _read_tasks_json(taskq_home)}
    assert records[task_id]["name"] == "nightly", (
        f"--name must persist as the task name; got {records[task_id]['name']!r}"
    )
    assert records[task_id]["command"] == "echo hi", (
        f"the --name flag must not leak into the command body; got "
        f"{records[task_id]['command']!r}"
    )


# NFR-09
def test_fr05_cli_main_status_plain_prints_every_field(taskq_home):
    """`status <id>` without `--json` prints one `key: value` line per
    persisted field. *(SPEC §3 FR-05 line 132 — 輸出該任務全欄位)*"""
    task_id = _submit_in_process("echo hi")

    exit_code, stdout, _stderr = _capture_main(["status", task_id])
    assert exit_code == 0, f"status must exit 0; got {exit_code}"

    printed = {
        line.split(":", 1)[0] for line in stdout.splitlines() if ":" in line
    }
    for field in ("id", "command", "name", "status", "created_at", "depends_on"):
        assert field in printed, (
            f"plain status must print a {field!r} line; got {stdout!r}"
        )
    assert f"id: {task_id}" in stdout, (
        f"plain status must echo the requested id; got {stdout!r}"
    )


# NFR-04 / NFR-09
def test_fr05_cli_main_status_unknown_id_exits_2(taskq_home):
    """`status <unknown-id>` exits 2 with `unknown task: <id>` on
    stderr. *(SPEC §7 line 384)*"""
    exit_code, stdout, stderr = _capture_main(["status", "deadbeef"])

    assert exit_code == 2, (
        f"unknown task id must exit 2; got {exit_code}; stderr={stderr!r}"
    )
    assert "unknown task: deadbeef" in stderr, (
        f"stderr must name the unknown id; got {stderr!r}"
    )
    assert stdout == "", (
        f"a rejected status lookup must print nothing on stdout; got {stdout!r}"
    )


# NFR-09
def test_fr05_cli_main_list_prints_tasks_plain_and_json(taskq_home):
    """`list` prints one row per task; `list --json` prints the same
    set as a single-line JSON array. *(SPEC §3 FR-05 line 133 + 139)*"""
    first = _submit_in_process("echo a")
    second = _submit_in_process("echo b")

    exit_code, stdout, _stderr = _capture_main(["list"])
    assert exit_code == 0, f"list must exit 0; got {exit_code}"
    rows = [line for line in stdout.splitlines() if line.strip()]
    assert len(rows) == 2, f"list must print one row per task; got {stdout!r}"
    for task_id in (first, second):
        assert task_id in stdout, (
            f"list must include {task_id!r}; got {stdout!r}"
        )
    assert all("pending" in row for row in rows), (
        f"each list row must carry the task status; got {rows!r}"
    )

    json_exit, json_stdout, _json_stderr = _capture_main(["list", "--json"])
    assert json_exit == 0, f"list --json must exit 0; got {json_exit}"
    payload = _json.loads(json_stdout.strip())
    assert [entry["id"] for entry in payload] == [first, second], (
        f"list --json must carry both ids in submission order; got {payload!r}"
    )
    assert payload[0]["command"] == "echo a", (
        f"list --json entries must carry the full task record; got {payload[0]!r}"
    )


# NFR-09
def test_fr05_cli_main_list_reports_corrupted_store(taskq_home):
    """A corrupted `tasks.json` surfaces as exit 1 + `store corrupted`
    on stderr — never a silent rebuild. *(SPEC §7 line 392)*"""
    (taskq_home / "tasks.json").write_text("not-valid-json", encoding="utf-8")

    exit_code, stdout, stderr = _capture_main(["list"])

    assert exit_code == 1, (
        f"a corrupted store must exit 1; got {exit_code}; stderr={stderr!r}"
    )
    assert "store corrupted" in stderr, (
        f"stderr must say 'store corrupted'; got {stderr!r}"
    )
    assert stdout == "", (
        f"a corrupted store must print no task rows; got {stdout!r}"
    )
    assert (taskq_home / "tasks.json").read_text(encoding="utf-8") == (
        "not-valid-json"
    ), "the corrupted file must be left untouched (no silent rebuild)"


# NFR-09
def test_fr05_cli_main_graph_renders_dependency_chain(taskq_home):
    """`graph` prints the DAG in topological order, indenting each node
    by its dependency depth. *(SPEC §3 FR-05 line 134; §3 FR-06 line 145)*"""
    a_id = _submit_in_process("echo a")
    b_id = _submit_in_process("echo b", "--after", a_id)
    c_id = _submit_in_process("echo c", "--after", b_id)

    exit_code, stdout, stderr = _capture_main(["graph"])

    assert exit_code == 0, (
        f"an acyclic graph must exit 0; got {exit_code}; stderr={stderr!r}"
    )
    assert stdout.splitlines() == [a_id, f"  {b_id}", f"    {c_id}"], (
        f"graph must emit the chain in topological order with one indent "
        f"level per depth; got {stdout!r}"
    )


# NFR-09
def test_fr05_cli_main_graph_reports_cycle(taskq_home):
    """A cyclic persisted graph exits 5 and prints the cycle path.
    *(SPEC §3 FR-05 line 140; §7 line 388)*"""
    a_id = _submit_in_process("echo a")
    b_id = _submit_in_process("echo b", "--after", a_id)

    # `submit` can only reference ids that already exist, so the closing
    # edge B -> A is written straight to the persisted graph.
    records = _read_tasks_json(taskq_home)
    for record in records:
        if record["id"] == a_id:
            record["depends_on"] = [b_id]
    (taskq_home / "tasks.json").write_text(
        _json.dumps(records), encoding="utf-8"
    )

    exit_code, stdout, stderr = _capture_main(["graph"])

    assert exit_code == 5, (
        f"a cyclic graph must exit 5; got {exit_code}; stderr={stderr!r}"
    )
    assert "dependency cycle" in stderr, (
        f"stderr must announce the cycle; got {stderr!r}"
    )
    for member in (a_id, b_id):
        assert member in stderr, (
            f"the cycle path must name {member!r}; got {stderr!r}"
        )
    assert stdout == "", (
        f"a cyclic graph must not print a partial tree; got {stdout!r}"
    )


# NFR-09
def test_fr05_cli_main_graph_reports_depth_cap_breach(taskq_home, monkeypatch):
    """A chain deeper than `TASKQ_MAX_DAG_DEPTH` exits 5 with
    `dependency chain too deep: <n> > <max>`. *(SPEC §7 line 389)*"""
    a_id = _submit_in_process("echo a")
    _submit_in_process("echo b", "--after", a_id)

    monkeypatch.setenv("TASKQ_MAX_DAG_DEPTH", "1")
    exit_code, stdout, stderr = _capture_main(["graph"])

    assert exit_code == 5, (
        f"a chain deeper than the cap must exit 5; got {exit_code}; "
        f"stderr={stderr!r}"
    )
    assert "dependency chain too deep: 2 > 1" in stderr, (
        f"stderr must report the measured depth and the cap; got {stderr!r}"
    )
    assert stdout == "", (
        f"a capped graph must not print the tree; got {stdout!r}"
    )


# NFR-09
def test_fr05_cli_main_graph_ignores_unparseable_depth_cap(
    taskq_home, monkeypatch
):
    """A non-numeric `TASKQ_MAX_DAG_DEPTH` falls back to the documented
    default (32) instead of crashing. *(SPEC §5.1 line 302)*"""
    a_id = _submit_in_process("echo a")
    _submit_in_process("echo b", "--after", a_id)

    monkeypatch.setenv("TASKQ_MAX_DAG_DEPTH", "not-an-int")
    exit_code, _stdout, stderr = _capture_main(["graph"])

    assert exit_code == 0, (
        f"an unparseable depth cap must fall back to the default and exit 0; "
        f"got {exit_code}; stderr={stderr!r}"
    )
    assert stderr == "", (
        f"the fallback must be silent, not a warning; got {stderr!r}"
    )


# NFR-04 / NFR-09
def test_fr05_cli_main_plugins_lists_allowlist(taskq_home, monkeypatch):
    """`plugins list` prints the `TASKQ_PLUGINS` allowlist and exits 0.
    *(SPEC §3 FR-05 line 135; §3 FR-07 line 157)*

    The FR-07 spec refines the listing surface: each plugin entry
    carries its module name, registered hooks, and load status. The
    test pins the new surface (`hooks=`, `status=`) so a regression
    that drops the status column is caught.
    """
    monkeypatch.setenv("TASKQ_PLUGINS", "json, os")

    exit_code, stdout, stderr = _capture_main(["plugins", "list"])

    assert exit_code == 0, (
        f"a well-formed allowlist must exit 0; got {exit_code}; "
        f"stderr={stderr!r}"
    )
    for name in ("json", "os"):
        assert name in stdout, (
            f"plugins list must print each declared module name "
            f"{name!r}; got {stdout!r}"
        )
    assert "status=" in stdout, (
        f"plugins list must print the load status per plugin; "
        f"got {stdout!r}"
    )
    assert "list" not in stdout.splitlines(), (
        f"the `list` verb is not itself a plugin spec; got {stdout!r}"
    )


# NFR-04 / NFR-09
def test_fr05_cli_main_plugins_rejects_path_form(taskq_home):
    """A path-form plugin spec is rejected with exit 6 *before* any
    import. *(SPEC §3 FR-07 line 157; §4 NFR-02 line 200; §7 line 390)*

    The FR-07 spec locks the rejection template to
    `rejected module: <name>` (TEST_SPEC §FR-07 row 2 sub-assertion
    `FR07-path-form-named-in-stderr`); the previous
    `plugin load failed: <name>: not a module name` template is
    obsolete.
    """
    exit_code, stdout, stderr = _capture_main(["plugins", "../evil.py"])

    assert exit_code == 6, (
        f"a path-form plugin spec must exit 6; got {exit_code}; "
        f"stderr={stderr!r}"
    )
    assert "rejected module: ../evil.py" in stderr, (
        f"stderr must name the rejected spec on the FR-07 template; "
        f"got {stderr!r}"
    )
    assert stdout == "", (
        f"a rejected spec must not be echoed on stdout; got {stdout!r}"
    )


# NFR-09
def test_fr05_cli_main_run_dispatch_shapes(taskq_home):
    """The `run` dispatcher forwards all three documented shapes:
    `run <id>`, `run <id> --cached`, and `run --all`; a bare `run`
    with neither is a usage error (exit 2). *(SPEC §3 FR-05 line 131)*"""
    task_id = _submit_in_process("echo hi")

    cached_exit, _stdout, cached_stderr = _capture_main(
        ["run", task_id, "--cached"]
    )
    assert cached_exit == 0, (
        f"`run <id> --cached` must exit 0; got {cached_exit}; "
        f"stderr={cached_stderr!r}"
    )

    all_exit, _all_stdout, all_stderr = _capture_main(["run", "--all"])
    assert all_exit == 0, (
        f"`run --all` must exit 0; got {all_exit}; stderr={all_stderr!r}"
    )

    bare_exit, _bare_stdout, bare_stderr = _capture_main(["run"])
    assert bare_exit == 2, (
        f"a bare `run` must be a usage error (exit 2); got {bare_exit}; "
        f"stderr={bare_stderr!r}"
    )
    assert "must supply a task id or --all" in bare_stderr, (
        f"stderr must explain the usage error; got {bare_stderr!r}"
    )


# ---------------------------------------------------------------------------
# `taskq_plus.storage.task_store` — the backends behind the FR-05 surface
# ---------------------------------------------------------------------------


# NFR-09
def test_fr05_in_process_store_backend_round_trip(taskq_home):
    """The in-process (`use_disk=False`) backend behind the FR-05
    handlers supports the full submit -> list -> run round trip, so the
    in-process surface never leaks into `$TASKQ_HOME`."""
    from taskq_plus.cli import commands

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        submit_exit = commands.submit(["echo hi"])
    assert submit_exit == 0, f"in-memory submit must exit 0; got {submit_exit}"
    task_id = buf.getvalue().strip()

    list_buf = io.StringIO()
    with contextlib.redirect_stdout(list_buf):
        list_exit = commands.list_tasks([])
    assert list_exit == 0, f"in-memory list must exit 0; got {list_exit}"
    assert task_id in list_buf.getvalue(), (
        f"the in-memory backend must return the submitted task; got "
        f"{list_buf.getvalue()!r}"
    )

    with contextlib.redirect_stdout(io.StringIO()):
        run_exit = commands.run([task_id])
    assert run_exit == 0, f"in-memory run must exit 0; got {run_exit}"

    status_buf = io.StringIO()
    with contextlib.redirect_stdout(status_buf):
        status_exit = commands.status([task_id, "--json"])
    assert status_exit == 0, f"in-memory status must exit 0; got {status_exit}"
    payload = _json.loads(status_buf.getvalue().strip())
    assert payload["status"] == "done", (
        f"the executed task must be persisted as done in the in-memory "
        f"backend; got {payload['status']!r}"
    )
    assert not (taskq_home / "tasks.json").exists(), (
        "the in-process backend must not write $TASKQ_HOME/tasks.json"
    )


# NFR-09
def test_fr05_store_update_on_missing_id_raises_key_error(taskq_home):
    """Both backends report an update against a removed id as a
    `KeyError` instead of resurrecting a record — the invariant `clear`
    (FR-05) depends on. *(SPEC §3 FR-05 line 136)*"""
    from taskq_plus.storage.task_store import get_store, make_disk_store

    in_memory = get_store(use_disk=False)
    with pytest.raises(KeyError) as in_memory_exc:
        in_memory.update("deadbeef", lambda task: task)
    assert "deadbeef" in str(in_memory_exc.value), (
        f"the in-memory KeyError must name the missing id; got "
        f"{in_memory_exc.value!r}"
    )

    on_disk = make_disk_store()
    with pytest.raises(KeyError) as disk_exc:
        on_disk.update("deadbeef", lambda task: task)
    assert "deadbeef" in str(disk_exc.value), (
        f"the on-disk KeyError must name the missing id; got "
        f"{disk_exc.value!r}"
    )


# ===========================================================================
# Coverage extension — exercise every branch in `commands.py` so the FR-05
# test_coverage dimension reaches the Gate 1 threshold. These tests are
# additive: they drive the same in-process surface the spec covers, but
# trigger paths the canonical spec cases leave untouched (env-var fallbacks,
# duplicate-name rejection, unknown-dep rejection, cache-hit short-circuit,
# breaker-OPEN short-circuit, run-timeout, run-not-found, run --all pool,
# submit --json output, pydantic ValidationError formatting).
# ===========================================================================


# NFR-09
def test_fr05_submit_emits_validation_error_in_process(taskq_home):
    """An empty command triggers pydantic `ValidationError` -> exit 2
    with the formatted single-line message on stderr. Covers
    `_format_validation_error` (lines 173-181) and the ValidationError
    branch of `submit` (lines 271-273)."""
    from taskq_plus.cli import commands

    exit_code, _stdout, stderr = _capture_main(["submit", ""])
    assert exit_code == 2, (
        f"empty command must exit 2; got {exit_code}; stderr={stderr!r}"
    )
    # Stderr must carry the formatted validation message; it must be
    # prefixed with `submit: `.
    assert stderr.startswith("submit: "), (
        f"validation failure must be prefixed with 'submit: '; got {stderr!r}"
    )
    # The pydantic error for an empty command carries a "Value error, "
    # prefix that `_format_validation_error` strips — assert the suffix
    # carries the post-strip body (no double "Value error, ").
    assert "Value error, Value error," not in stderr, (
        f"the 'Value error, ' prefix must be stripped; got {stderr!r}"
    )

    # Direct unit test of `_format_validation_error` to force the
    # `errors()` -> `first` path on a multi-error pydantic model.
    from pydantic import ValidationError as _PydValErr  # type: ignore

    from taskq_plus.models.task import TaskSubmission as _TS

    try:
        _TS(command="", depends_on=["x" * 5_000])
    except _PydValErr as exc:
        formatted = commands._format_validation_error(exc)
    else:
        pytest.fail("TaskSubmission with empty command + huge dep list must raise")

    # `formatted` is a non-empty stripped message — never the raw
    # "Value error, " prefix.
    assert formatted, "_format_validation_error must return a non-empty string"
    assert not formatted.startswith("Value error, "), (
        f"the 'Value error, ' prefix must be stripped; got {formatted!r}"
    )

    # Cover the empty-errors fallback (line 175). `_format_validation_error`
    # only ever calls `err.errors()`, so a duck-typed stand-in with an
    # empty `.errors()` payload reaches the defensive branch.
    class _EmptyErrorsExc:
        def errors(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            return []

    fallback = commands._format_validation_error(_EmptyErrorsExc())
    assert fallback == "validation failed", (
        f"the empty-errors fallback must return 'validation failed'; got "
        f"{fallback!r}"
    )


# NFR-09
def test_fr05_submit_rejects_duplicate_name_in_process(taskq_home):
    """Submitting two tasks with the same `--name` exits 2 with
    `duplicate name: <name>` on stderr. Covers lines 277-279."""
    exit_code, _stdout, _stderr1 = _capture_main(
        ["submit", "echo a", "--name", "shared"]
    )
    assert exit_code == 0, f"first submit must succeed; got {exit_code}"

    exit_code, _stdout, stderr = _capture_main(
        ["submit", "echo b", "--name", "shared"]
    )
    assert exit_code == 2, (
        f"duplicate name must exit 2; got {exit_code}; stderr={stderr!r}"
    )
    assert "duplicate name: shared" in stderr, (
        f"stderr must name the duplicate; got {stderr!r}"
    )


# NFR-09
def test_fr05_submit_rejects_unknown_dependency_in_process(taskq_home):
    """Submitting with `--after <missing>` exits 2 with
    `unknown dependency: <id>` on stderr. Covers lines 281-284."""
    exit_code, _stdout, stderr = _capture_main(
        ["submit", "echo hi", "--after", "deadbeef"]
    )
    assert exit_code == 2, (
        f"unknown --after must exit 2; got {exit_code}; stderr={stderr!r}"
    )
    assert "unknown dependency: deadbeef" in stderr, (
        f"stderr must name the unknown id; got {stderr!r}"
    )


# NFR-09
def test_fr05_submit_json_flag_prints_json_record_in_process(taskq_home):
    """`submit "<cmd>" --json` prints `{"id": ..., "status": ...}` on
    stdout. Covers line 294 (`args.as_json` branch of `submit`)."""
    exit_code, stdout, _stderr = _capture_main(["submit", "echo hi", "--json"])
    assert exit_code == 0, f"--json submit must exit 0; got {exit_code}"

    payload = _json.loads(stdout.strip())
    assert set(payload.keys()) == {"id", "status"}, (
        f"--json submit payload must carry exactly id+status; got {sorted(payload)!r}"
    )
    assert TASK_ID_RE.match(payload["id"]), (
        f"--json submit id must be an 8-hex token; got {payload['id']!r}"
    )
    assert payload["status"] == "pending", (
        f"a fresh submission must be pending; got {payload['status']!r}"
    )


# NFR-09
def test_fr05_env_task_timeout_unparseable_falls_back_in_process(
    taskq_home, monkeypatch
):
    """A non-numeric `TASKQ_TASK_TIMEOUT` falls back to the documented
    default (10.0 s) instead of crashing. Covers lines 199-202."""
    monkeypatch.setenv("TASKQ_TASK_TIMEOUT", "not-a-float")

    from taskq_plus.cli import commands

    assert commands._timeout_budget() == 10.0, (
        f"an unparseable TASKQ_TASK_TIMEOUT must fall back to 10.0; got "
        f"{commands._timeout_budget()!r}"
    )


# NFR-09
def test_fr05_env_max_workers_unparseable_falls_back_in_process(
    taskq_home, monkeypatch
):
    """A non-numeric `TASKQ_MAX_WORKERS` falls back to the documented
    default (4) instead of crashing. Covers lines 207-213."""
    monkeypatch.setenv("TASKQ_MAX_WORKERS", "not-an-int")

    from taskq_plus.cli import commands

    assert commands._max_workers() == 4, (
        f"an unparseable TASKQ_MAX_WORKERS must fall back to 4; got "
        f"{commands._max_workers()!r}"
    )


# NFR-09
def test_fr05_run_unknown_task_id_exits_2_in_process(taskq_home):
    """`run <missing>` exits 2 with `run: task '<id>' not found` on
    stderr. Covers lines 352-355."""
    exit_code, _stdout, stderr = _capture_main(["run", "deadbeef"])

    assert exit_code == 2, (
        f"unknown run id must exit 2; got {exit_code}; stderr={stderr!r}"
    )
    assert "run: task 'deadbeef' not found" in stderr, (
        f"stderr must name the missing id; got {stderr!r}"
    )


# NFR-09
def test_fr05_run_breaker_open_short_circuits_in_process(
    taskq_home, monkeypatch
):
    """A pre-failed `breaker.json` causes `run` to short-circuit with
    exit 3 and `breaker open` on stderr *before* any subprocess. Covers
    lines 345-350."""
    # Submit a task we will try to run.
    exit_code, stdout, _stderr = _capture_main(["submit", "echo hi"])
    assert exit_code == 0
    task_id = stdout.strip()

    # Plant an OPEN breaker file at the same $TASKQ_HOME the in-process
    # dispatcher resolves to.
    (taskq_home / "breaker.json").write_text(
        _json.dumps(
            {
                "state": "OPEN",
                "failure_count": 3,
                "opened_at": None,
                "threshold": 3,
                "cooldown_s": 60.0,
            }
        ),
        encoding="utf-8",
    )

    exit_code, _stdout, stderr = _capture_main(["run", task_id])

    assert exit_code == 3, (
        f"a pre-OPEN breaker must short-circuit with exit 3; got "
        f"{exit_code}; stderr={stderr!r}"
    )
    assert "breaker open" in stderr, (
        f"stderr must announce the breaker; got {stderr!r}"
    )


# NFR-09
def test_fr05_run_cached_path_short_circuits_executor_in_process(taskq_home):
    """A primed `cache.json` entry causes `run <id> --cached` to copy
    the entry into the task and exit 0 without invoking the executor.
    Covers lines 360-372."""
    from datetime import datetime, timezone

    exit_code, stdout, _stderr = _capture_main(["submit", "echo hi"])
    assert exit_code == 0
    task_id = stdout.strip()

    # Prime the cache with a fresh "done" entry whose `command` matches
    # the submitted task.
    sig = _hash_command("echo hi")
    finished_at = datetime.now(tz=timezone.utc).isoformat()
    (taskq_home / "cache.json").write_text(
        _json.dumps(
            {
                sig: {
                    "command": "echo hi",
                    "exit_code": 0,
                    "stdout_tail": "primed-cached-output\n",
                    "finished_at": finished_at,
                    "status": "done",
                }
            }
        ),
        encoding="utf-8",
    )

    exit_code, _stdout, stderr = _capture_main(["run", task_id, "--cached"])
    assert exit_code == 0, (
        f"`run --cached` against a primed cache must exit 0; got {exit_code}; "
        f"stderr={stderr!r}"
    )

    # The on-disk record must carry the cached fields.
    records = {r["id"]: r for r in _read_tasks_json(taskq_home)}
    assert records[task_id]["cached"] is True, (
        f"the cached path must persist cached=True; got "
        f"{records[task_id]['cached']!r}"
    )
    assert records[task_id]["status"] == "done", (
        f"the cached path must persist status=done; got "
        f"{records[task_id]['status']!r}"
    )
    assert records[task_id]["stdout_tail"] == "primed-cached-output\n", (
        f"the cached path must copy the cached stdout_tail; got "
        f"{records[task_id].get('stdout_tail')!r}"
    )


# NFR-09
def test_fr05_run_records_failure_on_failed_command_in_process(taskq_home):
    """`run` of a `false` command records a breaker failure and returns
    0 (the per-task failure is in the task's `status`, not the CLI exit
    code). Covers lines 396-400."""
    exit_code, stdout, _stderr = _capture_main(["submit", "false"])
    assert exit_code == 0
    task_id = stdout.strip()

    run_exit, _stdout, _stderr = _capture_main(["run", task_id])
    assert run_exit == 0, (
        f"a failed task must still let the CLI exit 0; got {run_exit}"
    )

    records = {r["id"]: r for r in _read_tasks_json(taskq_home)}
    assert records[task_id]["status"] == "failed", (
        f"a `false` task must persist status=failed; got "
        f"{records[task_id]['status']!r}"
    )

    breaker_path = taskq_home / "breaker.json"
    assert breaker_path.exists(), (
        f"the breaker must be persisted after a failure; missing "
        f"{breaker_path}"
    )
    payload = _json.loads(breaker_path.read_text(encoding="utf-8"))
    assert payload.get("failure_count", 0) >= 1, (
        f"a single failure must record failure_count >= 1; got {payload!r}"
    )


# NFR-09
def test_fr05_run_returns_4_on_timeout_in_process(taskq_home, monkeypatch):
    """`run` of a `sleep 5` command with `TASKQ_TASK_TIMEOUT=1` exits 4
    and persists `status=timeout`. Covers line 403."""
    monkeypatch.setenv("TASKQ_TASK_TIMEOUT", "1")

    exit_code, stdout, _stderr = _capture_main(["submit", "sleep 5"])
    assert exit_code == 0
    task_id = stdout.strip()

    run_exit, _stdout, _stderr = _capture_main(["run", task_id])
    assert run_exit == 4, (
        f"a timed-out task must exit 4; got {run_exit}"
    )

    records = {r["id"]: r for r in _read_tasks_json(taskq_home)}
    assert records[task_id]["status"] == "timeout", (
        f"a timed-out task must persist status=timeout; got "
        f"{records[task_id]['status']!r}"
    )


# NFR-09
def test_fr05_run_all_dispatches_pending_tasks_in_process(taskq_home):
    """`run --all` with two pending tasks executes both via the thread
    pool. Covers `_run_all` (lines 415-426)."""
    _submit_in_process("echo a")
    _submit_in_process("echo b")

    exit_code, _stdout, stderr = _capture_main(["run", "--all"])
    assert exit_code == 0, (
        f"`run --all` with pending tasks must exit 0; got {exit_code}; "
        f"stderr={stderr!r}"
    )

    records = {r["id"]: r for r in _read_tasks_json(taskq_home)}
    for task_id, record in records.items():
        assert record["status"] == "done", (
            f"`run --all` must execute every pending task; got "
            f"{records[task_id]['status']!r} for {task_id!r}"
        )


# NFR-09
def test_fr05_run_all_with_no_pending_returns_0_in_process(taskq_home):
    """`run --all` against an empty queue returns 0 (the `if not pending`
    early-return branch in `_run_all`). Covers lines 416-417."""
    exit_code, _stdout, stderr = _capture_main(["run", "--all"])
    assert exit_code == 0, (
        f"`run --all` against an empty queue must exit 0; got {exit_code}; "
        f"stderr={stderr!r}"
    )


# ---------------------------------------------------------------------------
# Additional coverage — bring commands.py and main.py to >=80%
# ---------------------------------------------------------------------------
# The cases below target the remaining uncovered branches in the FR-05
# in-process surface: the `submit` cycle / depth-cap pre-flight checks,
# the `_run_all` blocked-task / single-worker / multi-worker branches,
# `_utcnow()` (the timestamp used by the blocked path), the FR-08
# `export` dispatcher (parser + handler + cli.main export branch), and
# the new in-process plugin rejection template.


# NFR-09
def test_fr05_utcnow_is_aware_utc_in_process(taskq_home):
    """`commands._utcnow()` returns an aware UTC timestamp used by the
    `blocked` row's `finished_at` field. Covers line 216."""
    from taskq_plus.cli import commands

    now = commands._utcnow()
    assert now.tzinfo is not None, (
        f"_utcnow() must produce an aware datetime; got tzinfo={now.tzinfo!r}"
    )
    # The `datetime.now(timezone.utc)` timezone is `UTC` (a fixed
    # offset of 0); pin it so a future regression to a naive
    # `datetime.utcnow()` (deprecated, naive) is caught.
    assert now.utcoffset().total_seconds() == 0, (
        f"_utcnow() must use UTC; got offset={now.utcoffset()!r}"
    )


# NFR-09
def test_fr05_submit_rejects_existing_cycle_with_exit_five_in_process(taskq_home):
    """When a cycle is already persisted in the store, the next
    `submit` surfaces it as exit 5 + the cycle path on stderr
    (the `_kahn_order` / `_cycle_path` branch in `submit`). Covers
    lines 362-364."""
    from taskq_plus.cli import commands
    from taskq_plus.models.task import Task
    from taskq_plus.storage.task_store import get_store

    # Seed a two-node cycle out-of-band — `submit` rejects the closing
    # edge, so the only path into the cycle branch is an existing cycle.
    store = get_store()
    first = Task(command="echo a")
    second = Task(command="echo b", depends_on=[first.id])
    store.add(first.model_copy(update={"depends_on": [second.id]}))
    store.add(second)

    exit_code, _stdout, stderr = _capture_main(["submit", "echo c"])

    assert exit_code == 5, (
        f"a pre-existing cycle must exit 5 on submit; got {exit_code}; "
        f"stderr={stderr!r}"
    )
    assert "dependency cycle" in stderr, (
        f"stderr must announce the cycle; got {stderr!r}"
    )
    assert "→" in stderr, (
        f"the cycle path must be rendered with '→'; got {stderr!r}"
    )


# NFR-09
def test_fr05_submit_rejects_chain_deeper_than_cap_in_process(
    taskq_home, monkeypatch
):
    """Submitting into a chain already past `TASKQ_MAX_DAG_DEPTH`
    surfaces the depth-cap rejection (line 369-372)."""
    # Build a chain A -> B via real submits so the depth counter goes
    # through `_chain_depths` for the next submission.
    a_id = _submit_in_process("echo a")
    _submit_in_process("echo b", "--after", a_id)

    monkeypatch.setenv("TASKQ_MAX_DAG_DEPTH", "1")

    exit_code, _stdout, stderr = _capture_main(
        ["submit", "echo c", "--after", a_id]
    )

    assert exit_code == 5, (
        f"a chain deeper than the cap must exit 5; got {exit_code}; "
        f"stderr={stderr!r}"
    )
    assert "dependency chain too deep" in stderr, (
        f"stderr must report the cap breach; got {stderr!r}"
    )


# NFR-09
def test_fr05_run_all_returns_zero_on_out_of_band_cycle_in_process(taskq_home):
    """`run --all` against a cyclic persisted graph exits 0 without
    dispatching (the `if remaining: return 0` branch in `_run_all`).
    Covers line 585."""
    from taskq_plus.cli import commands
    from taskq_plus.models.task import Task
    from taskq_plus.storage.task_store import get_store

    store = get_store()
    first = Task(command="echo a")
    second = Task(command="echo b", depends_on=[first.id])
    store.add(first.model_copy(update={"depends_on": [second.id]}))
    store.add(second)

    exit_code, _stdout, stderr = _capture_main(["run", "--all"])
    assert exit_code == 0, (
        f"`run --all` against a cyclic graph must exit 0 without "
        f"dispatching; got {exit_code}; stderr={stderr!r}"
    )


# NFR-09
def test_fr05_run_all_skips_non_pending_tasks_in_process(taskq_home):
    """`_run_all` skips tasks whose `status` is already non-`pending`
    (e.g. an earlier `done` row when new submits land in the same
    session). Covers line 598."""
    first = _submit_in_process("echo first")
    _submit_in_process("echo second")

    # Execute the first task so it transitions out of `pending`.
    run_exit, _stdout, _stderr = _capture_main(["run", first])
    assert run_exit == 0, (
        f"setup run must succeed; got {run_exit}"
    )

    # `run --all` must dispatch the still-pending task and skip the
    # already-`done` one (the `if task.status != 'pending': continue`
    # branch on line 598).
    all_exit, _stdout, _stderr = _capture_main(["run", "--all"])
    assert all_exit == 0, (
        f"`run --all` must exit 0; got {all_exit}; stderr={_stderr!r}"
    )

    records = {r["id"]: r for r in _read_tasks_json(taskq_home)}
    assert records[first]["status"] == "done", (
        f"the previously-done task must remain done; got {records[first]['status']!r}"
    )


# NFR-09
def test_fr05_run_all_marks_blocked_task_when_prereq_failed_in_process(taskq_home):
    """When a prerequisite fails, downstream tasks are persisted as
    `blocked` and skipped (the `_utcnow` + `store.update` blocked-mark
    branch). Covers lines 606-622 and 626."""
    # A failing task + a downstream that depends on it.
    a_id = _submit_in_process("false")
    b_id = _submit_in_process("echo b", "--after", a_id)

    # Run A so its status becomes `failed`.
    run_exit, _stdout, _stderr = _capture_main(["run", a_id])
    assert run_exit == 0, f"run A must succeed; got {run_exit}"

    # Now run --all: B should be blocked (prereq A is non-`done`).
    all_exit, _stdout, _stderr = _capture_main(["run", "--all"])
    assert all_exit == 0, f"run --all must exit 0; got {all_exit}"

    records = {r["id"]: r for r in _read_tasks_json(taskq_home)}
    assert records[b_id]["status"] == "blocked", (
        f"B must be marked blocked when its prereq failed; got "
        f"{records[b_id]['status']!r}"
    )
    assert records[b_id]["finished_at"] is not None, (
        f"a blocked row must carry a finished_at timestamp; got "
        f"{records[b_id].get('finished_at')!r}"
    )


# NFR-09
def test_fr05_run_all_with_single_worker_dispatches_sequentially_in_process(
    taskq_home, monkeypatch
):
    """`TASKQ_MAX_WORKERS=1` collapses `_run_all` to the sequential
    `max_workers == 1` branch. Covers lines 635-644 (the
    `for task in runnable:` sequential loop)."""
    monkeypatch.setenv("TASKQ_MAX_WORKERS", "1")

    a_id = _submit_in_process("echo a")
    _submit_in_process("echo b")

    exit_code, _stdout, stderr = _capture_main(["run", "--all"])
    assert exit_code == 0, (
        f"`run --all` with max_workers=1 must exit 0; got {exit_code}; "
        f"stderr={stderr!r}"
    )

    records = {r["id"]: r for r in _read_tasks_json(taskq_home)}
    assert records[a_id]["status"] == "done", (
        f"`run --all` with 1 worker must still execute every pending "
        f"task; got {records[a_id]['status']!r}"
    )


# NFR-09
def test_fr05_run_all_multi_worker_records_failure_in_process(taskq_home):
    """With `TASKQ_MAX_WORKERS > 1` a failing task inside the layer's
    `ThreadPoolExecutor` block exercises the
    `elif status in ('failed', 'timeout'): breaker.record_failure()`
    branch. Covers line 655-656."""
    # Two failing tasks at the same depth (independent roots) so the
    # thread pool sees both.
    _submit_in_process("false")
    _submit_in_process("false")

    exit_code, _stdout, stderr = _capture_main(["run", "--all"])
    assert exit_code == 0, f"`run --all` must exit 0; got {exit_code}"

    breaker_path = taskq_home / "breaker.json"
    assert breaker_path.exists(), (
        f"the breaker must be persisted after a multi-worker failure; "
        f"missing {breaker_path}"
    )
    payload = _json.loads(breaker_path.read_text(encoding="utf-8"))
    assert payload.get("failure_count", 0) >= 2, (
        f"two failing tasks in the worker pool must record failure_count >= 2; "
        f"got {payload!r}"
    )


# NFR-09
def test_fr05_export_in_process_renders_json_csv_md_in_process(taskq_home):
    """`commands.export` runs the full parser + handler path that
    `_build_export_parser` (lines 834-844) and the `export` body
    (lines 866-878) share. Covers all of those lines."""
    from taskq_plus.cli import commands

    _submit_in_process("echo hi")

    for fmt in ("json", "csv", "md"):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            exit_code = commands.export(["--format", fmt], use_disk=False)
        assert exit_code == 0, (
            f"export --format {fmt} must exit 0; got {exit_code}; "
            f"stderr={err.getvalue()!r}"
        )
        assert out.getvalue(), (
            f"export --format {fmt} must write to stdout; got empty output"
        )


# NFR-04 / NFR-09
def test_fr05_main_dispatches_export_in_process(taskq_home):
    """`main(["export", "--format", "json"])` reaches the
    `args.command == "export"` branch (line 178 in main.py) and the
    `commands.export` handler."""
    from taskq_plus.cli.main import main

    _submit_in_process("echo hi")

    buf, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
        exit_code = main(["export", "--format", "json"])
    assert exit_code == 0, (
        f"main(['export', '--format', 'json']) must exit 0; got {exit_code}; "
        f"stderr={err.getvalue()!r}"
    )
    assert buf.getvalue(), (
        f"main(['export', '--format', 'json']) must write JSON to stdout; "
        f"got empty output"
    )


# ---------------------------------------------------------------------------
# Coverage helpers
# ---------------------------------------------------------------------------


def _hash_command(command: str) -> str:
    """Return the FR-04 sha256 signature for `command` (matches
    `taskq_plus.service.cache.signature`)."""
    import hashlib as _hashlib

    return _hashlib.sha256(command.encode("utf-8")).hexdigest()
