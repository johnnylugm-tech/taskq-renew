"""[FR-04] TTL cache lookup service.

Citations:
    SPEC.md §3 FR-04 — sha256 signatures and TTL replay semantics.
    SPEC.md §4 NFR-03 — cache storage safety.
"""
from __future__ import annotations

import hashlib
import os
import time
from datetime import datetime
from typing import Optional

from taskq_plus.storage.cache_store import make_cache_store


def signature(command: str) -> str:
    """[FR-04] Return the stable SHA-256 key for a command.

    Citations:
        SPEC.md §3 FR-04 — cache signature is `sha256(command)`.
    """
    return hashlib.sha256(command.encode("utf-8")).hexdigest()


def _timestamp(value) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def cache_ttl() -> float:
    """[FR-04] Read TASKQ_CACHE_TTL at call time, defaulting to 60 seconds.

    Citations:
        SPEC.md §3 FR-04 — TTL is controlled by TASKQ_CACHE_TTL.
    """
    try:
        return float(os.environ.get("TASKQ_CACHE_TTL", "60"))
    except (TypeError, ValueError):
        return 60.0


def lookup(
    command: str, *, ttl_s: float, now: float | None = None
) -> Optional[dict]:
    """[FR-04] Return a recent completed entry, or ``None`` on a miss.

    Citations:
        SPEC.md §3 FR-04 — replay only recent `done` executions.
        NP-07 — unreadable cache is a normal miss.
    """
    current = time.time() if now is None else now
    entry = make_cache_store().load().get(signature(command))
    if not isinstance(entry, dict) or entry.get("status") != "done":
        return None
    finished = _timestamp(entry.get("finished_at"))
    if finished is None or current - finished > ttl_s:
        return None
    return entry


def record(command: str, entry: dict) -> None:
    """[FR-04] Store a completed command result under its signature.

    Citations:
        SPEC.md §3 FR-04 — completed results populate the cache.
    """
    store = make_cache_store()
    entries = store.load()
    entries[signature(command)] = entry
    store.save(entries)
