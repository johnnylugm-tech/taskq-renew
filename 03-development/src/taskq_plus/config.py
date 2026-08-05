"""[FR-01] `TASKQ_*` environment configuration.

Declared by SPEC.md §6 as an *independence* module: it imports nothing
from the layered packages, so any layer may read configuration without
violating the `.importlinter` contract.

Every accessor reads `os.environ` at **call** time rather than import
time — the test suite (and real users) rewrite `TASKQ_HOME` per
invocation, and an import-time snapshot would silently pin the first
value seen.

Citations:
    SPEC.md §3 FR-01 line 90 — 原子寫入 `$TASKQ_HOME/tasks.json`.
    SPEC.md §6 line 339 — `config.py` — TASKQ_* env 讀取 (independence 模組).
    SPEC.md §6 line 375 — layer rule; `config` sits outside the layers.
"""
from __future__ import annotations

import os
from pathlib import Path

# pragma: no error-handling

#: Fallback when `TASKQ_HOME` is unset.
DEFAULT_TASKQ_HOME = "~/.taskq"

#: Basename of the task store inside `$TASKQ_HOME` (SPEC.md §3 FR-01 line 90).
TASKS_FILENAME = "tasks.json"


def taskq_home() -> Path:
    """[FR-01] Return the resolved `$TASKQ_HOME` directory.

    Citations:
        SPEC.md §3 FR-01 line 90 — storage lives under `$TASKQ_HOME`.
    """
    return Path(os.environ.get("TASKQ_HOME", DEFAULT_TASKQ_HOME)).expanduser()


def tasks_path() -> Path:
    """[FR-01] Return the absolute path of `$TASKQ_HOME/tasks.json`.

    Citations:
        SPEC.md §3 FR-01 line 90 — 原子寫入 `$TASKQ_HOME/tasks.json`.
    """
    return taskq_home() / TASKS_FILENAME
