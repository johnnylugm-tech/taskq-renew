"""[FR-01/FR-02/FR-05] CLI command handlers.

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
    SPEC.md §3 FR-05 lines 132-137 — `status` / `list` / `graph` /
        `plugins` / `clear` command surface.
    SPEC.md §3 FR-05 line 139 — global `--json` single-line output.
    SPEC.md §3 FR-05 line 140 — canonical exit-code roster.
    SPEC.md §5.2 lines 311-314 — the four `$TASKQ_HOME` data files.
    SPEC.md §7 line 384 — unknown task id → exit 2, stderr
        `unknown task: <id>`.
    SPEC.md §7 line 388 — 相依圖存在循環 → exit 5 + cycle path.
    SPEC.md §7 line 389 — 深度超限 → exit 5, stderr
        `dependency chain too deep: <n> > <max>`.
    SPEC.md §7 line 390 — plugin 名稱非法 → exit 6, stderr
        `plugin load failed: <name>: <reason>`.
    SPEC.md §7 line 392 — `tasks.json` 損壞 → exit 1, stderr
        `store corrupted` (不靜默重建).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from pydantic import ValidationError

from taskq_plus.config import taskq_home
from taskq_plus.models.task import Task, TaskSubmission
from taskq_plus.observability import audit as _audit
from taskq_plus.observability import export as _export
from taskq_plus.service.breaker import STATE_OPEN
from taskq_plus.service.cache import cache_ttl, lookup as cache_lookup, record as cache_record
from taskq_plus.service.dag import (
    chain_depths as _chain_depths,
    cycle_path as _cycle_path,
    dependency_edges as _dependency_edges,
    topo_sort as _kahn_order,
)
from taskq_plus.service.executor import run_task
from taskq_plus.service.plugins import (
    PLUGIN_NAME_RE,
    PluginRegistry,
    parse_plugin_specs,
)
from taskq_plus.storage.breaker_store import make_breaker_store
from taskq_plus.storage.task_store import get_store, reset_store_cache


#: [FR-02] Default per-task timeout (seconds) when `TASKQ_TASK_TIMEOUT`
#: is unset. SPEC §3 FR-02 line 110 spells out the env override.
DEFAULT_TASK_TIMEOUT: float = 10.0

#: [FR-02] Default `max_workers` for `--all`. SPEC §3 FR-02 line 122.
DEFAULT_MAX_WORKERS: int = 4

#: [FR-05] Every data file `clear` wipes from `$TASKQ_HOME`.
#: SPEC §5.2 lines 311-314 enumerate exactly these four.
DATA_FILENAMES: Tuple[str, ...] = (
    "tasks.json",
    "breaker.json",
    "cache.json",
    "audit.jsonl",
)

#: [FR-05/FR-06] Default dependency-chain depth cap when
#: `TASKQ_MAX_DAG_DEPTH` is unset (SPEC §5.1 line 302).
DEFAULT_MAX_DAG_DEPTH: int = 32


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
    parser.add_argument(
        "--cached",
        action="store_true",
        dest="use_cache",
        help="Replay a recent completed result when available.",
    )
    return parser


def _parse_args(parser: argparse.ArgumentParser, argv: Optional[List[str]]) -> argparse.Namespace:
    """Parse an optional token list consistently across command handlers."""
    return parser.parse_args(list(argv) if argv is not None else [])


def _task_payload(task: Task) -> Dict[str, object]:
    """Serialize a task once for both human and machine-readable output."""
    return task.model_dump(mode="json")


def _emit_json(payload: object) -> None:
    """Emit one compact, Unicode-preserving JSON record."""
    print(json.dumps(payload, ensure_ascii=False))


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


def _utcnow() -> datetime:
    """[FR-02/FR-06] UTC timestamp for `finished_at` (and blocked rows)."""
    return datetime.now(timezone.utc)


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
    """[FR-02 / NFR-04] Persist every field produced by a task execution.

    `stdout_tail` and `stderr_tail` are redacted via `audit._redact`
    before the row hits disk so a secret embedded in either tail never
    lands on `$TASKQ_HOME/tasks.json` unredacted (SPEC §4 NFR-04 line
    211-214 — redaction before write to disk).
    """
    result_fields = {
        "status": result.status,
        "exit_code": result.exit_code,
        "stdout_tail": _audit._redact(result.stdout_tail)
        if result.stdout_tail is not None
        else None,
        "stderr_tail": _audit._redact(result.stderr_tail)
        if result.stderr_tail is not None
        else None,
        "duration_ms": result.duration_ms,
        "finished_at": result.finished_at,
        "cached": False,
    }
    store.update(
        task.id,
        lambda stored_task: stored_task.model_copy(update=result_fields),
    )


def _execute_and_persist(
    task: Task,
    *,
    store,
    plugin_registry: Optional[PluginRegistry] = None,
) -> str:
    """[FR-02/FR-07] Execute `task`, persist its result, return its status.

    When a `plugin_registry` is supplied, the FR-07 `pre_run` /
    `post_run` hooks are invoked around the executor dispatch. A
    hook that raises is logged as a `plugin_error` audit event
    by the registry and the executor still runs — the plugin
    surface never aborts the task (SPEC §3 FR-07 line 159).
    """
    if plugin_registry is not None:
        plugin_registry.run_pre(
            task, task_id=task.id, correlation_id=task.id
        )
    result = run_task(task, timeout=_timeout_budget())
    _persist_result(store, task, result)
    if plugin_registry is not None:
        plugin_registry.run_post(
            task,
            result,
            task_id=task.id,
            correlation_id=task.id,
        )
    return result.status


def _build_plugin_registry() -> PluginRegistry:
    """[FR-07] Construct and load the plugins for the current run.

    Returns a `PluginRegistry` whose `load()` has parsed
    `TASKQ_PLUGINS` and attempted every import. The registry is
    shared across all tasks in one `run` invocation so the
    consecutive-failure counter survives between task dispatches
    (the 3-failure auto-disable is per-run, not per-task).
    """
    registry = PluginRegistry()
    registry.load()
    return registry


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
        5 — `--after` would close a cycle, or the resulting chain
            depth exceeds `TASKQ_MAX_DAG_DEPTH`.

    Citations:
        SPEC.md §7 line 383 — exit 2 on 空/非法命令.
        SPEC.md §7 line 385 — exit 2 on `--after` 不存在.
        SPEC.md §3 FR-06 line 147 — cycle detection.
        SPEC.md §3 FR-06 line 148 — chain depth cap.
        SPEC.md §7 line 388 — exit 5, stderr 列出循環路徑.
        SPEC.md §7 line 389 — exit 5, stderr
            `dependency chain too deep: <n> > <max>`.
    """
    parser = _build_submit_parser()
    args = _parse_args(parser, argv)

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

    # [FR-06] Validate cycle and depth against the *persisted* graph
    # BEFORE adding the new task. A cycle anywhere in the store —
    # including one introduced by an out-of-band edit to `tasks.json`
    # — must surface here so the next `submit --after` cannot close
    # it. The depth cap rejects chains whose new tail would exceed
    # `TASKQ_MAX_DAG_DEPTH`.
    existing = store.all()
    by_id: Dict[str, object] = {t.id: t for t in existing}
    by_id[task.id] = task
    deps = _dependency_edges([*existing, task])
    order, remaining = _kahn_order(deps)
    if remaining:
        path = _cycle_path(deps, remaining)
        _emit_stderr_error("dependency cycle: " + " → ".join(path))
        return 5
    depths = _chain_depths(deps, order)
    new_depth = depths[task.id]
    cap = _max_dag_depth()
    if new_depth > cap:
        _emit_stderr_error(
            f"dependency chain too deep: {new_depth} > {cap}"
        )
        return 5

    stored = store.add(task)

    # [FR-08] Emit the `submit` audit event for this CLI invocation.
    # The `correlation_id` is minted once per `submit` invocation and
    # carried on every audit event triggered by that invocation
    # (SPEC §3 FR-08 line 166). The `_audit_log_correlation_id` is
    # threaded through the rest of the FR-08 events so `run` and
    # `export` can share it when the same process emits multiple
    # events; `submit` is the one that always emits first.
    correlation_id = _audit.new_correlation_id()
    _audit.append_event(
        "submit",
        task_id=stored.id,
        correlation_id=correlation_id,
        detail={"command": stored.command, "name": stored.name},
    )

    if args.as_json:
        print(json.dumps({"id": stored.id, "status": stored.status}))
    else:
        print(stored.id)
    return 0


