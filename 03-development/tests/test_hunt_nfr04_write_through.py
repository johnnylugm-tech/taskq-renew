"""Hunt-RESOLVE repro tests for the two confirmed high-severity NFR-04 gaps.

These tests RED-fail on the bug (secret lands on disk unredacted),
turn GREEN once the fix is applied (secret replaced by `[REDACTED]`
in tasks.json / cache.json / audit.jsonl). Anti-fabrication gate per
hunt_bugs.md: each test must actually run the production write path
and inspect the on-disk file.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


SRC_ROOT = Path(__file__).resolve().parent.parent / "src"


def _seed_tasks_file(home: Path) -> None:
    """Write a single pending task to `$TASKQ_HOME/tasks.json`.

    Avoids running `submit` (which would itself need redaction-free paths).
    """
    (home / "tasks.json").write_text(
        json.dumps(
            [
                {
                    "id": "ab12cd34",
                    "status": "pending",
                    "command": "echo sk-SECRETABCDEFGH",
                    "name": None,
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "depends_on": [],
                    "exit_code": None,
                    "stdout_tail": None,
                    "stderr_tail": None,
                    "duration_ms": None,
                    "finished_at": None,
                    "cached": False,
                }
            ]
        ),
        encoding="utf-8",
    )


def _run_cli(home: Path, *args: str) -> subprocess.CompletedProcess:
    """Run `python -m taskq_plus <args>` with isolated `TASKQ_HOME`."""
    env = os.environ.copy()
    env["TASKQ_HOME"] = str(home)
    env["PYTHONPATH"] = str(SRC_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-m", "taskq_plus", *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


def test_hunt_nfr04_tasks_json_redacts_secret(
    tmp_path, monkeypatch
) -> None:
    """A secret in `stdout_tail` must NOT survive the `run` write path.

    Per SPEC §4 NFR-04 (line 211-214), `stdout_tail` / `stderr_tail`
    are redacted BEFORE the write to disk. The production path
    `commands._persist_result -> store.update -> atomic_write_json`
    currently passes the raw value through.

    The `command` field itself is NOT redacted (the SPEC names exactly
    three fields: stdout_tail / stderr_tail / audit `detail`) — only
    the captured output needs scrubbing. The assertion therefore
    inspects the `stdout_tail` / `stderr_tail` field of the stored
    record, not the whole file.
    """
    home = tmp_path / ".taskq"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("TASKQ_HOME", str(home))

    # Seed a pending task whose command echoes a secret, then run it.
    _seed_tasks_file(home)
    proc = _run_cli(home, "run", "ab12cd34")
    assert proc.returncode in (0, 1, 3), (
        f"unexpected exit; stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )

    tasks_file = home / "tasks.json"
    if not tasks_file.exists():
        pytest.skip("run did not execute; cannot exercise write path")
    payload = json.loads(tasks_file.read_text(encoding="utf-8"))
    record = next((t for t in payload if t.get("id") == "ab12cd34"), None)
    assert record is not None, "stored task missing"
    for field in ("stdout_tail", "stderr_tail"):
        value = record.get(field)
        if value is None:
            continue
        assert "sk-SECRETABCDEFGH" not in value, (
            f"{field} must be redacted before write to tasks.json; "
            f"got {value!r}"
        )


def test_hunt_nfr04_cache_json_redacts_secret(
    tmp_path, monkeypatch
) -> None:
    """A secret in `stdout_tail` must NOT survive the cache write path.

    `commands.run` lines 513-523 write `stdout_tail` straight into
    `cache.json` via `cache_record()` — no redaction. SPEC §4 NFR-04
    applies to all on-disk writes, not just the audit log. The
    assertion inspects the cache record's `stdout_tail` field, not
    the `command` field (the command is the cache key and stays raw).
    """
    home = tmp_path / ".taskq"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("TASKQ_HOME", str(home))

    # Submit a task whose output includes a clear OpenAI-style secret,
    # then run it through the CLI to populate the cache.
    proc_submit = _run_cli(home, "submit", "echo sk-CACHEABCDEFGH")
    assert proc_submit.returncode == 0, proc_submit.stderr
    task_id = proc_submit.stdout.strip()

    proc_run = _run_cli(home, "run", task_id)
    assert proc_run.returncode == 0, proc_run.stderr

    cache_file = home / "cache.json"
    if not cache_file.exists():
        pytest.skip("run did not populate the cache")
    payload = json.loads(cache_file.read_text(encoding="utf-8"))
    # Cache stores under sha256(command); pick the single entry.
    assert payload, "cache.json should have one entry"
    entry = next(iter(payload.values()))
    cached_stdout = entry.get("stdout_tail")
    if cached_stdout is None:
        pytest.skip("cache entry has no stdout_tail")
    assert "sk-CACHEABCDEFGH" not in cached_stdout, (
        f"cache.json stdout_tail must be redacted; got {cached_stdout!r}"
    )


def test_hunt_nfr04_audit_redacts_plugin_error_secret(
    tmp_path, monkeypatch
) -> None:
    """A plugin whose exception message contains a secret must be redacted.

    `plugins.append_audit_event` writes JSONL directly bypassing
    `audit._redact`. A plugin error message containing `sk-...` would
    land on disk verbatim, violating SPEC §4 NFR-04.
    """
    # Exercise the audit-append path directly through `plugins.append_audit_event`
    # to verify the production surface applies redaction.
    from taskq_plus.service import plugins

    home = tmp_path / ".taskq"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("TASKQ_HOME", str(home))

    plugins.append_audit_event(
        {
            "event": "plugin_error",
            "task_id": "ab12cd34",
            "correlation_id": "deadbeef",
            "detail": {
                "plugin": "evil",
                "hook": "pre_run",
                "consecutive_failures": 1,
                "error": "RuntimeError: token=PLUGINTOK12345",
            },
        }
    )

    audit_log = (home / "audit.jsonl").read_text(encoding="utf-8")
    assert "PLUGINTOK12345" not in audit_log, (
        f"plaintext secret in plugin audit event must be redacted; "
        f"got {audit_log!r}"
    )
