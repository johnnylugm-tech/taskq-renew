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


# NFR-09 / NFR-05 / NFR-11
def test_fr01_submit_valid_command(taskq_home, child_env):
    """AC-01-1: `python -m taskq_plus submit "echo hi"` -> exit 0;
    stdout is an 8-hex task id. *(SPEC §8 #4)*

    NFR-09 (test_assertion_quality): asserts on exit code AND stdout shape.
    NFR-05 (documentation): every public function carries [FR-XX] tag.
    NFR-11 (readability): single-purpose, low complexity.
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


# NFR-04 / NFR-09
def test_fr01_submit_empty_command_rejected(taskq_home, child_env):
    """AC-01-2: `python -m taskq_plus submit ""` -> exit 2. *(SPEC §8 #5)*

    NFR-04 (security): empty-string rejection path; no secret leakage on stderr.
    NFR-09 (test_assertion_quality): rejected path must assert exit code.
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


# NFR-02 / NFR-04 / NFR-09
def test_fr01_submit_injection_command_rejected(taskq_home, child_env):
    """AC-01-3: `python -m taskq_plus submit "echo hi; rm x"` -> exit 2
    (injection character rejected). *(SPEC §8 #6)*

    NFR-02 (security): seven-character injection blacklist enforcement (`;`).
    NFR-04 (security): rejection path; no secret leakage on stderr.
    NFR-09 (test_assertion_quality): rejected path must assert exit code.
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


# NFR-04 / NFR-09
def test_fr01_submit_duplicate_name_rejected(taskq_home, child_env):
    """AC-01-4: two `submit` calls with the same `--name` while the first
    remains `pending` -> the second exits 2.
    *(SPEC §3 FR-01 name-uniqueness rule + §7)*

    NFR-04 (security): name collision rejection; no secret leakage on stderr.
    NFR-09 (test_assertion_quality): rejected path must assert exit code.
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


# NFR-04 / NFR-09
def test_fr01_submit_unknown_dependency_rejected(taskq_home, child_env):
    """AC-01-5: `submit --after <unknown-id>` -> exit 2 with stderr
    `unknown dependency: <id>`. *(SPEC §3 FR-01 + §7)*

    NFR-04 (security): unknown-dependency rejection; no secret leakage on stderr.
    NFR-09 (test_assertion_quality): rejected path must assert exit code.
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


# NFR-02 / NFR-09 / NFR-11
def test_fr01_submit_command_at_length_limit_accepted(taskq_home, child_env):
    """Boundary: command is exactly 1000 chars ("echo " + 995 'x') -> exit 0.

    The length rule is *inclusive* on the cap: `command > 1000` is the
    rejection condition, so 1000 must be accepted. *(SPEC §3 FR-01
    length rule)*

    NFR-02 (security): length cap is part of the input-validation surface.
    NFR-09 (test_assertion_quality): boundary case must assert exit code.
    NFR-11 (readability): boundary case is single-purpose.
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


# NFR-02 / NFR-04 / NFR-09
def test_fr01_submit_command_over_length_limit_rejected(taskq_home, child_env):
    """Boundary: command is 1001 chars ("echo " + 996 'x') -> exit 2.

    *(SPEC §3 FR-01 length rule)*

    NFR-02 (security): length cap is part of the input-validation surface.
    NFR-04 (security): rejection path; no secret leakage on stderr.
    NFR-09 (test_assertion_quality): boundary case must assert exit code.
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


# ===========================================================================
# In-process coverage tests
# ---------------------------------------------------------------------------
# The seven cases above are the canonical TEST_SPEC.md §FR-01 rows and are
# NOT modified. The tests below are additive: they exercise the same FR-01
# validation and persistence surface through direct in-process calls so
# `coverage` can measure the dispatcher (`taskq_plus.cli.main`) and the
# disk-backed store (`taskq_plus.storage.task_store.DiskBackend`), neither
# of which is measurable through the `subprocess.run` acceptance path.
# ===========================================================================


# ---------------------------------------------------------------------------
# `taskq_plus.cli.main` — the `python -m taskq_plus` dispatcher
# ---------------------------------------------------------------------------


# NFR-09 / NFR-11
def test_fr01_cli_main_dispatches_submit_in_process(taskq_home):
    """`main(["submit", "echo hi"])` returns 0 and prints an 8-hex id.

    Same dispatcher the subprocess acceptance tests reach via
    `python -m taskq_plus`, driven in-process so it is measurable.

    NFR-09 (test_assertion_quality): asserts exit code AND stdout shape.
    """
    from taskq_plus.cli.main import main

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        exit_code = main(["submit", "echo hi"])

    assert exit_code == 0, f"main() must return 0; got {exit_code}"
    assert TASK_ID_RE.match(buf.getvalue().strip()), (
        f"main() stdout {buf.getvalue()!r} is not an 8-hex id"
    )


# NFR-02 / NFR-04 / NFR-09
def test_fr01_cli_main_propagates_validation_exit_two(taskq_home):
    """`main(["submit", "echo hi; rm x"])` propagates the exit-2 rejection.

    NFR-02 (security): injection blacklist reached through the dispatcher.
    NFR-09 (test_assertion_quality): asserts exit code AND stderr marker.
    """
    from taskq_plus.cli.main import main

    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        exit_code = main(["submit", "echo hi; rm x"])

    assert exit_code == 2, f"main() must return 2 on rejection; got {exit_code}"
    assert "injection" in err.getvalue(), (
        f"stderr must explain the injection rejection; got {err.getvalue()!r}"
    )


# NFR-09
def test_fr01_cli_main_writes_tasks_json_to_disk(taskq_home):
    """The dispatcher uses the disk backend: `$TASKQ_HOME/tasks.json` exists
    and holds the submitted command after `main()` returns."""
    import json as _json

    from taskq_plus.cli.main import main

    with contextlib.redirect_stdout(io.StringIO()):
        assert main(["submit", "echo persisted"]) == 0

    tasks_file = taskq_home / "tasks.json"
    assert tasks_file.exists(), "dispatcher must persist tasks.json to disk"
    payload = _json.loads(tasks_file.read_text(encoding="utf-8"))
    assert [t["command"] for t in payload] == ["echo persisted"]
    assert payload[0]["status"] == "pending"


# ---------------------------------------------------------------------------
# `taskq_plus.cli.commands` — remaining validation/output branches
# ---------------------------------------------------------------------------


class _NoDetailValidationError:
    """Stub standing in for a pydantic `ValidationError` with no details.

    `_format_validation_error` defends against an empty `errors()` list;
    pydantic never produces one in practice, so the branch is reached
    here with a structural stub rather than excluded from coverage.
    """

    def errors(self) -> list:
        return []


# NFR-09 / NFR-11
def test_fr01_format_validation_error_falls_back_when_no_details():
    """An error carrying no detail rows degrades to `"validation failed"`."""
    from taskq_plus.cli import commands

    assert (
        commands._format_validation_error(_NoDetailValidationError())
        == "validation failed"
    )


# NFR-09 / NFR-11
def test_fr01_format_validation_error_strips_pydantic_value_error_prefix():
    """Pydantic's `"Value error, "` prefix is stripped from the message."""
    from taskq_plus.cli import commands

    class _Err:
        def errors(self) -> list:
            return [{"msg": "Value error, command is empty"}]

    assert commands._format_validation_error(_Err()) == "command is empty"


# NFR-09
def test_fr01_submit_json_flag_emits_id_and_status(taskq_home):
    """`submit --json` emits a JSON object with `id` and `status`
    instead of the bare id (covers the `--json` output branch)."""
    import json as _json

    exit_code, stdout_value, stderr_value = _run_submit_inprocess(
        ["echo hi", "--json"]
    )
    assert exit_code == 0, f"--json submit must succeed; stderr={stderr_value!r}"

    payload = _json.loads(stdout_value.strip())
    assert TASK_ID_RE.match(payload["id"]), (
        f"--json payload id {payload['id']!r} is not an 8-hex id"
    )
    assert payload["status"] == "pending"


# NFR-09
def test_fr01_submit_accepts_known_dependency(taskq_home):
    """The `--after` happy path: an id that exists is accepted (exit 0).

    Complements TEST_SPEC row 5, which only pins the unknown-id rejection.
    """
    first_exit, first_id, first_err = _run_submit_inprocess(["echo a"])
    assert first_exit == 0, f"first submit must succeed; stderr={first_err!r}"
    dep_id = first_id.strip()

    exit_code, stdout_value, stderr_value = _run_submit_inprocess(
        ["echo b", "--after", dep_id]
    )
    assert exit_code == 0, (
        f"known dependency must be accepted; stderr={stderr_value!r}"
    )
    assert TASK_ID_RE.match(stdout_value.strip())


# ---------------------------------------------------------------------------
# `taskq_plus.storage.task_store` — DiskBackend + cache helpers
# ---------------------------------------------------------------------------


# NFR-09
def test_fr01_disk_backend_load_returns_empty_when_file_absent(tmp_path):
    """A fresh `$TASKQ_HOME` (no tasks.json yet) loads as an empty list."""
    from taskq_plus.storage.task_store import DiskBackend

    backend = DiskBackend(tmp_path / "tasks.json")
    assert backend.load() == []


# NFR-09
def test_fr01_disk_backend_add_then_load_round_trips(tmp_path):
    """`add()` persists atomically and `load()` reconstructs the Task."""
    from taskq_plus.models.task import Task
    from taskq_plus.storage.task_store import DiskBackend

    path = tmp_path / "tasks.json"
    backend = DiskBackend(path)
    stored = backend.add(Task(command="echo hi", name="alpha"))

    assert path.exists(), "add() must create tasks.json"
    reloaded = backend.load()
    assert len(reloaded) == 1
    assert reloaded[0].id == stored.id
    assert reloaded[0].command == "echo hi"
    assert reloaded[0].name == "alpha"
    assert reloaded[0].status == "pending"


# NFR-09
def test_fr01_disk_backend_add_appends_without_dropping_prior_tasks(tmp_path):
    """A second `add()` reads-modifies-writes; the first task survives."""
    from taskq_plus.models.task import Task
    from taskq_plus.storage.task_store import DiskBackend

    backend = DiskBackend(tmp_path / "tasks.json")
    backend.add(Task(command="echo a"))
    backend.add(Task(command="echo b"))

    assert [t.command for t in backend.load()] == ["echo a", "echo b"]


# NFR-09
def test_fr01_disk_backend_contains_name_tracks_active_status(tmp_path):
    """`contains_name` is True for a pending task and False once it is done
    (SPEC §3 line 83 — only pending/running hold a name slot)."""
    from taskq_plus.models.task import Task
    from taskq_plus.storage.task_store import DiskBackend

    backend = DiskBackend(tmp_path / "tasks.json")
    backend.add(Task(command="echo a", name="dup"))
    assert backend.contains_name("dup") is True
    assert backend.contains_name("other") is False

    backend.add(Task(command="echo c", name="finished", status="done"))
    assert backend.contains_name("finished") is False, (
        "a done task must free its name slot"
    )


# NFR-09
def test_fr01_disk_backend_creates_missing_parent_directory(tmp_path):
    """`_write_atomic` creates `$TASKQ_HOME` if it does not exist yet."""
    from taskq_plus.models.task import Task
    from taskq_plus.storage.task_store import DiskBackend

    nested = tmp_path / "does" / "not" / "exist"
    backend = DiskBackend(nested / "tasks.json")
    backend.add(Task(command="echo hi"))

    assert (nested / "tasks.json").exists()


# NFR-03 / NFR-09
def test_fr01_write_atomic_removes_tmp_file_and_reraises_on_failure(tmp_path):
    """A serialisation failure mid-write must remove the temp file, leave
    `tasks.json` untouched, and re-raise (atomic-write contract, NFR-03).

    NFR-03: `tmp + os.replace` must never leave a partial tasks.json or a
    stray `.tasks.*.json.tmp` behind.
    """
    from taskq_plus.storage.task_store import DiskBackend

    path = tmp_path / "tasks.json"
    backend = DiskBackend(path)

    # `object()` is not JSON-serialisable -> json.dump raises inside the
    # try block, after mkstemp has already created the temp file.
    with pytest.raises(TypeError):
        backend._write_atomic([{"bad": object()}])

    assert not path.exists(), "failed write must not create tasks.json"
    leftovers = list(tmp_path.glob(".tasks.*.json.tmp"))
    assert leftovers == [], f"temp files must be cleaned up; found {leftovers}"


# NFR-09
def test_fr01_disk_backend_load_raises_on_corrupt_json(tmp_path):
    """A corrupted tasks.json surfaces a decode error rather than silently
    rebuilding an empty store (SPEC §7 line 391)."""
    import json as _json

    from taskq_plus.storage.task_store import DiskBackend

    path = tmp_path / "tasks.json"
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(_json.JSONDecodeError):
        DiskBackend(path).load()


# NFR-09
def test_fr01_task_store_has_id_matches_only_existing_ids(tmp_path):
    """`TaskStore.has_id` is the dependency-existence primitive behind
    the `unknown dependency` rejection."""
    from taskq_plus.models.task import Task
    from taskq_plus.storage.task_store import DiskBackend, TaskStore

    store = TaskStore(DiskBackend(tmp_path / "tasks.json"))
    stored = store.add(Task(command="echo a"))

    assert store.has_id(stored.id) is True
    assert store.has_id("deadbeef") is False
    assert [t.id for t in store.load()] == [stored.id]


# NFR-09
def test_fr01_get_store_caches_per_taskq_home(taskq_home):
    """`get_store` returns the same instance for one `TASKQ_HOME` + backend
    kind, and a different instance when the backend kind changes."""
    from taskq_plus.storage import task_store

    task_store.reset_store_cache()
    memory_store = task_store.get_store(use_disk=False)
    assert task_store.get_store(use_disk=False) is memory_store, (
        "repeat lookup with the same backend kind must hit the cache"
    )

    disk_store = task_store.get_store(use_disk=True)
    assert disk_store is not memory_store, (
        "a use_disk=True lookup must not return the in-memory entry"
    )
    assert isinstance(disk_store._backend, task_store.DiskBackend)


# NFR-09
def test_fr01_reset_store_cache_drops_cached_backends(taskq_home):
    """`reset_store_cache()` clears the module-level backend cache."""
    from taskq_plus.storage import task_store

    first = task_store.get_store(use_disk=False)
    task_store.reset_store_cache()
    assert task_store.get_store(use_disk=False) is not first


# NFR-09
def test_fr01_make_disk_store_always_returns_a_fresh_disk_store(taskq_home):
    """`make_disk_store()` bypasses the cache so the entry point never
    reads a stale in-process snapshot."""
    from taskq_plus.storage import task_store

    first = task_store.make_disk_store()
    second = task_store.make_disk_store()

    assert first is not second, "make_disk_store must never return a cached store"
    assert isinstance(first._backend, task_store.DiskBackend)
    assert first._backend._path == taskq_home / "tasks.json"


# NFR-09
def test_fr01_in_memory_backend_round_trips_and_isolates_snapshots(taskq_home):
    """`InMemoryBackend.load()` returns a *copy*, so callers cannot mutate
    the store's internal list."""
    from taskq_plus.models.task import Task
    from taskq_plus.storage.task_store import InMemoryBackend

    backend = InMemoryBackend()
    assert backend.load() == []

    backend.add(Task(command="echo a", name="alpha"))
    snapshot = backend.load()
    snapshot.clear()

    assert len(backend.load()) == 1, "load() must return an isolated snapshot"
    assert backend.contains_name("alpha") is True
    assert backend.contains_name("missing") is False


# ===========================================================================
# In-process coverage tests for the CLI command surface
#
# The subprocess tests above exercise the real `python -m taskq_plus` entry
# point, but pytest-cov cannot measure inside a child process. The tests
# below drive the SAME code paths in-process through
# `taskq_plus.cli.commands` so the validation, execution, and query logic
# is measurable. Both layers are kept: the subprocess tests prove the
# user-facing contract, these prove the internals.
# ===========================================================================


def _capture(fn, *args, **kwargs) -> tuple:
    """Call `fn` capturing stdout/stderr; return (exit_code, stdout, stderr)."""
    stdout_buf, stderr_buf = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout_buf), contextlib.redirect_stderr(stderr_buf):
        exit_code = fn(*args, **kwargs)
    return exit_code, stdout_buf.getvalue(), stderr_buf.getvalue()


