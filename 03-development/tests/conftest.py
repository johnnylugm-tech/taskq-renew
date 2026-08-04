"""Shared pytest fixtures for FR tests.

The `taskq` CLI writes data files to `$TASKQ_HOME`. Every test must isolate
its data files so per-test state cannot leak between cases
(state_mode="isolate_per_test" in TEST_SPEC.md).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Iterator

import pytest


SRC_ROOT = Path(__file__).resolve().parent.parent / "src"


class _LiveEnv(dict):
    """Dict that re-reads `os.environ` on every `items()` iteration.

    The `child_env` fixture must surface env vars the test sets via
    `monkeypatch.setenv` *after* the fixture is resolved (e.g.
    `TASKQ_MAX_DAG_DEPTH` in the FR-06 depth-cap test). pytest resolves
    fixtures before the test body, so a plain `os.environ.copy()`
    snapshot would miss those later assignments.

    `subprocess.Popen._execute_child` reads `env.items()` at fork
    time, so a custom `items()` that re-reads `os.environ` is enough
    to propagate the latest values to the child process. The
    overrides dict carries the fixture-stable keys (`TASKQ_HOME`,
    `PYTHONPATH`) that must win over any process-wide value.
    """

    def __init__(self, overrides: dict) -> None:
        super().__init__()
        self._overrides: dict = dict(overrides)

    def items(self) -> Iterator[tuple]:
        env = os.environ.copy()
        for key, value in self._overrides.items():
            env[key] = value
        return iter(env.items())

    def __getitem__(self, key: str) -> str:
        if key in self._overrides:
            return self._overrides[key]
        return os.environ[key]

    def __contains__(self, key: object) -> bool:
        return key in self._overrides or key in os.environ

    def __iter__(self) -> Iterator[str]:
        seen: set = set()
        for key in self._overrides:
            yield key
            seen.add(key)
        for key in os.environ:
            if key not in seen:
                yield key
                seen.add(key)

    def __len__(self) -> int:
        return len(set(self._overrides) | set(os.environ.keys()))


@pytest.fixture(autouse=True)
def _ensure_src_on_path():
    """Make `taskq_plus` importable for in-process tests.

    pytest's `pythonpath` config does NOT propagate to subprocesses, so the
    subprocess tests in this directory build the child env explicitly.
    """
    src = str(SRC_ROOT)
    if src not in sys.path:
        sys.path.insert(0, src)
    yield


@pytest.fixture
def taskq_home(tmp_path, monkeypatch) -> Path:
    """Per-test `TASKQ_HOME` directory.

    Function-scoped: each test gets a fresh, empty data directory. This
    enforces `state_mode="isolate_per_test"` declared by every FR-01 case.
    """
    home = tmp_path / ".taskq"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("TASKQ_HOME", str(home))
    return home


@pytest.fixture
def child_env(taskq_home: Path) -> dict:
    """Environment for `subprocess.run` invocations of `python -m taskq_plus`.

    Propagates `TASKQ_HOME` and inserts `src/` into `PYTHONPATH` because
    pytest's `pythonpath` setting does NOT inherit into child processes.
    The dict is a `_LiveEnv` so env vars the test sets via
    `monkeypatch.setenv` AFTER this fixture is resolved still reach the
    subprocess (e.g. `TASKQ_MAX_DAG_DEPTH` in the FR-06 depth-cap test).
    """
    src_root = str(SRC_ROOT)
    existing = os.environ.get("PYTHONPATH", "")
    py_path = src_root + os.pathsep + existing if existing else src_root
    return _LiveEnv({
        "TASKQ_HOME": str(taskq_home),
        "PYTHONPATH": py_path,
    })
