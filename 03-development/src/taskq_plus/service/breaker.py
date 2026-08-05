"""[FR-03] `Breaker` — the cross-task, cross-process circuit breaker.

The breaker is the global throttle on task execution: after
`TASKQ_BREAKER_THRESHOLD` consecutive final failures the next `run` is
short-circuited (no subprocess launched, exit 3, stderr `breaker open`).
After `TASKQ_BREAKER_COOLDOWN` seconds the state moves to `HALF_OPEN`,
which admits exactly one probe; success closes the breaker, failure
re-opens it.

State machine (SPEC.md §3 FR-03):

    CLOSED  --(failure_count >= threshold)-->  OPEN
    OPEN    --(cooldown elapsed, .check())  -->  HALF_OPEN
    HALF_OPEN --(record_success)            -->  CLOSED (count=0)
    HALF_OPEN --(record_failure)            -->  OPEN

Injectables (per SPEC §7 — testability):
    clock        default `time.monotonic`. Anchors `opened_at`.
    sleep_fn     default `time.sleep`. The HALF_OPEN transition in
                 `commands.run` uses the *caller's* clock + sleep to
                 enforce cooldown wall-clock timing — the breaker
                 itself never sleeps.

Citations:
    SPEC.md §3 FR-03 — retry + breaker state machine.
    SPEC.md §3 FR-03 — consecutive final failures → OPEN.
    SPEC.md §4 NFR-03 — state persists at `$TASKQ_HOME/breaker.json`.
    SPEC.md §7 line 393 — `breaker open | exit 3, stderr breaker open,
        不執行`.
"""
from __future__ import annotations

import time
from typing import Callable, Optional

# pragma: no error-handling


#: [FR-03] Canonical states. SPEC §3 FR-03 — CLOSED / OPEN / HALF_OPEN.
STATE_CLOSED: str = "CLOSED"
STATE_OPEN: str = "OPEN"
STATE_HALF_OPEN: str = "HALF_OPEN"


class Breaker:
    """[FR-03] Circuit breaker — global, cross-task, cross-process.

    Tracks consecutive final failures and exposes state transitions
    through `record_failure`, `record_success`, and `check`.

    Construct with `Breaker(threshold=3, cooldown_s=60.0)` for the
    canonical defaults; tests inject `clock` and `sleep_fn` so they
    can drive transitions without real wall-clock time.

    Attributes:
        state: One of `STATE_CLOSED` / `STATE_OPEN` / `STATE_HALF_OPEN`.
        failure_count: Consecutive final failures (resets on CLOSE).
        opened_at: Monotonic timestamp of last OPEN transition, or None
            when not OPEN. Anchors `check()`'s cooldown comparison.
    """

    def __init__(
        self,
        *,
        threshold: int = 3,
        cooldown_s: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        self.threshold: int = int(threshold)
        self.cooldown_s: float = float(cooldown_s)
        self.clock: Callable[[], float] = clock
        self.sleep_fn: Callable[[float], None] = sleep_fn
        self.state: str = STATE_CLOSED
        self.failure_count: int = 0
        self.opened_at: Optional[float] = None

    def record_failure(self) -> None:
        """[FR-03] Increment `failure_count`; trip to OPEN at threshold.

        When `failure_count` reaches `threshold` the breaker trips to
        OPEN and `opened_at` is anchored to the injected clock so
        `check()` can compute cooldown eligibility. Subsequent
        failures while OPEN refresh `opened_at` so a burst of failures
        cannot let the cooldown lapse prematurely.
        """
        self.failure_count += 1
        if self.failure_count >= self.threshold:
            self.state = STATE_OPEN
        if self.state == STATE_OPEN:
            self.opened_at = self.clock()

    def record_success(self) -> None:
        """[FR-03] Reset on success; CLOSED + count = 0, or HALF_OPEN -> CLOSED.

        Called either after a successful run (while CLOSED — no-op
        on the count because failures only accumulate while broken)
        or after a successful HALF_OPEN probe — which transitions the
        breaker back to CLOSED and zeroes the failure count.
        """
        self.state = STATE_CLOSED
        self.failure_count = 0
        self.opened_at = None

    def check(self) -> None:
        """[FR-03] Test the cooldown gate; OPEN -> HALF_OPEN when elapsed.

        If the breaker is OPEN and `clock() - opened_at` ≥
        `cooldown_s`, the breaker moves to HALF_OPEN. Otherwise the
        state is left untouched (still OPEN). The function does not
        sleep — the cooldown wall-clock wait is the caller's
        responsibility (see `taskq_plus.cli.commands.run`).
        """
        if self.state != STATE_OPEN:
            return
        if self.opened_at is None:
            return
        if (self.clock() - self.opened_at) >= self.cooldown_s:
            self.state = STATE_HALF_OPEN
