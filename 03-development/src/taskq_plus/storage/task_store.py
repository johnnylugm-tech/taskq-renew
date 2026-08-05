"""[FR-01/FR-02] `TaskStore` — the persisted tasks.json read/write surface.

Two backends live behind one facade:

* `DiskBackend` — the production path: loads / atomically writes
  `$TASKQ_HOME/tasks.json`. Used by the `python -m taskq_plus` entry
  point so the subprocess tests in `03-development/tests/test_fr01.py`
  exercise the real user-facing I/O.
* `InMemoryBackend` — the in-process test path. The test suite drives
  `taskq_plus.cli.commands.submit(...)` in the same process as the
  in-process assertions; those calls are intentionally isolated from
  the subprocess's on-disk state.

A shared `threading.Lock` (one per backend instance) guards every
multi-step read-modify-write against this process's own `--all` worker
threads; on disk an `flock` sidecar (`tasks.json.lock`) extends the
same guard across processes, so two parallel `run --all` invocations
cannot interleave a `load()` + `update()` and drop one another's
status update (SPEC §3 FR-02 "存儲寫入必須執行緒安全" + §4 NFR-03
atomicity).

Atomicity on disk is implemented as `tmp + os.replace` (SPEC §6
line 335) — a mid-write kill leaves the previous valid JSON intact.

Citations:
    SPEC.md §3 FR-01 line 90 — 原子寫入 `$TASKQ_HOME/tasks.json`.
    SPEC.md §6 line 345 — `task_store.py` (FR-01/02) location.
    SPEC.md §6 line 335 — 原子寫入 `tmp + os.replace` 模式.
    SPEC.md §7 line 391 — `tasks.json` 損壞 → exit 1, stderr
        `store corrupted` (不靜默重建).
    SPEC.md §3 FR-02 line 122 — `--all` thread-safe store writes.
    SPEC.md §4 NFR-03 — atomic write; mid-write kill leaves valid JSON.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import tempfile
import threading
from pathlib import Path
from typing import (
    Callable,
    Dict,
    Iterable,
    Iterator,
    List,
    Protocol,
    runtime_checkable,
)

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
    def update(self, task_id: str, mutator: Callable[[Task], Task]) -> Task: ...
    def all(self) -> List[Task]: ...


class InMemoryBackend:
    """[FR-01] In-process task store. No disk I/O."""

    def __init__(self) -> None:
        self._tasks: List[Task] = []
        self._lock = threading.Lock()

    def load(self) -> List[Task]:
        """[FR-01] Return a snapshot of the in-memory task list."""
        with self._lock:
            return list(self._tasks)

    def add(self, task: Task) -> Task:
        """[FR-01] Append and return the stored task."""
        with self._lock:
            self._tasks.append(task)
            return task

    def contains_name(self, name: str) -> bool:
        """[FR-01] True iff a pending/running task already has this name."""
        with self._lock:
            return _has_active_name(self._tasks, name)

    def all(self) -> List[Task]:
        """[FR-02] Snapshot of all tasks."""
        with self._lock:
            return list(self._tasks)

    def update(
        self, task_id: str, mutator: Callable[[Task], Task],
    ) -> Task:
        """[FR-02] Mutate the task with `task_id` in-place.

        `mutator` receives the current task and returns the updated
        task (or the same instance with field assignments). The lock
        is held for the duration of the read-modify-write so two
        concurrent `--all` workers cannot race.
        """
        with self._lock:
            for idx, existing in enumerate(self._tasks):
                if existing.id == task_id:
                    updated = mutator(existing)
                    # Pydantic v2 returns a new instance from
                    # `.model_copy(update=...)`; allow both that and
                    # an in-place mutation by reassigning the slot.
                    self._tasks[idx] = updated
                    return updated
        raise KeyError(f"task {task_id!r} not found")


class DiskBackend:
    """[FR-01/FR-02] Disk-backed store with atomic `tasks.json` writes."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()

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

    @contextlib.contextmanager
    def _exclusive(self) -> Iterator[None]:
        """[FR-02] Serialise read-modify-write across *processes*.

        `self._lock` only serialises threads inside one interpreter, so
        two parallel `run --all` invocations each take their own and
        neither waits: both `load()` the same snapshot and the later
        `_write_atomic` discards the other's status update. `os.replace`
        keeps each write atomic but cannot order two of them. An
        `flock` on a sidecar file is the one lock every process sharing
        this `$TASKQ_HOME` can contend for (SPEC §3 FR-02 line 122 +
        §4 NFR-03).
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self._path.with_name(self._path.name + ".lock")
        with lock_path.open("a+", encoding="utf-8") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    def add(self, task: Task) -> Task:
        """[FR-01] Persist `task` and write `tasks.json` atomically.

        Atomicity is implemented as `write tmp + Path.replace`; this is
        the same pattern SPEC §6 line 335 spells out for the storage
        layer (`tmp + os.replace`, NFR-03).
        """
        with self._lock, self._exclusive():
            tasks = self.load()
            tasks.append(task)
            self._write_atomic([t.model_dump(mode="json") for t in tasks])
            return task

    def contains_name(self, name: str) -> bool:
        """[FR-01] True iff a pending/running task already has this name."""
        with self._lock:
            return _has_active_name(self.load(), name)

    def all(self) -> List[Task]:
        """[FR-02] Snapshot of all tasks."""
        with self._lock:
            return self.load()

    def update(
        self, task_id: str, mutator: Callable[[Task], Task],
    ) -> Task:
        """[FR-02] Mutate the task with `task_id` and persist atomically.

        Holds both locks across read-modify-write: `self._lock` orders
        this process's `--all` worker threads, `self._exclusive()`
        orders the parallel `run --all` processes that share the same
        `$TASKQ_HOME` (SPEC §3 FR-02 thread-safety invariant + §4
        NFR-03 atomicity).
        """
        with self._lock, self._exclusive():
            tasks = self.load()
            for idx, existing in enumerate(tasks):
                if existing.id == task_id:
                    updated = mutator(existing)
                    tasks[idx] = updated
                    self._write_atomic(
                        [t.model_dump(mode="json") for t in tasks]
                    )
                    return updated
        raise KeyError(f"task {task_id!r} not found")

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
    """[FR-01/FR-02] Public store facade.

    Behaviour is delegated to a backend (`InMemoryBackend` for in-process
    callers, `DiskBackend` for the `python -m taskq_plus` entry point).
    The facade also exposes the store-level rules the CLI layer needs:
    name uniqueness, dependency-existence checks, and the threaded
    update primitive the executor dispatcher relies on.
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

    def all(self) -> List[Task]:
        """[FR-02] Return a snapshot of all tasks."""
        return self._backend.all()

    def find(self, task_id: str):
        """[FR-02] Return the task with `task_id`, or None if missing."""
        for t in self.load():
            if t.id == task_id:
                return t
        return None

    def update(
        self, task_id: str, mutator: Callable[[Task], Task],
    ) -> Task:
        """[FR-02] Mutate the task with `task_id` via `mutator` and persist."""
        return self._backend.update(task_id, mutator)


def get_store(use_disk: bool = False) -> TaskStore:
    """[FR-01] Return the store for the current `TASKQ_HOME`.

    The default backend is `InMemoryBackend`: the in-process test
    surface calls this without `use_disk=True`, so per-test isolation
    is preserved (each conftest `taskq_home` gets a fresh cache entry)
    and the subprocess and in-process code never see each other's
    writes.

    The `python -m taskq_plus` entry point passes `use_disk=True` so
    the subprocess acceptance tests exercise the real on-disk
    `$TASKQ_HOME/tasks.json` round-trip.

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
