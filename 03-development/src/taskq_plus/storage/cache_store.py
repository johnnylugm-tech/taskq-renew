"""[FR-04] Atomic persistent cache for completed task results.

Citations:
    SPEC.md §3 FR-04 — cache.json persistence and TTL replay.
    SPEC.md §4 NFR-03 — atomic writes and thread-safe storage.
"""
from __future__ import annotations

import json
import os  # noqa: F401  — re-exported so FR tests can monkeypatch
           # `cache_store.os.replace` to force a failing-replace and
           # verify the temp-file cleanup branch in `storage.atomic`.
import threading
from pathlib import Path

from taskq_plus.config import taskq_home
from taskq_plus.storage.atomic import atomic_write_json


#: [FR-04] Basename of the persisted cache file (SPEC §3 FR-04).
CACHE_FILENAME: str = "cache.json"


class CacheStore:
    """[FR-04] Read and atomically write the result cache.

    Citations:
        SPEC.md §3 FR-04 — `$TASKQ_HOME/cache.json` persistence.
        SPEC.md §4 NFR-03 — atomic write requirement.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path: Path = path or (taskq_home() / CACHE_FILENAME)
        # Per-instance lock; one cache store per `TASKQ_HOME` in
        # production keeps the same serialisation as a module lock
        # while letting the test suite instantiate independent stores
        # for different paths without sharing state.
        self._lock = threading.RLock()

    def load(self) -> dict:
        """[FR-04] Load entries, treating missing/corrupt cache as empty.

        Citations:
            SPEC.md §3 FR-04 — cache lookup dependency.
            NP-07 — corrupt cache must fall back to execution.
        """
        with self._lock:
            try:
                if not self.path.exists():
                    return {}
                with self.path.open("r", encoding="utf-8") as handle:
                    payload = json.load(handle)
                return payload if isinstance(payload, dict) else {}
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                return {}

    def save(self, entries: dict) -> None:
        """[FR-04] Atomically replace cache.json with `entries`.

        Citations:
            SPEC.md §4 NFR-03 — `tmp + os.replace` atomic persistence.
        """
        with self._lock:
            self._write_atomic(entries)

    def _write_atomic(self, payload: dict) -> None:
        """[FR-04] Write JSON atomically via `storage.atomic` (NFR-03).

        The actual `tmp + os.replace` is delegated to the storage hub
        (SAD §2.3.1) so all three stores share one implementation;
        this wrapper only pins the `.cache.` temp-file prefix so a
        leftover can be diagnosed as this store's, not task_store's.

        Citations:
            SPEC.md §4 NFR-03 — atomic write invariant.
        """
        atomic_write_json(self.path, payload, tmp_prefix=".cache.")


def make_cache_store() -> CacheStore:
    """[FR-04] Construct a cache store for the current `TASKQ_HOME`.

    Citations:
        SPEC.md §3 FR-04 — cache location under `TASKQ_HOME`.
    """
    return CacheStore()
