"""FR-03: 重試與斷路器 — Retry and circuit breaker.

Test cases correspond 1:1 to TEST_SPEC.md §FR-03 (rows 1–5). The function
names below are the canonical names `spec-coverage-check` looks up — do
NOT rename. State machine per SPEC.md §3 FR-03:

    retry       run produced failed/timeout → automatic retry up to
                TASKQ_RETRY_LIMIT; before the n-th retry, sleep
                TASKQ_BACKOFF_BASE × 2^n seconds (exponential). The
                sleep function must be injectable for testability.
    breaker     global, cross-task, cross-process. Consecutive final
                failures ≥ TASKQ_BREAKER_THRESHOLD → state OPEN.
                While OPEN, any run rejects immediately: exit 3 +
                stderr "breaker open", no subprocess launched.
                After TASKQ_BREAKER_COOLDOWN seconds, transition to
                HALF_OPEN. Success → CLOSED + failure count = 0.
                Failure → re-OPEN.
    persistence state is persisted at $TASKQ_HOME/breaker.json
                (atomic write — see NFR-03).

Subprocess tests exercise the real `python -m taskq_plus` entry point
where the spec literally spells that out (Case 2 — the canonical
"exit 3 + breaker open + no subprocess" check). In-process tests
import the declared SAB modules directly so pytest-cov can measure
`taskq_plus.service.breaker` and `taskq_plus.service.executor` (the
subprocess acceptance path can never raise coverage on these — see
GATE1 SUBPROCESS COVERAGE CEILING in the integration guidelines).
"""
from __future__ import annotations

import contextlib
import io
import json as _json
import re
import subprocess
import sys
import time
from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# 8-hex-char task id pattern (uuid4 prefix).
TASK_ID_RE = re.compile(r"^[0-9a-f]{8}$")


def _run_submit_subprocess(args: list, env: dict) -> subprocess.CompletedProcess:
    """`python -m taskq_plus submit ...` in a child process.

    Out-of-process decision: the canonical SPEC §8 row #8 spells out
    `python -m taskq_plus run` literally, so the breaker-rejection
    acceptance test reproduces the user-facing entry point. The
    in-process tests below exercise the same path through the
    declared SAB modules so pytest-cov can measure them.
    """
    return subprocess.run(
        [sys.executable, "-m", "taskq_plus", "submit", *args],
        capture_output=True,
        text=True,
        env=env,
    )


def _run_subprocess(args: list, env: dict) -> subprocess.CompletedProcess:
    """`python -m taskq_plus run ...` in a child process."""
    return subprocess.run(
        [sys.executable, "-m", "taskq_plus", "run", *args],
        capture_output=True,
        text=True,
        env=env,
    )