def run(argv: Optional[List[str]] = None, *, use_disk: bool = False) -> int:
    """[FR-02/FR-03] Run a `run` invocation.

    `argv` is the token list *after* the leading `run` keyword. Two
    shapes are supported:

        run <id>      execute the task with the given id
        run --all     execute every pending task concurrently

    The per-task path consults the breaker (SPEC §3 FR-03) BEFORE
    launching any subprocess: a still-OPEN breaker short-circuits with
    exit 3 and stderr `breaker open`. After a successful or failed
    execution the outcome is recorded on the breaker and the state is
    persisted to `$TASKQ_HOME/breaker.json`.

    Returns:
        0 — done / failed (the failure is recorded in the task's
            `status` field; the CLI itself exits 0).
        3 — breaker OPEN (the run was rejected before any subprocess).
        4 — single-task timeout (SPEC §3 FR-02 line 120).
        2 — invalid usage (no id and no --all; id not found).

    Citations:
        SPEC.md §3 FR-02 lines 105-118 — state machine.
        SPEC.md §3 FR-02 line 120 — single-task timeout → exit 4.
        SPEC.md §3 FR-02 line 122 — `--all` thread-pool dispatch.
        SPEC.md §3 FR-03 — breaker rejection (`exit 3` + stderr
            `breaker open`).
    """
    parser = _build_run_parser()
    args = _parse_args(parser, argv)

    if not args.run_all and args.task_id is None:
        print("run: must supply a task id or --all", file=sys.stderr)
        return 2

    store = get_store(use_disk=use_disk)

    if args.run_all:
        return _run_all(store, _build_plugin_registry())

    # [FR-03] Breaker gate: reject the run BEFORE any task lookup or
    # subprocess dispatch when the breaker is OPEN. The state is read
    # fresh from `$TASKQ_HOME/breaker.json` so a flipped-to-OPEN
    # subprocess directly preceding this call is honoured.
    bstore = make_breaker_store()
    breaker = bstore.load()
    breaker.check()  # OPEN -> HALF_OPEN if cooldown elapsed (no-op on CLOSED)
    if breaker.state == STATE_OPEN:
        # [FR-08] Audit the breaker-open rejection as a single
        # `breaker_open` event so the operator can correlate it
        # with the CLI invocation that triggered it.
        _audit.append_event(
            "breaker_open",
            task_id=None,
            correlation_id=_audit.new_correlation_id(),
            detail={"reason": "breaker open"},
        )
        print("breaker open", file=sys.stderr)
        return 3

    task = store.find(args.task_id)
    if task is None:
        print(f"run: task {args.task_id!r} not found", file=sys.stderr)
        return 2

    # [FR-08] Mint one `correlation_id` for this `run` invocation;
    # both the `run_start` and `run_end` events share it so the
    # AC-08-1 invariant (one correlation_id per CLI invocation) holds.
    correlation_id = _audit.new_correlation_id()
    _audit.append_event(
        "run_start",
        task_id=task.id,
        correlation_id=correlation_id,
        detail={"command": task.command},
    )

    # [FR-04] A cache hit updates the task directly and never invokes the
    # executor. CacheStore handles corrupt files as ordinary misses.
    cached_entry = None
    if args.use_cache:
        cached_entry = cache_lookup(task.command, ttl_s=cache_ttl())
    if cached_entry is not None:
        cached_raw = cached_entry.get("stdout_tail")
        cached_fields = {
            "status": "done",
            "exit_code": cached_entry.get("exit_code"),
            "stdout_tail": _audit._redact(cached_raw)
            if cached_raw is not None
            else None,
            "cached": True,
        }
        store.update(task.id, lambda current: current.model_copy(update=cached_fields))
        breaker.record_success()
        bstore.save(breaker)
        _audit.append_event(
            "run_end",
            task_id=task.id,
            correlation_id=correlation_id,
            detail={"status": "done", "cached": True},
        )
        return 0

    status = _execute_and_persist(
        task, store=store, plugin_registry=_build_plugin_registry()
    )

    _audit.append_event(
        "run_end",
        task_id=task.id,
        correlation_id=correlation_id,
        detail={"status": status},
    )

    if status == "done":
        updated = store.find(task.id)
        if updated is not None:
            # [FR-04 / NFR-04] Keep the newest successful result available
            # for replay. `stdout_tail` is redacted before write so a
            # secret embedded in the captured output never lands on
            # `$TASKQ_HOME/cache.json` unredacted (SPEC §4 NFR-04 line
            # 211-214 — redaction before write to disk).
            raw_stdout = updated.stdout_tail
            cache_record(
                updated.command,
                {
                    "command": updated.command,
                    "exit_code": updated.exit_code,
                    "stdout_tail": _audit._redact(raw_stdout)
                    if raw_stdout is not None
                    else None,
                    "finished_at": updated.finished_at.isoformat()
                    if updated.finished_at is not None
                    else None,
                    "status": "done",
                },
            )

    # [FR-03] Persist the outcome on the breaker. `done` resets the
    # count to 0 (CLOSED — no failure memory); `failed` / `timeout`
    # increment and may trip the breaker OPEN.
    if status == "done":
        breaker.record_success()
    elif status in ("failed", "timeout"):
        breaker.record_failure()
    bstore.save(breaker)

    if status == "timeout":
        return 4
    return 0


