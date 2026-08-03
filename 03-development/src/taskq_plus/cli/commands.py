"""[FR-01/FR-02] CLI command handlers.

The `submit` function is the canonical entry point that the test suite
calls in-process (`taskq_plus.cli.commands.submit(argv)`); the
`python -m taskq_plus` entry point dispatches the same function via
`taskq_plus.cli.main`.

The `run` function (FR-02) is the dispatcher that accepts a task id
(or `--all`), calls the executor, and persists the result through the
store. The first positional argument is the task id; if it is
`--all`, every pending task is executed (DAG-topological sweep in
FR-06 — for FR-02 the iteration is over the pending set directly).

In-process callers (the test suite) see an isolated in-memory store so
per-test isolation is preserved without a global reset; subprocess
callers go through `main` which uses the disk-backed store so the
real `tasks.json` round-trip is exercised.

Citations:
    SPEC.md §3 FR-01 lines 72-94 — submission rules + accepted shape.
    SPEC.md §7 line 383 — 空/非法命令 → exit 2, stderr 說明.
    SPEC.md §7 line 385 — `--after` 指向不存在的 id → exit 2, stderr
        `unknown dependency: <id>`.
    SPEC.md §6 line 337 — `cli/commands` module location.
    SPEC.md §8 lines 406-408 — canonical acceptance commands.
    SPEC.md §3 FR-02 lines 105-118 — execution state machine.
    SPEC.md §3 FR-02 line 120 — single-task timeout → exit 4.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional

from pydantic import ValidationError

from taskq_plus.models.task import Task, TaskSubmission
from taskq_plus.service.executor import run_task
from taskq_plus.storage.task_store import get_store


#: [FR-02] Default per-task timeout (seconds) when `TASKQ_TASK_TIMEOUT`
#: is unset. SPEC §3 FR-02 line 110 spells out the env override.
DEFAULT_TASK_TIMEOUT: float = 10.0

#: [FR-02] Default `max_workers` for `--all`. SPEC §3 FR-02 line 122.
DEFAULT_MAX_WORKERS: int = 4


def _build_submit_parser() -> argparse.ArgumentParser:
    """[FR-01] Build the `submit` argument parser.

    `command` is `nargs="?"` so an empty string reaches pydantic
    validation as the empty-command reject case (SPEC §3 line 80)
    instead of being swallowed by argparse's flag-detection.
    """
    parser = argparse.ArgumentParser(
        prog="taskq submit",
        description="Submit a new task to the queue.",
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="",
        help="Shell command to run (validated).",
    )
    parser.add_argument("--name", default=None, help="Optional friendly name.")
    parser.add_argument(
        "--after",
        action="append",
        default=[],
        dest="after",
        help="Task id this submission depends on (repeatable).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit the new task as JSON instead of just the id.",
    )
    return parser


def _build_run_parser() -> argparse.ArgumentParser:
    """[FR-02] Build the `run` argument parser.

    Positional `[id]` is `nargs="?"` so `--all` can be supplied on its
    own. `--all` is a `store_true` flag rather than a positional
    sentinel so the dispatcher can disambiguate without relying on the
    hidden underlying token.
    """
    parser = argparse.ArgumentParser(
        prog="taskq run",
        description="Run a pending task by id, or all pending tasks.",
    )
    parser.add_argument(
        "task_id",
        nargs="?",
        default=None,
        help="Task id to run. Mutually exclusive with --all.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        dest="run_all",
        help="Run all pending tasks.",
    )
    return parser


def _format_validation_error(err: ValidationError) -> str:
    """[FR-01] Reduce a pydantic ValidationError to a single-line message."""
    errors = err.errors()
    if not errors:
        return "validation failed"
    first = errors[0]
    msg = first.get("msg", "validation failed")
    prefix = "Value error, "
    if msg.startswith(prefix):
        msg = msg[len(prefix):]
    return msg


def _emit_stderr_error(message: str) -> None:
    """[FR-01] Print `submit: <message>` to stderr in one place."""
    print(f"submit: {message}", file=sys.stderr)


def _timeout_budget() -> float:
    """[FR-02] Read `TASKQ_TASK_TIMEOUT` from the current environment.

    Returns the float value in seconds, or `DEFAULT_TASK_TIMEOUT` if
    unset or empty. Reads the env at call time (per FR-01 env-config
    convention) so tests can mutate the value via `monkeypatch`.
    """
    raw = os.environ.get("TASKQ_TASK_TIMEOUT", "")
    if raw == "":
        return DEFAULT_TASK_TIMEOUT
    try:
        return float(raw)
    except ValueError:
        return DEFAULT_TASK_TIMEOUT


def _max_workers() -> int:
    """[FR-02] Read `TASKQ_MAX_WORKERS` from the current environment."""
    raw = os.environ.get("TASKQ_MAX_WORKERS", "")
    if raw == "":
        return DEFAULT_MAX_WORKERS
    try:
        return int(raw)
    except ValueError:
        return DEFAULT_MAX_WORKERS


def _persist_result(store, task: Task, result) -> None:
    """[FR-02] Persist every field produced by a task execution."""
    result_fields = {
        "status": result.status,
        "exit_code": result.exit_code,
        "stdout_tail": result.stdout_tail,
        "stderr_tail": result.stderr_tail,
        "duration_ms": result.duration_ms,
        "finished_at": result.finished_at,
    }
    store.update(
        task.id,
        lambda stored_task: stored_task.model_copy(update=result_fields),
    )


def _execute_and_persist(task: Task, *, store) -> str:
    """[FR-02] Execute `task`, persist its result, and return its status."""
    result = run_task(task, timeout=_timeout_budget())
    _persist_result(store, task, result)
    return result.status


def submit(argv: Optional[List[str]] = None, *, use_disk: bool = False) -> int:
    """[FR-01] Run a `submit` invocation.

    `argv` is the token list *after* the leading `submit` keyword (the
    in-process helper in `tests/test_fr01.py` passes the user tokens
    directly; the `python -m taskq_plus` dispatcher strips the
    `submit` keyword before calling this function).

    `use_disk=True` selects the on-disk backend (used by the
    `python -m taskq_plus` entry point so the subprocess tests
    exercise the real `$TASKQ_HOME/tasks.json` round-trip); the
    default in-process backend is `InMemoryBackend` so the in-process
    test surface stays isolated from any on-disk state.

    Returns:
        0 — submission persisted.
        2 — validation / uniqueness / dependency rule failed.

    Citations:
        SPEC.md §7 line 383 — exit 2 on 空/非法命令.
        SPEC.md §7 line 385 — exit 2 on `--after` 不存在.
    """
    parser = _build_submit_parser()
    args = parser.parse_args(list(argv) if argv is not None else [])

    try:
        submission = TaskSubmission(
            command=args.command,
            name=args.name,
            depends_on=list(args.after),
        )
    except ValidationError as exc:
        _emit_stderr_error(_format_validation_error(exc))
        return 2

    store = get_store(use_disk=use_disk)

    if submission.name is not None and store.contains_name(submission.name):
        _emit_stderr_error(f"duplicate name: {submission.name}")
        return 2

    for dep in submission.depends_on:
        if not store.has_id(dep):
            _emit_stderr_error(f"unknown dependency: {dep}")
            return 2

    task = Task(
        command=submission.command,
        name=submission.name,
        depends_on=submission.depends_on,
    )
    stored = store.add(task)

    if args.as_json:
        print(json.dumps({"id": stored.id, "status": stored.status}))
    else:
        print(stored.id)
    return 0


def run(argv: Optional[List[str]] = None, *, use_disk: bool = False) -> int:
    """[FR-02] Run a `run` invocation.

    `argv` is the token list *after* the leading `run` keyword. Two
    shapes are supported:

        run <id>      execute the task with the given id
        run --all     execute every pending task concurrently

    Returns:
        0 — done / failed (the failure is recorded in the task's
            `status` field; the CLI itself exits 0).
        4 — single-task timeout (SPEC §3 FR-02 line 120).
        2 — invalid usage (no id and no --all; id not found).

    Citations:
        SPEC.md §3 FR-02 lines 105-118 — state machine.
        SPEC.md §3 FR-02 line 120 — single-task timeout → exit 4.
        SPEC.md §3 FR-02 line 122 — `--all` thread-pool dispatch.
    """
    parser = _build_run_parser()
    args = parser.parse_args(list(argv) if argv is not None else [])

    if not args.run_all and args.task_id is None:
        print("run: must supply a task id or --all", file=sys.stderr)
        return 2

    store = get_store(use_disk=use_disk)

    if args.run_all:
        return _run_all(store)

    task = store.find(args.task_id)
    if task is None:
        print(f"run: task {args.task_id!r} not found", file=sys.stderr)
        return 2

    status = _execute_and_persist(task, store=store)
    if status == "timeout":
        return 4
    return 0


def _run_all(store) -> int:
    """[FR-02] Execute every pending task through the thread pool.

    Returns 0 because the CLI exit code reflects the subprocess
    dispatch shape, not the per-task outcome (per-task outcome is in
    the task record's `status`). Concurrent writes are serialised by
    the store's shared lock — see `taskq_plus.storage.task_store`.
    """
    pending = [t for t in store.all() if t.status == "pending"]
    if not pending:
        return 0

    def _worker(task: Task) -> None:
        _execute_and_persist(task, store=store)

    max_workers = max(1, min(_max_workers(), len(pending)))
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        for task in pending:
            pool.submit(_worker, task)
    return 0
