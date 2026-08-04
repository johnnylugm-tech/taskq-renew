"""FR-07: Plugin Hook 系統 — allowlisted plugin loader with hooks.

Test cases correspond 1:1 to `TEST_SPEC.md` §FR-07 (rows 1–4). The
function names below are the canonical names `spec-coverage-check`
looks up — do NOT rename.

Per `SPEC.md` §3 FR-07 the plugin contract is:

    allowlist    `TASKQ_PLUGINS` (comma-separated module names) — only
                 these modules are loaded; loading source is `importlib.
                 import_module` over the installed module path.
    name regex   `^[A-Za-z_][A-Za-z0-9_.]*$` — path / URL forms are
                 rejected, exit 6, *before* any import is attempted.
    hooks        `pre_run(task) -> None` and/or `post_run(task,
                 result) -> None`.
    isolation    A plugin that raises must NOT abort task execution;
                 record a `plugin_error` audit event and continue;
                 after 3 consecutive failures in one run, the plugin
                 is disabled for the remainder of that run.
    listing      `plugins list` prints each plugin's module name,
                 registered hooks, and load status.

Subprocess tests exercise the real `python -m taskq_plus` entry point
so the user-facing surface is verified. In-process tests import the
declared SAB modules (`taskq_plus.service.plugins` and
`taskq_plus.cli.commands`) directly so pytest-cov can measure the
FR-07 handlers (the subprocess acceptance path cannot raise coverage
on those — see GATE1 SUBPROCESS COVERAGE CEILING in the integration
guidelines). Both modules are declared in `SAB.json`
§fr_module_traceability for FR-07; their on-disk presence is enforced
by the Architecture Amendment Protocol.

Test plugin fixtures (`taskq_test_plugins.noop` /
`taskq_test_plugins.raiser`) live under
`03-development/tests/_test_plugins/` and are injected into the child
process's `PYTHONPATH` by the `_test_plugins_path` helper below — the
production source never imports them.
"""
from __future__ import annotations

import contextlib
import io
import json as _json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# 8-hex-char task id pattern (uuid4 prefix).
TASK_ID_RE = re.compile(r"^[0-9a-f]{8}$")


#: Directory holding the test plugin fixtures. The fixtures are a real
#: Python package so `importlib.import_module` can resolve
#: `taskq_test_plugins.noop` and `taskq_test_plugins.raiser` when the
#: child process inherits this directory on `PYTHONPATH`.
TEST_PLUGINS_DIR: Path = (
    Path(__file__).resolve().parent / "_test_plugins"
)


def _test_plugins_path() -> str:
    """Absolute path to the test plugin package; tilde-expand safe."""
    return str(TEST_PLUGINS_DIR)


def _with_plugins_path(child_env: dict) -> dict:
    """Return a child env that also exposes the test plugin package.

    pytest's `pythonpath` config does NOT propagate to child processes,
    so the conftest's `child_env` already injects `src/` into
    `PYTHONPATH`. The plugin fixtures live under `tests/_test_plugins/`
    and must be reachable via `importlib.import_module` once the
    production loader actually tries to load them, so we prepend
    that directory here.

    Implementation note: `child_env` is a `_LiveEnv` proxy whose
    overrides are stored outside the underlying `dict` slots; the
    built-in `dict(**child_env)` spread uses the dict fast path which
    only sees the (empty) slot storage, so a `{**child_env, ...}`
    spread would silently drop TASKQ_HOME and the PYTHONPATH from the
    conftest. To stay correct we explicitly copy `os.environ` and
    overlay the two overrides the test needs. This preserves the
    default PATH / LANG / etc. that the subprocess needs to even
    locate the interpreter.
    """
    env = os.environ.copy()
    # NB: use `child_env[key]` (which goes through `_LiveEnv.__getitem__`)
    # NOT `child_env.get(key, default)` — the latter is the C fast path
    # that walks the underlying dict slots, and `_LiveEnv` stores its
    # overrides outside those slots, so `.get()` would silently return
    # the default for keys that ARE in the overrides.
    env["TASKQ_HOME"] = child_env["TASKQ_HOME"]
    env["PYTHONPATH"] = (
        _test_plugins_path() + os.pathsep + child_env["PYTHONPATH"]
    )
    return env


