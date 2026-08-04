"""FR-02: 任務執行器 — task execution.

Test cases correspond 1:1 to TEST_SPEC.md §FR-02 (rows 1–5). The function
names below are the canonical names `spec-coverage-check` looks up — do
NOT rename. State machine per SPEC §3 FR-02:

    pending → running → done | failed | timeout | blocked
    exit 0            → done
    exit ≠ 0          → failed
    TimeoutExpired    → timeout
    dependency unmet  → blocked  (FR-06)

The subprocess tests exercise the real `python -m taskq_plus` entry
point; the in-process tests at the bottom import the declared SAB
modules directly so pytest-cov can measure `taskq_plus.cli.commands`
and `taskq_plus.service.executor` (subprocess code shows 0% coverage
by design — pytest-cov cannot instrument code running inside another
process).
"""
from __future__ import annotations

import contextlib
import io
import json as _json
import re
import subprocess
import sys
from pathlib import Path



# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# 8-hex-char task id pattern (uuid4 prefix).
TASK_ID_RE = re.compile(r"^[0-9a-f]{8}$")


def _run_submit_subprocess(args: list, env: dict) -> subprocess.CompletedProcess:
    """`python -m taskq_plus submit ...` in a child process.

    Out-of-process decision: the canonical SPEC §8 rows spell out
    `python -m taskq_plus` literally, so the acceptance tests reproduce
    the user-facing entry point. The in-process helpers below exercise
    the same code paths through the declared SAB module so pytest-cov
    can measure them.
    """
    return subprocess.run(
        [sys.executable, "-m", "taskq_plus", "submit", *args],
        capture_output=True,
        text=True,
        env=env,
    )


def _run_subprocess(args: list, env: dict) -> subprocess.CompletedProcess:
    """`python -m taskq_plus run ...` in a child process.

    Accepts both `run <id>` (single-task) and `run --all` (multi-task
    DAG-topological sweep).
    """
    return subprocess.run(
        [sys.executable, "-m", "taskq_plus", "run", *args],
        capture_output=True,
        text=True,
        env=env,
    )


def _submit_and_get_id(args: list, env: dict) -> str:
    """Submit a task via the CLI and return its 8-hex id.

    Asserts the submit succeeded and the stdout matches the id regex so
    the caller can immediately use the id without re-asserting the same
    invariants.
    """
    proc = _run_submit_subprocess(args, env)
    assert proc.returncode == 0, (
        f"submit must exit 0; got {proc.returncode}; stderr={proc.stderr!r}"
    )
    task_id = proc.stdout.strip()
    assert TASK_ID_RE.match(task_id), (
        f"submit stdout {task_id!r} is not an 8-hex id"
    )
    return task_id