def _run_all(store, plugin_registry: Optional[PluginRegistry] = None) -> int:
    """[FR-02/FR-06] Execute every pending task in topological order.

    Walks the dependency graph layer by layer — a layer is the set of
    pending tasks whose prerequisites are all in earlier layers or are
    already in a terminal state in the persisted store. Within a layer
    the executor is dispatched concurrently (SPEC §3 FR-06 line 146
    "only in-degree-0 tasks are eligible for concurrent dispatch
    within a layer"); between layers the dispatcher waits so a
    downstream task cannot start before its prerequisite finishes.

    A task whose prerequisite ended in any non-`done` state is
    persisted as `status="blocked"` and skipped (SPEC §3 FR-06 line
    146 — "下游任務... 不執行"). The blocked task does NOT count
    toward the breaker failure counter; only tasks that actually
    launched a subprocess contribute to `record_failure` /
    `record_success`.

    Returns 0 because the CLI exit code reflects the subprocess
    dispatch shape, not the per-task outcome (per-task outcome is in
    the task record's `status`).

    Citations:
        SPEC.md §3 FR-02 line 122 — `--all` thread-pool dispatch.
        SPEC.md §3 FR-06 line 145 — Kahn topological sort.
        SPEC.md §3 FR-06 line 146 — blocked / breaker invariants.
        SPEC.md §3 FR-06 line 147 — cycle detection.
    """
    bstore = make_breaker_store()
    breaker = bstore.load()

    tasks = store.all()
    by_id = {t.id: t for t in tasks}
    if not any(t.status == "pending" for t in tasks):
        return 0

    # [FR-06] Build the full dependency graph (over ALL tasks, not just
    # pending ones — a persisted `failed` task is still a node whose
    # dependents must be blocked). The depth cap and cycle checks
    # already ran in `submit` for the *new* edges, but a `tasks.json`
    # edit can introduce a cycle out-of-band; in that case `run --all`
    # cannot make progress and exits 0 without dispatching.
    deps = _dependency_edges(tasks)
    order, remaining = _kahn_order(deps)
    if remaining:
        return 0

    depths = _chain_depths(deps, order)
    layers: Dict[int, List[str]] = {}
    for tid in order:
        layers.setdefault(depths[tid], []).append(tid)

    breaker_dirty = False
    for depth in sorted(layers):
        runnable: List[Task] = []
        for tid in layers[depth]:
            task = by_id[tid]
            if task.status != "pending":
                continue
            prereqs_ok = all(
                by_id[prereq].status == "done" for prereq in deps[tid]
            )
            if not prereqs_ok:
                # [FR-06] Mark `blocked` and skip. The blocked task
                # is NOT executed and does NOT increment the breaker
                # failure counter (SPEC §3 FR-06 line 146).
                now = _utcnow()
                store.update(
                    tid,
                    lambda t, _now=now: t.model_copy(update={
                        "status": "blocked",
                        "exit_code": None,
                        "stdout_tail": None,
                        "stderr_tail": None,
                        "duration_ms": 0,
                        "finished_at": _now,
                        "cached": False,
                    }),
                )
                refreshed = store.find(tid)
                if refreshed is not None:
                    by_id[tid] = refreshed
                continue
            runnable.append(task)

        if not runnable:
            continue

        def _dispatch(task: Task) -> str:
            return _execute_and_persist(
                task, store=store, plugin_registry=plugin_registry
            )

        max_workers = max(1, min(_max_workers(), len(runnable)))
        if max_workers == 1:
            for task in runnable:
                status = _dispatch(task)
                refreshed = store.find(task.id)
                if refreshed is not None:
                    by_id[task.id] = refreshed
                if status == "done":
                    breaker.record_success()
                elif status in ("failed", "timeout"):
                    breaker.record_failure()
                breaker_dirty = True
        else:
            # [FR-03] Aggregate per-layer statuses BEFORE updating the
            # breaker: a layer is one concurrent dispatch wave. Iterating
            # `futures.items()` in insertion order can interleave a
            # `record_success` after a `record_failure` and silently
            # reset the consecutive-failure counter; successes within
            # the same layer must NOT clobber same-layer failures.
            statuses: List[Tuple[Task, str]] = []
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                futures = {pool.submit(_dispatch, task): task for task in runnable}
                for future, task in futures.items():
                    status = future.result()
                    statuses.append((task, status))
                    refreshed = store.find(task.id)
                    if refreshed is not None:
                        by_id[task.id] = refreshed
                    breaker_dirty = True
            # Record every failure in the layer first. Then reset the
            # breaker ONLY when the layer produced zero failures — a
            # mixed-outcome layer preserves the net failure count.
            layer_had_failure = False
            for _task, status in statuses:
                if status in ("failed", "timeout"):
                    breaker.record_failure()
                    layer_had_failure = True
            if not layer_had_failure:
                for _task, status in statuses:
                    if status == "done":
                        breaker.record_success()

    if breaker_dirty:
        bstore.save(breaker)

    return 0


