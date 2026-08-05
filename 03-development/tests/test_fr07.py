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

import importlib
import json as _json
import os
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace



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
    # The IDs themselves are not asserted on (the circuit-breaker
    # assertion below observes the failure window, not the names);
    # assigning to `_` documents the canonical 3-consecutive pattern.
    _ = [
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


# ---------------------------------------------------------------------------
# In-process coverage: `_invoke_hook` `record.disabled` early-return path
# ---------------------------------------------------------------------------
# The subprocess tests above cover the user-visible surface (the audit
# trail records `plugin_disabled` and subsequent tasks skip the
# disabled plugin) but `_invoke_hook` itself (the per-call method) is
# only entered when the registry re-dispatches to a hook for a record
# whose `disabled` flag flipped on a *prior* iteration of the same
# `_invoke_phase` snapshot. The subprocess path cannot reach that
# branch from the outside — the snapshot is taken once per call. So
# an in-process test constructs a `PluginRegistry`, flips a record's
# `disabled` flag manually, and calls `run_pre` to land on the
# `if record.disabled: return` early-return at line 320.


def test_fr07_inprocess_disabled_hook_is_skipped(
    taskq_home, child_env, monkeypatch
):
    """`_call_hook` returns early when `record.disabled` is set.

    The `_invoke_phase` snapshot at lines 280-282 already filters
    disabled records before the loop, so the public-API path
    (`run_pre` / `run_post`) cannot reach line 320. To exercise the
    defensive check inside `_call_hook` we invoke it directly with a
    pre-disabled record — the post-condition is the same
    (`consecutive_failures` not bumped on a disabled record, no audit
    event emitted) so a future patch that re-enters the hook body
    fails this test instead of silently re-running a disabled plugin.
    """
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parent / "_test_plugins"))
    from taskq_plus.service.plugins import (
        DISABLE_THRESHOLD,
        PluginRecord,
        PluginRegistry,
    )

    # 1. Build a registry with one record whose `disabled=True`.
    raiser_mod = importlib.import_module("taskq_test_plugins.raiser")
    record = PluginRecord(
        name="taskq_test_plugins.raiser",
        module=raiser_mod,
        hooks=["pre_run"],
        status="loaded",
    )
    record.disabled = True
    record.consecutive_failures = DISABLE_THRESHOLD

    registry = PluginRegistry(plugin_env="taskq_test_plugins.raiser")
    registry._records.append(record)

    # 2. Clear the audit log so the post-condition is unambiguous.
    audit_log = taskq_home / "audit.jsonl"
    if audit_log.exists():
        audit_log.unlink()

    # 3. Call `_call_hook` directly so we land on the
    #    `if record.disabled: return` early-return at line 320.
    task = SimpleNamespace(command="echo x")
    registry._call_hook(
        record, raiser_mod, "pre_run",
        task, None,
        task_id="00000000", correlation_id="corr-test",
    )

    # 4. Post-condition: consecutive_failures unchanged, no audit entry.
    assert record.consecutive_failures == DISABLE_THRESHOLD, (
        f"disabled hook must not bump consecutive_failures; got "
        f"{record.consecutive_failures}"
    )
    if audit_log.exists():
        content = audit_log.read_text(encoding="utf-8")
        assert "plugin_error" not in content, (
            f"disabled record must not emit plugin_error; got: {content!r}"
        )


# ---------------------------------------------------------------------------
# In-process unit coverage for `taskq_plus.service.plugins`
# ---------------------------------------------------------------------------
# The four acceptance cases above drive the real `python -m taskq_plus`
# entry point, which is the user-facing contract TEST_SPEC rows 1-4 pin.
# A child process is not measured by the parent's coverage run (the
# GATE1 SUBPROCESS COVERAGE CEILING noted in the module docstring), so
# the loader/dispatcher internals need in-process tests to be measured
# at all. The cases below unit-test each documented branch of
# `PluginRegistry` and the audit-append helper directly.