def _submit_and_get_id(args: list, env: dict) -> str:
    """Submit a task via the CLI and return its 8-hex id.

    Asserts the submit succeeded and the stdout matches the id regex
    so the caller can immediately use the id without re-asserting the
    same invariants.
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
    """Read and parse `$TASKQ_HOME/tasks.json` (empty list if missing)."""
    tasks_file = taskq_home / "tasks.json"
    if not tasks_file.exists():
        return []
    return _json.loads(tasks_file.read_text(encoding="utf-8"))


def _read_breaker_json(taskq_home: Path) -> dict:
    """Read and parse `$TASKQ_HOME/breaker.json` (defaults if missing)."""
    breaker_file = taskq_home / "breaker.json"
    if not breaker_file.exists():
        return {"state": "CLOSED", "failure_count": 0, "opened_at": None}
    return _json.loads(breaker_file.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Case 1 — AC-03 happy path: transient failure (fail then succeed) -> done
# ---------------------------------------------------------------------------


# NFR-09 (test_assertion_quality) / NFR-15 (timeout, SAD-forced)
def test_fr03_transient_failure_succeeds_after_retry(taskq_home, monkeypatch):
    """FR-03 retry rule: a command sequence "fail,succeed" with
    `TASKQ_RETRY_LIMIT=2` ends in `status=done` after the executor
    retries the failing attempt and the second attempt succeeds. The
    sleep function is injectable — this test records (not sleeps) the
    backoff invocations. *(SPEC §3 FR-03 retry rule + §7)*

    NFR-09 (test_assertion_quality): asserts the final task status
    and that the sleep function was invoked (proving the retry path
    ran at least once). The exact backoff sequence is owned by case 5.

    Out-of-process decision: this test is in-process because the
    retry/backoff surface lives in `taskq_plus.service.executor`,
    which pytest-cov cannot measure through a subprocess. The
    in-process helper is also the test coverage verifier for
    GATE1 test_coverage.
    """
    # GREEN TODO: `taskq_plus.service.executor` must expose a
    # `run_with_retry(commands, *, timeout, sleep_fn, retry_limit,
    # backoff_base)` (or equivalent signature) that:
    #   - executes the first command; if status is failed/timeout,
    #     waits `backoff_base * 2^n` seconds via sleep_fn, then
    #     retries with the next command in the sequence
    #   - stops once any attempt reaches `done` or `retry_limit`
    #     retries are exhausted
    #   - returns the final TaskResult (status, exit_code, etc.)
    # The sleep function MUST be injectable (default time.sleep) so
    # the test can substitute a recording fake without slowing the
    # suite.
    from taskq_plus.models.task import Task
    from taskq_plus.service.executor import run_with_retry

    sleep_calls: list = []
    monkeypatch.setenv("TASKQ_RETRY_LIMIT", "2")
    monkeypatch.setenv("TASKQ_BACKOFF_BASE", "0.1")

    def recording_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    # "command_sequence=fail,succeed" — the test feeds a list so the
    # GREEN executor can pick the next command on each retry. The
    # first command is the failing one (`false`); the second is the
    # succeeding one (`echo hi`).
    result = run_with_retry(
        [Task(command="false"), Task(command="echo hi")],
        timeout=10.0,
        sleep_fn=recording_sleep,
    )

    assert result.status == "done", (
        f"transient-failure retry must end status=done, got {result.status!r}"
    )
    assert result.exit_code == 0, (
        f"succeeding attempt's exit_code must be 0, got {result.exit_code!r}"
    )
    # The retry path must have invoked the sleep function at least
    # once (the first attempt failed; the executor must have waited
    # before retrying).
    assert len(sleep_calls) >= 1, (
        f"retry path must invoke sleep_fn at least once after a "
        f"failed attempt; got {sleep_calls!r}"
    )


# ---------------------------------------------------------------------------
# Case 2 — AC-03-1: 3 consecutive final failures -> run exits 3, "breaker open"
# ---------------------------------------------------------------------------


# NFR-09 / NFR-02 (security — no shell=True) / NFR-15
def test_fr03_breaker_open_rejects_run(taskq_home, child_env, monkeypatch):
    """AC-03-1: After 3 consecutive final failures (`failing_command=false`,
    `consecutive_failures=3`, `breaker_threshold=3`), the next
    `python -m taskq_plus run <id>` exits 3 with stderr `breaker open`
    and does NOT spawn a subprocess. *(SPEC §8 #8 first half + §7)*

    NFR-09 (test_assertion_quality): asserts both the exit code and
    the canonical stderr marker. The "no subprocess" half of the AC
    is verified by re-reading the task's persisted status — a rejected
    run leaves the task in `pending`, because the executor never
    entered the subprocess layer.

    Out-of-process decision: the SPEC §8 row literally invokes
    `python -m taskq_plus run <id>`, so the acceptance test must
    reproduce that user-facing entry point. The in-process companion
    at the bottom of this file covers the same path through the
    declared SAB module for GATE1 test_coverage.
    """
    # 1. Configure the threshold via env so the test is self-contained
    #    (default is already 3, but pin it explicitly so a future
    #    change to the default cannot silently invalidate the AC).
    monkeypatch.setenv("TASKQ_BREAKER_THRESHOLD", "3")
    #    No sleep in the retry path either — speed the precondition.
    monkeypatch.setenv("TASKQ_RETRY_LIMIT", "0")

    # 2. Drive 3 consecutive final failures: each command is `false`,
    #    each retried 0 times, so the failure is final and counts
    #    against the breaker.
    for _ in range(3):
        task_id = _submit_and_get_id(["false"], child_env)
        proc = _run_subprocess([task_id], child_env)
        # The CLI itself exits 0 (failure is recorded in the task
        # record; per FR-02, only `timeout` maps to a non-zero CLI
        # exit). What matters is the *consecutive final failure* from
        # the breaker's point of view.
        assert proc.returncode == 0, (
            f"run on a failed task must exit 0 (failure in the task "
            f"record); got {proc.returncode}; stderr={proc.stderr!r}"
        )

    # 3. The breaker must now be OPEN. Re-read the persisted state so
    #    the test asserts what the subprocess actually wrote, not
    #    what the implementation might keep in memory.
    persisted = _read_breaker_json(taskq_home)
    assert persisted.get("state") == "OPEN", (
        f"3 consecutive final failures must drive breaker to OPEN; "
        f"got {persisted!r}"
    )
    assert int(persisted.get("failure_count", -1)) >= 3, (
        f"failure_count must reach the threshold of 3; got {persisted!r}"
    )

    # 4. Submit a fresh task. Its persisted status must remain
    #    `pending` after the rejected run (the breaker is supposed to
    #    short-circuit before any subprocess is launched).
    fresh_id = _submit_and_get_id(["echo hi"], child_env)
    before = _read_tasks_json(taskq_home)
    before_status = next(t for t in before if t["id"] == fresh_id)["status"]
    assert before_status == "pending", (
        f"freshly submitted task must be pending before its run; "
        f"got {before_status!r}"
    )

    # 5. Run the fresh task — the breaker must reject with exit 3 and
    #    the canonical stderr marker. The command would have succeeded
    #    if the breaker let it through.
    proc = _run_subprocess([fresh_id], child_env)
    assert proc.returncode == 3, (
        f"breaker OPEN must yield CLI exit 3; got {proc.returncode}; "
        f"stderr={proc.stderr!r}"
    )
    assert "breaker open" in proc.stderr, (
        f"stderr must contain canonical marker 'breaker open'; "
        f"got {proc.stderr!r}"
    )

    # 6. The task's persisted status must still be `pending` — proof
    #    that no subprocess was launched (a run on `echo hi` would
    #    have produced `status=done`).
    after = _read_tasks_json(taskq_home)
    after_status = next(t for t in after if t["id"] == fresh_id)["status"]
    assert after_status == "pending", (
        f"task must remain status=pending after a breaker-rejected run; "
        f"got {after_status!r} (the breaker must NOT have spawned a subprocess)"
    )


# ---------------------------------------------------------------------------
# Case 3 — AC-03-2: after TASKQ_BREAKER_COOLDOWN, breaker is CLOSED, count=0
# ---------------------------------------------------------------------------


# NFR-09
def test_fr03_breaker_cooldown_closes_breaker(taskq_home, child_env, monkeypatch):
    """AC-03-2: After `TASKQ_BREAKER_COOLDOWN=1` elapses, the next
    `run` succeeds and the breaker transitions back to CLOSED with
    `failure_count=0`. *(SPEC §8 #8 second half; §3 FR-03 / §7)*

    NFR-09 (test_assertion_quality): asserts the post-cooldown state
    and the failure-count reset on a successful probe.

    The implementation must accept an injected `clock` (default
    `time.monotonic`) and an injectable `sleep_fn` so the test does
    not have to wait real time. This test substitutes a fake clock +
    a no-op sleep so the 1-second cooldown completes instantly.
    """
    # GREEN TODO: `taskq_plus.service.breaker.Breaker` must accept a
    # `clock: Callable[[], float]` (default `time.monotonic`) and a
    # `sleep_fn: Callable[[float], None]` (default `time.sleep`) so
    # the test harness can substitute a fake clock that advances
    # synchronously without a real wall-clock wait. The HALF_OPEN
    # transition must use the injected clock to compare `now -
    # opened_at` against `cooldown_s`, and must NOT itself call
    # `time.sleep` directly.
    from taskq_plus.service.breaker import Breaker
    from taskq_plus.storage.breaker_store import (
        make_breaker_store,
        reset_breaker_store_cache,
    )

    reset_breaker_store_cache()
    monkeypatch.setenv("TASKQ_BREAKER_COOLDOWN", "1")
    monkeypatch.setenv("TASKQ_BREAKER_THRESHOLD", "3")

    # 1. Drive the breaker to OPEN by recording 3 failures through
    #    the declared breaker module.
    fake_now = [1_000.0]

    def clock() -> float:
        return fake_now[0]

    def no_sleep(seconds: float) -> None:  # noqa: ARG001 — injected
        return None

    breaker = Breaker(
        threshold=3,
        cooldown_s=1.0,
        clock=clock,
        sleep_fn=no_sleep,
    )
    for _ in range(3):
        breaker.record_failure()

    assert breaker.state == "OPEN", (
        f"3 failures must drive the breaker to OPEN; got {breaker.state!r}"
    )
    assert breaker.failure_count == 3, (
        f"failure_count must equal 3; got {breaker.failure_count!r}"
    )

    # 2. Persist the OPEN state — the run path reads from the store,
    #    so the test must round-trip through it.
    store = make_breaker_store()
    store.save(breaker)

    # 3. Advance the fake clock past the cooldown and let a probe
    #    through. A successful probe must transition OPEN -> CLOSED
    #    with failure_count reset to 0.
    fake_now[0] += 1.5  # > cooldown_s (1.0)

    # Re-instantiate so the clock is the one the store uses too.
    breaker_after = store.load(clock=clock)
    assert breaker_after.state == "OPEN", (
        f"before probe, state must still be OPEN; got {breaker_after.state!r}"
    )

    # The probe: a successful run transitions HALF_OPEN -> CLOSED.
    breaker_after.check()  # OPEN -> HALF_OPEN (cooldown elapsed)
    assert breaker_after.state == "HALF_OPEN", (
        f"after cooldown check, state must be HALF_OPEN; got "
        f"{breaker_after.state!r}"
    )
    breaker_after.record_success()
    assert breaker_after.state == "CLOSED", (
        f"successful probe must transition HALF_OPEN -> CLOSED; got "
        f"{breaker_after.state!r}"
    )
    assert breaker_after.failure_count == 0, (
        f"failure_count must reset to 0 on CLOSED; got "
        f"{breaker_after.failure_count!r}"
    )


# ---------------------------------------------------------------------------
# Case 4 — AC-03-3: OPEN -> CLOSED recovery time <= TASKQ_BREAKER_COOLDOWN + 1s
# ---------------------------------------------------------------------------


# NFR-09 / NFR-06 (latency SLA on recovery)
def test_fr03_breaker_recovery_within_cooldown_bound(
    taskq_home, child_env, monkeypatch
):
    """AC-03-3: `OPEN -> CLOSED` recovery time ≤ `TASKQ_BREAKER_COOLDOWN + 1s`.
    *(NFR-03 atomicity clause: breaker OPEN → CLOSED 恢復時間 ≤
    TASKQ_BREAKER_COOLDOWN + 1s)*

    NFR-09 (test_assertion_quality): measures wall-clock time across
    the full OPEN -> HALF_OPEN -> CLOSED cycle to defend the timing
    bound. The test uses a 1-second cooldown so the bound is
    concrete (`max_recovery_s=2`).

    The test uses a real clock and a real sleep (no injection) — the
    timing assertion IS the spec, and an injected clock would
    invalidate the measurement.
    """
    # GREEN TODO: `taskq_plus.cli.commands.run` (or the breaker
    # integration it calls) must let the next `run` through once
    # the cooldown has elapsed, and the actual wall-clock time from
    # "run invoked with breaker OPEN" to "run returned with breaker
    # CLOSED" must be ≤ TASKQ_BREAKER_COOLDOWN + 1s. The
    # implementation must not busy-wait or otherwise exceed the
    # budget; a simple `time.sleep(cooldown_s)` is the canonical
    # implementation.
    from taskq_plus.service.breaker import Breaker
    from taskq_plus.storage.breaker_store import (
        make_breaker_store,
        reset_breaker_store_cache,
    )

    reset_breaker_store_cache()
    monkeypatch.setenv("TASKQ_BREAKER_COOLDOWN", "1")
    monkeypatch.setenv("TASKQ_BREAKER_THRESHOLD", "3")

    # 1. Drive the breaker to OPEN through the declared module.
    breaker = Breaker(threshold=3, cooldown_s=1.0)
    for _ in range(3):
        breaker.record_failure()
    assert breaker.state == "OPEN"

    # 2. Persist OPEN so the run path picks it up. Also record the
    #    `opened_at` so the wall-clock measurement is anchored to the
    #    moment recovery *starts*, not the moment the test began.
    store = make_breaker_store()
    store.save(breaker)

    # 3. Measure wall-clock time from "invoke a successful run" to
    #    "breaker is CLOSED". The implementation's check() must
    #    sleep the cooldown synchronously (default time.sleep) so
    #    the next run can complete.
    started = time.monotonic()
    breaker_now = store.load()
    breaker_now.check()  # OPEN -> HALF_OPEN
    breaker_now.record_success()  # HALF_OPEN -> CLOSED
    store.save(breaker_now)
    elapsed = time.monotonic() - started

    assert breaker_now.state == "CLOSED"
    max_recovery_s = 2  # TASKQ_BREAKER_COOLDOWN (1) + 1 s tolerance
    assert elapsed <= max_recovery_s, (
        f"OPEN -> CLOSED recovery must be <= {max_recovery_s}s "
        f"(TASKQ_BREAKER_COOLDOWN=1 + 1s tolerance); got {elapsed:.3f}s"
    )


# ---------------------------------------------------------------------------
# Case 5 — backoff formula: TASKQ_BACKOFF_BASE * 2^n before the n-th retry
# ---------------------------------------------------------------------------


# NFR-09 (test_assertion_quality)
def test_fr03_retry_backoff_sleeps_exponentially(taskq_home, monkeypatch):
    """FR-03 backoff formula: with `TASKQ_RETRY_LIMIT=2` and
    `TASKQ_BACKOFF_BASE=1`, the sleep sequence before each retry is
    `2, 4` (i.e. `base × 2^1` then `base × 2^2`). *(SPEC §3 FR-03)*

    NFR-09 (test_assertion_quality): asserts the precise sleep
    sequence; the backoff function is the unit under test.

    All retry attempts must fail (so the executor reaches the
    `retry_limit`) and the injected sleep function records every
    invocation without actually sleeping.
    """
    # GREEN TODO: `taskq_plus.service.executor.run_with_retry` must
    # compute `backoff_base * 2**n` before the n-th retry, where
    # `n` is 1-indexed (the first retry waits `base * 2**1`, the
    # second `base * 2**2`, etc.). The sleep function MUST be
    # injectable so the test can substitute a recording fake.
    from taskq_plus.models.task import Task
    from taskq_plus.service.executor import run_with_retry

    sleep_calls: list = []
    monkeypatch.setenv("TASKQ_RETRY_LIMIT", "2")
    monkeypatch.setenv("TASKQ_BACKOFF_BASE", "1")

    def recording_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    # All attempts fail (every command is `false`) so the retry path
    # is fully exercised — the executor must invoke the backoff
    # sleep twice (once before retry 1, once before retry 2) and
    # then surface the final `failed` status.
    result = run_with_retry(
        [Task(command="false"), Task(command="false"), Task(command="false")],
        timeout=10.0,
        sleep_fn=recording_sleep,
    )

    # `retry_limit=2` means 2 retries (3 total attempts). The
    # executor must sleep before EACH retry, so the sequence is
    # exactly [2.0, 4.0].
    assert sleep_calls == [2.0, 4.0], (
        f"backoff sequence must be [base*2**1, base*2**2] = [2.0, 4.0] "
        f"with base=1, retry_limit=2; got {sleep_calls!r}"
    )
    assert result.status == "failed", (
        f"all-failing attempts must end status=failed; got "
        f"{result.status!r}"
    )


# ===========================================================================
# In-process coverage tests
# ---------------------------------------------------------------------------
# The five cases above are the canonical TEST_SPEC.md §FR-03 rows. The
# tests below are additive: they exercise the same FR-03 surface
# (retry + breaker) through direct in-process calls so `coverage` can
# measure `taskq_plus.service.breaker` and `taskq_plus.service.executor`
# (the subprocess acceptance path in Case 2 cannot raise coverage on
# these — see GATE1 SUBPROCESS COVERAGE CEILING in the integration
# guidelines). Both modules are declared in `SAB.json`
# §fr_module_traceability for FR-03; their on-disk presence is enforced
# by the Architecture Amendment Protocol.
# ===========================================================================


# ---------------------------------------------------------------------------
# `taskq_plus.service.breaker` — the in-process breaker surface
# ---------------------------------------------------------------------------


# NFR-09
def test_fr03_breaker_module_is_importable():
    """The breaker module is declared in SAB.json §fr_module_traceability
    for FR-03. Its on-disk presence is enforced by the Architecture
    Amendment Protocol.

    GREEN TODO: `taskq_plus.service.breaker` must exist as a leaf
    module (or a package) and expose at least a `Breaker` class with
    `state` / `failure_count` / `record_failure` / `record_success`
    / `check` (the OPEN -> HALF_OPEN transition) methods.
    """
    import taskq_plus.service.breaker as breaker_module  # noqa: F401
    assert breaker_module is not None


# NFR-09
def test_fr03_breaker_record_failure_increments_count():
    """`Breaker.record_failure()` increments `failure_count` while
    state is CLOSED. State must transition to OPEN once `failure_count`
    reaches the configured threshold.

    GREEN TODO: the Breaker must compare `failure_count` against
    `threshold` after every `record_failure` and flip `state` to
    `OPEN` when the threshold is met (or exceeded). It must also
    record `opened_at = clock()` at the transition so the cooldown
    math has an anchor.
    """
    from taskq_plus.service.breaker import Breaker

    breaker = Breaker(threshold=3, cooldown_s=1.0)
    assert breaker.state == "CLOSED"
    assert breaker.failure_count == 0

    breaker.record_failure()
    assert breaker.failure_count == 1
    assert breaker.state == "CLOSED", (
        f"single failure must not open the breaker (threshold=3); "
        f"got {breaker.state!r}"
    )

    breaker.record_failure()
    breaker.record_failure()
    assert breaker.failure_count == 3
    assert breaker.state == "OPEN", (
        f"3 failures must drive the breaker to OPEN; got {breaker.state!r}"
    )


# NFR-09
def test_fr03_breaker_record_success_while_closed_keeps_count_zero():
    """`Breaker.record_success()` while CLOSED does not raise and
    leaves `failure_count` at 0 (a closed breaker has no failure
    memory to reset).

    GREEN TODO: the Breaker's CLOSED branch must be a no-op on
    `record_success` — failure_count is already 0; do not fabricate
    a negative counter.
    """
    from taskq_plus.service.breaker import Breaker

    breaker = Breaker(threshold=3, cooldown_s=1.0)
    breaker.record_success()
    assert breaker.failure_count == 0
    assert breaker.state == "CLOSED"


# NFR-09
def test_fr03_breaker_check_before_cooldown_stays_open():
    """`Breaker.check()` called BEFORE `cooldown_s` has elapsed must
    leave the breaker in OPEN — the cooldown is the gate; calling
    `check` early is not a way to bypass it.

    GREEN TODO: the Breaker must use the injected `clock` to compare
    `now - opened_at` against `cooldown_s`. If the cooldown has not
    elapsed, the state stays OPEN.
    """
    from taskq_plus.service.breaker import Breaker

    fake_now = [1_000.0]

    def clock() -> float:
        return fake_now[0]

    breaker = Breaker(threshold=2, cooldown_s=5.0, clock=clock)
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.state == "OPEN"

    # Advance the clock by 1s — well under the 5s cooldown.
    fake_now[0] += 1.0
    breaker.check()
    assert breaker.state == "OPEN", (
        f"check() before cooldown must leave state=OPEN; got {breaker.state!r}"
    )


# ---------------------------------------------------------------------------
# `taskq_plus.storage.breaker_store` — the persisted breaker surface
# ---------------------------------------------------------------------------


# NFR-09 / NFR-13 (SAD-forced concurrency on storage) / NFR-03
def test_fr03_breaker_store_module_is_importable():
    """The breaker_store module is declared in SAB.json
    §fr_module_traceability for FR-03. Its on-disk presence is
    enforced by the Architecture Amendment Protocol.

    GREEN TODO: `taskq_plus.storage.breaker_store` must exist as a
    leaf module (or a package) and expose at least a
    `make_breaker_store()` factory and a `BreakerStore` class with
    `load(*, clock=time.monotonic) -> Breaker` and `save(breaker)
    -> None` methods. Writes must be atomic (tmp + os.replace —
    NFR-03).
    """
    import taskq_plus.storage.breaker_store as breaker_store_module  # noqa: F401
    assert breaker_store_module is not None


# NFR-09
def test_fr03_breaker_store_persists_open_state(taskq_home, monkeypatch):
    """`BreakerStore.save(breaker)` writes `$TASKQ_HOME/breaker.json`
    with the current state; `store.load()` re-hydrates a Breaker
    with that state. Round-trip preserves state and failure_count.

    GREEN TODO: the breaker store must serialise `state`,
    `failure_count`, and `opened_at` to `$TASKQ_HOME/breaker.json`
    and re-hydrate them on `load()`. Writes must be atomic
    (NFR-03 — `tmp + os.replace`).
    """
    from taskq_plus.service.breaker import Breaker
    from taskq_plus.storage.breaker_store import (
        make_breaker_store,
        reset_breaker_store_cache,
    )

    reset_breaker_store_cache()
    monkeypatch.setenv("TASKQ_BREAKER_THRESHOLD", "3")

    source = Breaker(threshold=3, cooldown_s=1.0)
    source.record_failure()
    source.record_failure()
    source.record_failure()
    assert source.state == "OPEN"

    store = make_breaker_store()
    store.save(source)

    breaker_file = taskq_home / "breaker.json"
    assert breaker_file.exists(), (
        f"breaker.json must be written under $TASKQ_HOME; "
        f"expected {breaker_file}"
    )

    reset_breaker_store_cache()
    store2 = make_breaker_store()
    loaded = store2.load()
    assert loaded.state == "OPEN", (
        f"loaded breaker must be OPEN; got {loaded.state!r}"
    )
    assert loaded.failure_count == 3, (
        f"loaded failure_count must be 3; got {loaded.failure_count!r}"
    )


# NFR-09
def test_fr03_breaker_store_atomic_write_keeps_prior_json(
    taskq_home, monkeypatch
):
    """The breaker store's write is atomic — a mid-write kill (or a
    fresh `load` after a crash) must observe either the prior
    valid JSON or the new valid JSON, never a torn record.

    NFR-09 + NFR-03 (atomicity): the file on disk must always be
    parseable JSON (the same invariant the canonical
    `test_nfr03_breaker_file_survives_mid_write_kill` checks at the
    subprocess level; this in-process test is the coverage mirror).
    """
    from taskq_plus.service.breaker import Breaker
    from taskq_plus.storage.breaker_store import (
        make_breaker_store,
        reset_breaker_store_cache,
    )

    reset_breaker_store_cache()
    monkeypatch.setenv("TASKQ_BREAKER_THRESHOLD", "3")

    # First write — CLOSED.
    first = Breaker(threshold=3, cooldown_s=1.0)
    store = make_breaker_store()
    store.save(first)

    breaker_file = taskq_home / "breaker.json"
    first_payload = breaker_file.read_text(encoding="utf-8")
    first_data = _json.loads(first_payload)
    assert first_data["state"] == "CLOSED"

    # Second write — drive to OPEN, save, re-read. The file must
    # remain valid JSON after every save.
    second = Breaker(threshold=3, cooldown_s=1.0)
    second.record_failure()
    second.record_failure()
    second.record_failure()
    assert second.state == "OPEN"
    store.save(second)

    second_payload = breaker_file.read_text(encoding="utf-8")
    second_data = _json.loads(second_payload)  # must not raise
    assert second_data["state"] == "OPEN"


# ---------------------------------------------------------------------------
# `taskq_plus.service.executor.run_with_retry` — the in-process retry surface
# ---------------------------------------------------------------------------


# NFR-09 / NFR-15 (timeout, SAD-forced)
def test_fr03_executor_run_with_retry_module_function_exists():
    """`taskq_plus.service.executor.run_with_retry` is the
    in-process retry surface that wraps `run_task` with the
    TASKQ_RETRY_LIMIT / TASKQ_BACKOFF_BASE policy from FR-03.

    GREEN TODO: the executor module must expose
    `run_with_retry(commands, *, timeout, sleep_fn=time.sleep,
    retry_limit=None, backoff_base=None) -> TaskResult`. The
    function must default `retry_limit` and `backoff_base` from the
    matching `TASKQ_*` env vars when not provided.
    """
    from taskq_plus.service import executor

    assert hasattr(executor, "run_with_retry"), (
        "taskq_plus.service.executor must expose a run_with_retry "
        "function for FR-03 retry coverage"
    )
    assert callable(executor.run_with_retry)


# NFR-09
def test_fr03_executor_run_with_retry_no_retry_on_first_success(taskq_home, monkeypatch):
    """`run_with_retry` on a single command that succeeds on the
    first attempt must NOT invoke the sleep function — the backoff
    is only relevant between retries, not before the first attempt.

    GREEN TODO: the executor must skip the backoff sleep when the
    first attempt succeeds (`status in {"done"}`); the sleep
    belongs strictly between attempts, not before the first.
    """
    from taskq_plus.models.task import Task
    from taskq_plus.service.executor import run_with_retry

    sleep_calls: list = []

    def recording_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    monkeypatch.setenv("TASKQ_RETRY_LIMIT", "2")
    monkeypatch.setenv("TASKQ_BACKOFF_BASE", "1")

    result = run_with_retry(
        [Task(command="echo hi")],
        timeout=10.0,
        sleep_fn=recording_sleep,
    )

    assert result.status == "done"
    assert sleep_calls == [], (
        f"first-attempt success must not sleep before retrying; "
        f"got {sleep_calls!r}"
    )


# NFR-09 / NFR-15
def test_fr03_executor_run_with_retry_timeout_triggers_retry(taskq_home, monkeypatch):
    """A `timeout` status (e.g. `sleep 5` with 1s budget) is also a
    *final failure* for retry purposes — the executor must retry
    the same way it retries non-zero exits.

    GREEN TODO: the executor must treat `status == "timeout"` as a
    retryable outcome exactly like `status == "failed"`. The retry
    policy must be agnostic to the failure mode.
    """
    from taskq_plus.models.task import Task
    from taskq_plus.service.executor import run_with_retry

    sleep_calls: list = []

    def recording_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    monkeypatch.setenv("TASKQ_RETRY_LIMIT", "1")
    monkeypatch.setenv("TASKQ_BACKOFF_BASE", "0.1")

    result = run_with_retry(
        [Task(command="sleep 5"), Task(command="echo hi")],
        timeout=1.0,
        sleep_fn=recording_sleep,
    )

    assert result.status == "done", (
        f"timeout on first attempt must be retried; final status "
        f"must be done; got {result.status!r}"
    )
    assert len(sleep_calls) >= 1, (
        f"retry path after timeout must invoke sleep_fn; got {sleep_calls!r}"
    )


# ---------------------------------------------------------------------------
# `taskq_plus.cli.commands.run` — the in-process breaker-gate surface
# ---------------------------------------------------------------------------


# NFR-09
def test_fr03_inprocess_run_returns_exit_3_when_breaker_open(
    taskq_home, monkeypatch
):
    """`commands.run(["<id>"])` returns exit 3 and prints
    `breaker open` to stderr when the breaker is OPEN — no
    subprocess is launched.

    The in-process mirror of Case 2: drives 3 failures through the
    declared breaker, persists OPEN, then invokes `commands.run`
    on a fresh task and asserts the rejection surface.

    GREEN TODO: `taskq_plus.cli.commands.run` must consult the
    breaker (via `taskq_plus.service.breaker.Breaker.check()`)
    BEFORE dispatching to the executor. If the breaker is OPEN,
    the dispatcher must:
      - print `breaker open` to stderr
      - return 3
      - NOT call `executor.run_task`
    """
    from taskq_plus.cli import commands
    from taskq_plus.models.task import Task
    from taskq_plus.service.breaker import Breaker
    from taskq_plus.storage.breaker_store import (
        make_breaker_store,
        reset_breaker_store_cache,
    )
    from taskq_plus.storage.task_store import (
        get_store,
        make_disk_store,
        reset_store_cache,
    )

    reset_store_cache()
    reset_breaker_store_cache()
    monkeypatch.setenv("TASKQ_BREAKER_THRESHOLD", "3")
    monkeypatch.setenv("TASKQ_RETRY_LIMIT", "0")

    # Drive 3 failures through the breaker module.
    breaker = Breaker(threshold=3, cooldown_s=5.0)
    for _ in range(3):
        breaker.record_failure()
    assert breaker.state == "OPEN"

    bstore = make_breaker_store()
    bstore.save(breaker)

    # Submit a fresh pending task.
    dstore = make_disk_store()
    fresh = dstore.add(Task(command="echo hi"))
    fresh_id = fresh.id

    # Run in-process and assert the rejection surface.
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        exit_code = commands.run([fresh_id], use_disk=True)

    assert exit_code == 3, (
        f"in-process run must exit 3 when breaker is OPEN; got {exit_code}; "
        f"stderr={err.getvalue()!r}"
    )
    assert "breaker open" in err.getvalue(), (
        f"in-process stderr must contain 'breaker open'; got {err.getvalue()!r}"
    )

    # The fresh task's status must still be `pending` — proof that no
    # subprocess was launched.
    reset_store_cache()
    store = get_store(use_disk=True)
    reloaded = [t for t in store.load() if t.id == fresh_id][0]
    assert reloaded.status == "pending", (
        f"breaker-rejected run must leave task status=pending; "
        f"got {reloaded.status!r} (subprocess was launched)"
    )


# ---------------------------------------------------------------------------
# `taskq_plus.service.breaker` — branch coverage for Breaker.check()
# ---------------------------------------------------------------------------


# NFR-09
def test_fr03_breaker_check_when_state_not_open_is_noop():
    """`Breaker.check()` is a no-op when the breaker is not OPEN —
    the cooldown gate only applies to an OPEN breaker. A CLOSED
    breaker must stay CLOSED through any number of `check()` calls
    and never transition to HALF_OPEN.

    Covers `taskq_plus.service.breaker.Breaker.check` line 113
    (the early return when `self.state != STATE_OPEN`).
    """
    from taskq_plus.service.breaker import Breaker

    breaker = Breaker(threshold=3, cooldown_s=1.0)
    assert breaker.state == "CLOSED"

    # Multiple check() calls on a CLOSED breaker are all no-ops.
    for _ in range(3):
        breaker.check()
    assert breaker.state == "CLOSED", (
        f"check() on a CLOSED breaker must leave state=CLOSED; "
        f"got {breaker.state!r}"
    )


# NFR-09
def test_fr03_breaker_check_when_opened_at_is_none_is_noop():
    """`Breaker.check()` is a no-op when the breaker is OPEN but
    `opened_at` has not been anchored (e.g. a state hydrated directly
    from disk with `opened_at=null`). The cooldown gate cannot be
    computed without a timestamp, so the breaker must stay OPEN
    until `record_failure` re-anchors `opened_at`.

    Covers `taskq_plus.service.breaker.Breaker.check` line 115
    (the early return when `self.opened_at is None`).
    """
    from taskq_plus.service.breaker import Breaker, STATE_OPEN

    fake_now = [1_000.0]

    def clock() -> float:
        return fake_now[0]

    breaker = Breaker(threshold=3, cooldown_s=1.0, clock=clock)
    # Force the OPEN state without going through record_failure so
    # `opened_at` stays None.
    breaker.state = STATE_OPEN
    breaker.opened_at = None

    # Advance the clock well past the cooldown. Without an
    # `opened_at` anchor, the gate must NOT open.
    fake_now[0] += 100.0
    breaker.check()
    assert breaker.state == "OPEN", (
        f"check() with opened_at=None must leave state=OPEN; "
        f"got {breaker.state!r}"
    )


# ---------------------------------------------------------------------------
# `taskq_plus.service.executor` — branch coverage for the env-var parser
# ---------------------------------------------------------------------------


# NFR-09
def test_fr03_executor_env_value_returns_default_when_unset(monkeypatch):
    """`_env_value` returns the supplied default when the env var is
    unset or empty (the read is `os.environ.get(name, "")`, which
    yields the empty string for both `unsetenv` and `setenv("")`).

    Covers `taskq_plus.service.executor._env_value` line 64
    (the `return default` branch when `raw == ""`).
    """
    from taskq_plus.service.executor import _env_value

    # Make sure the variable is unset.
    monkeypatch.delenv("TASKQ_TEST_UNSET_VAR", raising=False)

    # The function must return the default for both the int and float
    # ctor paths because the empty-string branch is the first check.
    assert _env_value("TASKQ_TEST_UNSET_VAR", 99, int) == 99, (
        "_env_value with unset env must return the int default"
    )
    assert _env_value("TASKQ_TEST_UNSET_VAR", 7.5, float) == 7.5, (
        "_env_value with unset env must return the float default"
    )

    # And the same branch is hit when the variable is present-but-empty.
    monkeypatch.setenv("TASKQ_TEST_UNSET_VAR", "")
    assert _env_value("TASKQ_TEST_UNSET_VAR", 42, int) == 42, (
        "_env_value with empty env must return the default"
    )


# NFR-09
def test_fr03_executor_env_value_falls_back_on_invalid_value(monkeypatch):
    """`_env_value` returns the default when the env var is set to a
    value the typed ctor cannot parse (e.g. `int("abc")` raises
    `ValueError`; `_env_value` catches `ValueError` and `TypeError`).

    Covers `taskq_plus.service.executor._env_value` lines 67–68
    (the `except (ValueError, TypeError): return default` branch).
    """
    from taskq_plus.service.executor import _env_value

    # int() raises ValueError on non-numeric strings.
    monkeypatch.setenv("TASKQ_TEST_BAD_INT", "not-a-number")
    assert _env_value("TASKQ_TEST_BAD_INT", 99, int) == 99, (
        "_env_value with unparseable int env must return the default"
    )

    # float() raises ValueError on non-numeric strings.
    monkeypatch.setenv("TASKQ_TEST_BAD_FLOAT", "not-a-float")
    assert _env_value("TASKQ_TEST_BAD_FLOAT", 3.5, float) == 3.5, (
        "_env_value with unparseable float env must return the default"
    )


# NFR-09
def test_fr03_executor_tail_truncates_text_over_two_thousand_chars():
    """`_tail` returns the last `TAIL_CHARS` (2000) characters of an
    over-long string — the executor's stdout/stderr tail invariant
    from SPEC §3 FR-02 line 116.

    Covers `taskq_plus.service.executor._tail` line 87
    (the `return text[-TAIL_CHARS:]` slicing branch).
    """
    from taskq_plus.service.executor import _tail, TAIL_CHARS

    long_text = "x" * (TAIL_CHARS + 500)
    truncated = _tail(long_text)
    assert truncated is not None
    assert len(truncated) == TAIL_CHARS, (
        f"_tail must truncate to TAIL_CHARS={TAIL_CHARS}; got {len(truncated)}"
    )
    # The kept slice must be the LAST TAIL_CHARS of the input — the
    # tail invariant means "the most recent N chars", not "the first
    # N chars".
    assert truncated == long_text[-TAIL_CHARS:], (
        "_tail must keep the LAST TAIL_CHARS characters"
    )


# NFR-09
def test_fr03_executor_run_with_retry_rejects_empty_commands():
    """`run_with_retry(commands=[], ...)` raises `ValueError` because
    the function requires at least one command. An empty sequence has
    no first attempt to dispatch.

    Covers `taskq_plus.service.executor.run_with_retry` line 237
    (the `raise ValueError(...)` precondition check).
    """
    from taskq_plus.service.executor import run_with_retry

    sleep_calls: list = []

    def recording_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    raised = False
    try:
        run_with_retry([], timeout=1.0, sleep_fn=recording_sleep)
    except ValueError as exc:
        raised = True
        assert "at least one command" in str(exc), (
            f"ValueError message must mention 'at least one command'; "
            f"got {exc!r}"
        )
    assert raised, (
        "run_with_retry([]) must raise ValueError; the empty-commands "
        "precondition is part of the FR-03 retry contract"
    )
    # And the function must NOT have invoked the sleep function — the
    # precondition is checked before the loop, so no backoff is
    # recorded.
    assert sleep_calls == [], (
        f"precondition failure must not invoke sleep_fn; got {sleep_calls!r}"
    )


# ---------------------------------------------------------------------------
# `taskq_plus.storage.breaker_store` — branch coverage for BreakerStore.load
# ---------------------------------------------------------------------------


# NFR-09
def test_fr03_breaker_store_load_returns_fresh_breaker_when_file_missing(
    taskq_home, monkeypatch
):
    """`BreakerStore.load()` returns a fresh `Breaker` (CLOSED,
    count=0, opened_at=None) when the file does not exist on disk —
    the no-prior-state short-circuit. This is the first-run path
    the store must support.

    Covers `taskq_plus.storage.breaker_store.BreakerStore.load`
    line 83 (the early `return breaker` when the file is missing).
    """
    from taskq_plus.service.breaker import Breaker
    from taskq_plus.storage.breaker_store import (
        make_breaker_store,
        reset_breaker_store_cache,
    )

    reset_breaker_store_cache()

    # The file must NOT exist for the missing-file branch to fire.
    breaker_file = taskq_home / "breaker.json"
    assert not breaker_file.exists(), (
        f"precondition: breaker.json must not exist; found {breaker_file}"
    )

    store = make_breaker_store()
    loaded = store.load()
    assert isinstance(loaded, Breaker)
    assert loaded.state == "CLOSED", (
        f"missing-file load must yield CLOSED; got {loaded.state!r}"
    )
    assert loaded.failure_count == 0, (
        f"missing-file load must yield failure_count=0; got "
        f"{loaded.failure_count!r}"
    )
    assert loaded.opened_at is None, (
        f"missing-file load must yield opened_at=None; got "
        f"{loaded.opened_at!r}"
    )


# NFR-09
def test_fr03_breaker_store_load_sanitises_invalid_state_value(
    taskq_home, monkeypatch
):
    """`BreakerStore.load()` sanitises a persisted `state` value that
    is not one of CLOSED / OPEN / HALF_OPEN — it falls back to
    `STATE_CLOSED` rather than rehydrating an unknown state that
    the breaker logic does not understand. A corrupt or hand-edited
    `breaker.json` must NOT yield an unusable breaker.

    Covers `taskq_plus.storage.breaker_store.BreakerStore.load`
    line 90 (the `state = STATE_CLOSED` fallback when the persisted
    state is invalid).
    """
    from taskq_plus.storage.breaker_store import (
        make_breaker_store,
        reset_breaker_store_cache,
    )

    reset_breaker_store_cache()

    # Hand-write a breaker.json with a state value the breaker logic
    # does not recognise. The payload also includes a stale
    # failure_count to verify the sanitisation does NOT zero the
    # count — only the state is replaced.
    breaker_file = taskq_home / "breaker.json"
    breaker_file.write_text(
        _json.dumps(
            {
                "state": "BANANA",  # not CLOSED / OPEN / HALF_OPEN
                "failure_count": 5,
                "opened_at": None,
                "threshold": 3,
                "cooldown_s": 1.0,
            }
        ),
        encoding="utf-8",
    )

    store = make_breaker_store()
    loaded = store.load()
    assert loaded.state == "CLOSED", (
        f"invalid persisted state must be sanitised to CLOSED; "
        f"got {loaded.state!r}"
    )
    # The failure_count survives the state sanitisation — the
    # breaker still records it on the next record_failure.
    assert loaded.failure_count == 5, (
        f"failure_count must survive state sanitisation; got "
        f"{loaded.failure_count!r}"
    )