# ===========================================================================
# [FR-05] Query / inspection / maintenance handlers
# ===========================================================================


def _build_status_parser() -> argparse.ArgumentParser:
    """[FR-05] Build the `status` argument parser.

    Citations:
        SPEC.md §3 FR-05 line 132 — `status <id>` 輸出該任務全欄位.
        SPEC.md §3 FR-05 line 139 — 全域 flag `--json`.
    """
    parser = argparse.ArgumentParser(
        prog="taskq status",
        description="Print every stored field of one task.",
    )
    parser.add_argument("task_id", help="Task id to inspect.")
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit the task as a single-line JSON object.",
    )
    return parser


def status(argv: Optional[List[str]] = None, *, use_disk: bool = False) -> int:
    """[FR-05] Print every field of one task; return an exit code.

    `argv` is the token list *after* the leading `status` keyword. With
    `--json` the whole record is emitted as one parseable line (the
    machine-readable channel of SPEC §3 FR-05 line 139); without it the
    same fields are printed as `key: value` lines.

    The payload is `Task.model_dump(mode="json")`, so it carries every
    field the submission API persists (`id`, `command`, `name`,
    `status`, `created_at`, `depends_on`) plus the FR-02 result fields.
    Dumping the model rather than hand-listing keys is deliberate: a
    field added to `Task` cannot silently fall out of the `--json`
    surface.

    Returns:
        0 — task found and printed.
        2 — unknown task id.

    Citations:
        SPEC.md §3 FR-05 line 132 — 輸出該任務全欄位.
        SPEC.md §3 FR-05 line 139 — `--json` 單行 JSON.
        SPEC.md §7 line 384 — unknown task id → exit 2, stderr
            `unknown task: <id>`.
    """
    parser = _build_status_parser()
    args = _parse_args(parser, argv)

    store = get_store(use_disk=use_disk)
    task = store.find(args.task_id)
    if task is None:
        print(f"unknown task: {args.task_id}", file=sys.stderr)
        return 2

    payload = _task_payload(task)
    if args.as_json:
        _emit_json(payload)
    else:
        for key in payload:
            print(f"{key}: {payload[key]}")
    return 0