def _submit_id(command: str, *extra) -> str:
    """Submit `command` in-process and return the new task id."""
    from taskq_plus.cli import commands

    exit_code, stdout, stderr = _capture(commands.submit, [command, *extra])
    assert exit_code == 0, f"submit unexpectedly failed: {stderr!r}"
    return stdout.strip()


def _seed_cycle(store) -> tuple:
    """Persist a two-node dependency cycle directly into `store`.

    `submit` refuses to create a cycle, so an out-of-band edit is the only
    way to reach the cycle-detection branches (SPEC §7 line 388).
    """
    from taskq_plus.models.task import Task

    first = Task(command="echo a")
    second = Task(command="echo b", depends_on=[first.id])
    store.add(first.model_copy(update={"depends_on": [second.id]}))
    store.add(second)
    return first.id, second.id


# ---------------------------------------------------------------------------
# submit — FR-06 cycle / depth-cap rejection paths (exit 5)
# ---------------------------------------------------------------------------


# NFR-09
def test_fr01_submit_rejects_cycle_with_exit_five(taskq_home):
    """A cycle already persisted in the store surfaces on the next submit
    as exit 5 plus the cycle path on stderr. *(SPEC §7 line 388)*"""
    from taskq_plus.cli import commands
    from taskq_plus.storage.task_store import get_store

    _seed_cycle(get_store())

    exit_code, stdout, stderr = _capture(commands.submit, ["echo c"])

    assert exit_code == 5, f"cycle must exit 5, got {exit_code}"
    assert "dependency cycle:" in stderr
    assert "→" in stderr, "the cycle path must be rendered"
    assert stdout == "", "a rejected submit must not emit an id"