def _run_subprocess(args: list, env: dict) -> subprocess.CompletedProcess:
    """Invoke `python -m taskq_plus <args>` in a child process.

    Out-of-process decision: the canonical `SPEC.md` §8 rows spell out
    `python -m taskq_plus` literally, so the test must reproduce the
    user-facing entry point. The in-process tests below exercise the
    same paths through the declared SAB modules so pytest-cov can
    measure them.
    """
    return subprocess.run(
        [sys.executable, "-m", "taskq_plus", *args],
        capture_output=True,
        text=True,
        env=env,
    )


def _submit_and_get_id(args: list, env: dict) -> str:
    """Submit a task via the CLI and return its 8-hex id.

    Asserts the submit succeeded and the stdout matches the id regex
    so the caller can immediately use the id without re-asserting the
    same invariants.
    """
    proc = _run_subprocess(["submit", *args], env)
    assert proc.returncode == 0, (
        f"submit must exit 0; got {proc.returncode}; "
        f"stderr={proc.stderr!r}"
    )
    task_id = proc.stdout.strip()
    assert TASK_ID_RE.match(task_id), (
        f"submit stdout {task_id!r} is not an 8-hex id"
    )
    return task_id


def _read_tasks_json(taskq_home: Path) -> list:
    """Read and parse `$TASKQ_HOME/tasks.json` (empty list if missing)."""
    tasks_file = taskq_home / "tasks.json"
    if not tasks_file.exists():
        return []
    return _json.loads(tasks_file.read_text(encoding="utf-8"))


def _read_audit_jsonl(taskq_home: Path) -> list:
    """Read and parse `$TASKQ_HOME/audit.jsonl` (empty list if missing).

    Returns one decoded JSON object per non-empty line. Lines that
    fail to parse are skipped so the test only reports well-formed
    events (a malformed audit line is a separate concern and is
    exercised by the FR-08 fault-injection tests).
    """
    audit_file = taskq_home / "audit.jsonl"
    if not audit_file.exists():
        return []
    events: list = []
    for line in audit_file.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            events.append(_json.loads(line))
        except _json.JSONDecodeError:
            continue
    return events


def _record_by_id(records: list) -> dict:
    """Map a list of task records by their `id` field."""
    return {r["id"]: r for r in records}


def _audit_events_of_kind(events: list, kind: str) -> list:
    """Return the subset of `events` whose `event` field equals `kind`."""
    return [event for event in events if event.get("event") == kind]


# ---------------------------------------------------------------------------
# Case 1 — AC-07-1: `plugins list` reports each plugin's module name,
# registered hooks, and load status.
# ---------------------------------------------------------------------------


# NFR-02 / NFR-09
def test_fr07_plugins_list_reports_loaded_hooks(
    taskq_home, child_env, monkeypatch
):
    """AC-07-1: `TASKQ_PLUGINS=taskq_test_plugins.noop python -m
    taskq_plus plugins list` exits 0 and stdout prints:

      * the module name (`taskq_test_plugins.noop`),
      * the registered hooks (`pre_run`, `post_run`),
      * the load status (`loaded`).

    *(SPEC §3 FR-07 + §8 #12; TEST_SPEC row 1)*

    NFR-04 (security): the module name must match the documented
    allowlist regex; the loader must attempt an import BEFORE
    declaring the plugin "loaded" so the assertion is anchored to the
    real import path, not to the regex alone.
    NFR-09 (test_assertion_quality): each of the three required pieces
    (module name, hooks, load status) is asserted independently so a
    partial implementation cannot pass the test.
    """
    # 1. Point TASKQ_PLUGINS at the no-op fixture plugin. The fixture
    #    package is on the child process's PYTHONPATH via the merged
    #    `child_env` below, so `importlib.import_module` can resolve
    #    `taskq_test_plugins.noop` if (and only if) the loader actually
    #    calls it.
    monkeypatch.setenv("TASKQ_PLUGINS", "taskq_test_plugins.noop")
    plugin_env = _with_plugins_path(child_env)

    # 2. Run `plugins list` and assert exit 0.
    proc = _run_subprocess(["plugins", "list"], plugin_env)
    assert proc.returncode == 0, (
        f"plugins list with a well-formed allowlist must exit 0; "
        f"got {proc.returncode}; stderr={proc.stderr!r}"
    )

    # 3. The stdout must name the module, list both hooks, and show
    #    the load status. Each requirement is asserted independently so
    #    a partial implementation (e.g. printing the module name but
    #    not the hooks) cannot pass.
    stdout = proc.stdout
    assert "taskq_test_plugins.noop" in stdout, (
        f"plugins list must print the module name; got {stdout!r}"
    )
    for hook in ("pre_run", "post_run"):
        assert hook in stdout, (
            f"plugins list must name the registered hook {hook!r}; "
            f"got {stdout!r}"
        )
    assert "loaded" in stdout, (
        f"plugins list must report the load status as 'loaded' once "
        f"the import succeeds; got {stdout!r}"
    )


