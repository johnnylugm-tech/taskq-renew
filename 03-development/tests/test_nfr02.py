"""NFR-02: 安全 — 七字元注入黑名單 + plugin allowlist 正則 + bandit gate.

Each test maps 1:1 to a row in TEST_SPEC.md §NFR-02 (rows 1–10). Names are
canonical — `spec-coverage-check` keys on them — do NOT rename.

Coverage:
  Row 1  forbidden_execution_patterns_absent  — static grep gate
  Row 2  injection_semicolon_rejected          — unit (TaskSubmission)
  Row 3  injection_pipe_rejected               — unit (TaskSubmission)
  Row 4  injection_ampersand_rejected          — unit (TaskSubmission)
  Row 5  injection_dollar_rejected             — unit (TaskSubmission)
  Row 6  injection_greater_than_rejected       — unit (TaskSubmission)
  Row 7  injection_less_than_rejected          — unit (TaskSubmission)
  Row 8  injection_backtick_rejected           — unit (TaskSubmission)
  Row 9  bandit_has_no_high_or_medium_findings — static (bandit)
  Row 10 plugin_allowlist_rejects_path_module  — unit (PLUGIN_NAME_RE)
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


SRC_ROOT = Path(__file__).resolve().parent.parent / "src"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def project_root() -> Path:
    """Repo root (three levels above `tests/`)."""
    return Path(__file__).resolve().parent.parent.parent


# ---------------------------------------------------------------------------
# NFR-02 — injection blacklist (unit, rows 2–8)
#
# `ast-assertions` is a per-FunctionDef AST scan — helper calls do NOT count.
# Each test therefore inlines the `pytest.raises(...)` block directly so the
# scanner sees the assertion statement in the test's own body.
# ---------------------------------------------------------------------------


def test_nfr02_injection_semicolon_rejected() -> None:
    """A command containing `;` is rejected by `_validate_command`."""
    from taskq_plus.models.task import TaskSubmission
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as excinfo:
        TaskSubmission(command="echo ; bad")
    assert "injection" in str(excinfo.value), (
        f"rejection message must mention `injection`; got {excinfo.value!r}"
    )


def test_nfr02_injection_pipe_rejected() -> None:
    """A command containing `|` is rejected by `_validate_command`."""
    from taskq_plus.models.task import TaskSubmission
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as excinfo:
        TaskSubmission(command="echo | bad")
    assert "injection" in str(excinfo.value), (
        f"rejection message must mention `injection`; got {excinfo.value!r}"
    )


def test_nfr02_injection_ampersand_rejected() -> None:
    """A command containing `&` is rejected by `_validate_command`."""
    from taskq_plus.models.task import TaskSubmission
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as excinfo:
        TaskSubmission(command="echo & bad")
    assert "injection" in str(excinfo.value), (
        f"rejection message must mention `injection`; got {excinfo.value!r}"
    )


def test_nfr02_injection_dollar_rejected() -> None:
    """A command containing `$` is rejected by `_validate_command`."""
    from taskq_plus.models.task import TaskSubmission
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as excinfo:
        TaskSubmission(command="echo $ bad")
    assert "injection" in str(excinfo.value), (
        f"rejection message must mention `injection`; got {excinfo.value!r}"
    )


def test_nfr02_injection_greater_than_rejected() -> None:
    """A command containing `>` is rejected by `_validate_command`."""
    from taskq_plus.models.task import TaskSubmission
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as excinfo:
        TaskSubmission(command="echo > bad")
    assert "injection" in str(excinfo.value), (
        f"rejection message must mention `injection`; got {excinfo.value!r}"
    )


def test_nfr02_injection_less_than_rejected() -> None:
    """A command containing `<` is rejected by `_validate_command`."""
    from taskq_plus.models.task import TaskSubmission
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as excinfo:
        TaskSubmission(command="echo < bad")
    assert "injection" in str(excinfo.value), (
        f"rejection message must mention `injection`; got {excinfo.value!r}"
    )


def test_nfr02_injection_backtick_rejected() -> None:
    """A command containing `` ` `` is rejected by `_validate_command`."""
    from taskq_plus.models.task import TaskSubmission
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as excinfo:
        TaskSubmission(command="echo ` bad")
    assert "injection" in str(excinfo.value), (
        f"rejection message must mention `injection`; got {excinfo.value!r}"
    )