# NFR-09
def test_fr01_submit_rejects_chain_deeper_than_cap(taskq_home, monkeypatch):
    """A chain exceeding `TASKQ_MAX_DAG_DEPTH` exits 5 with the
    `dependency chain too deep: <n> > <max>` message. *(SPEC §7 line 389)*"""
    from taskq_plus.cli import commands

    first = _submit_id("echo a")
    monkeypatch.setenv("TASKQ_MAX_DAG_DEPTH", "1")

    exit_code, stdout, stderr = _capture(
        commands.submit, ["echo b", "--after", first]
    )

    assert exit_code == 5
    assert "dependency chain too deep: 2 > 1" in stderr
    assert stdout == ""


# ---------------------------------------------------------------------------
# run — single-task dispatch (FR-02 / FR-03 / FR-04)
# ---------------------------------------------------------------------------


# NFR-09
def test_fr01_run_without_id_or_all_returns_two(taskq_home):
    """`run` with neither an id nor `--all` is invalid usage -> exit 2."""
    from taskq_plus.cli import commands

    exit_code, stdout, stderr = _capture(commands.run, [])

    assert exit_code == 2
    assert "must supply a task id or --all" in stderr


# NFR-09
def test_fr01_run_unknown_task_id_returns_two(taskq_home):
    """An id that matches no stored task -> exit 2. *(SPEC §7 line 384)*"""
    from taskq_plus.cli import commands

    exit_code, stdout, stderr = _capture(commands.run, ["deadbeef"])

    assert exit_code == 2
    assert "deadbeef" in stderr
    assert "not found" in stderr


