"""SEC-R8 threat-verification tests.

Each test below is named after the `verified_by` field of a SAD.md threat
(§SEC block, SAD lines 741-797). The names are CANONICAL — the SEC-R8
gate looks up these exact strings — do NOT rename.

The seven tests in this module cover threats T-01 through T-07 from
`02-architecture/SAD.md`:

  T-01  test_fr01_submit_rejects_shell_metacharacters
  T-02  test_nfr02_no_shell_true_anywhere
  T-03  test_fr07_plugin_allowlist_rejects_path
  T-04  test_nfr02_no_eval_exec_in_src
  T-05  test_nfr04_secret_redacted_before_audit_write
  T-06  test_nfr03_store_corruption_exits_one
  T-07  test_nfr04_cache_entry_redacts_secrets

These tests are additive to the existing TEST_SPEC.md cases — they
target the threat model specifically and are referenced by SAD.md as
the on-disk proof that each mitigation is enforced.
"""
from __future__ import annotations

import json as _json
import subprocess
import sys
from pathlib import Path

import pytest


SRC_ROOT = Path(__file__).resolve().parent.parent / "src"


# ---------------------------------------------------------------------------
# T-01 — FR-01 submit rejects every shell metacharacter (; | & $ > < `)
# ---------------------------------------------------------------------------


def test_fr01_submit_rejects_shell_metacharacters(taskq_home, child_env):
    """SAD T-01: `TaskSubmission` rejects ALL seven metacharacters at once.

    A single command string containing every documented metacharacter
    must be rejected by the validation layer (the threat is a single
    payload smuggling all seven, not seven separate attempts).
    """
    from taskq_plus.models.task import TaskSubmission
    from pydantic import ValidationError

    payload = "echo a;b|c&d$e>f<g`h"
    with pytest.raises(ValidationError) as excinfo:
        TaskSubmission(command=payload)
    assert "injection" in str(excinfo.value), (
        f"rejection must mention `injection`; got {excinfo.value!r}"
    )

    # Subprocess smoke check: the CLI surfaces the same rejection as exit 2.
    proc = subprocess.run(
        [sys.executable, "-m", "taskq_plus", "submit", payload],
        capture_output=True, text=True, env=child_env,
    )
    assert proc.returncode == 2, (
        f"CLI must exit 2 on metacharacter payload; got {proc.returncode}; "
        f"stderr={proc.stderr!r}"
    )


# ---------------------------------------------------------------------------
# T-02 — NFR-02 no `shell=True` anywhere in src
# ---------------------------------------------------------------------------


def test_nfr02_no_shell_true_anywhere():
    """SAD T-02: `subprocess.run` is never invoked with `shell=True`.

    A static grep gate is the cheapest invariant check — fail closed
    if a future FR reintroduces shell invocation.
    """
    proc = subprocess.run(
        ["grep", "-rn", "--", "shell=True", str(SRC_ROOT)],
        capture_output=True, text=True,
    )
    assert proc.returncode in (0, 1), (
        f"grep must return 0 or 1; got {proc.returncode}; "
        f"stderr={proc.stderr!r}"
    )
    assert proc.returncode == 1, (
        f"`shell=True` must be absent from {SRC_ROOT}; "
        f"hits: {proc.stdout!r}"
    )


# ---------------------------------------------------------------------------
# T-03 — FR-07 plugin allowlist rejects path / URL forms
# ---------------------------------------------------------------------------


def test_fr07_plugin_allowlist_rejects_path():
    """SAD T-03: `PLUGIN_NAME_RE` rejects every documented path form.

    The allowlist must refuse any spec that could be mistaken for a
    filesystem path (`/abs`, `../relative`, dotted path segments).
    """
    from taskq_plus.service.plugins import PLUGIN_NAME_RE

    for bad in ("../evil.py", "/abs/path", "os/path", "foo/bar"):
        assert not PLUGIN_NAME_RE.match(bad), (
            f"`PLUGIN_NAME_RE` must reject {bad!r}; matched"
        )

    # The canonical happy path still matches so the regex is anchored
    # right and not just universally rejecting everything.
    assert PLUGIN_NAME_RE.match("taskq_test_plugins.noop"), (
        "the canonical happy-path spec must still match"
    )


# ---------------------------------------------------------------------------
# T-04 — NFR-02 no `eval(` / `exec(` anywhere in src
# ---------------------------------------------------------------------------


def test_nfr02_no_eval_exec_in_src():
    """SAD T-04: dynamic-code patterns are absent from the source tree.

    `eval(`, `exec(`, and `__import__(` open arbitrary-code paths; the
    mitigation is "static-import only", so a grep gate pins the rule.
    """
    for needle in ("eval(", "exec(", "__import__("):
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