def _build_list_parser() -> argparse.ArgumentParser:
    """[FR-05] Build the `list` argument parser.

    Citations:
        SPEC.md §3 FR-05 line 133 — `list [--status S]`.
    """
    parser = argparse.ArgumentParser(
        prog="taskq list",
        description="List stored tasks.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit the task list as a single-line JSON array.",
    )
    return parser


def list_tasks(argv: Optional[List[str]] = None, *, use_disk: bool = False) -> int:
    """[FR-05] List stored tasks; return an exit code.

    A corrupted `tasks.json` surfaces as `store corrupted` on stderr
    and exit 1 rather than a silent rebuild — the store deliberately
    lets `json.JSONDecodeError` escape `load()` so this handler can
    make the corruption visible (SPEC §7 line 392).

    Returns:
        0 — tasks listed.
        1 — `tasks.json` is not valid JSON.

    Citations:
        SPEC.md §3 FR-05 line 133 — `list` 列出任務.
        SPEC.md §3 FR-05 line 140 — `1` 其他內部錯誤.
        SPEC.md §7 line 392 — `tasks.json` 損壞 → exit 1, stderr
            `store corrupted` (不靜默重建).
    """
    parser = _build_list_parser()
    args = _parse_args(parser, argv)

    store = get_store(use_disk=use_disk)
    try:
        tasks = store.all()
    except json.JSONDecodeError:
        print("store corrupted", file=sys.stderr)
        return 1

    if args.as_json:
        _emit_json([_task_payload(task) for task in tasks])
    else:
        for task in tasks:
            print(f"{task.id}\t{task.status}\t{task.command}")
    return 0


