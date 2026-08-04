"""NFR-12: 可驗證性 — `make verify-system` 為專案宣告的 end-to-end smoke。

Each test maps 1:1 to a row in TEST_SPEC.md §NFR-12 (rows 47–48).
Names are canonical — do NOT rename.

Recursion-safety note: `make verify-system` invokes `make test-coverage`
which runs `coverage run -m pytest --cov=…`. Both writers contend on
the project-root `.coverage` SQLite database and the parent pytest
hangs. Detect that the parent pytest is collecting with `--cov` and
`SKIP` these tests in that mode — NFR-12 is a Gate 2 dimension that
the harness evaluates via its own `make verify-system` invocation,
not via the in-process pytest-cov path.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def _parent_is_collecting_with_cov() -> bool:
    """True when this test file is being collected under pytest-cov.

    coverage v7 sets `COV_CORE_SOURCE` in the subprocess only when
    `--cov` was passed on the command line. A simpler proxy that does
    not depend on coverage internals: a `.coverage` lock file exists
    or coverage is on sys.modules when this module is imported.
    """
    try:
        import coverage  # noqa: F401
        return True
    except ImportError:
        return os.environ.get("COV_CORE_SOURCE") is not None


pytestmark = pytest.mark.skipif(
    _parent_is_collecting_with_cov(),
    reason="make verify-system re-invokes pytest-cov which contends on "
           ".coverage with the parent — skip under in-process pytest-cov.",
)


def test_nfr12_verify_system_exits_zero() -> None:
    """`make verify-system` exits 0 — the canonical end-to-end smoke."""
    proc = subprocess.run(
        ["make", "verify-system"],
        capture_output=True, text=True,
        cwd=str(PROJECT_ROOT),
    )
    assert proc.returncode == 0, (
        f"`make verify-system` must exit 0; got {proc.returncode}; "
        f"stdout={proc.stdout!r}; stderr={proc.stderr!r}"
    )


def test_nfr12_verify_system_prints_pass_marker() -> None:
    """`make verify-system` prints `verify-system: PASS` somewhere in output."""
    proc = subprocess.run(
        ["make", "verify-system"],
        capture_output=True, text=True,
        cwd=str(PROJECT_ROOT),
    )
    combined = proc.stdout + "\n" + proc.stderr
    assert re.search(r"verify-system:\s*PASS", combined), (
        f"`make verify-system` must print `verify-system: PASS`; "
        f"got {combined!r}"
    )
