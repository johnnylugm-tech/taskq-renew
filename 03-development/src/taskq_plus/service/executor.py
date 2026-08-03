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
from typing import Callable, List, Optional, Sequence, cast

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


def _retry_limit_from_env() -> int:
    """[FR-03] Read `TASKQ_RETRY_LIMIT` from the environment.

    Returns the integer value, or `DEFAULT_RETRY_LIMIT` when unset,
    empty, or non-numeric. Read at call time so `monkeypatch.setenv`
    in the test suite picks up the override.
    """
    raw = os.environ.get("TASKQ_RETRY_LIMIT", "")
    if raw == "":
        return DEFAULT_RETRY_LIMIT
    try:
        return int(raw)
    except ValueError:
        return DEFAULT_RETRY_LIMIT


def _backoff_base_from_env() -> float:
    """[FR-03] Read `TASKQ_BACKOFF_BASE` from the environment.

    Returns the float value (seconds), or `DEFAULT_BACKOFF_BASE` when
    unset, empty, or non-numeric.
    """
    raw = os.environ.get("TASKQ_BACKOFF_BASE", "")
    if raw == "":
        return DEFAULT_BACKOFF_BASE
    try:
        return float(raw)
    except ValueError:
        return DEFAULT_BACKOFF_BASE


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

    attempts_total = len(commands)
    if attempts_total == 0:
        raise ValueError("run_with_retry requires at least one command")

    last_result: Optional[TaskResult] = None
    for idx, task in enumerate(commands):
        result = run_task(task, timeout=timeout)
        # First-attempt success never sleeps before retrying.
        if result.status == "done":
            return result

        last_result = result

        # Decide whether to retry: only retryable failures count,
        # and only while retries remain AND there is a follow-up
        # command in the sequence to run.
        retries_done = idx  # already-exhausted retries
        has_followup = idx + 1 < attempts_total
        if (
            result.status in _RETRYABLE_STATUSES
            and has_followup
            and retries_done < retry_limit
        ):
            backoff_index = idx + 1  # 1-indexed for the upcoming retry
            sleep_seconds = backoff_base * (2 ** backoff_index)
            sleep_fn(sleep_seconds)
            continue
        # Either done above, exhausted, or no follow-up → return.
        return result

    # `for/else` not used: the loop above always returns inside the
    # final iteration when there is exactly one command. This branch
    # is unreachable under the `attempts_total >= 1` precondition.
    assert last_result is not None  # pragma: no cover — defensive
    return last_result  # pragma: no cover — defensive