def _plugins_module():
    """Import `taskq_plus.service.plugins` with `src/` on `sys.path`.

    The conftest puts `src/` on `sys.path` from an autouse *fixture*,
    which runs at call time rather than at module-import time, so the
    import is deferred into the test body (the same pattern the
    disabled-hook test above uses).
    """
    return importlib.import_module("taskq_plus.service.plugins")


def _fake_module(**hooks):
    """Build a stand-in plugin module exposing exactly `hooks`.

    `_call_hook` only does attribute access on the module object, so a
    `SimpleNamespace` is a faithful stand-in and lets a test choose the
    hook's behaviour (raise vs. record the call) without shipping a new
    file under `_test_plugins/`.
    """
    return SimpleNamespace(**hooks)


def test_fr07_inprocess_audit_path_is_under_taskq_home(taskq_home):
    """`_audit_path()` resolves to `$TASKQ_HOME/audit.jsonl` (SPEC §5.2)."""
    plugins_mod = _plugins_module()

    assert plugins_mod._audit_path() == taskq_home / "audit.jsonl", (
        f"audit log must resolve under $TASKQ_HOME; got "
        f"{plugins_mod._audit_path()!r}"
    )


def test_fr07_inprocess_parse_plugin_specs_strips_and_drops_blanks():
    """`parse_plugin_specs` strips whitespace and drops empty segments.

    A trailing comma or an all-blank `TASKQ_PLUGINS` must yield no
    specs rather than an empty-string spec — an empty spec would fail
    the name regex and be surfaced to the operator as a bogus
    `rejected` record.
    """
    plugins_mod = _plugins_module()

    assert plugins_mod.parse_plugin_specs("a.b, c ,,d,") == [
        "a.b",
        "c",
        "d",
    ], "specs must be stripped with blank segments dropped"
    assert plugins_mod.parse_plugin_specs("") == [], (
        "an empty allowlist must yield no specs"
    )
    assert plugins_mod.parse_plugin_specs("  ,  ,") == [], (
        "an all-blank allowlist must yield no specs, not empty specs"
    )


def test_fr07_inprocess_append_audit_event_appends_jsonl(taskq_home):
    """`append_audit_event` appends one JSON object per call.

    NFR-03 pins append semantics for `audit.jsonl`: a second event must
    not truncate the first, and each line must be independently
    decodable.
    """
    plugins_mod = _plugins_module()

    plugins_mod.append_audit_event({"event": "plugin_error", "seq": 1})
    plugins_mod.append_audit_event({"event": "plugin_disabled", "seq": 2})

    events = _read_audit_jsonl(taskq_home)
    assert [event["seq"] for event in events] == [1, 2], (
        f"both events must be appended in order; got {events!r}"
    )
    assert [event["event"] for event in events] == [
        "plugin_error",
        "plugin_disabled",
    ], f"event discriminators must round-trip; got {events!r}"


def test_fr07_inprocess_append_audit_event_serialises_non_json_values(
    taskq_home,
):
    """Non-JSON-serialisable values fall back to `str` instead of raising.

    `append_audit_event` passes `default=str` to `json.dumps` so an
    audit append can never itself become the reason a plugin error goes
    unrecorded.
    """
    plugins_mod = _plugins_module()

    plugins_mod.append_audit_event(
        {"event": "plugin_error", "detail": {"path": Path("/tmp/x")}}
    )

    events = _read_audit_jsonl(taskq_home)
    assert len(events) == 1, f"exactly one event expected; got {events!r}"
    assert events[0]["detail"]["path"] == "/tmp/x", (
        f"a non-serialisable value must be stringified; got {events[0]!r}"
    )


