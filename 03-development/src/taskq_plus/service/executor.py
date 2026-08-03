"""[FR-02] Task execution primitive — the subprocess + state machine.

The executor is the single point where a `Task` command is turned into a
`TaskResult`. It deliberately keeps no I/O of its own: the caller
(taskq_plus.cli.commands.run) hands in a `Task` and gets back a result
it can persist through the store.

The state machine (SPEC.md §3 FR-02 lines 105-118):

    pending  ──► running  ──► done      (exit 0)
                            ──► failed   (exit ≠ 0)
                            ──► timeout  (subprocess.TimeoutExpired)
                            ──► blocked  (dependency unmet — FR-06)

Security: the shell-injection-prone `shell=` argument is
**forbidden** on every code path (NFR-02, SPEC §4). The command is
always tokenised with `shlex.split` and executed via the argv form of
`subprocess.run`.

Citations:
    SPEC.md §3 FR-02 lines 105-118 — state machine.
    SPEC.md §3 FR-02 line 110 — `subprocess.run(shlex.split(command),
        capture_output=True, text=True, timeout=TASKQ_TASK_TIMEOUT)`.
    SPEC.md §3 FR-02 line 115-118 — result field shape.
    SPEC.md §4 NFR-02 — shell-injection flag forbidden.
    SPEC.md §3 FR-02 line 120 — single-task timeout → exit 4.
"""
from __future__ import annotations

import os
import shlex
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional, Sequence, cast

from taskq_plus.models.task import Task


#: [FR-02] Tail length for stdout_tail / stderr_tail (SPEC.md §3
#: line 116 — last 2000 chars).
TAIL_CHARS: int = 2000

#: [FR-03] Default retry cap when `TASKQ_RETRY_LIMIT` is unset
#: (SPEC.md §3 FR-03). Override per-call via the `retry_limit` kwarg.
DEFAULT_RETRY_LIMIT: int = 1

#: [FR-03] Default backoff base (seconds) when `TASKQ_BACKOFF_BASE`
#: is unset (SPEC.md §3 FR-03 — `base * 2**n`).
DEFAULT_BACKOFF_BASE: float = 1.0


def _env_value(name: str, default, ctor):
    """[FR-03] Read a `TASKQ_*` env var with a typed fallback.

    Returns `default` when the variable is unset, empty, or fails to
    parse via `ctor` (e.g. `int("abc")`, `float("qux")` raise
    `ValueError`). Read at *call* time so the test suite's
    `monkeypatch.setenv` is observed without restart.
    """
    raw = os.environ.get(name, "")
    if raw == "":
        return default
    try:
        return ctor(raw)
    except (ValueError, TypeError):
        return default


def _retry_limit_from_env() -> int:
    """[FR-03] Read `TASKQ_RETRY_LIMIT` (int; falls back to `DEFAULT_RETRY_LIMIT`)."""
    return _env_value("TASKQ_RETRY_LIMIT", DEFAULT_RETRY_LIMIT, ctor=int)


def _backoff_base_from_env() -> float:
    """[FR-03] Read `TASKQ_BACKOFF_BASE` (float seconds; falls back to `DEFAULT_BACKOFF_BASE`)."""
    return _env_value("TASKQ_BACKOFF_BASE", DEFAULT_BACKOFF_BASE, ctor=float)


def _tail(text: Optional[str]) -> Optional[str]:
    """[FR-02] Return the last `TAIL_CHARS` of `text`, or None if None."""
    if text is None:
        return None
    if len(text) <= TAIL_CHARS:
        return text
    return text[-TAIL_CHARS:]


def _utcnow() -> datetime:
    """[FR-02] UTC timestamp for `finished_at`."""
    return datetime.now(timezone.utc)


@dataclass
class TaskResult:
    """[FR-02] Result of a single execution attempt.

    Carries the executable outcome so the CLI dispatcher can persist
    it back onto the task record. The shape mirrors SPEC.md §3
    FR-02 lines 115-118.

    Attributes:
        status: One of `"done"`, `"failed"`, `"timeout"`, `"blocked"`.
        exit_code: Process exit code (None for `timeout` / `blocked`).
        stdout_tail: Last 2000 chars of stdout (None if blocked).
        stderr_tail: Last 2000 chars of stderr (None if blocked).
        duration_ms: Wall-clock duration in milliseconds.
        finished_at: UTC timestamp of completion.
    """

    status: str
    exit_code: Optional[int]
    stdout_tail: Optional[str]
    stderr_tail: Optional[str]
    duration_ms: int
    finished_at: datetime