def clear(argv: Optional[List[str]] = None) -> int:
    """[FR-05] Wipe every data file in `$TASKQ_HOME`; return an exit code.

    Removes `tasks.json`, `breaker.json`, `cache.json`, and
    `audit.jsonl` — the exact four files SPEC §5.2 lines 311-314
    declare. The operation is idempotent: a file that is already
    absent is not an error, so `clear` on a fresh `$TASKQ_HOME`
    still exits 0.

    The in-process store cache is reset alongside the files so a
    caller that keeps running in the same process does not read a
    stale snapshot of a store whose backing file no longer exists.

    Returns:
        0 — every data file removed (or already absent).

    Citations:
        SPEC.md §3 FR-05 line 137 — `clear` 清空 `$TASKQ_HOME` 全部資料檔.
        SPEC.md §5.2 lines 311-314 — the four data files.
    """
    parser = argparse.ArgumentParser(
        prog="taskq clear",
        description="Remove every data file in $TASKQ_HOME.",
    )
    parser.parse_args(list(argv) if argv is not None else [])

    home = taskq_home()
    for filename in DATA_FILENAMES:
        (home / filename).unlink(missing_ok=True)
    reset_store_cache()
    return 0


# ---------------------------------------------------------------------------
# [FR-08] Three-format task export
# ---------------------------------------------------------------------------


