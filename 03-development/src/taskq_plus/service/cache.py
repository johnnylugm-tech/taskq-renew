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

from taskq_plus.storage.cache_store import make_cache_store


#: [FR-04] Fallback TTL (seconds) when `TASKQ_CACHE_TTL` is unset.
DEFAULT_CACHE_TTL: float = 60.0


def signature(command: str) -> str:
    """[FR-04] Return the stable SHA-256 key for a command.

    Citations:
        SPEC.md §3 FR-04 — cache signature is `sha256(command)`.
    """
    return hashlib.sha256(command.encode("utf-8")).hexdigest()


def cache_ttl() -> float:
    """[FR-04] Read `TASKQ_CACHE_TTL` at call time, falling back to
    `DEFAULT_CACHE_TTL` (60 s).

    Citations:
        SPEC.md §3 FR-04 — TTL is controlled by `TASKQ_CACHE_TTL`.
    """
    raw = os.environ.get("TASKQ_CACHE_TTL", "")
    if raw == "":
        return DEFAULT_CACHE_TTL
    try:
        return float(raw)
    except ValueError:
        return DEFAULT_CACHE_TTL


def _parse_timestamp(value) -> float | None:
    """[FR-04] Coerce `value` to a POSIX timestamp, or `None` if it
    cannot be interpreted.

    Accepts numeric inputs as-is. Strings are parsed as ISO-8601
    datetimes (a trailing `Z` is rewritten to `+00:00` for
    `datetime.fromisoformat`). Any parse failure yields `None` so the
    caller can treat the entry as a cache miss.
    """
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def _is_fresh(entry: dict, now: float, ttl_s: float) -> bool:
    """[FR-04] True iff `entry` is a `done` record finished within
    `ttl_s` of `now`.

    Anything other than `status="done"` is treated as a miss; an
    unreadable `finished_at` is also a miss.
    """
    if entry.get("status") != "done":
        return False
    finished = _parse_timestamp(entry.get("finished_at"))
    if finished is None:
        return False
    return now - finished <= ttl_s


def lookup(
    command: str, *, ttl_s: float, now: float | None = None
) -> dict | None:
    """[FR-04] Return a recent completed entry, or `None` on a miss.

    Citations:
        SPEC.md §3 FR-04 — replay only recent `done` executions.
        NP-07 — unreadable cache is a normal miss.
    """
    current = time.time() if now is None else now
    entry = make_cache_store().load().get(signature(command))
    if not isinstance(entry, dict):
        return None
    return entry if _is_fresh(entry, current, ttl_s) else None


def record(command: str, entry: dict) -> None:
    """[FR-04] Store a completed command result under its signature.

    Citations:
        SPEC.md §3 FR-04 — completed results populate the cache.
    """
    store = make_cache_store()
    entries = store.load()
    entries[signature(command)] = entry
    store.save(entries)
