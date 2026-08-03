"""[FR-04] Atomic persistent cache for completed task results.

Citations:
    SPEC.md §3 FR-04 — cache.json persistence and TTL replay.
    SPEC.md §4 NFR-03 — atomic writes and thread-safe storage.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path

from taskq_plus.config import taskq_home


_LOCK = threading.RLock()


class CacheStore:
    """[FR-04] Read and atomically write the result cache.

    Citations:
        SPEC.md §3 FR-04 — `$TASKQ_HOME/cache.json` persistence.
        SPEC.md §4 NFR-03 — atomic write requirement.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (taskq_home() / "cache.json")

    def load(self) -> dict:
        """[FR-04] Load entries, treating missing/corrupt cache as empty.

        Citations:
            SPEC.md §3 FR-04 — cache lookup dependency.
            NP-07 — corrupt cache must fall back to execution.
        """
        with _LOCK:
            try:
                if not self.path.exists():
                    return {}
                with self.path.open("r", encoding="utf-8") as handle:
                    payload = json.load(handle)
                return payload if isinstance(payload, dict) else {}
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                return {}

    def save(self, entries: dict) -> None:
        """[FR-04] Atomically replace cache.json with ``entries``.

        Citations:
            SPEC.md §4 NFR-03 — `tmp + os.replace` atomic persistence.
        """
        with _LOCK:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(
                prefix=".cache.", suffix=".json.tmp", dir=self.path.parent
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(entries, handle, ensure_ascii=False, indent=2)
                os.chmod(temporary, 0o644)
                os.replace(temporary, self.path)
            except Exception:
                if os.path.exists(temporary):
                    os.unlink(temporary)
                raise


def make_cache_store() -> CacheStore:
    """[FR-04] Construct a cache store for the current TASKQ_HOME.

    Citations:
        SPEC.md §3 FR-04 — cache location under TASKQ_HOME.
    """
    return CacheStore()