# NFR-09
def test_fr01_run_executes_task_and_persists_done(taskq_home):
    """A successful run persists status/exit_code/stdout_tail/duration."""
    from taskq_plus.cli import commands
    from taskq_plus.storage.task_store import get_store

    task_id = _submit_id("echo hi")

    exit_code, stdout, stderr = _capture(commands.run, [task_id])

    assert exit_code == 0, f"run failed: {stderr!r}"
    stored = get_store().find(task_id)
    assert stored is not None
    assert stored.status == "done"
    assert stored.exit_code == 0
    assert stored.stdout_tail.strip() == "hi"
    assert stored.cached is False
    assert stored.finished_at is not None


# NFR-09
def test_fr01_run_nonzero_exit_marks_task_failed(taskq_home):
    """A non-zero task exit maps to status `failed`, CLI still exits 0."""
    from taskq_plus.cli import commands
    from taskq_plus.storage.task_store import get_store

    task_id = _submit_id("false")

    exit_code, stdout, stderr = _capture(commands.run, [task_id])

    assert exit_code == 0, "the CLI exit reflects dispatch, not task outcome"
    stored = get_store().find(task_id)
    assert stored.status == "failed"
    assert stored.exit_code != 0


# NFR-09
def test_fr01_run_cached_replays_recorded_result(taskq_home):
    """`run --cached` replays a recent done result without re-executing."""
    from taskq_plus.cli import commands
    from taskq_plus.storage.task_store import get_store

    first = _submit_id("echo hi")
    assert _capture(commands.run, [first])[0] == 0

    second = _submit_id("echo hi")
    exit_code, stdout, stderr = _capture(commands.run, [second, "--cached"])

    assert exit_code == 0
    stored = get_store().find(second)
    assert stored.status == "done"
    assert stored.cached is True, "a cache hit must set cached=True"
    assert stored.exit_code == 0


# NFR-09
def test_fr01_run_breaker_open_returns_three(taskq_home):
    """An OPEN breaker rejects the run before any subprocess -> exit 3."""
    from taskq_plus.cli import commands
    from taskq_plus.service.breaker import STATE_OPEN
    from taskq_plus.storage.breaker_store import make_breaker_store
    from taskq_plus.storage.task_store import get_store

    task_id = _submit_id("echo hi")

    bstore = make_breaker_store()
    breaker = bstore.load()
    for _ in range(breaker.threshold):
        breaker.record_failure()
    assert breaker.state == STATE_OPEN, "precondition: breaker must be OPEN"
    bstore.save(breaker)

    exit_code, stdout, stderr = _capture(commands.run, [task_id])

    assert exit_code == 3
    assert "breaker open" in stderr
    assert get_store().find(task_id).status == "pending", (
        "a breaker-rejected run must not execute the task"
    )


# ---------------------------------------------------------------------------
# run --all — FR-02 concurrent sweep / FR-06 topological order
# ---------------------------------------------------------------------------


# NFR-09
def test_fr01_run_all_executes_every_pending_task(taskq_home):
    """`run --all` dispatches every pending task concurrently."""
    from taskq_plus.cli import commands
    from taskq_plus.storage.task_store import get_store

    first = _submit_id("echo a")
    second = _submit_id("echo b")

    exit_code, stdout, stderr = _capture(commands.run, ["--all"])

    assert exit_code == 0
    store = get_store()
    assert store.find(first).status == "done"
    assert store.find(second).status == "done"
    assert store.find(first).stdout_tail.strip() == "a"
    assert store.find(second).stdout_tail.strip() == "b"


# NFR-09
def test_fr01_run_all_single_worker_runs_sequentially(taskq_home, monkeypatch):
    """`TASKQ_MAX_WORKERS=1` takes the sequential dispatch branch."""
    from taskq_plus.cli import commands
    from taskq_plus.storage.task_store import get_store

    monkeypatch.setenv("TASKQ_MAX_WORKERS", "1")
    first = _submit_id("echo a")
    second = _submit_id("echo b")

    exit_code, stdout, stderr = _capture(commands.run, ["--all"])

    assert exit_code == 0
    store = get_store()
    assert store.find(first).status == "done"
    assert store.find(second).status == "done"