# ---------------------------------------------------------------------------
# NFR-02 — static gates (rows 1, 9)
# ---------------------------------------------------------------------------


def test_nfr02_forbidden_execution_patterns_absent(project_root: Path) -> None:
    """Source tree contains zero `shell=True` / `eval(` / `exec(` hits.

    NFR-02 / SPEC §4 forbids shell-injection-prone calls. A static
    grep gate is the cheapest invariant check — fail closed if a
    future FR reintroduces any of these patterns.
    """
    proc = subprocess.run(
        ["grep", "-rn", "--", "shell=True", str(SRC_ROOT)],
        capture_output=True, text=True,
    )
    # grep returns 0 (hits found) or 1 (no hits); 2 means a usage error.
    assert proc.returncode in (0, 1), (
        f"grep must return 0 (hits) or 1 (no hits); got {proc.returncode}; "
        f"stderr={proc.stderr!r}"
    )
    assert proc.returncode == 1, (
        f"`shell=True` must be absent from {SRC_ROOT}; "
        f"hits: {proc.stdout!r}"
    )

    for needle in ("eval(", "exec("):
        proc = subprocess.run(
            ["grep", "-rn", "--", needle, str(SRC_ROOT)],
            capture_output=True, text=True,
        )
        assert proc.returncode in (0, 1), (
            f"grep({needle!r}) must return 0 or 1; got {proc.returncode}"
        )
        assert proc.returncode == 1, (
            f"`{needle}` must be absent from {SRC_ROOT}; "
            f"hits: {proc.stdout!r}"
        )


def test_nfr02_bandit_has_no_high_or_medium_findings(project_root: Path) -> None:
    """bandit reports 0 HIGH / 0 MEDIUM findings on the source tree.

    Scoped at `03-development/src/` so the gate measures the product
    code (not tests, which bandit treats differently and which would
    pollute the result).
    """
    proc = subprocess.run(
        [
            sys.executable, "-m", "bandit",
            "-r", str(SRC_ROOT),
            "-f", "json",
            "--exit-zero",
        ],
        capture_output=True, text=True,
    )
    assert proc.returncode in (0, 1), (
        f"bandit must exit 0 or 1; got {proc.returncode}; "
        f"stderr={proc.stderr!r}"
    )
    import json as _json
    payload = _json.loads(proc.stdout)
    metrics = payload.get("metrics", {}).get("_totals", {})
    high = int(metrics.get("SEVERITY.HIGH", 0) or 0)
    medium = int(metrics.get("SEVERITY.MEDIUM", 0) or 0)
    assert high == 0, (
        f"bandit reported {high} HIGH finding(s); expected 0"
    )
    assert medium == 0, (
        f"bandit reported {medium} MEDIUM finding(s); expected 0"
    )


# ---------------------------------------------------------------------------
# NFR-02 — plugin allowlist regex (row 10)
# ---------------------------------------------------------------------------


def test_nfr02_plugin_allowlist_rejects_path_module() -> None:
    """`PLUGIN_NAME_RE` rejects `os/path`-style dotted paths.

    The allowlist (SPEC §3 FR-07 line 157) requires a Python module
    name: a single leading identifier character followed by
    identifier / digit / underscore / dot. Path segments with a
    leading slash never match — the regex anchors with `^[A-Za-z_]`.
    """
    from taskq_plus.service.plugins import PLUGIN_NAME_RE

    for bad in ("os/path", "/abs/path", "../relative", "foo/bar"):
        assert not PLUGIN_NAME_RE.match(bad), (
            f"`PLUGIN_NAME_RE` must reject {bad!r}; matched"
        )

    # And the canonical happy path still matches.
    assert PLUGIN_NAME_RE.match("taskq_test_plugins.happy")
    assert PLUGIN_NAME_RE.match("a")
    assert PLUGIN_NAME_RE.match("_leading_underscore")
