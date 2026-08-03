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
