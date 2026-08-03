"""[FR-02] Domain errors used by the execution layer.

Citations:
    SPEC.md §3 FR-02 lines 105-118 — state machine and timeout
        classification.
"""
from __future__ import annotations


class TaskExecutionError(Exception):
    """[FR-02] Raised when a task cannot be executed at all (lookup miss,
    structurally invalid input). Distinguishes runtime-level execution
    failures (which produce a `failed` TaskResult) from
    before-the-fact errors that should never produce a task record.
    """
