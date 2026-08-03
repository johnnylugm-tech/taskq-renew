"""Shared pytest fixtures for FR tests.

The `taskq` CLI writes data files to `$TASKQ_HOME`. Every test must isolate
its data files so per-test state cannot leak between cases
(state_mode="isolate_per_test" in TEST_SPEC.md).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest


SRC_ROOT = Path(__file__).resolve().parent.parent / "src"


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
    """
    env = os.environ.copy()
    env["TASKQ_HOME"] = str(taskq_home)
    src_root = str(SRC_ROOT)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = src_root + os.pathsep + existing if existing else src_root
    return env