# NFR-09
def test_fr01_run_all_blocks_dependent_of_failed_prereq(taskq_home):
    """A task whose prerequisite ended non-`done` is persisted `blocked`
    and is never executed. *(SPEC §3 FR-06 line 146)*"""
    from taskq_plus.cli import commands
    from taskq_plus.storage.task_store import get_store

    prereq = _submit_id("false")
    dependent = _submit_id("echo b", "--after", prereq)

    exit_code, stdout, stderr = _capture(commands.run, ["--all"])

    assert exit_code == 0
    store = get_store()
    assert store.find(prereq).status == "failed"
    blocked = store.find(dependent)
    assert blocked.status == "blocked"
    assert blocked.exit_code is None, "a blocked task never ran"
    assert blocked.stdout_tail is None
    assert blocked.finished_at is not None


# NFR-09
def test_fr01_run_all_returns_zero_when_nothing_pending(taskq_home):
    """`run --all` on an empty store is a no-op that exits 0."""
    from taskq_plus.cli import commands

    exit_code, stdout, stderr = _capture(commands.run, ["--all"])

    assert exit_code == 0
    assert stdout == ""


# NFR-09
def test_fr01_run_all_returns_zero_on_out_of_band_cycle(taskq_home):
    """A cycle introduced out-of-band makes `run --all` exit 0 without
    dispatching anything (no progress is possible)."""
    from taskq_plus.cli import commands
    from taskq_plus.storage.task_store import get_store

    store = get_store()
    first, second = _seed_cycle(store)

    exit_code, stdout, stderr = _capture(commands.run, ["--all"])

    assert exit_code == 0
    assert store.find(first).status == "pending"
    assert store.find(second).status == "pending"


# ---------------------------------------------------------------------------
# status — FR-05 single-task inspection
# ---------------------------------------------------------------------------


# NFR-09
def test_fr01_status_prints_every_stored_field(taskq_home):
    """`status <id>` prints each field as a `key: value` line."""
    from taskq_plus.cli import commands

    task_id = _submit_id("echo hi")

    exit_code, stdout, stderr = _capture(commands.status, [task_id])

    assert exit_code == 0
    assert f"id: {task_id}" in stdout
    assert "command: echo hi" in stdout
    assert "status: pending" in stdout


# NFR-09
def test_fr01_status_json_emits_single_line_object(taskq_home):
    """`status --json` emits one parseable line carrying every field."""
    import json as _json

    from taskq_plus.cli import commands

    task_id = _submit_id("echo hi")

    exit_code, stdout, stderr = _capture(commands.status, [task_id, "--json"])

    assert exit_code == 0
    assert len(stdout.strip().splitlines()) == 1, "--json must be single-line"
    payload = _json.loads(stdout)
    for field in ("id", "command", "name", "status", "created_at", "depends_on"):
        assert field in payload, f"missing field {field!r}"
    assert payload["id"] == task_id


# NFR-09
def test_fr01_status_unknown_task_returns_two(taskq_home):
    """An unknown id -> exit 2, stderr `unknown task: <id>`."""
    from taskq_plus.cli import commands

    exit_code, stdout, stderr = _capture(commands.status, ["deadbeef"])

    assert exit_code == 2
    assert "unknown task: deadbeef" in stderr


# ---------------------------------------------------------------------------
# list — FR-05 listing + corrupt-store surfacing
# ---------------------------------------------------------------------------


# NFR-09
def test_fr01_list_tasks_prints_one_row_per_task(taskq_home):
    """`list` prints `<id>\\t<status>\\t<command>` per task."""
    from taskq_plus.cli import commands

    task_id = _submit_id("echo hi")

    exit_code, stdout, stderr = _capture(commands.list_tasks, [])

    assert exit_code == 0
    assert stdout.strip() == f"{task_id}\tpending\techo hi"


# NFR-09
def test_fr01_list_tasks_json_emits_array(taskq_home):
    """`list --json` emits a single-line JSON array of task records."""
    import json as _json

    from taskq_plus.cli import commands

    task_id = _submit_id("echo hi")

    exit_code, stdout, stderr = _capture(commands.list_tasks, ["--json"])

    assert exit_code == 0
    payload = _json.loads(stdout)
    assert [record["id"] for record in payload] == [task_id]


# NFR-09
def test_fr01_list_tasks_corrupt_store_returns_one(taskq_home):
    """A corrupt `tasks.json` surfaces as exit 1 + `store corrupted`,
    never a silent rebuild. *(SPEC §7 line 392)*"""
    from taskq_plus.cli import commands

    (taskq_home / "tasks.json").write_text("{not json", encoding="utf-8")

    exit_code, stdout, stderr = _capture(
        commands.list_tasks, [], use_disk=True
    )

    assert exit_code == 1
    assert "store corrupted" in stderr
    assert (taskq_home / "tasks.json").read_text(encoding="utf-8") == "{not json", (
        "the corrupt file must be left intact, not rebuilt"
    )


# ---------------------------------------------------------------------------
# clear — FR-05 data-file wipe
# ---------------------------------------------------------------------------


# NFR-09
def test_fr01_clear_removes_every_data_file(taskq_home):
    """`clear` removes exactly the four `$TASKQ_HOME` data files."""
    from taskq_plus.cli import commands

    assert len(commands.DATA_FILENAMES) == 4
    for filename in commands.DATA_FILENAMES:
        (taskq_home / filename).write_text("seeded", encoding="utf-8")

    exit_code, stdout, stderr = _capture(commands.clear, [])

    assert exit_code == 0
    for filename in commands.DATA_FILENAMES:
        assert not (taskq_home / filename).exists(), f"{filename} survived clear"


# NFR-09
def test_fr01_clear_is_idempotent_on_fresh_home(taskq_home):
    """`clear` on a `$TASKQ_HOME` with no data files still exits 0."""
    from taskq_plus.cli import commands

    exit_code, stdout, stderr = _capture(commands.clear, [])

    assert exit_code == 0
    assert stderr == ""


