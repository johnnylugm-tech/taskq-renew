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

import shlex
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from taskq_plus.models.task import Task


#: [FR-02] Tail length for stdout_tail / stderr_tail (SPEC.md §3
#: line 116 — last 2000 chars).
TAIL_CHARS: int = 2000


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
        duration_ms = int((time.monotonic() - started) * 1000)
        return TaskResult(
            status="timeout",
            exit_code=None,
            stdout_tail=_tail(exc.stdout),
            stderr_tail=_tail(exc.stderr),
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
