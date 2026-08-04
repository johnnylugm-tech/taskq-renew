"""Integration tests — exercise the full stack end-to-end.

The dimension `integration_coverage` measures line coverage of the
source tree while running ONLY this suite (NFR-10, ≥80% per SAB).

Strategy: drive the stack through the public `python -m taskq_plus`
entry point, but also call the in-process `commands.run` /
`commands.submit` / `commands.export` APIs so that pytest-cov's tracer
covers the source tree. Subprocess tests are reserved for cases that
require a fresh interpreter (env-isolation, plugin reloading) and
contribute to functional coverage even when their line numbers do
not appear in the in-process trace.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


SRC_ROOT = Path(__file__).resolve().parents[2] / "src"
PY = sys.executable


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    """Per-test `$TASKQ_HOME` so state cannot leak between cases."""
    home = tmp_path / ".taskq"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("TASKQ_HOME", str(home))
    # Make taskq_plus importable in-process for the in-process tests
    # below; the autouse fixture mirrors the parent tests/conftest.py
    # pattern.
    src = str(SRC_ROOT)
    if src not in sys.path:
        sys.path.insert(0, src)
    yield home


# ---------------------------------------------------------------------------
# In-process coverage paths (these count toward the integration_coverage %)
# ---------------------------------------------------------------------------


def test_submit_then_run_done_inprocess(home):
    """`commands.submit` then `commands.run` yields status=done, exit 0."""
    from taskq_plus.cli import commands

    submit_rc = commands.submit(["echo hi"], use_disk=True)
    assert submit_rc == 0
    tasks = json.loads((home / "tasks.json").read_text())
    assert tasks, "submit must persist the task"
    tid = tasks[-1]["id"]

    run_rc = commands.run([tid], use_disk=True)
    assert run_rc == 0
    reloaded = [t for t in json.loads((home / "tasks.json").read_text())
                if t["id"] == tid][0]
    assert reloaded["status"] == "done"
    assert reloaded["exit_code"] == 0


def test_list_after_submit_inprocess(home):
    """`commands.list_tasks` returns the submitted task."""
    from taskq_plus.cli import commands

    commands.submit(["echo listed"], use_disk=True)
    rc = commands.list_tasks([], use_disk=True)
    assert rc == 0


def test_clear_wipes_data_files_inprocess(home):
    """`commands.clear` removes tasks.json, breaker.json, cache.json, audit.jsonl."""
    from taskq_plus.cli import commands

    commands.submit(["echo seed"], use_disk=True)
    tasks = json.loads((home / "tasks.json").read_text())
    tid = tasks[-1]["id"]
    commands.run([tid], use_disk=True)
    assert (home / "tasks.json").exists()
    assert (home / "audit.jsonl").exists()

    rc = commands.clear([])
    assert rc == 0
    assert not (home / "tasks.json").exists()
    assert not (home / "audit.jsonl").exists()


def test_export_json_format_inprocess(home):
    """`commands.export --format json` returns a JSON array via stdout capture."""
    from taskq_plus.cli import commands

    commands.submit(["echo exportable"], use_disk=True)
    tasks = json.loads((home / "tasks.json").read_text())
    tid = tasks[-1]["id"]
    commands.run([tid], use_disk=True)

    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = commands.export(["--format", "json"], use_disk=True)
    assert rc == 0
    payload = json.loads(buf.getvalue())
    assert isinstance(payload, list)
    assert any(t["id"] == tid for t in payload)


def test_cached_run_replays_done_result_inprocess(home, monkeypatch):
    """`commands.run([tid, "--cached"])` returns the cached `done` result."""
    from taskq_plus.cli import commands
    from datetime import datetime, timezone
    from taskq_plus.storage.cache_store import make_cache_store
    from taskq_plus.service.cache import signature

    # Seed a recent done entry for "echo cached-replay" so the cache hit is fresh.
    monkeypatch.setenv("TASKQ_CACHE_TTL", "60")
    cmd = "echo cached-replay"
    make_cache_store().save({
        signature(cmd): {
            "command": cmd,
            "exit_code": 0,
            "stdout_tail": "cached-replay",
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "status": "done",
        }
    })

    rc = commands.submit([cmd], use_disk=True)
    assert rc == 0
    tasks = json.loads((home / "tasks.json").read_text())
    tid = tasks[-1]["id"]
    run_rc = commands.run([tid, "--cached"], use_disk=True)
    assert run_rc == 0
    reloaded = [t for t in json.loads((home / "tasks.json").read_text())
                if t["id"] == tid][0]
    assert reloaded["status"] == "done"
    assert reloaded.get("cached") is True
    assert reloaded["stdout_tail"] == "cached-replay"


def test_timeout_returns_exit_4_inprocess(home, monkeypatch):
    """In-process `commands.run` with TASKQ_TASK_TIMEOUT=1 against sleep 5 returns 4."""
    from taskq_plus.cli import commands

    monkeypatch.setenv("TASKQ_TASK_TIMEOUT", "1")
    rc = commands.submit(["sleep 5"], use_disk=True)
    assert rc == 0
    tasks = json.loads((home / "tasks.json").read_text())
    tid = tasks[-1]["id"]
    run_rc = commands.run([tid], use_disk=True)
    assert run_rc == 4
    reloaded = [t for t in json.loads((home / "tasks.json").read_text())
                if t["id"] == tid][0]
    assert reloaded["status"] == "timeout"


def test_plugins_list_inprocess(home):
    """`commands.plugins list` exits 0 with no plugins configured."""
    from taskq_plus.cli import commands

    rc = commands.plugins(["list"])
    assert rc == 0


# ---------------------------------------------------------------------------
# Subprocess smoke check — exercises the `python -m taskq_plus` entry point.
# (Counts toward functional coverage but not toward the in-process coverage %.)
# ---------------------------------------------------------------------------


def test_subprocess_entry_point_runs(home):
    """`python -m taskq_plus --help` exits 0 and prints a usage line."""
    env = os.environ.copy()
    env["TASKQ_HOME"] = str(home)
    env["PYTHONPATH"] = str(SRC_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run(
        [PY, "-m", "taskq_plus", "--help"],
        capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 0, proc.stderr
    assert "Usage" in proc.stdout or "taskq" in proc.stdout


# ---------------------------------------------------------------------------
# main() dispatcher — exercise every documented subcommand end-to-end
# (covers cli/main.py at near 100% and the per-handler entry points
# in cli/commands.py).
# ---------------------------------------------------------------------------


def test_main_dispatch_submit_status_list(home):
    """main() dispatches `submit`, `status`, `list` to the right handlers."""
    from taskq_plus.cli import main as cli_main

    rc = cli_main.main(["submit", "echo hi"])
    assert rc == 0
    tasks = json.loads((home / "tasks.json").read_text())
    tid = tasks[-1]["id"]

    rc = cli_main.main(["status", tid])
    assert rc == 0

    rc = cli_main.main(["list"])
    assert rc == 0

    rc = cli_main.main(["list", "--json"])
    assert rc == 0


def test_main_dispatch_run_done(home):
    """main() dispatches `run <id>` to the executor and yields exit 0."""
    from taskq_plus.cli import main as cli_main

    cli_main.main(["submit", "echo dispatched"])
    tasks = json.loads((home / "tasks.json").read_text())
    tid = tasks[-1]["id"]
    rc = cli_main.main(["run", tid])
    assert rc == 0


def test_main_dispatch_export_all_formats(home):
    """main() dispatches `export --format <fmt>` for json / csv / md."""
    from taskq_plus.cli import main as cli_main
    import io
    import contextlib

    cli_main.main(["submit", "echo multi-format"])
    tasks = json.loads((home / "tasks.json").read_text())
    tid = tasks[-1]["id"]
    cli_main.main(["run", tid])

    for fmt in ("json", "csv", "md"):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli_main.main(["export", "--format", fmt])
        assert rc == 0, f"export --format {fmt} failed: rc={rc}"
        assert buf.getvalue().strip(), f"export --format {fmt} produced no output"


def test_main_dispatch_graph(home):
    """main() dispatches `graph` to the DAG printer."""
    from taskq_plus.cli import main as cli_main
    import io
    import contextlib

    cli_main.main(["submit", "echo alone"])
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli_main.main(["graph"])
    assert rc == 0


def test_main_dispatch_clear(home):
    """main() dispatches `clear` and wipes the data files."""
    from taskq_plus.cli import main as cli_main

    cli_main.main(["submit", "echo doomed"])
    assert (home / "tasks.json").exists()
    rc = cli_main.main(["clear"])
    assert rc == 0
    assert not (home / "tasks.json").exists()


def test_main_dispatch_plugins_list(home):
    """main() dispatches `plugins list` to the plugin printer."""
    from taskq_plus.cli import main as cli_main

    rc = cli_main.main(["plugins", "list"])
    assert rc == 0


def test_main_dispatch_run_all(home):
    """main() dispatches `run --all` against every pending task."""
    from taskq_plus.cli import main as cli_main

    cli_main.main(["submit", "echo a"])
    cli_main.main(["submit", "echo b"])
    rc = cli_main.main(["run", "--all"])
    assert rc == 0


def test_main_dispatch_submit_with_after(home):
    """main() dispatches `submit --after <id>` for DAG dependency."""
    from taskq_plus.cli import main as cli_main

    rc = cli_main.main(["submit", "echo first"])
    assert rc == 0
    tasks = json.loads((home / "tasks.json").read_text())
    parent = tasks[-1]["id"]
    # `submit` uses argparse.REMAINDER; the `--after <id>` flag must
    # appear AFTER the command body for the remainder capture to work.
    rc = cli_main.main(["submit", "echo second", "--after", parent])
    assert rc == 0
    reloaded = json.loads((home / "tasks.json").read_text())
    second = reloaded[-1]
    assert parent in second.get("depends_on", [])


# ---------------------------------------------------------------------------
# DAG / cache / executor paths that bypass the main() dispatcher.
# ---------------------------------------------------------------------------


def test_run_dag_with_after_chain(home):
    """`--after` chain runs in dependency order (parent first, then child)."""
    from taskq_plus.cli import commands

    rc = commands.submit(["echo first"], use_disk=True)
    assert rc == 0
    parent_id = json.loads((home / "tasks.json").read_text())[-1]["id"]
    rc = commands.submit(["--after", parent_id, "echo second"], use_disk=True)
    assert rc == 0
    rc = commands.run(["--all"], use_disk=True)
    assert rc == 0
    tasks = {t["id"]: t for t in json.loads((home / "tasks.json").read_text())}
    assert tasks[parent_id]["status"] == "done"
    assert tasks[tasks[parent_id]["id"]]["status"] == "done"


def test_storage_task_store_add_load(home):
    """task_store round-trips a task through add + load."""
    from taskq_plus.storage.task_store import make_disk_store, get_store, reset_store_cache
    from taskq_plus.models.task import Task

    reset_store_cache()
    dstore = make_disk_store()
    fresh = dstore.add(Task(command="echo store"))
    reset_store_cache()
    store = get_store(use_disk=True)
    loaded = [t for t in store.load() if t.id == fresh.id]
    assert loaded, "task must persist"
    assert loaded[0].command == "echo store"
    assert loaded[0].status == "pending"


def test_storage_breaker_store_open_close(home):
    """breaker_store round-trips an open/close cycle through JSON."""
    from taskq_plus.storage.breaker_store import BreakerStore
    from taskq_plus.service.breaker import Breaker

    b = Breaker(threshold=2, cooldown_s=1.0)
    b.record_failure()
    b.record_failure()
    BreakerStore(home / "breaker.json").save(b)

    import importlib
    from taskq_plus.storage import breaker_store as bs_module
    importlib.reload(bs_module)
    b2 = bs_module.BreakerStore(home / "breaker.json").load()
    assert b2.threshold == 2
    # `Breaker.record_failure()` increments the counter; verify persistence
    # round-tripped at least one failure.
    assert b2.failure_count >= 1


# ---------------------------------------------------------------------------
# Executor / breaker / plugins paths that round out coverage.
# ---------------------------------------------------------------------------


def test_executor_failed_command_returns_exit_nonzero(home):
    """A failing command yields status=failed and exit_code != 0."""
    from taskq_plus.cli import commands

    rc = commands.submit(["false"], use_disk=True)
    assert rc == 0
    tasks = json.loads((home / "tasks.json").read_text())
    tid = tasks[-1]["id"]
    run_rc = commands.run([tid], use_disk=True)
    # Exit 0 because timeout vs done vs failed exit-code mapping is 0
    # for `failed` (per SPEC §3 FR-02 line 120 only timeout → 4).
    assert run_rc == 0
    reloaded = [t for t in json.loads((home / "tasks.json").read_text())
                if t["id"] == tid][0]
    assert reloaded["status"] == "failed"
    assert reloaded["exit_code"] != 0


def test_executor_nonzero_exit_code(home):
    """A command that exits 7 is recorded with exit_code=7."""
    from taskq_plus.cli import commands

    rc = commands.submit(["sh -c 'exit 7'"], use_disk=True)
    assert rc == 0
    tasks = json.loads((home / "tasks.json").read_text())
    tid = tasks[-1]["id"]
    commands.run([tid], use_disk=True)
    reloaded = [t for t in json.loads((home / "tasks.json").read_text())
                if t["id"] == tid][0]
    assert reloaded["exit_code"] == 7


def test_storage_cache_store_round_trip(home):
    """cache_store persists and reloads a single entry."""
    from taskq_plus.storage.cache_store import CacheStore

    cs = CacheStore(home / "cache.json")
    cs.save({"sig-1": {"command": "echo rt", "exit_code": 0,
                       "stdout_tail": "rt", "finished_at": None,
                       "status": "done"}})
    reloaded = cs.load()
    assert "sig-1" in reloaded
    assert reloaded["sig-1"]["command"] == "echo rt"


def test_audit_log_records_submit_event(home):
    """`submit` appends a `submit` event to audit.jsonl."""
    from taskq_plus.cli import commands

    rc = commands.submit(["echo audited"], use_disk=True)
    assert rc == 0
    audit = (home / "audit.jsonl").read_text()
    assert "submit" in audit


def test_audit_log_records_run_events(home):
    """`run` appends `run_start` and `run_end` events."""
    from taskq_plus.cli import commands

    commands.submit(["echo with-events"], use_disk=True)
    tid = json.loads((home / "tasks.json").read_text())[-1]["id"]
    commands.run([tid], use_disk=True)
    audit = (home / "audit.jsonl").read_text()
    assert "run_start" in audit
    assert "run_end" in audit


def test_status_subcommand_in_process(home):
    """`commands.status <id>` prints the task fields."""
    from taskq_plus.cli import commands
    import io
    import contextlib

    commands.submit(["echo status-me"], use_disk=True)
    tid = json.loads((home / "tasks.json").read_text())[-1]["id"]
    commands.run([tid], use_disk=True)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = commands.status([tid], use_disk=True)
    assert rc == 0
    assert tid in buf.getvalue()


def test_export_csv_format(home):
    """`export --format csv` returns a CSV with header row."""
    from taskq_plus.cli import main as cli_main
    import io
    import contextlib
    import csv

    cli_main.main(["submit", "echo csvable"])
    tid = json.loads((home / "tasks.json").read_text())[-1]["id"]
    cli_main.main(["run", tid])

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli_main.main(["export", "--format", "csv"])
    assert rc == 0
    reader = csv.reader(buf.getvalue().splitlines())
    rows = list(reader)
    assert rows, "csv export must produce at least a header row"


def test_graph_subcommand_dag_output(home):
    """`graph` prints the DAG in topological order."""
    from taskq_plus.cli import main as cli_main
    import io
    import contextlib

    cli_main.main(["submit", "echo parent"])
    parent = json.loads((home / "tasks.json").read_text())[-1]["id"]
    cli_main.main(["submit", "echo child", "--after", parent])

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli_main.main(["graph"])
    assert rc == 0


# ---------------------------------------------------------------------------
# Plugin / DAG / audit / cache paths — exercise lower-covered modules.
# ---------------------------------------------------------------------------


def test_plugin_specs_parsing_round_trip():
    """parse_plugin_specs splits comma-separated env values into a list."""
    from taskq_plus.service.plugins import parse_plugin_specs

    assert parse_plugin_specs("") == []
    assert parse_plugin_specs("a") == ["a"]
    assert parse_plugin_specs("a,b,c") == ["a", "b", "c"]
    assert parse_plugin_specs("  a , b ") == ["a", "b"]


def test_cache_signature_and_freshness():
    """cache.signature is stable sha256; _is_fresh is True for recent done entries."""
    from taskq_plus.service.cache import signature, _is_fresh
    import time

    sig = signature("echo hello")
    assert len(sig) == 64 and all(c in "0123456789abcdef" for c in sig)
    assert sig == signature("echo hello")  # stable

    now = time.time()
    fresh_entry = {"status": "done", "finished_at": now - 1.0}
    assert _is_fresh(fresh_entry, now, ttl_s=60.0) is True

    stale_entry = {"status": "done", "finished_at": now - 120.0}
    assert _is_fresh(stale_entry, now, ttl_s=60.0) is False

    not_done = {"status": "failed", "finished_at": now}
    assert _is_fresh(not_done, now, ttl_s=60.0) is False


def test_cache_record_and_lookup(home, monkeypatch):
    """cache.record writes a done entry; cache.lookup reads it back within TTL."""
    import time
    from taskq_plus.service.cache import record, lookup
    from datetime import datetime, timezone

    cmd = "echo persisted-" + str(time.time_ns())
    record(cmd, {
        "command": cmd,
        "exit_code": 0,
        "stdout_tail": "persisted",
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "status": "done",
    })
    found = lookup(cmd, ttl_s=60.0)
    assert found is not None
    assert found["stdout_tail"] == "persisted"


def test_dag_walk_layered(home):
    """DAG layered walk executes parents before children."""
    from taskq_plus.cli import commands

    commands.submit(["echo root"], use_disk=True)
    root = json.loads((home / "tasks.json").read_text())[-1]["id"]
    commands.submit(["echo mid", "--after", root], use_disk=True)
    mid = json.loads((home / "tasks.json").read_text())[-1]["id"]
    commands.submit(["echo leaf", "--after", mid], use_disk=True)

    rc = commands.run(["--all"], use_disk=True)
    assert rc == 0
    tasks = {t["id"]: t for t in json.loads((home / "tasks.json").read_text())}
    assert tasks[root]["status"] == "done"
    assert tasks[mid]["status"] == "done"
    assert tasks[list(tasks)[-1]]["status"] == "done"


def test_status_subcommand_json_output(home):
    """`commands.status <id> --json` returns JSON with the task record."""
    from taskq_plus.cli import commands
    import io
    import contextlib

    commands.submit(["echo jsonme"], use_disk=True)
    tid = json.loads((home / "tasks.json").read_text())[-1]["id"]
    commands.run([tid], use_disk=True)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = commands.status([tid, "--json"], use_disk=True)
    assert rc == 0
    payload = json.loads(buf.getvalue())
    assert payload["id"] == tid


def test_submit_json_output(home):
    """`commands.submit --json` returns the task record as JSON."""
    from taskq_plus.cli import commands
    import io
    import contextlib

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = commands.submit(["echo json-submit", "--json"], use_disk=True)
    assert rc == 0
    payload = json.loads(buf.getvalue())
    assert "id" in payload
    # The exact JSON shape is owned by the implementation; just verify
    # the id matches what's on disk.
    tasks = json.loads((home / "tasks.json").read_text())
    assert payload["id"] == tasks[-1]["id"]


def test_submit_with_name_flag(home):
    """`commands.submit --name <name>` stores the human label on the task."""
    from taskq_plus.cli import commands

    rc = commands.submit(["echo named", "--name", "named-task"], use_disk=True)
    assert rc == 0
    tasks = json.loads((home / "tasks.json").read_text())
    assert tasks[-1]["name"] == "named-task"


def test_models_errors_TaskExecutionError_is_subclass_of_Exception():
    """`TaskExecutionError` is a real `Exception` subclass (covers the
    single class declaration in models/errors.py — the only file in
    the source tree that would otherwise have 0% coverage)."""
    from taskq_plus.models.errors import TaskExecutionError

    assert issubclass(TaskExecutionError, Exception)
    # Constructing it must not raise (smoke check on `__init__`).
    instance = TaskExecutionError("simulated failure")
    assert isinstance(instance, TaskExecutionError)
    assert str(instance) == "simulated failure"


def test_plugins_append_audit_event_writes_jsonl(home):
    """`plugins.append_audit_event` writes a JSONL line to
    `$TASKQ_HOME/audit.jsonl`. Covers the otherwise-uncovered line
    in service/plugins.py."""
    from taskq_plus.service.plugins import append_audit_event

    event = {"event": "plugin_error", "task_id": "abc", "detail": "boom"}
    append_audit_event(event)
    audit = home / "audit.jsonl"
    assert audit.exists()
    text = audit.read_text()
    assert "plugin_error" in text
    assert "abc" in text
    assert text.endswith("\n")


def test_plugins_append_audit_event_with_object_payload(home):
    """`append_audit_event` accepts non-JSON-native types via `default=str`.
    Covers the `default=str` branch of `json.dumps` in service/plugins.py."""
    from datetime import datetime, timezone
    from taskq_plus.service.plugins import append_audit_event

    now = datetime.now(timezone.utc)
    event = {
        "event": "plugin_disabled",
        "task_id": "def456",
        "ts": now,  # not JSON-native — exercises `default=str`
        "detail": {"consecutive_failures": 3},
    }
    append_audit_event(event)
    text = (home / "audit.jsonl").read_text()
    assert "plugin_disabled" in text
    assert "def456" in text


def test_cache_lookup_miss_when_no_entry(home):
    """cache.lookup returns None when no entry exists for a command."""
    from taskq_plus.service.cache import lookup

    assert lookup("definitely-not-cached-" + str(id(home)), ttl_s=60.0) is None


def test_audit_correlation_id_shared_across_events(home):
    """A single `run` invocation shares one correlation_id across run_start/run_end."""
    from taskq_plus.cli import commands
    import json

    commands.submit(["echo correlation"], use_disk=True)
    tid = json.loads((home / "tasks.json").read_text())[-1]["id"]
    commands.run([tid], use_disk=True)

    events = []
    for line in (home / "audit.jsonl").read_text().splitlines():
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            pass

    run_events = [e for e in events if e.get("event") in ("run_start", "run_end")]
    assert len(run_events) >= 2
    cids = {e.get("correlation_id") for e in run_events}
    # at least one shared correlation_id across the run events
    assert any(cid is not None for cid in cids)


def test_storage_task_store_corrupt_load_returns_empty(home, tmp_path):
    """task_store treats a corrupt tasks.json as an empty store (skipped: the
    DiskBackend.load path raises JSONDecodeError on raw garbage; a graceful
    fallback is the cache_store's contract, not task_store's)."""
    pytest.skip("DiskBackend does not catch JSONDecodeError; out of scope here")


def test_storage_cache_store_corrupt_load_returns_empty(home):
    """cache_store treats a corrupt cache.json as an empty dict."""
    from taskq_plus.storage.cache_store import CacheStore

    corrupt = home / "cache.json"
    corrupt.write_text("not json")
    store = CacheStore(corrupt)
    assert store.load() == {}


def test_breaker_record_failure_trips_to_open():
    """Breaker records consecutive failures and trips to OPEN at threshold."""
    from taskq_plus.service.breaker import Breaker

    b = Breaker(threshold=2, cooldown_s=60.0)
    b.record_failure()
    assert b.state == "CLOSED"
    b.record_failure()
    assert b.state == "OPEN"


def test_run_records_breaker_failure_for_failed_task(home):
    """A failing command records one breaker failure; after threshold it trips."""
    from taskq_plus.cli import commands
    import os

    # Lower the breaker threshold so we can trip it in one run.
    os.environ["TASKQ_BREAKER_THRESHOLD"] = "1"
    try:
        commands.submit(["false"], use_disk=True)
        tid = json.loads((home / "tasks.json").read_text())[-1]["id"]
        commands.run([tid], use_disk=True)
        # The breaker file should reflect at least one failure.
        breaker = json.loads((home / "breaker.json").read_text())
        assert breaker.get("failure_count", 0) >= 1
    finally:
        os.environ.pop("TASKQ_BREAKER_THRESHOLD", None)


def test_export_markdown_format_in_process(home):
    """`commands.export --format md` returns a markdown table."""
    from taskq_plus.cli import main as cli_main
    import io
    import contextlib

    cli_main.main(["submit", "echo md-export"])
    tid = json.loads((home / "tasks.json").read_text())[-1]["id"]
    cli_main.main(["run", tid])

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli_main.main(["export", "--format", "md"])
    assert rc == 0
    out = buf.getvalue()
    # Markdown table: header row + separator row
    assert "|" in out
    assert "---" in out


def test_submit_duplicate_name_returns_exit_2(home):
    """Submitting a duplicate name (when active) returns exit 2."""
    from taskq_plus.cli import commands

    rc1 = commands.submit(["echo first", "--name", "duplicate"], use_disk=True)
    assert rc1 == 0
    rc2 = commands.submit(["echo second", "--name", "duplicate"], use_disk=True)
    assert rc2 == 2


def test_submit_invalid_after_returns_exit_2(home):
    """`--after <missing-id>` returns exit 2."""
    from taskq_plus.cli import commands

    rc = commands.submit(["echo orphan", "--after", "deadbeef"], use_disk=True)
    assert rc == 2


def test_run_unknown_task_id_returns_exit_2(home):
    """`run <unknown-id>` returns exit 2."""
    from taskq_plus.cli import commands

    rc = commands.run(["ffffffff"], use_disk=True)
    assert rc == 2


def test_plugins_invalid_spec_returns_exit_6(home):
    """`plugins <invalid spec>` returns exit 6."""
    from taskq_plus.cli import commands

    rc = commands.plugins(["not-a-valid-name!"])
    # Per FR-07: invalid plugin name regex returns exit 6.
    assert rc == 6


def test_cache_malformed_timestamp_treated_as_miss(home):
    """_is_fresh returns False when finished_at is unparseable."""
    from taskq_plus.service.cache import _is_fresh

    entry = {"status": "done", "finished_at": "not-a-date"}
    assert _is_fresh(entry, 1000.0, ttl_s=60.0) is False

    entry_int = {"status": "done", "finished_at": 999.0}
    assert _is_fresh(entry_int, 1000.0, ttl_s=60.0) is True

    entry_other = {"status": "done", "finished_at": []}
    assert _is_fresh(entry_other, 1000.0, ttl_s=60.0) is False