def test_fr07_inprocess_records_property_returns_a_snapshot():
    """`records` hands back a copy, not the live internal list.

    Mutating the returned list must not corrupt the registry, otherwise
    a `plugins list` caller could silently drop entries the dispatcher
    still iterates.
    """
    plugins_mod = _plugins_module()

    registry = plugins_mod.PluginRegistry(plugin_env="")
    registry._records.append(plugins_mod.PluginRecord(name="a"))

    snapshot = registry.records
    snapshot.clear()

    assert len(registry.records) == 1, (
        "mutating the `records` snapshot must not affect the registry"
    )


def test_fr07_inprocess_load_resolves_every_spec_without_raising():
    """`load()` produces one record per spec and never raises.

    The allowlist mixes a loadable module, a regex-rejected path form
    and an unimportable name; the loader must return a record for each
    so one bad spec cannot hide the rest from `plugins list`.
    """
    sys.path.insert(0, _test_plugins_path())
    plugins_mod = _plugins_module()

    registry = plugins_mod.PluginRegistry(
        plugin_env="taskq_test_plugins.noop,../evil.py,no_such_module_xyz"
    )
    registry.load()

    records = {record.name: record for record in registry.records}
    assert set(records) == {
        "taskq_test_plugins.noop",
        "../evil.py",
        "no_such_module_xyz",
    }, f"every spec must yield a record; got {sorted(records)!r}"
    assert records["taskq_test_plugins.noop"].status == "loaded"
    assert records["../evil.py"].status == "rejected"
    assert records["no_such_module_xyz"].status == "failed"


def test_fr07_inprocess_registry_defaults_to_taskq_plugins_env(monkeypatch):
    """With no explicit override the registry reads `TASKQ_PLUGINS`."""
    sys.path.insert(0, _test_plugins_path())
    plugins_mod = _plugins_module()
    monkeypatch.setenv("TASKQ_PLUGINS", "taskq_test_plugins.noop")

    registry = plugins_mod.PluginRegistry()
    registry.load()

    assert [record.name for record in registry.records] == [
        "taskq_test_plugins.noop"
    ], "the registry must default to the TASKQ_PLUGINS allowlist"


def test_fr07_inprocess_resolve_spec_loads_module_and_lists_hooks():
    """A well-formed spec is imported and its recognised hooks recorded."""
    sys.path.insert(0, _test_plugins_path())
    plugins_mod = _plugins_module()

    record = plugins_mod.PluginRegistry._resolve_spec(
        "taskq_test_plugins.noop"
    )

    assert record.status == "loaded", f"got status={record.status!r}"
    assert record.error is None, f"a loaded record carries no error; {record!r}"
    assert record.module is not None, "a loaded record must carry the module"
    assert record.hooks == ["pre_run", "post_run"], (
        f"hooks must be reported in HOOK_NAMES order; got {record.hooks!r}"
    )


def test_fr07_inprocess_resolve_spec_rejects_path_form_before_import():
    """A path form fails the regex and is never handed to importlib.

    NFR-02: the whitelist check must run *before* the import, so the
    test also asserts `importlib.import_module` was not called at all.
    """
    plugins_mod = _plugins_module()
    calls = []

    original = plugins_mod.importlib.import_module

    def _spy(name, *args, **kwargs):
        calls.append(name)
        return original(name, *args, **kwargs)

    plugins_mod.importlib.import_module = _spy
    try:
        record = plugins_mod.PluginRegistry._resolve_spec("../evil.py")
    finally:
        plugins_mod.importlib.import_module = original

    assert record.status == "rejected", f"got status={record.status!r}"
    assert record.error == "not a module name", f"got error={record.error!r}"
    assert record.module is None, "a rejected spec must not carry a module"
    assert calls == [], (
        f"a path form must be rejected before any import; importlib was "
        f"called with {calls!r}"
    )