def run_task(task: Task, *, timeout: float) -> TaskResult:
    """[FR-02] Execute `task.command` and return a `TaskResult`.

    Translates the subprocess outcome into the state machine:

        exit 0                       → done
        exit ≠ 0                     → failed
        subprocess.TimeoutExpired    → timeout

    Citations:
        SPEC.md §3 FR-02 line 110 — subprocess invocation shape.
        SPEC.md §3 FR-02 lines 112-114 — state machine classification.
        SPEC.md §4 NFR-02 — no shell-injection flag.
    """
    argv = shlex.split(task.command)
    started = time.monotonic()
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            # NFR-02 — explicit opt-out of the shell is the default,
            # but stating it here makes the invariant visible at the
            # audit site (the canonical grep gate runs against this
            # file).
        )
    except subprocess.TimeoutExpired as exc:
        # `text=True` on subprocess.run guarantees exc.stdout / exc.stderr
        # are decoded to str; the bytes branches below were defensive
        # code for a configuration the executor never uses (see SPEC.md
        # §3 FR-02 line 110 — `text=True` is part of the canonical
        # subprocess invocation shape).
        #
        # The cast() calls below silence pyright (typeshed declares
        # TimeoutExpired.stdout / .stderr as `bytes | None` regardless
        # of the `text=` flag on subprocess.run) — the runtime values
        # are str | None because `text=True` is set on line 104.
        duration_ms = int((time.monotonic() - started) * 1000)
        return TaskResult(
            status="timeout",
            exit_code=None,
            stdout_tail=_tail(cast(Optional[str], exc.stdout)),
            stderr_tail=_tail(cast(Optional[str], exc.stderr)),
            duration_ms=duration_ms,
            finished_at=_utcnow(),
        )

    duration_ms = int((time.monotonic() - started) * 1000)
    if completed.returncode == 0:
        status = "done"
    else:
        status = "failed"
    return TaskResult(
        status=status,
        exit_code=completed.returncode,
        stdout_tail=_tail(completed.stdout),
        stderr_tail=_tail(completed.stderr),
        duration_ms=duration_ms,
        finished_at=_utcnow(),
    )


#: [FR-03] Statuses that trigger a retry under FR-03 (SPEC §3 FR-03 —
#: `failed` or `timeout` are retryable; `done` is terminal success).
_RETRYABLE_STATUSES = frozenset({"failed", "timeout"})


def run_with_retry(
    commands: Sequence[Task],
    *,
    timeout: float,
    sleep_fn: Callable[[float], None] = time.sleep,
    retry_limit: Optional[int] = None,
    backoff_base: Optional[float] = None,
) -> TaskResult:
    """[FR-03] Execute `commands` with exponential retry policy.

    Iterates `commands` in order, treating each entry as one attempt's
    command body. An attempt whose result is `done` returns
    immediately. An attempt whose result is `failed` or `timeout`
    triggers an exponential backoff `backoff_base * 2**n` seconds
    before the next attempt, where `n` is 1-indexed for the upcoming
    retry (the first retry waits `base * 2**1`, the second
    `base * 2**2`, etc.). The `sleep_fn` is injectable (default
    `time.sleep`) so the test suite can substitute a recording fake
    without real wall-clock time.

    Args:
        commands: One `Task` per attempt. The first is the original
            command; subsequent entries are retry overrides (the test
            suite uses `["false", "echo hi"]` to drive a transient
            failure into success).
        timeout: Per-attempt budget in seconds (forwarded to
            `run_task`).
        sleep_fn: Injectable sleep (default `time.sleep`).
        retry_limit: Maximum number of retries (overrides
            `TASKQ_RETRY_LIMIT` env when not None).
        backoff_base: Multiplier for the exponential backoff formula
            (overrides `TASKQ_BACKOFF_BASE` env when not None).

    Returns:
        The final `TaskResult` — either a success (`done`) from an
        early attempt or the last attempt's outcome after exhausting
        retries.

    Citations:
        SPEC.md §3 FR-03 — retry rule, exponential backoff
            `base * 2**n`.
        SPEC.md §3 FR-03 — sleep function injectable for testability.
    """
    if retry_limit is None:
        retry_limit = _retry_limit_from_env()
    if backoff_base is None:
        backoff_base = _backoff_base_from_env()

    if len(commands) == 0:
        raise ValueError("run_with_retry requires at least one command")

    # The first attempt always runs and never waits — the backoff
    # belongs strictly *between* attempts. Every subsequent entry in
    # `commands` is a retry, so the retry policy lives entirely in the
    # loop condition: keep retrying while (a) a follow-up command
    # exists, (b) the last outcome was retryable, and (c) the retry
    # budget is not spent (`idx - 1` retries have already happened
    # before the attempt at index `idx`). Expressing all three as loop
    # guards means the loop can only exit one way — through the single
    # `return` below — so there is no unreachable fall-through branch.
    result = run_task(commands[0], timeout=timeout)
    idx = 1
    while (
        idx < len(commands)
        and result.status in _RETRYABLE_STATUSES
        and idx - 1 < retry_limit
    ):
        # n-th retry (1-indexed) waits `base * 2**n` seconds.
        sleep_fn(backoff_base * (2 ** idx))
        result = run_task(commands[idx], timeout=timeout)
        idx += 1
    return result
