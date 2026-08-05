"""[NFR-01] Micro-benchmarks for Gate 3 `performance` dimension.

pytest-benchmark fixtures; suite runs with `pytest --benchmark-only --benchmark-disable-gc`
per evaluate_dimension.md (performance, Tier 3).

Targets: hot-path functions exercised on every CLI invocation. Each must
have a mean latency well under the 3000 ms penalty threshold so the
dimension scores 100; passing benchmarks also document the regression
ceiling for future contributors.
"""
from __future__ import annotations

import json
from pathlib import Path

from taskq_plus.models.task import Task
from taskq_plus.storage.task_store import InMemoryBackend, TaskStore
from taskq_plus.storage.atomic import atomic_write_json, atomic_append_jsonl


def _make_task(idx: int) -> Task:
    return Task(
        id=f"bench{idx:06d}",
        status="pending",
        command=f"echo hello-{idx}",
        name=f"bench-task-{idx}",
    )


def test_bench_task_store_add(benchmark) -> None:
    """Hot path: add a task to the in-memory store."""
    store = TaskStore(InMemoryBackend())
    counter = {"i": 0}

    def _add() -> None:
        counter["i"] += 1
        store.add(_make_task(counter["i"]))

    benchmark(_add)
    # Sanity: counter advanced
    assert counter["i"] >= 1


def test_bench_task_store_all(benchmark) -> None:
    """Hot path: list all tasks (used by every CLI `list` invocation)."""
    store = TaskStore(InMemoryBackend())
    for i in range(50):
        store.add(_make_task(i))

    def _list() -> None:
        store.all()

    benchmark(_list)
    # Sanity: list returned the seeded tasks
    assert len(store.all()) == 50


def test_bench_atomic_write_json(benchmark, tmp_path: Path) -> None:
    """Hot path: atomic JSON write — used by every persist call."""
    target = tmp_path / "bench.json"
    payload = {"tasks": [{"id": f"t-{i}", "status": "pending"} for i in range(20)]}

    def _write() -> None:
        atomic_write_json(target, payload, tmp_prefix=".bench.")

    benchmark(_write)
    assert target.exists()


def test_bench_atomic_append_jsonl(benchmark, tmp_path: Path) -> None:
    """Hot path: append JSONL record — used by audit pipeline."""
    target = tmp_path / "bench.jsonl"
    line = json.dumps({"kind": "x", "ts": 1.0, "payload": {"k": "v"}}) + "\n"

    def _append() -> None:
        atomic_append_jsonl(target, line)

    benchmark(_append)
    assert target.exists()