def _build_export_parser() -> argparse.ArgumentParser:
    """[FR-08] Build the `export` argument parser.

    `--format` is required so the dispatcher exits 2 on a missing
    value rather than guessing a default that could silently mask a
    caller bug.
    """
    parser = argparse.ArgumentParser(
        prog="taskq export",
        description="Export the task list in json/csv/md form.",
    )
    parser.add_argument(
        "--format",
        required=True,
        choices=sorted(_export.VALID_FORMATS),
        help="Output format: json, csv, or md.",
    )
    return parser


def export(argv: Optional[List[str]] = None, *, use_disk: bool = False) -> int:
    """[FR-08] Export the task list in the requested format; return an exit code.

    The three renderers (`json` / `csv` / `md`) all consume the same
    record list, so the field set and the row count are guaranteed
    to agree across formats (SPEC §3 FR-08 line 187; the test
    `test_fr08_exports_agree_and_escape_csv_fields[formats_agree]`
    asserts this invariant).

    Returns:
        0 — records rendered to stdout.
        2 — unknown / unsupported format (argparse rejects an
            out-of-set value before the handler runs).

    Citations:
        SPEC.md §3 FR-08 lines 181-191 — three-format export.
        SPEC.md §8 row #14 — acceptance row for `export --format
            json|csv|md`.
    """
    parser = _build_export_parser()
    try:
        args = _parse_args(parser, argv)
    except SystemExit as exc:
        # [FR-08] argparse calls `sys.exit(2)` on a `choices` rejection
        # (e.g. `--format xml`). The handler contract is to *return* the
        # exit code rather than let SystemExit propagate, so in-process
        # callers can assert `rc == 2` without rescuing the exception.
        return exc.code

    store = get_store(use_disk=use_disk)
    tasks = store.all()
    records = _export.to_records(tasks)

    output = _export.render(records, args.format)
    if output:
        sys.stdout.write(output)
        if not output.endswith("\n"):
            sys.stdout.write("\n")
    return 0


# ---------------------------------------------------------------------------
# [FR-05/FR-06] Dependency-graph inspection
# ---------------------------------------------------------------------------


def _max_dag_depth() -> int:
    """[FR-05/FR-06] Read `TASKQ_MAX_DAG_DEPTH` from the environment.

    Citations:
        SPEC.md §5.1 line 302 — `TASKQ_MAX_DAG_DEPTH` default `32`.
    """
    raw = os.environ.get("TASKQ_MAX_DAG_DEPTH", "")
    if raw == "":
        return DEFAULT_MAX_DAG_DEPTH
    try:
        return int(raw)
    except ValueError:
        return DEFAULT_MAX_DAG_DEPTH


def graph(argv: Optional[List[str]] = None, *, use_disk: bool = False) -> int:
    """[FR-05/FR-06] Print the dependency graph; return an exit code.

    Runs the cycle detector and the depth cap across the persisted
    graph before rendering. Both violations map to the same exit code
    (5) but carry distinct stderr messages so the operator can tell a
    cycle from a pathological chain.

    Returns:
        0 — graph is a DAG within the depth cap; the tree was printed.
        5 — the graph contains a cycle, or a chain exceeds
            `TASKQ_MAX_DAG_DEPTH`.

    Citations:
        SPEC.md §3 FR-05 line 134 — `graph` 輸出相依圖.
        SPEC.md §3 FR-05 line 140 — `5` 相依圖存在循環或深度超限.
        SPEC.md §3 FR-06 lines 147-148 — cycle detection + depth cap.
        SPEC.md §7 line 388 — stderr 列出循環路徑.
        SPEC.md §7 line 389 — stderr `dependency chain too deep:
            <n> > <max>`.
    """
    parser = argparse.ArgumentParser(
        prog="taskq graph",
        description="Print the task dependency graph.",
    )
    parser.parse_args(list(argv) if argv is not None else [])

    store = get_store(use_disk=use_disk)
    tasks = store.all()
    deps = _dependency_edges(tasks)

    order, remaining = _kahn_order(deps)
    if remaining:
        path = _cycle_path(deps, remaining)
        print("dependency cycle: " + " → ".join(path), file=sys.stderr)
        return 5

    depths = _chain_depths(deps, order)
    deepest = max(depths.values(), default=0)
    cap = _max_dag_depth()
    if deepest > cap:
        print(
            f"dependency chain too deep: {deepest} > {cap}", file=sys.stderr
        )
        return 5

    for node in order:
        indent = "  " * (depths[node] - 1)
        print(f"{indent}{node}")
    return 0


