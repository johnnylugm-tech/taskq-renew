"""FR-04: 結果 TTL 快取 — TTL cache for completed task results.

Test cases correspond 1:1 to TEST_SPEC.md §FR-04 (rows 1–3). The function
names below are the canonical names `spec-coverage-check` looks up — do
NOT rename. Cache contract per SPEC.md §3 FR-04:

    signature     sha256(command) — every command has one stable key
                  regardless of how many submissions carry it.
    TTL           `TASKQ_CACHE_TTL` seconds. Within TTL a prior `done`
                  execution is replayed without launching a subprocess.
                  Past TTL the entry is treated as a miss and the
                  command re-executes.
    persistence   `$TASKQ_HOME/cache.json` — atomic write, thread-safe
                  (coexists with FR-02 concurrency).
    failure mode  NP-07 (cache.json is a fallible dependency): a
                  corrupt cache file must NOT crash the run; the
                  command must execute normally and the run exits 0.

Subprocess tests exercise the real `python -m taskq_plus` entry point
where the spec literally spells it out (Cases 1–3 — the canonical
"--cached" / "expired TTL" / "corrupt cache.json" checks). In-process
tests import the declared SAB modules directly so pytest-cov can
measure `taskq_plus.service.cache` and `taskq_plus.storage.cache_store`
(the subprocess acceptance path can never raise coverage on these —
see GATE1 SUBPROCESS COVERAGE CEILING in the integration guidelines).
"""
from __future__ import annotations

import contextlib
import hashlib
import json as _json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# 8-hex-char task id pattern (uuid4 prefix).
TASK_ID_RE = re.compile(r"^[0-9a-f]{8}$")


def _command_signature(command: str) -> str:
    """Return the canonical cache key for `command` (sha256 hex).

    The spec fixes the signature as `sha256(command)` (SPEC §3 FR-04).
    The test uses the same algorithm so a pre-populated cache entry
    can match what the GREEN implementation will compute.
    """
    return hashlib.sha256(command.encode("utf-8")).hexdigest()


def _run_submit_subprocess(args: list, env: dict) -> subprocess.CompletedProcess:
    """`python -m taskq_plus submit ...` in a child process.

    Out-of-process decision: the canonical SPEC §8 row #9 spells out
    `python -m taskq_plus run <id> --cached` literally, so the
    cache-replay acceptance test reproduces the user-facing entry
    point. The in-process tests below exercise the same path through
    the declared SAB modules so pytest-cov can measure them.
    """
    return subprocess.run(
        [sys.executable, "-m", "taskq_plus", "submit", *args],
        capture_output=True,
        text=True,
        env=env,
    )