def test_fr07_inprocess_resolve_spec_records_import_failure():
    """An unimportable (but well-formed) name becomes a `failed` record."""
    plugins_mod = _plugins_module()

    record = plugins_mod.PluginRegistry._resolve_spec("no_such_module_xyz")

    assert record.status == "failed", f"got status={record.status!r}"
    assert record.module is None, "a failed import must not carry a module"
    assert "ModuleNotFoundError" in (record.error or ""), (
        f"the error must name the exception type; got {record.error!r}"
    )


def test_fr07_inprocess_resolve_spec_flags_module_with_no_hooks():
    """An importable module exposing neither hook is reported as `failed`.

    The import itself succeeded, so the record still carries the module
    object — the operator needs to distinguish "could not import" from
    "imported but exposes no dispatchable hook".
    """
    plugins_mod = _plugins_module()

    record = plugins_mod.PluginRegistry._resolve_spec("json")

    assert record.status == "failed", f"got status={record.status!r}"
    assert record.error == "no pre_run or post_run hook", (
        f"got error={record.error!r}"
    )
    assert record.hooks == [], f"got hooks={record.hooks!r}"
    assert record.module is not None, (
        "a module that imported must be retained even with no hooks"
    )


def test_fr07_inprocess_run_pre_and_run_post_dispatch_hooks(taskq_home):
    """`run_pre`/`run_post` invoke the matching hook with the spec's args.

    `pre_run` receives the task; `post_run` receives the task and the
    result (SPEC §3 FR-07 hook signatures).
    """
    plugins_mod = _plugins_module()
    seen = []

    module = _fake_module(
        pre_run=lambda task: seen.append(("pre_run", task)),
        post_run=lambda task, result: seen.append(("post_run", task, result)),
    )
    record = plugins_mod.PluginRecord(
        name="fake",
        module=module,
        hooks=["pre_run", "post_run"],
        status="loaded",
    )
    registry = plugins_mod.PluginRegistry(plugin_env="")
    registry._records.append(record)

    task = SimpleNamespace(command="echo x")
    result = SimpleNamespace(exit_code=0)
    registry.run_pre(task, task_id="00000000", correlation_id="corr-1")
    registry.run_post(
        task, result, task_id="00000000", correlation_id="corr-1"
    )

    assert seen == [
        ("pre_run", task),
        ("post_run", task, result),
    ], f"both hooks must receive their documented arguments; got {seen!r}"
    assert record.consecutive_failures == 0, (
        "successful dispatch must leave the failure counter at zero"
    )


def test_fr07_inprocess_phase_skips_disabled_and_hookless_records(taskq_home):
    """Dispatch skips disabled records and records lacking the hook.

    Three records share one registry: a disabled one, one that declares
    no `pre_run`, and a healthy one. Only the healthy record's hook may
    fire, so a regression that drops either guard is caught.
    """
    plugins_mod = _plugins_module()
    seen = []

    disabled = plugins_mod.PluginRecord(
        name="disabled",
        module=_fake_module(pre_run=lambda task: seen.append("disabled")),
        hooks=["pre_run"],
        status="loaded",
    )
    disabled.disabled = True
    post_only = plugins_mod.PluginRecord(
        name="post-only",
        module=_fake_module(
            post_run=lambda task, result: seen.append("post-only")
        ),
        hooks=["post_run"],
        status="loaded",
    )
    healthy = plugins_mod.PluginRecord(
        name="healthy",
        module=_fake_module(pre_run=lambda task: seen.append("healthy")),
        hooks=["pre_run"],
        status="loaded",
    )

    registry = plugins_mod.PluginRegistry(plugin_env="")
    registry._records.extend([disabled, post_only, healthy])
    registry.run_pre(
        SimpleNamespace(command="echo x"),
        task_id="00000000",
        correlation_id="corr-1",
    )

    assert seen == ["healthy"], (
        f"only the healthy record's pre_run may fire; got {seen!r}"
    )


