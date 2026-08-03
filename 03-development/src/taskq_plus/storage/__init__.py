"""[FR-01] Storage layer — L2 of the layered architecture.

Citations:
    SPEC.md §6 lines 344-349 — `storage/` is L2 (依賴 models).
    SPEC.md §6 line 375 — layer rule.
    SPEC.md §3 FR-01 line 90 — atomic write of `$TASKQ_HOME/tasks.json`.
"""
from __future__ import annotations

from taskq_plus.storage.task_store import (
    TaskStore,
    get_store,
    reset_store_cache,
)

__all__ = ["TaskStore", "get_store", "reset_store_cache"]