# ---------------------------------------------------------------------------
# [FR-05/FR-07] Plugin inspection
# ---------------------------------------------------------------------------


def plugins(argv: Optional[List[str]] = None) -> int:
    """[FR-05/FR-07] List the declared plugin allowlist; return an exit code.

    Plugin specs come from the positional arguments when supplied, and
    otherwise from the comma-separated `TASKQ_PLUGINS` allowlist. The
    bare `list` keyword (`taskq plugins list`) is the documented verb
    and is not itself a plugin spec.

    Every spec must match `^[A-Za-z_][A-Za-z0-9_.]*$`. A path or URL
    form (`../evil.py`, `https://...`) fails that whitelist and is
    rejected with exit 6 *before* any import is attempted — the
    security rule that keeps `TASKQ_PLUGINS` from becoming an
    arbitrary-code-execution entry point (SPEC §3 FR-07 lines 155-157,
    NFR-02).

    For each well-formed spec the loader tries to import the module
    and reports the registered hooks (`pre_run`, `post_run`) plus the
    load status (`loaded`, `failed`, `not_loaded`). The output is
    one record per plugin, space-separated.

    Returns:
        0 — every declared spec is a well-formed module name (its
            import may still have failed, which is reported as
            `status=failed`).
        6 — a spec is not a module name (path / URL form).

    Citations:
        SPEC.md §3 FR-05 line 135 — `plugins list`.
        SPEC.md §3 FR-05 line 140 — `6` plugin 載入失敗.
        SPEC.md §3 FR-07 line 157 — 模組名必須匹配
            `^[A-Za-z_][A-Za-z0-9_.]*$`,不符 → 拒絕載入, exit 6.
        SPEC.md §3 FR-07 line 160 — `plugins list` 輸出每個 plugin
            的模組名、註冊的 hook、載入狀態.
        SPEC.md §4 NFR-02 line 200 — 不得接受檔案路徑或 URL.
        SPEC.md §7 line 390 — path form rejected, exit 6.
    """
    parser = argparse.ArgumentParser(
        prog="taskq plugins",
        description="List the declared plugin allowlist.",
    )
    parser.add_argument(
        "specs",
        nargs="*",
        default=[],
        help="Plugin module names; defaults to $TASKQ_PLUGINS.",
    )
    args = _parse_args(parser, argv)

    specs = [spec for spec in args.specs if spec != "list"]
    if not specs:
        specs = parse_plugin_specs(os.environ.get("TASKQ_PLUGINS", ""))

    # Phase 1 — regex whitelist. Any spec that fails the regex is a
    # path / URL form and is rejected with exit 6 *before* any
    # import is attempted (FR-07 + NFR-02 security rule).
    for spec in specs:
        if not PLUGIN_NAME_RE.match(spec):
            print(f"rejected module: {spec}", file=sys.stderr)
            return 6

    # Phase 2 — load every well-formed spec via the registry and
    # print one record per plugin: module name, registered hooks,
    # and load status.
    registry = PluginRegistry(plugin_env=",".join(specs))
    registry.load()
    for record in registry.records:
        hooks_str = ",".join(record.hooks) if record.hooks else "-"
        line = f"{record.name}  hooks={hooks_str}  status={record.status}"
        if record.error and record.status != "loaded":
            line += f"  error={record.error}"
        print(line)
    return 0
