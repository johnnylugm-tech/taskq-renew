"""[FR-03] `BreakerStore` — persisted breaker state at
`$TASKQ_HOME/breaker.json`.

The breaker state survives process restarts (and is shared across
parallel `python -m taskq_plus` invocations) because every transition
is written to disk atomically. Serialisation is a plain JSON object
with `state`, `failure_count`, and `opened_at` — the same shape the
test suite inspects verbatim.

Atomicity is implemented as `tmp + os.replace` (SPEC §4 NFR-03): a
mid-write kill leaves the prior valid JSON intact or the new valid
JSON — never a torn record.

Citations:
    SPEC.md §3 FR-03 — state persists at `$TASKQ_HOME/breaker.json`.
    SPEC.md §4 NFR-03 — atomic write; mid-write kill leaves valid JSON.
    SPEC.md §6 line 335 — `tmp + os.replace` 寫入模式.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Dict, Optional

# pragma: no error-handling

from taskq_plus.config import taskq_home
from taskq_plus.service.breaker import (
    STATE_CLOSED,
    Breaker,
    STATE_OPEN,
    STATE_HALF_OPEN,
)
from taskq_plus.storage.atomic import atomic_write_json


#: [FR-03] Basename of the persisted breaker file (SPEC §3 FR-03).
BREAKER_FILENAME: str = "breaker.json"


#: Per-test module-level store cache. Keyed by the *resolved* breaker
#: path so the conftest's `taskq_home` fixture (a fresh `tmp_path`
#: per test) implicitly resets the cache between tests.
_STORE_CACHE: Dict[Path, "BreakerStore"] = {}


def _breaker_path() -> Path:
    """[FR-03] Return the absolute path of `$TASKQ_HOME/breaker.json`."""
    return taskq_home() / BREAKER_FILENAME


class BreakerStore:
    """[FR-03] Persisted breaker state facade.

    `load()` rehydrates a `Breaker` from `$TASKQ_HOME/breaker.json`
    (defaults to a fresh `Breaker` when the file is missing). `save()`
    writes the current state atomically. The factory
    `make_breaker_store()` resolves the path from `$TASKQ_HOME` at
    *call* time so the test suite's `monkeypatch.setenv("TASKQ_HOME",
    ...)` flows through transparently.
    """

    def __init__(self, path: Path) -> None:
        self._path: Path = path

    def load(
        self, *, clock: Optional[Callable[[], float]] = None,
    ) -> Breaker:
        """[FR-03] Rehydrate the breaker from disk.

        Returns a fresh `Breaker` (CLOSED, count=0, opened_at=None)
        when the file does not exist. The optional `clock` is the
        monotonic anchor for any future `record_failure` calls; it
        defaults to `Breaker`'s built-in `time.monotonic` when None.
        """
        # Only pass `clock` when the caller provides one; otherwise
        # let `Breaker.__init__` keep its `time.monotonic` default.
        kwargs: dict = {}
        if clock is not None:
            kwargs["clock"] = clock
        breaker = Breaker(**kwargs)

        if not self._path.exists():
            return breaker

        with self._path.open("r", encoding="utf-8") as fh:
            payload = json.load(fh)

        state = payload.get("state", STATE_CLOSED)
        if state not in (STATE_CLOSED, STATE_OPEN, STATE_HALF_OPEN):
            state = STATE_CLOSED
        breaker.state = state
        breaker.failure_count = int(payload.get("failure_count", 0))
        opened_at = payload.get("opened_at", None)
        breaker.opened_at = (
            float(opened_at) if opened_at is not None else None
        )
        # [FR-03] Threshold and cooldown are persisted so a rehydrated
        # Breaker honours the values that were configured when the
        # state was last saved (NOT the current process defaults).
        if "threshold" in payload:
            breaker.threshold = int(payload["threshold"])
        if "cooldown_s" in payload:
            breaker.cooldown_s = float(payload["cooldown_s"])
        return breaker

    def save(self, breaker: Breaker) -> None:
        """[FR-03] Persist `breaker` atomically (`tmp + os.replace`).

        NFR-03 atomicity: a mid-write kill leaves the prior valid
        JSON or the new valid JSON on disk — never a torn record.
        """
        payload = {
            "state": breaker.state,
            "failure_count": int(breaker.failure_count),
            "opened_at": breaker.opened_at,
            "threshold": breaker.threshold,
            "cooldown_s": breaker.cooldown_s,
        }
        self._write_atomic(payload)

    def _write_atomic(self, payload: dict) -> None:
        """[FR-03] Write JSON atomically via `storage.atomic` (NFR-03).

        The actual `tmp + os.replace` pattern is delegated to the
        storage hub (SAD §2.3.1) so all three stores share one
        implementation; this wrapper only fixes the temp-file prefix
        so a leftover `.breaker.*.json.tmp` can be diagnosed as this
        store's, not task_store's.

        Citations:
            SPEC.md §6 line 335 — `tmp + os.replace` 原子寫入.
            SPEC.md §4 NFR-03 — atomic write invariant.
        """
        atomic_write_json(self._path, payload, tmp_prefix=".breaker.")


def make_breaker_store() -> BreakerStore:
    """[FR-03] Return the store for the current `TASKQ_HOME`.

    Cache key is the *resolved* breaker path so a fresh conftest
    `taskq_home` (one per test) implicitly gets a fresh backend — no
    global reset call is needed for normal tests. Use
    `reset_breaker_store_cache()` to force a fresh read.
    """
    path = _breaker_path()
    cached = _STORE_CACHE.get(path)
    if cached is not None:
        return cached
    store = BreakerStore(path)
    _STORE_CACHE[path] = store
    return store


def reset_breaker_store_cache() -> None:
    """[FR-03] Clear the in-process breaker-store cache (test-only)."""
    _STORE_CACHE.clear()