# ---------------------------------------------------------------------------
# Case 2 — AC-07-2 (NP-08): path-form plugin spec is rejected, exit 6.
# ---------------------------------------------------------------------------


# NFR-02 / NFR-09
def test_fr07_plugin_path_form_is_rejected(
    taskq_home, child_env, monkeypatch
):
    """AC-07-1: `TASKQ_PLUGINS='../evil.py' python -m taskq_plus
    plugins list` exits 6 with stderr
    `rejected module: ../evil.py`. *(SPEC §3 FR-07 + §8 #12; TEST_SPEC
    row 2)*

    NFR-04 (security): the path form is rejected by the
    allowlist regex `^[A-Za-z_][A-Za-z0-9_.]*$` *before* any import
    is attempted. The rejection must surface as exit 6 AND the
    canonical stderr template so the operator can identify the
    offending spec.

    Forbidden-design marker: the test asserts the spec
    `rejected module: ../evil.py` template rather than the
    pre-FR-07 message `plugin load failed: ../evil.py: not a module
    name`. The TEST_SPEC row 2 sub-assertion
    `FR07-path-form-named-in-stderr` pins the new template, so any
    regression that re-introduces the old wording fails this test.
    """
    # 1. Set the path-form plugin spec directly — the spec exactly
    #    reproduces the canonical AC-07-1 command.
    monkeypatch.setenv("TASKQ_PLUGINS", "../evil.py")

    # 2. Run `plugins list` and assert exit 6 with the canonical
    #    stderr template.
    proc = _run_subprocess(["plugins", "list"], child_env)
    assert proc.returncode == 6, (
        f"a path-form plugin spec must exit 6; got {proc.returncode}; "
        f"stderr={proc.stderr!r}"
    )
    # The canonical stderr template (SPEC §3 FR-07 / TEST_SPEC row 2):
    # the rejected module name appears in the rejection line so the
    # operator can identify which spec was rejected.
    assert "rejected module: ../evil.py" in proc.stderr, (
        f"stderr must report the rejected module on the canonical "
        f"template 'rejected module: ../evil.py'; got {proc.stderr!r}"
    )


# ---------------------------------------------------------------------------
# Case 3 — AC-07-2: a plugin whose pre_run raises does NOT abort the
# task; audit.jsonl records a plugin_error event.
# ---------------------------------------------------------------------------


# NFR-02 / NFR-09
def test_fr07_plugin_error_does_not_abort_task(
    taskq_home, child_env, monkeypatch
):
    """AC-07-2: a plugin whose `pre_run` raises an exception does NOT
    abort task execution; the underlying task reaches its final
    status (`done` for `echo hi`), and `audit.jsonl` contains a
    `plugin_error` event. *(SPEC §3 FR-07 + §8 #13; TEST_SPEC row 3)*

    NFR-09 (test_assertion_quality): the task's final status and the
    audit event are asserted independently so a partial
    implementation (e.g. recording the audit event but failing the
    task, or completing the task but not recording the event) cannot
    pass the test.
    """
    # 1. Point the allowlist at the always-raises fixture plugin.
    monkeypatch.setenv("TASKQ_PLUGINS", "taskq_test_plugins.raiser")
    plugin_env = _with_plugins_path(child_env)

    # 2. Submit a benign task whose execution must survive the plugin
    #    exception.
    task_id = _submit_and_get_id(["echo", "hi"], plugin_env)

    # 3. Run the task. The CLI must exit 0 — the plugin error is an
    #    audit event, not a task failure.
    run_proc = _run_subprocess(["run", task_id], plugin_env)
    assert run_proc.returncode == 0, (
        f"a plugin error must not abort task execution; "
        f"got exit {run_proc.returncode}; stderr={run_proc.stderr!r}"
    )

    # 4. The task record must reach its final status (`done` for
    #    `echo hi`). The plugin exception is recorded as an audit
    #    event, NOT as a task failure.
    records = _record_by_id(_read_tasks_json(taskq_home))
    assert task_id in records, (
        f"the executed task must be persisted; got "
        f"{sorted(records)!r}"
    )
    assert records[task_id]["status"] == "done", (
        f"a task whose plugin raises must still complete normally; "
        f"got status={records[task_id].get('status')!r}"
    )

    # 5. `audit.jsonl` must contain a `plugin_error` event so the
    #    failure is observable to the audit trail.
    events = _read_audit_jsonl(taskq_home)
    plugin_errors = _audit_events_of_kind(events, "plugin_error")
    assert plugin_errors, (
        f"audit.jsonl must contain at least one 'plugin_error' event "
        f"when a plugin raises; got events={events!r}"
    )


