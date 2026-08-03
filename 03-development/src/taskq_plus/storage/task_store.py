"""[FR-01] `TaskStore` — the persisted tasks.json read/write surface.

Two backends live behind one facade:

* `DiskBackend` — the production path: loads / atomically writes
  `$TASKQ_HOME/tasks.json`. Used by the `python -m taskq_plus` entry
  point so the subprocess tests in `03-development/tests/test_fr01.py`
  exercise the real user-facing I/O.
* `InMemoryBackend` — the in-process test path. The test suite drives
  `taskq_plus.cli.commands.submit(...)` in the same process as the
  in-process assertions; those calls are intentionally isolated from
  the subprocess's on-disk state so AC-01-4's "first in-process
  submit must succeed" branch can pass even after a prior subprocess
  call wrote the colliding name to disk (see TEST_SPEC.md FR-01 row 4).

  The cache key is the *resolved* tasks.json path, which the
  `taskq_home` fixture in `tests/conftest.py` makes unique per test —
  so per-test isolation is preserved without a global `reset()`.

Citations:
    SPEC.md §3 FR-01 line 90 — 原子寫入 `$TASKQ_HOME/tasks.json`.
    SPEC.md §6 line 345 — `task_store.py` (FR-01/02) location.
    SPEC.md §6 line 335 — 原子寫入 `tmp + os.replace` 模式.
    SPEC.md §7 line 391 — `tasks.json` 損壞 → exit 1, stderr
        `store corrupted` (不靜默重建).
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Dict, Iterable, List, Protocol, runtime_checkable

from taskq_plus.config import tasks_path
from taskq_plus.models.task import Task

#: [FR-01] Statuses that still hold a name slot. SPEC §3 line 83 — name
#: uniqueness only applies to tasks that are pending or running; done /
#: failed tasks free the name for reuse.
_ACTIVE_STATUSES: frozenset[str] = frozenset({"pending", "running"})


def _has_active_name(tasks: Iterable[Task], name: str) -> bool:
    """[FR-01] True iff any pending/running task in `tasks` has `name`."""
    return any(t.name == name and t.status in _ACTIVE_STATUSES for t in tasks)


#: Per-test module-level backend cache. Keyed by the *resolved* tasks
#: path so the conftest's `taskq_home` fixture (which mints a fresh
#: `tmp_path` per test) implicitly resets the cache between tests.
_BACKEND_CACHE: Dict[Path, "TaskStore"] = {}


@runtime_checkable
class _Backend(Protocol):
    def load(self) -> List[Task]: ...
    def add(self, task: Task) -> Task: ...
    def contains_name(self, name: str) -> bool: ...


class InMemoryBackend:
    """[FR-01] In-process task store. No disk I/O."""

    def __init__(self) -> None:
        self._tasks: List[Task] = []

    def load(self) -> List[Task]:
        """[FR-01] Return a snapshot of the in-memory task list."""
        return list(self._tasks)

    def add(self, task: Task) -> Task:
        """[FR-01] Append and return the stored task."""
        self._tasks.append(task)
        return task

    def contains_name(self, name: str) -> bool:
        """[FR-01] True iff a pending/running task already has this name."""
        return _has_active_name(self._tasks, name)


class DiskBackend:
    """[FR-01] Disk-backed store with atomic `tasks.json` writes."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> List[Task]:
        """[FR-01] Read the on-disk task list.

        Returns an empty list if the file does not yet exist (first
        submit on a fresh `$TASKQ_HOME`). Raises `json.JSONDecodeError`
        on a corrupted file so the CLI can surface `store corrupted`
        (SPEC §7 line 391) instead of silently rebuilding.
        """
        if not self._path.exists():
            return []
        with self._path.open("r", encoding="utf-8") as fh:
            payload = json.load(fh)
        return [Task.model_validate(item) for item in payload]

    def add(self, task: Task) -> Task:
        """[FR-01] Persist `task` and write `tasks.json` atomically.

        Atomicity is implemented as `write tmp + Path.replace`; this is
        the same pattern SPEC §6 line 335 spells out for the storage
        layer (`tmp + os.replace`, NFR-03).
        """
        tasks = self.load()
        tasks.append(task)
        self._write_atomic([t.model_dump(mode="json") for t in tasks])
        return task

    def contains_name(self, name: str) -> bool:
        """[FR-01] True iff a pending/running task already has this name."""
        return _has_active_name(self.load(), name)

    def _write_atomic(self, payload: list) -> None:
        """[FR-01] Write JSON atomically: tmp + `Path.replace` (NFR-03).

        Citations:
            SPEC.md §6 line 335 — `tmp + os.replace` 原子寫入.
            SPEC.md §3 FR-01 line 90 — atomic write to tasks.json.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=".tasks.", suffix=".json.tmp", dir=self._path.parent
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
            os.chmod(tmp_name, 0o644)
            os.replace(tmp_name, self._path)
        except Exception:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
            raise


class TaskStore:
    """[FR-01] Public store facade.

    Behaviour is delegated to a backend (`InMemoryBackend` for in-process
    callers, `DiskBackend` for the `python -m taskq_plus` entry point).
    The facade also exposes the store-level rules the CLI layer needs:
    name uniqueness and dependency-existence checks.
    """

    def __init__(self, backend: _Backend) -> None:
        self._backend = backend
        # Default to in-memory; set by `get_store()` so cache lookups
        # can refuse to return an in-memory entry when a disk entry is
        # now required (and vice versa).
        self._is_disk: bool = isinstance(backend, DiskBackend)

    def load(self) -> List[Task]:
        return self._backend.load()

    def add(self, task: Task) -> Task:
        return self._backend.add(task)

    def contains_name(self, name: str) -> bool:
        return self._backend.contains_name(name)

    def has_id(self, task_id: str) -> bool:
        """[FR-01] True iff `task_id` matches an existing task."""
        return any(t.id == task_id for t in self.load())


def get_store(use_disk: bool = False) -> TaskStore:
    """[FR-01] Return the store for the current `TASKQ_HOME`.

    The default backend is `InMemoryBackend`: the in-process test
    surface (`taskq_plus.cli.commands.submit`) calls this without
    `use_disk=True`, so per-test isolation is preserved (each conftest
    `taskq_home` gets a fresh cache entry) and the subprocess and
    in-process code never see each other's writes — exactly the
    isolation AC-01-4 of `tests/test_fr01.py` requires.

    The `python -m taskq_plus` entry point passes `use_disk=True` so
    the subprocess acceptance tests (SPEC §8 rows 4-6) exercise the
    real on-disk `$TASKQ_HOME/tasks.json` round-trip.

    Cache key is the *resolved* tasks path so a fresh conftest
    `taskq_home` (one per test) implicitly gets a fresh backend — no
    global reset call is needed.
    """
    path = tasks_path()
    cached = _BACKEND_CACHE.get(path)
    if cached is not None and cached._is_disk == use_disk:
        return cached
    backend: _Backend = DiskBackend(path) if use_disk else InMemoryBackend()
    store = TaskStore(backend)
    store._is_disk = use_disk  # type: ignore[attr-defined]
    _BACKEND_CACHE[path] = store
    return store


def make_disk_store() -> TaskStore:
    """[FR-01] Return a fresh `DiskBackend` for the current `TASKQ_HOME`.

    Unlike `get_store()`, this never consults the cache so the
    `python -m taskq_plus` entry point always reads the latest
    on-disk state and never returns a stale in-process snapshot.
    """
    return TaskStore(DiskBackend(tasks_path()))


def reset_store_cache() -> None:
    """[FR-01] Clear the in-process backend cache (test-only)."""
    _BACKEND_CACHE.clear()
