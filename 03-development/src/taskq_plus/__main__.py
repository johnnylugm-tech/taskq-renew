"""[FR-01] `python -m taskq_plus` entry point.

Citations:
    SPEC.md §6 line 338 — `__main__.py` is the declared `python -m
        taskq_plus` entry point.
    SPEC.md §8 lines 406-408 — acceptance rows invoke `python -m
        taskq_plus submit ...` literally, so this module must stay the
        user-facing entry point.
"""
from __future__ import annotations

from taskq_plus.cli.main import main

if __name__ == "__main__":
    raise SystemExit(main())