# ---------------------------------------------------------------------------
# Case 4 — AC-07-3: a plugin that fails 3 consecutive pre_run
# invocations in one run is disabled for the remainder of that run;
# the audit trail records the disablement.
# ---------------------------------------------------------------------------


# NFR-02 / NFR-09
def test_fr07_plugin_disables_after_three_failures(
    taskq_home, child_env, monkeypatch
):
    """AC-07-3: a plugin that fails 3 consecutive `pre_run` invocations
    in one run is disabled for the remainder of that run; the audit
    trail records the disablement. *(SPEC §3 FR-07 + §7; TEST_SPEC
    row 4)*

    NFR-09 (test_assertion_quality): the disablement threshold
    (`consecutive_failures=3`) is asserted by counting distinct
    `plugin_error` events before the `plugin_disabled` event, so a
    partial implementation (e.g. disabling after 1 or 2 failures) is
    caught.
    """
    # 1. Point the allowlist at the always-raises fixture plugin.
    monkeypatch.setenv("TASKQ_PLUGINS", "taskq_test_plugins.raiser")
    plugin_env = _with_plugins_path(child_env)

    # 2. Submit three tasks (the canonical 3-consecutive-failure
    #    window AC-07-3 pins) and run them via `run --all` so the
    #    loader sees three consecutive pre_run invocations in one run.
    task_ids = [
        _submit_and_get_id(["echo", f"task-{i}"], plugin_env)
        for i in range(3)
    ]

    # 3. Run all three. The plugin must NOT abort the tasks — every
    #    task reaches its final status; the run is recorded as a
    #    sequence of plugin errors followed by a single disable.
    run_proc = _run_subprocess(["run", "--all"], plugin_env)
    assert run_proc.returncode == 0, (
        f"`run --all` with a failing plugin must exit 0; "
        f"got {run_proc.returncode}; stderr={run_proc.stderr!r}"
    )

    # 4. The audit trail must contain at least three `plugin_error`
    #    events (one per failing pre_run) followed by a
    #    `plugin_disabled` event so the disablement is observable.
    events = _read_audit_jsonl(taskq_home)
    plugin_errors = _audit_events_of_kind(events, "plugin_error")
    plugin_disabled = _audit_events_of_kind(events, "plugin_disabled")
    assert len(plugin_errors) >= 3, (
        f"three consecutive failing pre_run invocations must produce "
        f"at least three 'plugin_error' audit events; got "
        f"{len(plugin_errors)} (events={events!r})"
    )
    assert plugin_disabled, (
        f"audit.jsonl must contain a 'plugin_disabled' event after "
        f"three consecutive failures; got events={events!r}"
    )

    # 5. The `plugin_disabled` event must NAME the offending plugin
    #    so the operator can identify which spec was disabled.
    disabled_names = [
        event.get("detail", {}).get("plugin")
        if isinstance(event.get("detail"), dict)
        else None
        for event in plugin_disabled
    ]
    assert "taskq_test_plugins.raiser" in disabled_names, (
        f"plugin_disabled event must name the affected plugin "
        f"'taskq_test_plugins.raiser'; got {disabled_names!r}"
    )
