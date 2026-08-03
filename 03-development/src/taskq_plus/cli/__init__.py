"""[FR-01] Command-line interface layer — L5 (top of the stack).

Citations:
    SPEC.md §6 lines 354-360 — `cli/` is L5; declared modules
        `taskq_plus.cli.main` and `taskq_plus.cli.commands`.
    SPEC.md §6 line 375 — layer rule.
"""
from __future__ import annotations

from taskq_plus.cli.commands import submit

__all__ = ["submit"]