# ---------------------------------------------------------------------------
# export — FR-08 three-format render
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fmt", ["json", "csv", "md"])
# NFR-09
def test_fr01_export_renders_each_format(taskq_home, fmt):
    """Every declared format renders the task and ends with a newline."""
    from taskq_plus.cli import commands

    task_id = _submit_id("echo hi")

    exit_code, stdout, stderr = _capture(commands.export, ["--format", fmt])

    assert exit_code == 0
    assert task_id in stdout, f"{fmt} export dropped the task id"
    assert "echo hi" in stdout
    assert stdout.endswith("\n")


# NFR-09
def test_fr01_export_empty_store_emits_no_rows(taskq_home):
    """An empty store renders no CSV rows and still exits 0."""
    from taskq_plus.cli import commands

    exit_code, stdout, stderr = _capture(commands.export, ["--format", "csv"])

    assert exit_code == 0
    assert stdout == ""


# NFR-09
def test_fr01_export_invalid_format_returns_two(taskq_home):
    """`export --format <bogus>` is rejected by argparse; the handler
    swallows `SystemExit` and returns the integer code (2) so in-process
    callers can assert on the return value rather than rescuing the
    exception. Covers commands.py lines 905-913 (the `except SystemExit`
    branch of `export`).
    """
    from taskq_plus.cli import commands

    exit_code, stdout, stderr = _capture(commands.export, ["--format", "xml"])

    assert exit_code == 2
    assert stdout == ""


# ---------------------------------------------------------------------------
# graph — FR-05/FR-06 dependency-graph inspection
# ---------------------------------------------------------------------------


# NFR-09
def test_fr01_graph_prints_topological_tree(taskq_home):
    """`graph` prints ids in topological order, indented by chain depth."""
    from taskq_plus.cli import commands

    first = _submit_id("echo a")
    second = _submit_id("echo b", "--after", first)

    exit_code, stdout, stderr = _capture(commands.graph, [])

    assert exit_code == 0
    lines = stdout.splitlines()
    assert lines == [first, f"  {second}"], (
        "depth-1 node unindented, depth-2 node indented by two spaces"
    )


# NFR-09
def test_fr01_graph_reports_cycle_with_exit_five(taskq_home):
    """A cyclic graph exits 5 and renders the cycle path on stderr."""
    from taskq_plus.cli import commands
    from taskq_plus.storage.task_store import get_store

    _seed_cycle(get_store())

    exit_code, stdout, stderr = _capture(commands.graph, [])

    assert exit_code == 5
    assert "dependency cycle:" in stderr
    assert "→" in stderr
    assert stdout == ""


# NFR-09
def test_fr01_graph_depth_cap_returns_five(taskq_home, monkeypatch):
    """A chain deeper than the cap exits 5 with the depth message."""
    from taskq_plus.cli import commands

    first = _submit_id("echo a")
    _submit_id("echo b", "--after", first)
    monkeypatch.setenv("TASKQ_MAX_DAG_DEPTH", "1")

    exit_code, stdout, stderr = _capture(commands.graph, [])

    assert exit_code == 5
    assert "dependency chain too deep: 2 > 1" in stderr


# ---------------------------------------------------------------------------
# plugins — FR-05/FR-07 allowlist inspection
# ---------------------------------------------------------------------------


# NFR-09 / NFR-11 (security: path form must never be imported)
def test_fr01_plugins_rejects_path_form_with_exit_six(taskq_home):
    """A path-form spec is rejected by the regex whitelist BEFORE any
    import is attempted -> exit 6. *(SPEC §7 line 390, NFR-02)*"""
    from taskq_plus.cli import commands

    exit_code, stdout, stderr = _capture(commands.plugins, ["../evil.py"])

    assert exit_code == 6
    assert "rejected module: ../evil.py" in stderr
    assert stdout == "", "a rejected spec must produce no plugin record"


# NFR-09
def test_fr01_plugins_lists_declared_specs(taskq_home):
    """`plugins list <name>` prints one record per well-formed spec; the
    bare `list` verb is not itself treated as a plugin spec."""
    from taskq_plus.cli import commands

    exit_code, stdout, stderr = _capture(commands.plugins, ["list", "json"])

    assert exit_code == 0
    lines = stdout.splitlines()
    assert len(lines) == 1, "the `list` verb must not become a plugin record"
    assert lines[0].startswith("json  hooks=")
    assert "status=" in lines[0]


# NFR-09
def test_fr01_plugins_reports_failed_import(taskq_home):
    """A well-formed spec whose import fails is reported as
    `status=failed` with an error, and still exits 0."""
    from taskq_plus.cli import commands

    exit_code, stdout, stderr = _capture(
        commands.plugins, ["taskq_no_such_plugin_module"]
    )

    assert exit_code == 0, "an import failure is reported, not an exit code"
    assert "taskq_no_such_plugin_module" in stdout
    assert "status=failed" in stdout
    assert "error=" in stdout


# NFR-09
def test_fr01_plugins_falls_back_to_env_allowlist(taskq_home, monkeypatch):
    """With no positional specs the allowlist comes from `TASKQ_PLUGINS`."""
    from taskq_plus.cli import commands

    monkeypatch.setenv("TASKQ_PLUGINS", "json")

    exit_code, stdout, stderr = _capture(commands.plugins, ["list"])

    assert exit_code == 0
    assert stdout.splitlines()[0].startswith("json  hooks=")


# ---------------------------------------------------------------------------
# Environment-backed configuration helpers
# ---------------------------------------------------------------------------


# NFR-09
def test_fr01_timeout_budget_reads_env_with_fallback(monkeypatch):
    """`TASKQ_TASK_TIMEOUT` is read at call time; unset/garbage falls back."""
    from taskq_plus.cli import commands

    monkeypatch.delenv("TASKQ_TASK_TIMEOUT", raising=False)
    assert commands._timeout_budget() == commands.DEFAULT_TASK_TIMEOUT

    monkeypatch.setenv("TASKQ_TASK_TIMEOUT", "2.5")
    assert commands._timeout_budget() == 2.5

    monkeypatch.setenv("TASKQ_TASK_TIMEOUT", "not-a-number")
    assert commands._timeout_budget() == commands.DEFAULT_TASK_TIMEOUT