def test_fr07_inprocess_record_without_module_is_skipped(taskq_home):
    """A record carrying no module is skipped rather than dereferenced.

    `_resolve_spec` only populates `hooks` after assigning `module`, so
    this state is defensive — but the guard must hold, otherwise a
    rejected/failed record would raise `AttributeError` on dispatch and
    abort the run the FR-07 isolation rule protects.
    """
    plugins_mod = _plugins_module()

    record = plugins_mod.PluginRecord(
        name="rejected",
        module=None,
        hooks=["pre_run"],
        status="rejected",
    )
    registry = plugins_mod.PluginRegistry(plugin_env="")
    registry._records.append(record)

    registry.run_pre(
        SimpleNamespace(command="echo x"),
        task_id="00000000",
        correlation_id="corr-1",
    )

    assert record.consecutive_failures == 0, (
        "a module-less record must be skipped, not counted as a failure"
    )
    assert _read_audit_jsonl(taskq_home) == [], (
        "skipping a module-less record must not emit an audit event"
    )


def test_fr07_inprocess_hook_failure_emits_plugin_error_event(taskq_home):
    """A raising hook is caught, counted and recorded as `plugin_error`.

    AC-07-2: the exception must not propagate out of the dispatcher.
    The audit envelope must name the plugin, the hook and the current
    consecutive-failure count so the operator can act on it.
    """
    plugins_mod = _plugins_module()

    def _boom(task):
        raise RuntimeError("kaboom")

    record = plugins_mod.PluginRecord(
        name="boom",
        module=_fake_module(pre_run=_boom),
        hooks=["pre_run"],
        status="loaded",
    )
    registry = plugins_mod.PluginRegistry(plugin_env="")
    registry._records.append(record)

    registry.run_pre(
        SimpleNamespace(command="echo x"),
        task_id="deadbeef",
        correlation_id="corr-9",
    )

    assert record.consecutive_failures == 1, (
        f"a raising hook must bump the counter once; got "
        f"{record.consecutive_failures}"
    )
    assert record.disabled is False, (
        "one failure is below DISABLE_THRESHOLD; the plugin stays enabled"
    )

    errors = _audit_events_of_kind(_read_audit_jsonl(taskq_home), "plugin_error")
    assert len(errors) == 1, f"exactly one plugin_error expected; got {errors!r}"
    event = errors[0]
    assert event["task_id"] == "deadbeef", f"got {event!r}"
    assert event["correlation_id"] == "corr-9", f"got {event!r}"
    assert event["detail"]["plugin"] == "boom", f"got {event!r}"
    assert event["detail"]["hook"] == "pre_run", f"got {event!r}"
    assert event["detail"]["consecutive_failures"] == 1, f"got {event!r}"
    assert "RuntimeError: kaboom" == event["detail"]["error"], f"got {event!r}"


def test_fr07_inprocess_post_run_failure_is_isolated(taskq_home):
    """A raising `post_run` is isolated on the same terms as `pre_run`."""
    plugins_mod = _plugins_module()

    def _boom(task, result):
        raise ValueError("post boom")

    record = plugins_mod.PluginRecord(
        name="boom-post",
        module=_fake_module(post_run=_boom),
        hooks=["post_run"],
        status="loaded",
    )
    registry = plugins_mod.PluginRegistry(plugin_env="")
    registry._records.append(record)

    registry.run_post(
        SimpleNamespace(command="echo x"),
        SimpleNamespace(exit_code=0),
        task_id="deadbeef",
        correlation_id="corr-9",
    )

    errors = _audit_events_of_kind(_read_audit_jsonl(taskq_home), "plugin_error")
    assert len(errors) == 1, f"exactly one plugin_error expected; got {errors!r}"
    assert errors[0]["detail"]["hook"] == "post_run", f"got {errors[0]!r}"
    assert errors[0]["detail"]["error"] == "ValueError: post boom", (
        f"got {errors[0]!r}"
    )


