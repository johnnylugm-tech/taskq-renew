"""NFR-04: 敏感資料遮蔽 — redaction regex applied to audit/task surfaces.

Each test maps 1:1 to a row in TEST_SPEC.md §NFR-04 (rows 11–14).
Names are canonical — do NOT rename.
"""
from __future__ import annotations

from pathlib import Path


SRC_ROOT = Path(__file__).resolve().parent.parent / "src"
TASKQ_HOME = Path("/tmp")


def _scratch_home(tmp_path: Path, monkeypatch) -> Path:
    """Per-test `$TASKQ_HOME` directory."""
    home = tmp_path / ".taskq"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("TASKQ_HOME", str(home))
    return home


def _scratch_env(tmp_path: Path, monkeypatch) -> dict:
    """Subprocess env pointing at the scratch `TASKQ_HOME`."""
    env_path = str(SRC_ROOT)
    existing = __import__("os").environ.get("PYTHONPATH", "")
    py_path = env_path + __import__("os").sep + existing if existing else env_path
    monkeypatch.setenv("TASKQ_HOME", str(tmp_path / ".taskq"))
    return {
        "TASKQ_HOME": str(tmp_path / ".taskq"),
        "PYTHONPATH": py_path,
    }


# ---------------------------------------------------------------------------
# NFR-04 — token / Bearer redaction (unit, row 11 + 13)
# ---------------------------------------------------------------------------


def test_nfr04_token_secret_is_redacted() -> None:
    """`audit._redact` replaces `token=...` substrings with `[REDACTED]`."""
    from taskq_plus.observability.audit import _redact

    out = _redact("authorization: token=ABCD1234EFGH")
    assert "[REDACTED]" in out, (
        f"token= must be redacted; got {out!r}"
    )
    assert "ABCD1234EFGH" not in out, (
        f"token value must NOT survive redaction; got {out!r}"
    )


def test_nfr04_bearer_secret_is_redacted() -> None:
    """`audit._redact` replaces `Bearer ...` substrings with `[REDACTED]`."""
    from taskq_plus.observability.audit import _redact

    out = _redact("authorization: Bearer eyJabc.def_GHI")
    assert "[REDACTED]" in out, (
        f"Bearer must be redacted; got {out!r}"
    )
    assert "eyJabc.def_GHI" not in out, (
        f"Bearer payload must NOT survive redaction; got {out!r}"
    )


# ---------------------------------------------------------------------------
# NFR-04 — task-result / audit-log surfaces (integration, rows 12 + 14)
# ---------------------------------------------------------------------------


def test_nfr04_secret_is_redacted_from_task_results(
    tmp_path, monkeypatch
) -> None:
    """A `sk-...` secret in the task record is rewritten to `[REDACTED]`."""
    from taskq_plus.observability.audit import _redact

    record = {
        "command": "echo sk-ABCD1234EFGH",
        "stdout_tail": "sk-ABCD1234EFGH echoed",
        "stderr_tail": "",
        "status": "done",
    }
    redacted = _redact(record)
    assert redacted["stdout_tail"] == "[REDACTED] echoed", (
        f"task stdout_tail must be redacted; got {redacted!r}"
    )


def test_nfr04_secret_is_redacted_from_audit_log(tmp_path, monkeypatch) -> None:
    """`append_event` writes a redacted audit JSONL — no plaintext secret."""
    from taskq_plus.observability import audit

    home = _scratch_home(tmp_path, monkeypatch)
    monkeypatch.setattr(audit, "_audit_path", lambda: home / "audit.jsonl")

    audit.append_event(
        "test_event",
        detail={"secret": "sk-ABCD1234EFGH", "ok": True},
    )
    audit_log = (home / "audit.jsonl").read_text(encoding="utf-8")
    assert "sk-ABCD1234EFGH" not in audit_log, (
        f"plaintext secret must NOT appear in audit log; got {audit_log!r}"
    )
    assert "[REDACTED]" in audit_log, (
        f"redaction marker must appear in audit log; got {audit_log!r}"
    )