# NFR-09
def test_fr01_max_workers_reads_env_with_fallback(monkeypatch):
    """`TASKQ_MAX_WORKERS` is read at call time; unset/garbage falls back."""
    from taskq_plus.cli import commands

    monkeypatch.delenv("TASKQ_MAX_WORKERS", raising=False)
    assert commands._max_workers() == commands.DEFAULT_MAX_WORKERS

    monkeypatch.setenv("TASKQ_MAX_WORKERS", "7")
    assert commands._max_workers() == 7

    monkeypatch.setenv("TASKQ_MAX_WORKERS", "many")
    assert commands._max_workers() == commands.DEFAULT_MAX_WORKERS


# NFR-09
def test_fr01_max_dag_depth_reads_env_with_fallback(monkeypatch):
    """`TASKQ_MAX_DAG_DEPTH` is read at call time; unset/garbage falls back."""
    from taskq_plus.cli import commands

    monkeypatch.delenv("TASKQ_MAX_DAG_DEPTH", raising=False)
    assert commands._max_dag_depth() == commands.DEFAULT_MAX_DAG_DEPTH

    monkeypatch.setenv("TASKQ_MAX_DAG_DEPTH", "5")
    assert commands._max_dag_depth() == 5

    monkeypatch.setenv("TASKQ_MAX_DAG_DEPTH", "deep")
    assert commands._max_dag_depth() == commands.DEFAULT_MAX_DAG_DEPTH


# NFR-09
def test_fr01_utcnow_is_timezone_aware():
    """`_utcnow()` returns an aware UTC timestamp (never naive)."""
    from datetime import timezone

    from taskq_plus.cli import commands

    now = commands._utcnow()

    assert now.tzinfo is not None, "finished_at must be timezone-aware"
    assert now.utcoffset() == timezone.utc.utcoffset(None)


# ---------------------------------------------------------------------------
# Execution / serialization internals
# ---------------------------------------------------------------------------


# NFR-09
def test_fr01_execute_and_persist_records_every_result_field(taskq_home):
    """`_execute_and_persist` writes back the full FR-02 result shape."""
    from taskq_plus.cli import commands
    from taskq_plus.models.task import Task
    from taskq_plus.storage.task_store import get_store

    store = get_store()
    task = store.add(Task(command="echo hi"))

    status = commands._execute_and_persist(task, store=store)

    assert status == "done"
    stored = store.find(task.id)
    assert stored.status == "done"
    assert stored.exit_code == 0
    assert stored.stdout_tail.strip() == "hi"
    assert stored.duration_ms >= 0
    assert stored.finished_at is not None
    assert stored.cached is False


# NFR-09
def test_fr01_execute_and_persist_invokes_plugin_hooks(taskq_home):
    """`pre_run` / `post_run` hooks bracket the executor dispatch."""
    from taskq_plus.cli import commands
    from taskq_plus.models.task import Task
    from taskq_plus.storage.task_store import get_store

    class _RecordingRegistry:
        def __init__(self) -> None:
            self.pre: list = []
            self.post: list = []

        def run_pre(self, task, *, task_id, correlation_id) -> None:
            self.pre.append((task_id, correlation_id))

        def run_post(self, task, result, *, task_id, correlation_id) -> None:
            self.post.append((task_id, result.status))

    store = get_store()
    task = store.add(Task(command="echo hi"))
    registry = _RecordingRegistry()

    status = commands._execute_and_persist(
        task, store=store, plugin_registry=registry
    )

    assert status == "done"
    assert registry.pre == [(task.id, task.id)]
    assert registry.post == [(task.id, "done")], "post_run sees the result"


# NFR-09
def test_fr01_build_plugin_registry_loads_declared_allowlist(taskq_home, monkeypatch):
    """`_build_plugin_registry` returns a registry whose `load()` already ran."""
    from taskq_plus.cli import commands

    monkeypatch.setenv("TASKQ_PLUGINS", "json")

    registry = commands._build_plugin_registry()

    assert [record.name for record in registry.records] == ["json"]


# NFR-09
def test_fr01_persist_result_writes_result_fields_onto_task(taskq_home):
    """`_persist_result` copies every executor field onto the stored task."""
    from datetime import datetime, timezone

    from taskq_plus.cli import commands
    from taskq_plus.models.task import Task
    from taskq_plus.service.executor import TaskResult
    from taskq_plus.storage.task_store import get_store

    store = get_store()
    task = store.add(Task(command="echo hi"))
    finished = datetime(2026, 1, 1, tzinfo=timezone.utc)
    result = TaskResult(
        status="failed",
        exit_code=3,
        stdout_tail="out",
        stderr_tail="err",
        duration_ms=42,
        finished_at=finished,
    )

    commands._persist_result(store, task, result)

    stored = store.find(task.id)
    assert stored.status == "failed"
    assert stored.exit_code == 3
    assert stored.stdout_tail == "out"
    assert stored.stderr_tail == "err"
    assert stored.duration_ms == 42
    assert stored.finished_at == finished
    assert stored.cached is False


# NFR-09
def test_fr01_task_payload_and_emit_json_round_trip():
    """`_task_payload` dumps a JSON-safe record `_emit_json` can emit."""
    import json as _json

    from taskq_plus.cli import commands
    from taskq_plus.models.task import Task

    task = Task(command="echo hi", name="alpha")
    payload = commands._task_payload(task)

    assert payload["command"] == "echo hi"
    assert payload["name"] == "alpha"
    assert isinstance(payload["created_at"], str), "mode='json' stringifies dates"

    exit_code, stdout, stderr = _capture(
        lambda: (commands._emit_json(payload), 0)[1]
    )

    assert exit_code == 0
    assert len(stdout.strip().splitlines()) == 1
    assert _json.loads(stdout)["name"] == "alpha"