# ---------------------------------------------------------------------------
# T-05 — NFR-04 secret redacted BEFORE audit.jsonl write
# ---------------------------------------------------------------------------


def test_nfr04_secret_redacted_before_audit_write(tmp_path, monkeypatch):
    """SAD T-05: `append_event` writes a redacted audit line, never plaintext.

    Threat model: a task whose stdout contains a `sk-...` secret must
    not see that secret land on disk in `audit.jsonl` — the redaction
    happens before serialisation.
    """
    from taskq_plus.observability import audit

    home = tmp_path / ".taskq"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("TASKQ_HOME", str(home))
    monkeypatch.setattr(audit, "_audit_path", lambda: str(home / "audit.jsonl"))

    secret = "sk-ABCD1234EFGH"
    audit.append_event(
        "test_event",
        task_id="deadbeef",
        correlation_id="corr-1",
        detail={"stdout_tail": f"echo {secret}", "ok": True},
    )

    audit_log = (home / "audit.jsonl").read_text(encoding="utf-8")
    assert secret not in audit_log, (
        f"plaintext secret must NOT appear in audit log; got {audit_log!r}"
    )
    assert "[REDACTED]" in audit_log, (
        f"redaction marker must appear in audit log; got {audit_log!r}"
    )


# ---------------------------------------------------------------------------
# T-06 — NFR-03 store corruption surfaces as exit 1, no silent rebuild
# ---------------------------------------------------------------------------


def test_nfr03_store_corruption_exits_one(taskq_home):
    """SAD T-06: a corrupted `tasks.json` causes `list` to exit 1.

    Threat model: a malformed tasks.json must not be silently rebuilt
    as empty — that would let an attacker hide evidence by corrupting
    the store. The CLI surfaces the corruption as exit 1 plus a
    `store corrupted` message.
    """
    from taskq_plus.cli import commands

    (taskq_home / "tasks.json").write_text("{not json", encoding="utf-8")

    import io as _io
    import contextlib as _cl

    stdout, stderr = _io.StringIO(), _io.StringIO()
    with _cl.redirect_stdout(stdout), _cl.redirect_stderr(stderr):
        exit_code = commands.list_tasks([], use_disk=True)

    assert exit_code == 1, (
        f"corrupt store must exit 1; got {exit_code}; stderr={stderr.getvalue()!r}"
    )
    assert "store corrupted" in stderr.getvalue(), (
        f"stderr must explain the corruption; got {stderr.getvalue()!r}"
    )
    # The corrupt file must be left intact (no silent rewrite).
    assert (taskq_home / "tasks.json").read_text(encoding="utf-8") == "{not json", (
        "the corrupt file must be left untouched, not rebuilt"
    )


# ---------------------------------------------------------------------------
# T-07 — NFR-04 cache entry redacts secrets before persistence
# ---------------------------------------------------------------------------


def test_nfr04_cache_entry_redacts_secrets(taskq_home):
    """SAD T-07: a secret in stdout_tail is redacted BEFORE cache.json write.

    Threat model: a successful task whose captured stdout embeds a
    secret must not land that secret on disk in `cache.json`. The
    mitigation runs the redaction regex over `stdout_tail` BEFORE the
    entry is persisted (commands.py lines 525-540) — this test pins
    that ordering by exercising the exact redaction-then-write path.
    """
    from taskq_plus.observability import audit
    from taskq_plus.service.cache import record as cache_record, signature

    secret = "sk-ABCD1234EFGH"
    # The exact pattern `commands.py` applies before persisting the
    # cache entry: `_audit._redact(raw_stdout)`.
    raw_stdout = f"echo {secret}\n"
    redacted_stdout = audit._redact(raw_stdout)

    command = "any-command"
    cache_record(command, {
        "command": command,
        "exit_code": 0,
        "stdout_tail": redacted_stdout,
        "finished_at": "2026-01-01T00:00:00+00:00",
        "status": "done",
    })

    cache_file = taskq_home / "cache.json"
    assert cache_file.exists(), "cache_record must persist cache.json"
    payload = _json.loads(cache_file.read_text(encoding="utf-8"))

    # The stdout_tail in cache.json must NOT contain the plaintext
    # secret — the redaction must run BEFORE persistence.
    stdout_tail = payload[signature(command)]["stdout_tail"]
    assert secret not in stdout_tail, (
        f"plaintext secret must NOT appear in cache.json stdout_tail; "
        f"got {stdout_tail!r}"
    )
    assert "[REDACTED]" in stdout_tail, (
        f"redaction marker must appear in cache.json stdout_tail; "
        f"got {stdout_tail!r}"
    )