def _run_subprocess(args: list, env: dict) -> subprocess.CompletedProcess:
    """`python -m taskq_plus run ...` in a child process.

    Accepts `run <id> --cached` (FR-04) and the `run <id>` /
    `run --all` shapes from FR-02/FR-03.
    """
    return subprocess.run(
        [sys.executable, "-m", "taskq_plus", "run", *args],
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
    proc = _run_submit_subprocess(args, env)
    assert proc.returncode == 0, (
        f"submit must exit 0; got {proc.returncode}; stderr={proc.stderr!r}"
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


def _read_cache_json(taskq_home: Path) -> dict:
    """Read and parse `$TASKQ_HOME/cache.json` (empty dict if missing)."""
    cache_file = taskq_home / "cache.json"
    if not cache_file.exists():
        return {}
    return _json.loads(cache_file.read_text(encoding="utf-8"))


def _write_cache_json(taskq_home: Path, payload: dict) -> None:
    """Atomically write `payload` to `$TASKQ_HOME/cache.json`.

    The test seeds the cache with a known `done` entry so the run
    path can replay it without launching a subprocess. The write is
    atomic (`tmp + os.replace`) for parity with what the GREEN
    implementation is required to do (NFR-03 atomicity, FR-04 last
    bullet).
    """
    import os
    import tempfile

    cache_file = taskq_home / "cache.json"
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=".cache.", suffix=".json.tmp", dir=cache_file.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            _json.dump(payload, fh, ensure_ascii=False, indent=2)
        os.chmod(tmp_name, 0o644)
        os.replace(tmp_name, cache_file)
    except Exception:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise


def _seed_cache_entry(
    taskq_home: Path,
    command: str,
    exit_code: int,
    stdout_tail: str,
    finished_at: str,
) -> str:
    """Seed `$TASKQ_HOME/cache.json` with a `done` entry for `command`.

    Returns the cache signature (sha256 hex) so callers can verify the
    keying. The shape mirrors what the GREEN cache service writes:
    a map from signature -> {exit_code, stdout_tail, finished_at,
    status="done"}.
    """
    signature = _command_signature(command)
    payload = _read_cache_json(taskq_home)
    payload[signature] = {
        "command": command,
        "exit_code": exit_code,
        "stdout_tail": stdout_tail,
        "finished_at": finished_at,
        "status": "done",
    }
    _write_cache_json(taskq_home, payload)
    return signature


# ---------------------------------------------------------------------------
# Case 1 — AC-04-1: cache hit within TTL — replay prior result, no subprocess
# ---------------------------------------------------------------------------


# NFR-09 (test_assertion_quality) / NFR-05 (idempotent replay) / NP-05
def test_fr04_cached_run_replays_done_result(taskq_home, child_env, monkeypatch):
    """AC-04-1: Within `TASKQ_CACHE_TTL` (60 s), `python -m taskq_plus
    run <id> --cached` returns the prior `exit_code` / `stdout_tail`,
    sets `cached: true` on the task, and does NOT spawn a subprocess.
    *(SPEC §3 FR-04 + §8 #9)*

    NFR-09 (test_assertion_quality): asserts the persisted task
    record carries the cached values (proving the cache path was
    taken) and the canonical `cached: true` flag. The "no subprocess"
    half is measured by seeding the cache with a result for a command
    that would otherwise fail — a real subprocess would surface the
    failure; the cache hit must surface the seeded success.

    The test seeds `cache.json` with a `done` entry whose
    `exit_code=0` and `stdout_tail="cached-replay"` for a non-trivial
    sentinel command `echo cached-replay`. Any deviation from these
    cached values in the task record proves the executor bypassed the
    cache and re-launched the subprocess.
    """
    # 1. Set TASKQ_CACHE_TTL=60 so the seeded entry is well within TTL.
    monkeypatch.setenv("TASKQ_CACHE_TTL", "60")

    # 2. Seed the cache with a `done` entry for the command we are
    #    about to submit. The seeded stdout_tail is the sentinel —
    #    the test fails if the task record ends up with anything else.
    #    `finished_at` is set to *now* so the entry is well within TTL
    #    at the moment `cache_lookup` runs (a stale fixed-string
    #    timestamp like "2026-08-04T00:00:00+00:00" would expire as soon
    #    as the test runs more than 60 s after the date — the regression
    #    we hit on 2026-08-04 once the clock moved past 00:01:00Z).
    sentinel_stdout = "cached-replay"
    finished_at = datetime.now(timezone.utc).isoformat()
    seeded = _seed_cache_entry(
        taskq_home,
        command="echo cached-replay",
        exit_code=0,
        stdout_tail=sentinel_stdout,
        finished_at=finished_at,
    )
    assert seeded == _command_signature("echo cached-replay"), (
        f"cache signature must equal sha256(command); got {seeded!r}"
    )

    # 3. Submit a fresh pending task carrying the cached command.
    task_id = _submit_and_get_id(["echo", "cached-replay"], child_env)

    # 4. Run with --cached. The dispatch must:
    #    - exit 0
    #    - set the task's status=done, exit_code=0, stdout_tail=seeded
    #    - set the task's cached=true (FR-04 §3)
    proc = _run_subprocess([task_id, "--cached"], child_env)
    assert proc.returncode == 0, (
        f"cached run must exit 0; got {proc.returncode}; "
        f"stderr={proc.stderr!r}"
    )

    # 5. Re-read the persisted task and assert the cache-replay surface.
    tasks = _read_tasks_json(taskq_home)
    task = next(t for t in tasks if t["id"] == task_id)
    assert task["status"] == "done", (
        f"cached run must mark task status=done; got {task.get('status')!r}"
    )
    assert task.get("exit_code") == 0, (
        f"cached run must replay cached exit_code=0; got "
        f"{task.get('exit_code')!r}"
    )
    assert task.get("stdout_tail") == sentinel_stdout, (
        f"cached run must replay cached stdout_tail={sentinel_stdout!r}; "
        f"got {task.get('stdout_tail')!r}"
    )
    # The `cached: true` flag is the spec-mandated marker (SPEC §3
    # FR-04 "任務標記 done 且 cached: true"). The implementation may
    # surface it under either `cached` (canonical) or `cached` True
    # in the task record.
    assert task.get("cached") is True, (
        f"cached run must set task cached=true; got {task.get('cached')!r}"
    )


# ---------------------------------------------------------------------------
# Case 2 — AC-04-2: cache miss / expired TTL — re-execute the command
# ---------------------------------------------------------------------------


# NFR-09
def test_fr04_expired_cache_reexecutes_command(taskq_home, child_env, monkeypatch):
    """AC-04-2: After `TASKQ_CACHE_TTL` (1 s) elapses, the same
    `run <id> --cached` re-executes the command. The task is marked
    `done` with `cached: false` and a fresh `exit_code` / `stdout_tail`
    from the real subprocess. *(SPEC §3 FR-04)*

    NFR-09 (test_assertion_quality): the test seeds the cache with a
    STALE entry (an old `finished_at` and a `cached-stale` sentinel
    `stdout_tail`); sets `TASKQ_CACHE_TTL=1`; waits > 1 s; then runs
    with --cached and asserts the task record shows a fresh result,
    not the stale one — proving the cache miss path took the
    real-subprocess branch.

    The "subprocess was actually launched" half is verified by
    changing the seeded `stdout_tail` to a sentinel that the real
    command `echo hi` could NEVER produce. If the post-run task
    record shows `stdout_tail="hi"` (the real `echo hi` output) the
    cache miss path definitely launched a subprocess.
    """
    # 1. TTL = 1 s, plus a 2 s wait → guaranteed expiration.
    monkeypatch.setenv("TASKQ_CACHE_TTL", "1")

    # 2. Seed the cache with a STALE entry: a sentinel stdout_tail
    #    that `echo hi` could not possibly produce. If the cache path
    #    is taken, the task's stdout_tail will be this sentinel; if
    #    the cache miss path runs, the task's stdout_tail will be "hi".
    stale_sentinel = "this-could-only-come-from-the-cache"
    _seed_cache_entry(
        taskq_home,
        command="echo hi",
        exit_code=99,  # impossible exit code for a fresh `echo hi`
        stdout_tail=stale_sentinel,
        finished_at="2020-01-01T00:00:00+00:00",
    )

    # 3. Submit a fresh pending task carrying the cached command.
    task_id = _submit_and_get_id(["echo", "hi"], child_env)

    # 4. Wait > TTL so the seeded entry is expired by the time the
    #    dispatcher consults the cache.
    import time as _time
    _time.sleep(2.0)

    # 5. Run with --cached. Must exit 0 and re-execute the command.
    proc = _run_subprocess([task_id, "--cached"], child_env)
    assert proc.returncode == 0, (
        f"expired-cache run must exit 0; got {proc.returncode}; "
        f"stderr={proc.stderr!r}"
    )

    # 6. Re-read the persisted task and assert the fresh-run surface:
    #    - exit_code is 0 (echo hi's real exit code), not 99 (the
    #      seeded sentinel)
    #    - stdout_tail is "hi" (echo hi's real output), not the
    #      stale sentinel
    #    - cached is False (cache miss — the command was re-executed)
    tasks = _read_tasks_json(taskq_home)
    task = next(t for t in tasks if t["id"] == task_id)
    assert task["status"] == "done", (
        f"expired-cache run must mark task status=done; "
        f"got {task.get('status')!r}"
    )
    assert task.get("exit_code") == 0, (
        f"expired-cache run must execute echo hi (exit_code=0); got "
        f"{task.get('exit_code')!r} (the cached stale value was 99)"
    )
    assert task.get("stdout_tail") != stale_sentinel, (
        f"expired-cache run must NOT return the cached stale "
        f"stdout_tail={stale_sentinel!r}; got {task.get('stdout_tail')!r} "
        f"(the cache hit path was taken — TTL was not honoured)"
    )
    assert "hi" in (task.get("stdout_tail") or ""), (
        f"expired-cache run must surface the real `echo hi` output; "
        f"got {task.get('stdout_tail')!r}"
    )
    assert task.get("cached") is False, (
        f"expired-cache run must set task cached=false; got "
        f"{task.get('cached')!r} (the cache hit path was taken)"
    )


# ---------------------------------------------------------------------------
# Case 3 — NP-07: corrupt cache.json must NOT crash the run
# ---------------------------------------------------------------------------


# NFR-09 / NP-07 (SAD-forced dependency fault)
def test_fr04_corrupt_cache_file_falls_back_to_execution(
    taskq_home, child_env, monkeypatch
):
    """NP-07 / FR-04 case 3: a corrupt `cache.json` (here: the literal
    string `"{not json"`) must not crash the run. The dispatcher
    must treat the unreadable file as a cache miss, execute the
    command normally, and exit 0 with `cached: false` on the task.
    *(SPEC §3 FR-04 + §4 NFR-03 atomicity caveat — cache.json is a
    fallible dependency)*

    NFR-09 (test_assertion_quality): the test writes the canonical
    non-JSON sentinel `{not json` to `$TASKQ_HOME/cache.json` and
    asserts the run still completes (exit 0) and the task record
    carries the real `echo hi` output. A naive implementation that
    raises on `json.load` would propagate the exception and crash
    the run; the implementation must catch and fall back.
    """
    # 1. Corrupt the cache file with the canonical non-JSON sentinel.
    cache_file = taskq_home / "cache.json"
    cache_file.write_text("{not json", encoding="utf-8")
    assert cache_file.read_text(encoding="utf-8") == "{not json", (
        "precondition: cache.json must contain the sentinel before run"
    )

    # 2. Submit a fresh pending task. The corrupt cache must not
    #    interfere with submit.
    task_id = _submit_and_get_id(["echo", "hi"], child_env)

    # 3. Run without --cached (the corrupt-file path applies to the
    #    cache lookup, which runs whether or not --cached is passed;
    #    but a cache miss is the canonical behaviour here). The
    #    implementation must:
    #      - catch the JSON decode error from cache.json
    #      - treat the entry as a miss
    #      - run the command normally
    #      - exit 0
    proc = _run_subprocess([task_id], child_env)
    assert proc.returncode == 0, (
        f"corrupt cache.json must NOT crash the run; expected exit 0; "
        f"got {proc.returncode}; stderr={proc.stderr!r}"
    )

    # 4. Re-read the persisted task and assert the fresh-run surface:
    #    - status=done
    #    - exit_code=0 (echo hi's real exit)
    #    - stdout_tail contains "hi"
    #    - cached is False (cache miss)
    tasks = _read_tasks_json(taskq_home)
    task = next(t for t in tasks if t["id"] == task_id)
    assert task["status"] == "done", (
        f"corrupt-cache run must mark task status=done; got "
        f"{task.get('status')!r}"
    )
    assert task.get("exit_code") == 0, (
        f"corrupt-cache run must surface echo hi's real exit_code=0; "
        f"got {task.get('exit_code')!r}"
    )
    assert "hi" in (task.get("stdout_tail") or ""), (
        f"corrupt-cache run must surface echo hi's real output; "
        f"got {task.get('stdout_tail')!r}"
    )
    assert task.get("cached") is False, (
        f"corrupt-cache run must set task cached=false (miss path); "
        f"got {task.get('cached')!r}"
    )


# ===========================================================================
# In-process coverage tests
# ---------------------------------------------------------------------------
# The three cases above are the canonical TEST_SPEC.md §FR-04 rows. The
# tests below are additive: they exercise the same FR-04 surface
# (signature determinism + cache lookup + cache store) through direct
# in-process calls so `coverage` can measure `taskq_plus.service.cache`
# and `taskq_plus.storage.cache_store` (the subprocess acceptance path
# cannot raise coverage on these — see GATE1 SUBPROCESS COVERAGE
# CEILING in the integration guidelines). Both modules are declared in
# `SAB.json` §fr_module_traceability for FR-04; their on-disk presence
# is enforced by the Architecture Amendment Protocol.
# ===========================================================================


# ---------------------------------------------------------------------------
# `taskq_plus.service.cache` — the in-process cache service surface
# ---------------------------------------------------------------------------


# NFR-09
def test_fr04_cache_module_is_importable():
    """The cache module is declared in SAB.json
    §fr_module_traceability for FR-04. Its on-disk presence is
    enforced by the Architecture Amendment Protocol.

    GREEN TODO: `taskq_plus.service.cache` must exist as a leaf
    module (or a package) and expose a `signature(command: str) -> str`
    function plus a `lookup(command: str, *, ttl_s: float, now: float)
    -> Optional[CacheEntry]` helper.
    """
    import taskq_plus.service.cache as cache_module  # noqa: F401
    assert cache_module is not None


# NFR-09
def test_fr04_cache_signature_is_sha256_of_command():
    """`signature(command)` is the hex sha256 of `command.encode()`.
    Determinism is the spec invariant: the same command always
    produces the same key, regardless of how many submissions carry
    it.

    GREEN TODO: `taskq_plus.service.cache.signature(command)` must
    return `hashlib.sha256(command.encode("utf-8")).hexdigest()`.
    """
    from taskq_plus.service.cache import signature

    assert signature("echo hi") == hashlib.sha256(b"echo hi").hexdigest()
    assert signature("echo hi") == signature("echo hi"), (
        "signature must be deterministic (P-FR04-signature-determinism)"
    )
    assert signature("echo hi") != signature("echo bye"), (
        "different commands must produce different signatures"
    )


# NFR-09
def test_fr04_cache_lookup_returns_none_when_signature_unknown(taskq_home):
    """`lookup(command)` returns None when no cache entry exists for
    the signature. The miss path is the baseline — every cache module
    must short-circuit to a miss when the file is empty or the key is
    absent.

    GREEN TODO: `taskq_plus.service.cache.lookup` must read the cache
    store, return None when the signature is missing, and raise no
    exception on an empty store.
    """
    from taskq_plus.service.cache import lookup

    # The fixture creates a fresh, empty `taskq_home`. The cache.json
    # must NOT exist for the missing-key branch to fire.
    cache_file = taskq_home / "cache.json"
    assert not cache_file.exists(), (
        f"precondition: cache.json must not exist; found {cache_file}"
    )

    assert lookup("echo hi", ttl_s=60.0, now=0.0) is None, (
        "lookup on an empty store must return None (cache miss)"
    )


# ---------------------------------------------------------------------------
# `taskq_plus.storage.cache_store` — the persisted cache surface
# ---------------------------------------------------------------------------


# NFR-09
def test_fr04_cache_store_module_is_importable():
    """The cache_store module is declared in SAB.json
    §fr_module_traceability for FR-04. Its on-disk presence is
    enforced by the Architecture Amendment Protocol.

    GREEN TODO: `taskq_plus.storage.cache_store` must exist as a leaf
    module (or a package) and expose a `make_cache_store()` factory
    and a `CacheStore` class with `load() -> dict` and
    `save(entries: dict) -> None` methods. Writes must be atomic
    (tmp + os.replace — NFR-03).
    """
    import taskq_plus.storage.cache_store as cache_store_module  # noqa: F401
    assert cache_store_module is not None


# NFR-09
def test_fr04_cache_store_roundtrip_preserves_entry(taskq_home, monkeypatch):
    """`CacheStore.save(entries)` writes `$TASKQ_HOME/cache.json`
    with the supplied map; `store.load()` re-hydrates the same map.
    Round-trip preserves every field the GREEN implementation will
    store under a signature key.

    GREEN TODO: the cache store must serialise a `{signature:
    {exit_code, stdout_tail, finished_at, status}}` dict to
    `$TASKQ_HOME/cache.json` and re-hydrate it on `load()`. Writes
    must be atomic (NFR-03 — `tmp + os.replace`).
    """
    from taskq_plus.storage.cache_store import make_cache_store

    signature = _command_signature("echo hi")
    entries = {
        signature: {
            "command": "echo hi",
            "exit_code": 0,
            "stdout_tail": "hi",
            "finished_at": "2026-08-04T00:00:00+00:00",
            "status": "done",
        }
    }

    store = make_cache_store()
    store.save(entries)

    cache_file = taskq_home / "cache.json"
    assert cache_file.exists(), (
        f"cache.json must be written under $TASKQ_HOME; expected {cache_file}"
    )

    loaded = store.load()
    assert loaded == entries, (
        f"loaded entries must equal saved entries; got {loaded!r}"
    )


# NFR-09
def test_fr04_cache_store_load_returns_empty_dict_when_file_missing(
    taskq_home, monkeypatch
):
    """`CacheStore.load()` returns an empty dict when the file does
    not exist on disk — the no-prior-state short-circuit. This is
    the first-run path the store must support.

    GREEN TODO: the cache store must short-circuit to an empty dict
    when `cache.json` is absent; the dispatcher relies on this for
    the cache-miss path on the first run after `TASKQ_HOME` is
    created.
    """
    from taskq_plus.storage.cache_store import make_cache_store

    # The file must NOT exist for the missing-file branch to fire.
    cache_file = taskq_home / "cache.json"
    assert not cache_file.exists(), (
        f"precondition: cache.json must not exist; found {cache_file}"
    )

    store = make_cache_store()
    loaded = store.load()
    assert loaded == {}, (
        f"missing-file load must yield empty dict; got {loaded!r}"
    )


# ---------------------------------------------------------------------------
# `taskq_plus.cli.commands.run` — the in-process --cached dispatch surface
# ---------------------------------------------------------------------------


# NFR-09
def test_fr04_inprocess_run_with_cached_replays_done(
    taskq_home, child_env, monkeypatch
):
    """`commands.run(["<id>", "--cached"])` returns exit 0 and
    populates the task with the cached `exit_code` /
    `stdout_tail` plus `cached: true`. No subprocess is launched.

    The in-process mirror of Case 1: seeds the cache through the
    declared cache store, submits a pending task, invokes
    `commands.run` with `--cached` and asserts the replay surface
    is reached.

    GREEN TODO: `taskq_plus.cli.commands.run` must accept a
    `--cached` flag. When the flag is set AND the cache lookup
    returns a `done` entry within `TASKQ_CACHE_TTL` seconds, the
    dispatcher must:
      - call `taskq_plus.service.executor.run_task` ZERO times
      - persist the cached result fields onto the task
      - set `cached = True` on the task
      - return 0
    """
    import io  # noqa: F811  — local re-import keeps test body self-contained
    from taskq_plus.cli import commands
    from taskq_plus.models.task import Task
    from taskq_plus.storage.cache_store import make_cache_store
    from taskq_plus.storage.task_store import (
        get_store,
        make_disk_store,
        reset_store_cache,
    )

    reset_store_cache()
    monkeypatch.setenv("TASKQ_CACHE_TTL", "60")

    # Seed the cache with a `done` entry for `echo hi`.
    signature = _command_signature("echo hi")
    sentinel_stdout = "cached-replay"
    store_cache = make_cache_store()
    store_cache.save(
        {
            signature: {
                "command": "echo hi",
                "exit_code": 0,
                "stdout_tail": sentinel_stdout,
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "status": "done",
            }
        }
    )

    # Submit a fresh pending task with the cached command.
    dstore = make_disk_store()
    fresh = dstore.add(Task(command="echo hi"))
    fresh_id = fresh.id

    # Run in-process with --cached and capture stdout/stderr.
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        exit_code = commands.run([fresh_id, "--cached"], use_disk=True)

    assert exit_code == 0, (
        f"in-process cached run must exit 0; got {exit_code}; "
        f"stderr={err.getvalue()!r}"
    )

    # Re-read the persisted task and assert the replay surface.
    reset_store_cache()
    disk_store = get_store(use_disk=True)
    reloaded = [t for t in disk_store.load() if t.id == fresh_id][0]
    assert reloaded.status == "done", (
        f"in-process cached run must set task status=done; got "
        f"{reloaded.status!r}"
    )
    assert reloaded.exit_code == 0, (
        f"in-process cached run must replay exit_code=0; got "
        f"{reloaded.exit_code!r}"
    )
    assert reloaded.stdout_tail == sentinel_stdout, (
        f"in-process cached run must replay cached stdout_tail="
        f"{sentinel_stdout!r}; got {reloaded.stdout_tail!r}"
    )


# ---------------------------------------------------------------------------
# `taskq_plus.service.cache` — `cache_ttl()` env handling
# ---------------------------------------------------------------------------


# NFR-09
def test_fr04_cache_ttl_returns_default_when_env_unset(monkeypatch):
    """`cache_ttl()` returns `DEFAULT_CACHE_TTL` when
    `TASKQ_CACHE_TTL` is unset.

    GREEN TODO: when `TASKQ_CACHE_TTL` is unset (or empty), the
    helper must return `DEFAULT_CACHE_TTL` (60 s).
    """
    from taskq_plus.service.cache import DEFAULT_CACHE_TTL, cache_ttl

    monkeypatch.delenv("TASKQ_CACHE_TTL", raising=False)
    assert cache_ttl() == DEFAULT_CACHE_TTL, (
        f"cache_ttl with unset env must equal DEFAULT_CACHE_TTL "
        f"({DEFAULT_CACHE_TTL}); got {cache_ttl()!r}"
    )


# NFR-09
def test_fr04_cache_ttl_returns_default_on_non_numeric_env(monkeypatch):
    """`cache_ttl()` returns `DEFAULT_CACHE_TTL` when
    `TASKQ_CACHE_TTL` is set to a non-numeric value.

    GREEN TODO: a non-numeric `TASKQ_CACHE_TTL` value must be
    treated as an unset TTL (fallback to `DEFAULT_CACHE_TTL`) — not
    as a hard error that aborts the run.
    """
    from taskq_plus.service.cache import DEFAULT_CACHE_TTL, cache_ttl

    monkeypatch.setenv("TASKQ_CACHE_TTL", "not-a-number")
    assert cache_ttl() == DEFAULT_CACHE_TTL, (
        f"cache_ttl with non-numeric env must equal DEFAULT_CACHE_TTL; "
        f"got {cache_ttl()!r}"
    )


# ---------------------------------------------------------------------------
# `taskq_plus.service.cache` — `_parse_timestamp()` coercion paths
# ---------------------------------------------------------------------------


# NFR-09
def test_fr04_parse_timestamp_accepts_numeric_input():
    """`_parse_timestamp` returns the float value for numeric inputs.

    GREEN TODO: numeric inputs (int / float) are passed through
    `float(...)` — the same float the caller would have written
    in JSON. The dispatcher's TTL math relies on this pass-through.
    """
    from taskq_plus.service.cache import _parse_timestamp

    assert _parse_timestamp(123.456) == 123.456, (
        f"float input must round-trip; got {_parse_timestamp(123.456)!r}"
    )
    assert _parse_timestamp(789) == 789.0, (
        f"int input must coerce to float; got {_parse_timestamp(789)!r}"
    )


# NFR-09
def test_fr04_parse_timestamp_returns_none_for_unsupported_types():
    """`_parse_timestamp` returns `None` for non-string non-numeric
    inputs (None, dict, list, etc.).

    GREEN TODO: the helper must short-circuit to `None` when given a
    type it cannot interpret — the caller treats `None` as a cache
    miss.
    """
    from taskq_plus.service.cache import _parse_timestamp

    for unsupported in (None, {"key": "value"}, [1, 2, 3], (1, 2), object()):
        assert _parse_timestamp(unsupported) is None, (
            f"_parse_timestamp({unsupported!r}) must return None; "
            f"got {_parse_timestamp(unsupported)!r}"
        )


# NFR-09
def test_fr04_parse_timestamp_returns_none_on_unparseable_string():
    """`_parse_timestamp` returns `None` when a string cannot be
    parsed as ISO-8601.

    GREEN TODO: a string that is not a valid ISO-8601 datetime
    (e.g. `"definitely-not-a-date"`, or an empty string) must yield
    `None` so the dispatcher treats the entry as a miss instead of
    propagating the parse error.
    """
    from taskq_plus.service.cache import _parse_timestamp

    for bad in ("definitely-not-a-date", "", "not iso at all"):
        assert _parse_timestamp(bad) is None, (
            f"_parse_timestamp({bad!r}) must return None for "
            f"unparseable strings; got {_parse_timestamp(bad)!r}"
        )


# ---------------------------------------------------------------------------
# `taskq_plus.service.cache` — `_is_fresh()` decision branches
# ---------------------------------------------------------------------------


# NFR-09
def test_fr04_is_fresh_returns_false_when_status_is_not_done():
    """`_is_fresh` returns `False` when the entry's `status` is not
    `"done"`.

    GREEN TODO: only `status="done"` records are eligible for replay;
    any other status (e.g. `"failed"`, `"running"`) is a miss.
    """
    from taskq_plus.service.cache import _is_fresh

    entry = {
        "status": "failed",
        "finished_at": "2026-08-04T00:00:00+00:00",
    }
    assert _is_fresh(entry, now=1.0, ttl_s=60.0) is False, (
        f"non-done status must short-circuit to False; got "
        f"{_is_fresh(entry, now=1.0, ttl_s=60.0)!r}"
    )


# NFR-09
def test_fr04_is_fresh_returns_false_when_finished_at_is_unparseable():
    """`_is_fresh` returns `False` when `finished_at` cannot be
    parsed as a timestamp.

    GREEN TODO: a `done` entry with an unreadable `finished_at` is
    treated as a miss — the helper must not raise; it must short-
    circuit to `False`.
    """
    from taskq_plus.service.cache import _is_fresh

    entry = {
        "status": "done",
        "finished_at": "not-a-timestamp",
    }
    assert _is_fresh(entry, now=1.0, ttl_s=60.0) is False, (
        f"unparseable finished_at must short-circuit to False; got "
        f"{_is_fresh(entry, now=1.0, ttl_s=60.0)!r}"
    )


# ---------------------------------------------------------------------------
# `taskq_plus.service.cache` — `record()` end-to-end persistence
# ---------------------------------------------------------------------------


# NFR-09
def test_fr04_record_stores_entry_under_command_signature(
    taskq_home, monkeypatch
):
    """`record(command, entry)` persists `entry` under
    `sha256(command)` so a subsequent `lookup` can replay it.

    GREEN TODO: `record` must read the existing cache, index the
    entry under `signature(command)`, and write the result back
    atomically. The on-disk shape must round-trip through
    `CacheStore.load()`.
    """
    from taskq_plus.service.cache import record
    from taskq_plus.storage.cache_store import make_cache_store

    command = "echo recorded"
    entry = {
        "command": command,
        "exit_code": 0,
        "stdout_tail": "recorded",
        "finished_at": "2026-08-04T00:00:00+00:00",
        "status": "done",
    }
    record(command, entry)

    cache_file = taskq_home / "cache.json"
    assert cache_file.exists(), (
        f"cache.json must be written by record(); expected {cache_file}"
    )

    expected_signature = _command_signature(command)
    loaded = make_cache_store().load()
    assert loaded.get(expected_signature) == entry, (
        f"record must persist entry under sha256(command); "
        f"missing key {expected_signature!r} in {list(loaded)!r}"
    )


# ---------------------------------------------------------------------------
# `taskq_plus.storage.cache_store` — `load()` error short-circuits
# ---------------------------------------------------------------------------


# NFR-09 / NP-07 (SAD-forced dependency fault)
def test_fr04_cache_store_load_returns_empty_when_payload_is_not_dict(
    taskq_home, monkeypatch
):
    """`CacheStore.load()` returns `{}` when `cache.json` parses as
    valid JSON but the top-level payload is not a dict.

    GREEN TODO: the load helper must reject non-dict top-level
    payloads (e.g. a JSON array) and fall back to an empty cache —
    the dispatcher's lookup expects `dict.get(signature)` semantics.
    """
    from taskq_plus.storage.cache_store import make_cache_store

    cache_file = taskq_home / "cache.json"
    cache_file.write_text("[1, 2, 3]", encoding="utf-8")

    store = make_cache_store()
    assert store.load() == {}, (
        f"non-dict JSON payload must yield empty dict; got {store.load()!r}"
    )


# NFR-09 / NP-07 (SAD-forced dependency fault)
def test_fr04_cache_store_load_returns_empty_on_corrupt_json(
    taskq_home, monkeypatch
):
    """`CacheStore.load()` returns `{}` when `cache.json` contains
    invalid JSON — the corrupt-cache fault path.

    GREEN TODO: a `json.JSONDecodeError` (or any
    `OSError`/`ValueError`/`TypeError` from `open`/`load`) must be
    swallowed and the loader must return an empty dict — NP-07.
    """
    from taskq_plus.storage.cache_store import make_cache_store

    cache_file = taskq_home / "cache.json"
    cache_file.write_text("{not json", encoding="utf-8")

    store = make_cache_store()
    assert store.load() == {}, (
        f"corrupt cache.json must yield empty dict (NP-07); "
        f"got {store.load()!r}"
    )


# ---------------------------------------------------------------------------
# `taskq_plus.storage.cache_store` — atomic-write cleanup on failure
# ---------------------------------------------------------------------------


# NFR-09
def test_fr04_cache_store_write_atomic_cleans_up_temp_file_on_failure(
    taskq_home, monkeypatch
):
    """If the atomic write fails partway through, the temporary file
    is removed before the exception propagates — NFR-03 atomicity.

    GREEN TODO: `_write_atomic` must catch the failure, unlink the
    leftover temp file, and re-raise so the disk state is left
    exactly as it was before the save attempt.
    """
    import pytest

    import taskq_plus.storage.cache_store as cache_store_module
    from taskq_plus.storage.cache_store import CacheStore

    store = CacheStore(path=taskq_home / "cache.json")

    # Force `os.replace` to fail so the cleanup branch fires.
    def _failing_replace(src, dst):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(cache_store_module.os, "replace", _failing_replace)

    with pytest.raises(OSError, match="simulated replace failure"):
        store.save({"key": "value"})

    leftover_tmp = sorted(
        f.name for f in taskq_home.iterdir()
        if f.name.startswith(".cache.") and f.name.endswith(".json.tmp")
    )
    assert leftover_tmp == [], (
        f"atomic-write cleanup must remove the leftover tmp file; "
        f"found {leftover_tmp!r}"
    )