def _read_tasks_json(taskq_home: Path) -> list:
    """Read and parse `$TASKQ_HOME/tasks.json` (empty list if missing).

    Used by every FR-02 case to verify the persisted result fields
    (`status`, `exit_code`, `stdout_tail`, `stderr_tail`, `duration_ms`,
    `finished_at`) without coupling to the in-memory store.
    """
    tasks_file = taskq_home / "tasks.json"
    if not tasks_file.exists():
        return []
    return _json.loads(tasks_file.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Case 1 — AC-02-1: run on a previously done command reproduces result
# ---------------------------------------------------------------------------


# NFR-09 (test_assertion_quality)
def test_fr02_run_done_reproduces_result(taskq_home, child_env):
    """AC-02-1: `python -m taskq_plus run <id>` on a task whose command
    is `echo hi` produces `status=done`, `exit_code=0`, `stdout_tail`
    containing "hi". A replay `run` reproduces the same exit_code and
    stdout_tail. *(SPEC §3 FR-02 + §7)*

    NFR-09 (test_assertion_quality): asserts both the CLI exit code and
    the persisted result fields (status, exit_code, stdout_tail) for
    state-machine verification.
    """
    # 1. Submit a pending task
    task_id = _submit_and_get_id(["echo hi"], child_env)

    # 2. Run the task (first invocation)
    proc = _run_subprocess([task_id], child_env)
    assert proc.returncode == 0, (
        f"first run must exit 0, got {proc.returncode}; "
        f"stderr={proc.stderr!r}"
    )

    # 3. Verify the persisted result fields
    payload = _read_tasks_json(taskq_home)
    assert len(payload) == 1, f"expected 1 task, got {len(payload)}"
    task = payload[0]
    assert task["id"] == task_id
    assert task["status"] == "done", (
        f"echo hi must be status=done, got {task['status']!r}"
    )
    assert task["exit_code"] == 0
    assert "hi" in task["stdout_tail"], (
        f"stdout_tail must contain 'hi'; got {task['stdout_tail']!r}"
    )

    # 4. Replay: run the same id again. The result must be reproducible
    #    (echo hi is deterministic; the on-disk record re-emits the same
    #    shape).
    proc2 = _run_subprocess([task_id], child_env)
    assert proc2.returncode == 0, (
        f"replay run must exit 0, got {proc2.returncode}; "
        f"stderr={proc2.stderr!r}"
    )
    payload2 = _read_tasks_json(taskq_home)
    task2 = payload2[0]
    assert task2["status"] == "done"
    assert task2["exit_code"] == 0
    assert "hi" in task2["stdout_tail"], (
        f"replay stdout_tail must contain 'hi'; got {task2['stdout_tail']!r}"
    )


# ---------------------------------------------------------------------------
# Case 2 — AC-02-2: TASKQ_TASK_TIMEOUT=1 + sleep 5 → exit 4, status timeout
# ---------------------------------------------------------------------------


# NFR-15 (timeout, SAD-forced) / NFR-09
def test_fr02_run_timeout_returns_exit_4(taskq_home, child_env):
    """AC-02-2: `TASKQ_TASK_TIMEOUT=1 python -m taskq_plus run <sleep-5-id>`
    produces `status=timeout`, `exit_code=4`. *(SPEC §8 #7)*

    NFR-15 (timeout, SAD-forced): exercises the
    `subprocess.run(..., timeout=)` exception path. Out-of-process
    because the timeout budget is configured via the child env.
    """
    # 1. Submit a long-running task
    task_id = _submit_and_get_id(["sleep 5"], child_env)

    # 2. Force a 1-second timeout budget (overrides the 10s default).
    #    Mutating the per-test `child_env` is safe because the fixture
    #    is function-scoped; this is the only place TASKQ_TASK_TIMEOUT
    #    is set.
    child_env["TASKQ_TASK_TIMEOUT"] = "1"

    # 3. Run; expect exit 4 (the explicit timeout CLI exit code).
    proc = _run_subprocess([task_id], child_env)
    assert proc.returncode == 4, (
        f"timeout must yield exit 4, got {proc.returncode}; "
        f"stderr={proc.stderr!r}"
    )

    # 4. Verify the persisted status
    payload = _read_tasks_json(taskq_home)
    assert len(payload) == 1
    task = payload[0]
    assert task["id"] == task_id
    assert task["status"] == "timeout", (
        f"timed-out task must be status=timeout, got {task['status']!r}"
    )


# ---------------------------------------------------------------------------
# Case 3 — AC-02-3: grep -rn "shell=True" 03-development/src/ → 0 hits
# ---------------------------------------------------------------------------


# NFR-02 (security) / NFR-09
def test_fr02_source_contains_no_shell_true():
    """AC-02-3: `grep -rn "shell=True" 03-development/src/` yields 0
    hits. *(SPEC §8 #15; NFR-02)*

    NFR-02 (security): `shell=True` is forbidden on every code path
    (SPEC §3 FR-02 + §4 NFR-02). This is the canonical grep gate that
    protects the executor from shell injection.

    Out-of-process decision: the AC literally invokes `grep` as a
    subprocess; the test reproduces that exactly so the gate runs
    against the same line-by-line scan CI will run.

    CWD independence: mutmut's baseline test run executes pytest from a
    temp workdir where the literal SPEC path ``03-development/src/``
    does not exist. Resolve the source root from ``__file__`` so the
    scan stays anchored to ``03-development/src/`` regardless of cwd.
    """
    src_root = Path(__file__).resolve().parent.parent / "src"
    proc = subprocess.run(
        ["grep", "-rn", "--", "shell=True", str(src_root)],
        capture_output=True,
        text=True,
    )
    # grep returns 0 (hits found) or 1 (no hits); 2 means a usage error.
    assert proc.returncode in (0, 1), (
        f"grep must return 0 (hits) or 1 (no hits); got {proc.returncode}; "
        f"stderr={proc.stderr!r}"
    )
    assert proc.stdout == "", (
        f"shell=True must not appear in {src_root}; got:\n"
        f"{proc.stdout!r}"
    )


# ---------------------------------------------------------------------------
# Case 4 — AC-02-4: two parallel run --all; tasks.json remains valid JSON
# ---------------------------------------------------------------------------


# NFR-13 (concurrency, SAD-forced) / NFR-03 (atomicity) / NFR-09
def test_fr02_run_all_concurrent_write_is_atomic(taskq_home, child_env):
    """AC-02-4: Two parallel `python -m taskq_plus run --all` invocations
    on independent commands do not corrupt `$TASKQ_HOME/tasks.json`;
    mid-write state remains valid JSON. *(SPEC §3 FR-02 + §4 NFR-03)*

    NFR-13 (concurrency, SAD-forced): the `ThreadPoolExecutor` + shared
    `threading.Lock` invariant on `taskq_plus.storage.task_store`.
    NFR-03 (atomicity): every write is `tmp + os.replace`; a mid-write
    kill must leave the on-disk file as valid JSON.

    Out-of-process decision: the two invocations share the same
    `$TASKQ_HOME` and contend for the same file, so the test must
    spawn real child processes (in-process serial runs cannot exercise
    the cross-process lock invariant). `PYTHONPATH` propagation is
    handled by the `child_env` fixture in `conftest.py`.
    """
    # 1. Submit two independent pending tasks
    task_a_id = _submit_and_get_id(["echo a"], child_env)
    task_b_id = _submit_and_get_id(["echo b"], child_env)

    # 2. Spawn two parallel `run --all` subprocesses against the same
    #    $TASKQ_HOME. Each invocation iterates the pending set via
    #    ThreadPoolExecutor(max_workers=TASKQ_MAX_WORKERS) and writes
    #    status updates back to tasks.json. The atomicity invariant
    #    requires the file to remain valid JSON throughout.
    proc_a = subprocess.Popen(
        [sys.executable, "-m", "taskq_plus", "run", "--all"],
        env=child_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    proc_b = subprocess.Popen(
        [sys.executable, "-m", "taskq_plus", "run", "--all"],
        env=child_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    _out_a, _err_a = proc_a.communicate(timeout=30)
    _out_b, _err_b = proc_b.communicate(timeout=30)

    # 3. Both invocations must have completed (the test only checks the
    #    atomicity invariant — exit codes can be 0 or non-zero depending
    #    on which subprocess won the race for each task).
    assert proc_a.returncode is not None, "proc_a must complete"
    assert proc_b.returncode is not None, "proc_b must complete"

    # 4. tasks.json must remain valid JSON (atomicity invariant).
    tasks_file = taskq_home / "tasks.json"
    assert tasks_file.exists(), "tasks.json must exist after run --all"
    payload = _json.loads(tasks_file.read_text(encoding="utf-8"))
    assert isinstance(payload, list), "tasks.json must decode to a list"

    # 5. Both submitted tasks must be present in the final state.
    ids = {t["id"] for t in payload}
    assert task_a_id in ids, f"task {task_a_id!r} must appear in tasks.json"
    assert task_b_id in ids, f"task {task_b_id!r} must appear in tasks.json"

    # 6. Every task must be in a terminal state (no half-written record).
    terminal = {"done", "failed", "timeout", "blocked"}
    for t in payload:
        assert t["status"] in terminal, (
            f"task {t['id']!r} has non-terminal status {t['status']!r}; "
            f"possible mid-write corruption"
        )


# ---------------------------------------------------------------------------
# Case 5 — AC-02-5: non-zero exit (`false`) → status=failed
# ---------------------------------------------------------------------------


# NFR-09
def test_fr02_nonzero_exit_marks_task_failed(taskq_home, child_env):
    """AC-02-5: `python -m taskq_plus run <id>` on a task whose command
    is `false` (exit code 1) produces `status=failed`, `exit_code=1`.
    *(SPEC §3 FR-02 state machine)*

    NFR-09 (test_assertion_quality): the non-zero exit path must be
    classified as `failed` (not `done`). Per SPEC §3 FR-02 only
    `timeout` maps to a non-zero CLI exit code (exit 4); the CLI itself
    exits 0 for `failed` because the failure is recorded in the task
    record.
    """
    # 1. Submit a task whose command exits with code 1.
    task_id = _submit_and_get_id(["false"], child_env)

    # 2. Run it. The CLI invocation itself succeeds (exit 0); the
    #    failure is recorded in the task's `status` and `exit_code`.
    proc = _run_subprocess([task_id], child_env)
    assert proc.returncode == 0, (
        f"run on a failed task must exit 0 (failure is in the task "
        f"record); got {proc.returncode}; stderr={proc.stderr!r}"
    )

    # 3. Verify the persisted status
    payload = _read_tasks_json(taskq_home)
    assert len(payload) == 1
    task = payload[0]
    assert task["id"] == task_id
    assert task["status"] == "failed", (
        f"non-zero exit must be status=failed, got {task['status']!r}"
    )
    assert task["exit_code"] == 1, (
        f"task exit_code must be 1 (false's exit), got {task['exit_code']!r}"
    )


# ===========================================================================
# In-process coverage tests
# ---------------------------------------------------------------------------
# The five cases above are the canonical TEST_SPEC.md §FR-02 rows and are
# NOT modified. The tests below are additive: they exercise the same FR-02
# execution surface through direct in-process calls so `coverage` can
# measure `taskq_plus.cli.commands.run` and `taskq_plus.service.executor`
# (neither of which is measurable through the `subprocess.run` acceptance
# path). Both modules are declared in `SAB.json` §fr_module_traceability
# for FR-02 — their presence is enforced by the Architecture Amendment
# Protocol.
# ===========================================================================


# ---------------------------------------------------------------------------
# `taskq_plus.cli.commands` — the `run` dispatcher
# ---------------------------------------------------------------------------


# NFR-09
def test_fr02_inprocess_run_echo_hi_completes(taskq_home):
    """In-process: `commands.run(["<id>"])` executes `echo hi` and writes
    the result fields back to tasks.json.

    Subprocess code shows 0% coverage by design — this in-process test
    is the path that exercises the dispatcher the user-facing CLI also
    drives.
    """
    # GREEN TODO: `taskq_plus.cli.commands` must expose a callable that
    # accepts argv (excluding the leading "run" keyword) and returns an
    # integer exit code — for example:
    #     def run(argv: list[str], *, use_disk: bool = False) -> int: ...
    # The handler must:
    #   - resolve the task id from argv[0]
    #   - dispatch to `taskq_plus.service.executor.run_task(task, timeout=...)`
    #   - persist the resulting TaskResult (exit_code, stdout_tail,
    #     stderr_tail, duration_ms, finished_at, status) via the store
    #   - return 0 for done / failed, 4 for timeout
    from taskq_plus.cli import commands
    from taskq_plus.models.task import Task
    from taskq_plus.storage.task_store import (
        get_store,
        make_disk_store,
        reset_store_cache,
    )

    reset_store_cache()
    store = make_disk_store()
    task = store.add(Task(command="echo hi"))
    task_id = task.id

    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    with contextlib.redirect_stdout(stdout_buf), contextlib.redirect_stderr(stderr_buf):
        exit_code = commands.run([task_id], use_disk=True)

    assert exit_code == 0, (
        f"in-process run must exit 0; stderr={stderr_buf.getvalue()!r}"
    )

    # Re-read the on-disk state and verify the result fields
    reset_store_cache()
    store = get_store(use_disk=True)
    finished = [t for t in store.load() if t.id == task_id]
    assert len(finished) == 1, "task must be persisted"
    updated = finished[0]
    assert updated.status == "done", (
        f"echo hi must be status=done, got {updated.status!r}"
    )
    assert updated.exit_code == 0
    assert "hi" in (updated.stdout_tail or ""), (
        f"stdout_tail must contain 'hi'; got {updated.stdout_tail!r}"
    )


# NFR-09 / NFR-15
def test_fr02_inprocess_run_sleep_exceeds_timeout(taskq_home, monkeypatch):
    """In-process: `commands.run(["<id>"])` on a `sleep 5` task with a
    1-second timeout budget marks the task `timeout` and returns 4.

    GREEN TODO: `commands.run` must read `TASKQ_TASK_TIMEOUT` at call
    time and pass it through to the executor; the executor must wrap
    `subprocess.run(..., timeout=budget)` and translate
    `TimeoutExpired` into a `timeout` TaskResult.
    """
    monkeypatch.setenv("TASKQ_TASK_TIMEOUT", "1")
    from taskq_plus.cli import commands
    from taskq_plus.models.task import Task
    from taskq_plus.storage.task_store import (
        get_store,
        make_disk_store,
        reset_store_cache,
    )

    reset_store_cache()
    store = make_disk_store()
    task = store.add(Task(command="sleep 5"))
    task_id = task.id

    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    with contextlib.redirect_stdout(stdout_buf), contextlib.redirect_stderr(stderr_buf):
        exit_code = commands.run([task_id], use_disk=True)

    assert exit_code == 4, (
        f"timeout must yield CLI exit 4; got {exit_code}; "
        f"stderr={stderr_buf.getvalue()!r}"
    )

    reset_store_cache()
    store = get_store(use_disk=True)
    finished = [t for t in store.load() if t.id == task_id][0]
    assert finished.status == "timeout", (
        f"sleep 5 with 1s budget must be status=timeout, "
        f"got {finished.status!r}"
    )


# NFR-09
def test_fr02_inprocess_run_false_marks_task_failed(taskq_home):
    """In-process: `commands.run(["<id>"])` on a `false` command records
    `status=failed` and `exit_code=1`, returning CLI exit 0.

    GREEN TODO: the executor must return a TaskResult with
    `status="failed"` for any non-zero exit code (other than
    `TimeoutExpired`); the CLI dispatcher must propagate that to the
    store.
    """
    from taskq_plus.cli import commands
    from taskq_plus.models.task import Task
    from taskq_plus.storage.task_store import (
        get_store,
        make_disk_store,
        reset_store_cache,
    )

    reset_store_cache()
    store = make_disk_store()
    task = store.add(Task(command="false"))
    task_id = task.id

    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    with contextlib.redirect_stdout(stdout_buf), contextlib.redirect_stderr(stderr_buf):
        exit_code = commands.run([task_id], use_disk=True)

    assert exit_code == 0, (
        f"run on a failed task must exit 0; got {exit_code}; "
        f"stderr={stderr_buf.getvalue()!r}"
    )

    reset_store_cache()
    store = get_store(use_disk=True)
    finished = [t for t in store.load() if t.id == task_id][0]
    assert finished.status == "failed", (
        f"non-zero exit must be status=failed, got {finished.status!r}"
    )
    assert finished.exit_code == 1


# ---------------------------------------------------------------------------
# `taskq_plus.service.executor` — the subprocess + state machine primitive
# ---------------------------------------------------------------------------


# NFR-02 (security) / NFR-09
def test_fr02_executor_module_is_importable():
    """The executor module is declared in SAB.json §fr_module_traceability
    for FR-02. Its on-disk presence is enforced by the Architecture
    Amendment Protocol.

    GREEN TODO: `taskq_plus.service.executor` must exist as a leaf
    module (or a package) and expose at least `run_task(task, *, timeout)`
    implementing the state machine (pending → running → done | failed |
    timeout | blocked). It must call
    `subprocess.run(shlex.split(task.command), capture_output=True,
    text=True, timeout=timeout)` and **never** use `shell=True` (NFR-02).
    """
    import taskq_plus.service.executor as executor_module  # noqa: F401
    assert executor_module is not None


# NFR-09
def test_fr02_executor_run_task_returns_done_for_successful_command():
    """`taskq_plus.service.executor.run_task(task, timeout=...)` returns
    a TaskResult with `status="done"`, `exit_code=0`, and `stdout_tail`
    containing the command's output for a successful command.

    GREEN TODO: the executor must define a `TaskResult` model (or
    equivalent) carrying `exit_code`, `stdout_tail`, `stderr_tail`,
    `duration_ms`, `finished_at`, `status` — the persisted result shape
    from SPEC §3 FR-02.
    """
    from taskq_plus.models.task import Task
    from taskq_plus.service import executor

    task = Task(command="echo hi")
    result = executor.run_task(task, timeout=10.0)

    assert result.status == "done", (
        f"echo hi must complete as done, got {result.status!r}"
    )
    assert result.exit_code == 0
    assert "hi" in (result.stdout_tail or ""), (
        f"stdout_tail must contain 'hi'; got {result.stdout_tail!r}"
    )
    assert result.duration_ms is not None
    assert result.finished_at is not None


# NFR-09 / NFR-15
def test_fr02_executor_run_task_returns_timeout_when_subprocess_exceeds_budget():
    """`run_task` translates `subprocess.TimeoutExpired` into
    `status="timeout"`.

    GREEN TODO: the executor must catch `subprocess.TimeoutExpired`
    around the `subprocess.run(..., timeout=budget)` call and surface
    a TaskResult with `status="timeout"`.
    """
    from taskq_plus.models.task import Task
    from taskq_plus.service import executor

    task = Task(command="sleep 5")
    result = executor.run_task(task, timeout=1.0)

    assert result.status == "timeout", (
        f"sleep 5 with 1s budget must be status=timeout, "
        f"got {result.status!r}"
    )


# NFR-09
def test_fr02_executor_run_task_returns_failed_for_nonzero_exit():
    """`run_task` classifies any non-zero exit (other than
    `TimeoutExpired`) as `status="failed"` and propagates the
    subprocess's `returncode` as `exit_code`.

    GREEN TODO: the executor must check the completed process's
    `returncode` after `subprocess.run` returns and map non-zero
    returns to `status="failed"`, leaving `TimeoutExpired` to the
    timeout branch.
    """
    from taskq_plus.models.task import Task
    from taskq_plus.service import executor

    task = Task(command="false")
    result = executor.run_task(task, timeout=10.0)

    assert result.status == "failed", (
        f"non-zero exit must be status=failed, got {result.status!r}"
    )
    assert result.exit_code == 1


# ---------------------------------------------------------------------------
# `taskq_plus.cli.commands.submit` — in-process validation paths
# ---------------------------------------------------------------------------
# These drive `commands.submit(...)` directly so `pytest-cov` can measure
# the same validation surfaces the subprocess tests in `test_fr01.py`
# exercise. They are the in-process counterpart to FR-01 cases 1–7; from
# FR-02's perspective they exist solely to keep coverage measurable on
# the shared CLI dispatcher (subprocess tests cannot raise coverage here).


def _capture_submit(argv):
    """Run `commands.submit(argv)` with stdout/stderr redirected."""
    from taskq_plus.cli import commands  # local import — sys.path injection is per-test

    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = commands.submit(argv)
    return rc, out.getvalue(), err.getvalue()


def _capture_run(argv, *, use_disk: bool = False):
    """Run `commands.run(argv)` with stdout/stderr redirected."""
    from taskq_plus.cli import commands  # local import — sys.path injection is per-test

    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = commands.run(argv, use_disk=use_disk)
    return rc, out.getvalue(), err.getvalue()


# NFR-09
def test_fr02_inprocess_submit_valid_command(taskq_home):
    """In-process: `commands.submit(["echo hi"])` returns 0 and prints the
    new task id.
    """
    from taskq_plus.storage.task_store import (
        get_store,
        reset_store_cache,
    )

    reset_store_cache()
    rc, stdout, stderr = _capture_submit(["echo hi"])
    assert rc == 0, f"valid submit must exit 0; stderr={stderr!r}"
    task_id = stdout.strip()
    assert TASK_ID_RE.match(task_id), (
        f"submit must print an 8-hex id; got {task_id!r}"
    )

    # Do NOT reset the store cache here — `submit()` just populated the
    # in-memory backend; clearing would drop the task before we can read it.
    store = get_store(use_disk=False)
    tasks = store.load()
    assert len(tasks) == 1
    assert tasks[0].id == task_id
    assert tasks[0].command == "echo hi"
    assert tasks[0].status == "pending"


# NFR-09
def test_fr02_inprocess_submit_empty_command_rejected(taskq_home):
    """In-process: an empty `command` is rejected by pydantic validation
    and surfaces via `_format_validation_error` + `_emit_stderr_error`
    with exit code 2.
    """
    from taskq_plus.storage.task_store import reset_store_cache

    reset_store_cache()
    rc, stdout, stderr = _capture_submit([""])
    assert rc == 2, f"empty command must exit 2; got {rc}"
    assert "submit:" in stderr, (
        f"validation error must be prefixed with 'submit:'; got {stderr!r}"
    )
    assert "empty" in stderr.lower(), (
        f"empty-command validation message must mention 'empty'; "
        f"got {stderr!r}"
    )


# NFR-09 / NFR-02 (injection blacklist)
def test_fr02_inprocess_submit_injection_rejected(taskq_home):
    """In-process: a command containing an injection character (`;`)
    is rejected with exit 2 and the validation message names the bad
    character.
    """
    from taskq_plus.storage.task_store import reset_store_cache

    reset_store_cache()
    rc, stdout, stderr = _capture_submit(["echo hi; rm x"])
    assert rc == 2, f"injection command must exit 2; got {rc}"
    assert "submit:" in stderr
    assert ";" in stderr, (
        f"injection-char error must name the bad character ';'; "
        f"got {stderr!r}"
    )


# NFR-09
def test_fr02_inprocess_submit_duplicate_name_rejected(taskq_home):
    """In-process: a second submission with the same `--name` against a
    pending task is rejected with exit 2 and the duplicate-name marker.
    """
    from taskq_plus.storage.task_store import reset_store_cache

    reset_store_cache()
    rc1, _, _ = _capture_submit(["echo a", "--name", "dup"])
    assert rc1 == 0, "first submit must succeed"

    rc2, stdout, stderr = _capture_submit(["echo b", "--name", "dup"])
    assert rc2 == 2, f"duplicate name must exit 2; got {rc2}"
    assert "duplicate name" in stderr, (
        f"duplicate-name error must say 'duplicate name'; got {stderr!r}"
    )
    assert "dup" in stderr, (
        f"duplicate-name error must echo the offending name; got {stderr!r}"
    )


# NFR-09
def test_fr02_inprocess_submit_unknown_dependency_rejected(taskq_home):
    """In-process: `--after deadbeef` against a non-existent task is
    rejected with exit 2 and the unknown-dependency marker.
    """
    from taskq_plus.storage.task_store import reset_store_cache

    reset_store_cache()
    rc, stdout, stderr = _capture_submit(
        ["echo b", "--after", "deadbeef"]
    )
    assert rc == 2, f"unknown dependency must exit 2; got {rc}"
    assert "unknown dependency" in stderr, (
        f"unknown-dep error must say 'unknown dependency'; got {stderr!r}"
    )
    assert "deadbeef" in stderr, (
        f"unknown-dep error must echo the offending id; got {stderr!r}"
    )


# NFR-09
def test_fr02_inprocess_submit_json_flag_emits_json(taskq_home):
    """In-process: `--json` causes `commands.submit` to print a JSON
    object with the new task's `id` and `status` (instead of just the id).
    """
    from taskq_plus.storage.task_store import reset_store_cache

    reset_store_cache()
    rc, stdout, stderr = _capture_submit(["echo hi", "--json"])
    assert rc == 0, f"--json submit must exit 0; stderr={stderr!r}"
    payload = _json.loads(stdout.strip())
    assert "id" in payload, f"--json payload must include 'id'; got {payload!r}"
    assert payload["status"] == "pending", (
        f"new task must be status=pending; got {payload['status']!r}"
    )
    assert TASK_ID_RE.match(payload["id"]), (
        f"--json payload id must match 8-hex pattern; got {payload['id']!r}"
    )


# NFR-09 (boundary)
def test_fr02_inprocess_submit_at_length_limit_accepted(taskq_home):
    """In-process: a command whose length is exactly 1000 chars is
    accepted (length cap is strict-greater-than, SPEC §3 line 81).
    """
    from taskq_plus.storage.task_store import reset_store_cache

    # `echo ` (5) + 995 x chars = exactly 1000 chars.
    boundary_cmd = "echo " + ("x" * 995)
    assert len(boundary_cmd) == 1000

    reset_store_cache()
    rc, stdout, stderr = _capture_submit([boundary_cmd])
    assert rc == 0, (
        f"1000-char command must be accepted; got exit {rc}; "
        f"stderr={stderr!r}"
    )


# NFR-09 (boundary)
def test_fr02_inprocess_submit_over_length_limit_rejected(taskq_home):
    """In-process: a command whose length is 1001 chars is rejected
    with exit 2 and a length-cap message.
    """
    from taskq_plus.storage.task_store import reset_store_cache

    over_cmd = "echo " + ("x" * 996)
    assert len(over_cmd) == 1001

    reset_store_cache()
    rc, stdout, stderr = _capture_submit([over_cmd])
    assert rc == 2, f"1001-char command must be rejected; got exit {rc}"
    assert "submit:" in stderr
    assert "length" in stderr.lower() or "exceeds" in stderr.lower(), (
        f"length-cap error must mention length/exceeds; got {stderr!r}"
    )


# ---------------------------------------------------------------------------
# `taskq_plus.cli.commands.run` — in-process error / --all paths
# ---------------------------------------------------------------------------


# NFR-09
def test_fr02_inprocess_run_no_args_returns_exit_2(taskq_home):
    """In-process: `commands.run([])` (no id, no --all) returns 2 with a
    stderr marker — the user-facing usage error.
    """
    from taskq_plus.storage.task_store import reset_store_cache

    reset_store_cache()
    rc, stdout, stderr = _capture_run([], use_disk=True)
    assert rc == 2, f"no-args run must exit 2; got {rc}"
    assert "run:" in stderr, (
        f"usage error must be prefixed with 'run:'; got {stderr!r}"
    )


# NFR-09
def test_fr02_inprocess_run_task_not_found_returns_exit_2(taskq_home):
    """In-process: `commands.run(["deadbeef"])` returns 2 with a
    `run: task 'deadbeef' not found` stderr marker.
    """
    from taskq_plus.storage.task_store import reset_store_cache

    reset_store_cache()
    rc, stdout, stderr = _capture_run(["deadbeef"], use_disk=True)
    assert rc == 2, f"missing-id run must exit 2; got {rc}"
    assert "run:" in stderr
    assert "deadbeef" in stderr, (
        f"missing-id error must echo the id; got {stderr!r}"
    )
    assert "not found" in stderr, (
        f"missing-id error must say 'not found'; got {stderr!r}"
    )


# NFR-13 (concurrency, SAD-forced) / NFR-09
def test_fr02_inprocess_run_all_executes_pending(taskq_home):
    """In-process: `commands.run(["--all"])` executes every pending task
    via the thread pool and returns 0.
    """
    from taskq_plus.models.task import Task
    from taskq_plus.storage.task_store import (
        get_store,
        make_disk_store,
        reset_store_cache,
    )

    reset_store_cache()
    store = make_disk_store()
    store.add(Task(command="echo a"))
    store.add(Task(command="echo b"))

    rc, stdout, stderr = _capture_run(["--all"], use_disk=True)
    assert rc == 0, f"run --all must exit 0; stderr={stderr!r}"

    reset_store_cache()
    store = get_store(use_disk=True)
    finished = store.load()
    assert len(finished) == 2
    assert all(t.status == "done" for t in finished), (
        f"both tasks must be status=done; got "
        f"{[t.status for t in finished]!r}"
    )


# NFR-09
def test_fr02_inprocess_run_all_with_no_pending_returns_zero(taskq_home):
    """In-process: `commands.run(["--all"])` with no pending tasks returns
    0 immediately (the `_run_all` short-circuit).
    """
    from taskq_plus.storage.task_store import reset_store_cache

    reset_store_cache()
    rc, stdout, stderr = _capture_run(["--all"], use_disk=True)
    assert rc == 0, f"empty run --all must exit 0; stderr={stderr!r}"


# NFR-09
def test_fr02_inprocess_timeout_budget_invalid_value_falls_back(
    taskq_home, monkeypatch
):
    """In-process: `_timeout_budget()` returns `DEFAULT_TASK_TIMEOUT` (10.0)
    when `TASKQ_TASK_TIMEOUT` is set to a non-numeric value (the
    `except ValueError` branch).
    """
    from taskq_plus.cli import commands  # local import — sys.path injection is per-test
    from taskq_plus.models.task import Task
    from taskq_plus.storage.task_store import (
        make_disk_store,
        reset_store_cache,
    )

    monkeypatch.setenv("TASKQ_TASK_TIMEOUT", "not-a-number")
    reset_store_cache()
    store = make_disk_store()
    task = store.add(Task(command="echo hi"))

    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = commands.run([task.id], use_disk=True)

    assert rc == 0, (
        f"invalid TASKQ_TASK_TIMEOUT must fall back to default and "
        f"let echo hi complete; got exit {rc}; stderr={err.getvalue()!r}"
    )


# NFR-09
def test_fr02_inprocess_max_workers_invalid_value_falls_back(
    taskq_home, monkeypatch
):
    """In-process: `_max_workers()` returns `DEFAULT_MAX_WORKERS` (4) when
    `TASKQ_MAX_WORKERS` is set to a non-integer value (the
    `except ValueError` branch).
    """
    from taskq_plus.cli import commands  # local import — sys.path injection is per-test
    from taskq_plus.models.task import Task
    from taskq_plus.storage.task_store import (
        make_disk_store,
        reset_store_cache,
    )

    monkeypatch.setenv("TASKQ_MAX_WORKERS", "not-a-number")
    reset_store_cache()
    store = make_disk_store()
    store.add(Task(command="echo a"))
    store.add(Task(command="echo b"))

    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = commands.run(["--all"], use_disk=True)

    assert rc == 0, (
        f"invalid TASKQ_MAX_WORKERS must fall back to default and let "
        f"run --all complete; got exit {rc}; stderr={err.getvalue()!r}"
    )


# NFR-09
def test_fr02_inprocess_max_workers_explicit_value(taskq_home, monkeypatch):
    """In-process: a valid integer `TASKQ_MAX_WORKERS` is honored by
    `_max_workers()` (the int-conversion success branch).
    """
    from taskq_plus.cli import commands  # local import — sys.path injection is per-test
    from taskq_plus.models.task import Task
    from taskq_plus.storage.task_store import (
        make_disk_store,
        reset_store_cache,
    )

    monkeypatch.setenv("TASKQ_MAX_WORKERS", "2")
    reset_store_cache()
    store = make_disk_store()
    store.add(Task(command="echo a"))

    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = commands.run(["--all"], use_disk=True)

    assert rc == 0, (
        f"explicit TASKQ_MAX_WORKERS=2 must let run --all complete; "
        f"got exit {rc}; stderr={err.getvalue()!r}"
    )


# ---------------------------------------------------------------------------
# `taskq_plus.service.executor._tail` — long-output truncation
# ---------------------------------------------------------------------------


# NFR-09
def test_fr02_executor_tail_truncates_long_output():
    """`executor._tail(text)` returns the last `TAIL_CHARS` (2000) chars
    of `text` when the input is longer than `TAIL_CHARS`. This is the
    long-output branch of the executor's stdout/stderr shaper.
    """
    from taskq_plus.service import executor

    long_text = "x" * 2500
    out = executor._tail(long_text)
    assert out is not None
    assert len(out) == executor.TAIL_CHARS, (
        f"long output must be truncated to {executor.TAIL_CHARS} chars; "
        f"got {len(out)}"
    )
    assert out == "x" * executor.TAIL_CHARS, (
        "truncated output must be the *last* TAIL_CHARS chars"
    )


# NFR-09
def test_fr02_executor_run_task_long_output_is_truncated_in_result():
    """`run_task` records the truncated `stdout_tail` for a command whose
    output exceeds `TAIL_CHARS`. End-to-end check that `_tail` is wired
    into the result path (defends against accidental removal).
    """
    from taskq_plus.models.task import Task
    from taskq_plus.service import executor

    # `python -c "print('x'*2500)"` emits exactly 2500 x chars + newline,
    # so `stdout_tail` is the last 2000 chars after `_tail`.
    task = Task(command="python -c \"print('x'*2500)\"")
    result = executor.run_task(task, timeout=10.0)

    assert result.status == "done"
    assert result.exit_code == 0
    assert result.stdout_tail is not None
    assert len(result.stdout_tail) <= executor.TAIL_CHARS, (
        f"stdout_tail must be <= TAIL_CHARS; got {len(result.stdout_tail)}"
    )


# ---------------------------------------------------------------------------
# `commands._format_validation_error` — defensive empty-errors branch
# ---------------------------------------------------------------------------


# NFR-09
def test_fr02_format_validation_error_handles_empty_errors_list():
    """`_format_validation_error` returns `"validation failed"` when
    `err.errors()` yields an empty list. This is the defensive fallback
    branch (line 118) — pydantic v2 always populates the list, but the
    guard is there so the CLI never crashes on a malformed error.
    """
    from unittest.mock import MagicMock

    from pydantic import ValidationError

    from taskq_plus.cli import commands

    fake_err = MagicMock(spec=ValidationError)
    fake_err.errors.return_value = []

    msg = commands._format_validation_error(fake_err)
    assert msg == "validation failed", (
        f"empty-errors branch must return 'validation failed'; got {msg!r}"
    )
