"""[FR-01] Domain models — the L1 base of the layered architecture.

Citations:
    SPEC.md §6 lines 340-343 — `models/` is L1 (零內部依賴).
    SPEC.md §6 line 375 — layer rule: `cli > observability > service >
        storage > models`.
"""
from __future__ import annotations

from taskq_plus.models.task import INJECTION_CHARS, Task, TaskSubmission

__all__ = ["INJECTION_CHARS", "Task", "TaskSubmission"]