def test_fr07_inprocess_third_consecutive_failure_disables_plugin(taskq_home):
    """AC-07-3: the plugin is disabled on the `DISABLE_THRESHOLD`-th failure.

    The boundary is asserted from both sides — the plugin is still
    enabled after `DISABLE_THRESHOLD - 1` failures and disabled on the
    next one — so an off-by-one (disabling at 2, or at 4) fails here.
    """
    plugins_mod = _plugins_module()
    threshold = plugins_mod.DISABLE_THRESHOLD
    calls = []

    def _boom(task):
        calls.append("call")
        raise RuntimeError("kaboom")

    record = plugins_mod.PluginRecord(
        name="boom",
        module=_fake_module(pre_run=_boom),
        hooks=["pre_run"],
        status="loaded",
    )
    registry = plugins_mod.PluginRegistry(plugin_env="")
    registry._records.append(record)

    task = SimpleNamespace(command="echo x")
    for _ in range(threshold - 1):
        registry.run_pre(task, task_id="deadbeef", correlation_id="corr-9")
    assert record.disabled is False, (
        f"the plugin must survive {threshold - 1} failures"
    )

    registry.run_pre(task, task_id="deadbeef", correlation_id="corr-9")
    assert record.disabled is True, (
        f"the plugin must be disabled on failure #{threshold}"
    )

    events = _read_audit_jsonl(taskq_home)
    assert len(_audit_events_of_kind(events, "plugin_error")) == threshold, (
        f"one plugin_error per failure expected; got {events!r}"
    )
    disabled_events = _audit_events_of_kind(events, "plugin_disabled")
    assert len(disabled_events) == 1, (
        f"exactly one plugin_disabled event expected; got {events!r}"
    )
    assert disabled_events[0]["detail"]["plugin"] == "boom", (
        f"the disable event must name the plugin; got {disabled_events[0]!r}"
    )
    assert (
        disabled_events[0]["detail"]["consecutive_failures"] == threshold
    ), f"got {disabled_events[0]!r}"

    # Once disabled the plugin is skipped for the remainder of the run.
    registry.run_pre(task, task_id="deadbeef", correlation_id="corr-9")
    assert len(calls) == threshold, (
        f"a disabled plugin must not be invoked again; got {len(calls)} calls"
    )


def test_fr07_inprocess_success_resets_consecutive_failure_counter(taskq_home):
    """A successful hook resets the counter, so failures must be *consecutive*.

    Alternating fail/succeed past `DISABLE_THRESHOLD` total failures
    must never disable the plugin — this is what makes the SPEC's
    "連續 3 次" wording load-bearing rather than a running total.
    """
    plugins_mod = _plugins_module()
    threshold = plugins_mod.DISABLE_THRESHOLD
    should_raise = {"value": True}

    def _flaky(task):
        if should_raise["value"]:
            raise RuntimeError("kaboom")

    record = plugins_mod.PluginRecord(
        name="flaky",
        module=_fake_module(pre_run=_flaky),
        hooks=["pre_run"],
        status="loaded",
    )
    registry = plugins_mod.PluginRegistry(plugin_env="")
    registry._records.append(record)

    task = SimpleNamespace(command="echo x")
    for _ in range(threshold + 2):
        should_raise["value"] = True
        registry.run_pre(task, task_id="deadbeef", correlation_id="corr-9")
        assert record.consecutive_failures == 1, (
            f"counter must be 1 after a single failure; got "
            f"{record.consecutive_failures}"
        )
        should_raise["value"] = False
        registry.run_pre(task, task_id="deadbeef", correlation_id="corr-9")
        assert record.consecutive_failures == 0, (
            "a successful hook must reset the consecutive-failure counter"
        )

    assert record.disabled is False, (
        f"alternating fail/succeed must never disable the plugin even past "
        f"{threshold} total failures"
    )
    events = _read_audit_jsonl(taskq_home)
    assert _audit_events_of_kind(events, "plugin_disabled") == [], (
        f"no plugin_disabled event may be emitted; got {events!r}"
    )