# ---------------------------------------------------------------------------
# task_store — update / find primitives (FR-02 read-modify-write)
# ---------------------------------------------------------------------------


# NFR-09
def test_fr01_in_memory_backend_update_replaces_stored_task(taskq_home):
    """`InMemoryBackend.update` swaps the slot and returns the new task."""
    from taskq_plus.models.task import Task
    from taskq_plus.storage.task_store import InMemoryBackend

    backend = InMemoryBackend()
    task = backend.add(Task(command="echo a"))

    updated = backend.update(
        task.id, lambda t: t.model_copy(update={"status": "done", "exit_code": 0})
    )

    assert updated.status == "done"
    assert backend.load()[0].status == "done"
    assert backend.load()[0].exit_code == 0


# NFR-09
def test_fr01_in_memory_backend_update_unknown_id_raises_keyerror(taskq_home):
    """Updating an absent id raises `KeyError` rather than silently passing."""
    from taskq_plus.models.task import Task
    from taskq_plus.storage.task_store import InMemoryBackend

    backend = InMemoryBackend()
    backend.add(Task(command="echo a"))

    with pytest.raises(KeyError, match="deadbeef"):
        backend.update("deadbeef", lambda t: t)


# NFR-09
def test_fr01_disk_backend_update_persists_atomically(tmp_path):
    """`DiskBackend.update` writes the mutation through to `tasks.json`."""
    from taskq_plus.models.task import Task
    from taskq_plus.storage.task_store import DiskBackend

    path = tmp_path / "tasks.json"
    backend = DiskBackend(path)
    task = backend.add(Task(command="echo a"))

    updated = backend.update(
        task.id, lambda t: t.model_copy(update={"status": "done", "exit_code": 0})
    )

    assert updated.status == "done"
    reloaded = DiskBackend(path).load()
    assert [t.status for t in reloaded] == ["done"], "mutation must reach disk"
    assert reloaded[0].exit_code == 0


# NFR-09
def test_fr01_disk_backend_update_unknown_id_raises_keyerror(tmp_path):
    """Updating an absent id on disk raises `KeyError`."""
    from taskq_plus.models.task import Task
    from taskq_plus.storage.task_store import DiskBackend

    backend = DiskBackend(tmp_path / "tasks.json")
    backend.add(Task(command="echo a"))

    with pytest.raises(KeyError, match="deadbeef"):
        backend.update("deadbeef", lambda t: t)


# NFR-09
def test_fr01_task_store_find_returns_task_or_none(tmp_path):
    """`TaskStore.find` returns the matching task, or None when absent."""
    from taskq_plus.models.task import Task
    from taskq_plus.storage.task_store import DiskBackend, TaskStore

    store = TaskStore(DiskBackend(tmp_path / "tasks.json"))
    stored = store.add(Task(command="echo a"))

    assert store.find(stored.id).id == stored.id
    assert store.find("deadbeef") is None


# NFR-09
def test_fr01_task_store_update_delegates_to_backend(tmp_path):
    """`TaskStore.update` delegates the read-modify-write to its backend."""
    from taskq_plus.models.task import Task
    from taskq_plus.storage.task_store import DiskBackend, TaskStore

    store = TaskStore(DiskBackend(tmp_path / "tasks.json"))
    stored = store.add(Task(command="echo a"))

    updated = store.update(
        stored.id, lambda t: t.model_copy(update={"status": "done"})
    )

    assert updated.status == "done"
    assert store.find(stored.id).status == "done"


# NFR-09
def test_fr01_run_timeout_returns_exit_four(taskq_home, monkeypatch):
    """A single-task timeout maps to exit 4 and status `timeout`.
    *(SPEC §3 FR-02 line 120)*"""
    from taskq_plus.cli import commands
    from taskq_plus.storage.task_store import get_store

    monkeypatch.setenv("TASKQ_TASK_TIMEOUT", "1")
    task_id = _submit_id("sleep 5")

    exit_code, stdout, stderr = _capture(commands.run, [task_id])

    assert exit_code == 4, "a timed-out task must exit 4"
    stored = get_store().find(task_id)
    assert stored.status == "timeout"
    assert stored.exit_code is None, "a timed-out task has no exit code"


# NFR-09
def test_fr01_run_all_skips_already_terminal_tasks(taskq_home):
    """`run --all` leaves an already-`done` task untouched and only
    dispatches the still-pending ones."""
    from taskq_plus.cli import commands
    from taskq_plus.storage.task_store import get_store

    first = _submit_id("echo a")
    assert _capture(commands.run, [first])[0] == 0
    store = get_store()
    finished_at = store.find(first).finished_at
    assert store.find(first).status == "done", "precondition: first is terminal"

    second = _submit_id("echo b")
    exit_code, stdout, stderr = _capture(commands.run, ["--all"])

    assert exit_code == 0
    assert store.find(second).status == "done", "the pending task ran"
    assert store.find(first).finished_at == finished_at, (
        "an already-terminal task must not be re-executed"
    )


# NFR-09
def test_fr01_run_all_concurrent_failures_count_toward_breaker(taskq_home):
    """Two failing tasks dispatched through the thread pool each record a
    breaker failure, tripping it OPEN at the threshold."""
    from taskq_plus.cli import commands
    from taskq_plus.service.breaker import STATE_OPEN
    from taskq_plus.storage.breaker_store import make_breaker_store
    from taskq_plus.storage.task_store import get_store

    bstore = make_breaker_store()
    seeded = bstore.load()
    seeded.record_failure()
    bstore.save(seeded)

    first = _submit_id("false")
    second = _submit_id("false")

    exit_code, stdout, stderr = _capture(commands.run, ["--all"])

    assert exit_code == 0
    store = get_store()
    assert store.find(first).status == "failed"
    assert store.find(second).status == "failed"
    persisted = make_breaker_store().load()
    assert persisted.failure_count >= 3, "both pool failures were recorded"
    assert persisted.state == STATE_OPEN, "the breaker tripped at threshold"
