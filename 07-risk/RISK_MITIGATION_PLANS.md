# 07-risk/RISK_MITIGATION_PLANS.md — taskq Mitigation Plans (HIGH risks)

> **Phase 7 Mitigation Plans** • Generated: 2026-08-05
> Scope: ONLY HIGH risks (Likelihood × Impact ≥ 9) per SPEC §9 and Gate 3/4 review.
>
> **Severity band policy**: HIGH = L×I ≥ 9 (operational definition in
> `RISK_REGISTER.md §6`). This document freezes a formal mitigation plan — with
> named owner, deadline, and verifiable success criterion — for every HIGH risk
> surfaced by the Phase 7 risk author.
>
> **Sources**: `RISK_REGISTER.md §2-3`; SPEC.md §9; `gate3_result.json`;
> `gate4_result.json`; `bug_hunt_report.json`; `state.json`.
>
> Five HIGH risks seeded from the canonical register:
> - **R1** Concurrent writes corrupt `tasks.json` (L=3, I=5, S=15)
> - **R2** Subprocess hangs / zombie tasks (L=3, I=3, S=9)
> - **R5** Secrets leak to disk (L=3, I=5, S=15)
> - **R6** Plugin loader becomes arbitrary-code-execution entrypoint (L=3, I=5, S=15)
> - **R8** Plugin exception breaks main flow (L=3, I=3, S=9)
>
> MEDIUM and LOW risks do **not** appear here; they are tracked in the register
> and status report only. Each HIGH plan links to a **verifiable success
> criterion** and a concrete deadline so this document is operable, not ceremonial.

---

## Plan format (template applied to every HIGH risk)

```
1. Risk
2. Why HIGH
3. Current controls (what is already in place)
4. Owner
5. Deadline
6. Action items
7. Success criterion (verifiable)
8. Verification artefact (file/test/gate)
9. Status update cadence
```

---

## R1 — Concurrent writes corrupt `tasks.json` (S=15)

### 1. Risk
Two `taskq submit` invocations writing `$TASKQ_HOME/tasks.json` at the same
time, or a CLI command reading while a write is mid-flight, race on the JSON
file and produce a corrupt (truncated/garbled) tasks store.

### 2. Why HIGH (L=3, I=5)
- **L=3**: Medium likelihood. CLI is single-process per default, but
  `concurrent.futures.ThreadPoolExecutor` (SPEC §2) and the
  `taskq run --all` fan-out create concurrent writer paths in the same
  process. Concurrent shell sessions are a documented usage.
- **I=5**: Catastrophic. Corrupt tasks.json ⇒ total queue loss ⇒ user loses
  every submitted task.

### 3. Current controls
- `taskq_plus.storage.atomic.atomic_write_json` does **tmp + `os.replace`**
  (POSIX-atomic on same filesystem) — this is the file-system-level guarantee.
- `taskq_plus.storage.task_store.TaskStore` wraps a module-level
  `threading.Lock` around `load → mutate → save` for the in-process critical
  section.
- Cross-process safety relies on the atomic replace; that requires the OS to
  hold an exclusive write lock on the rename target for the brief moment of
  `os.replace`.

### 4. Owner
`executor-lead` (storage layer owner; storage/atomic.py + storage/task_store.py).

### 5. Deadline
Hard-merged by **Gate 5 close-out** (next gate after Phase 7 finalises); target
document version `RISK_STATUS_REPORT.md` row R1 to read `status=Closed`.

### 6. Action items
| # | Action | Status |
|---|--------|--------|
| A1 | Lock-protected critical section in `TaskStore.add` / `.update` (already shipped). | DONE (verified by Gate 2/Gate 3 FR-02 sentinels) |
| A2 | Stress test: 32 concurrent `add()` calls into the same `TaskStore` instance — assert zero data loss and zero JSON parse errors. | DONE (covered by `test_atomic_exceptions.py`) |
| A3 | Cross-process stress test: 16 subprocesses concurrently write to one `$TASKQ_HOME/tasks.json` and read back; assert each load returns JSON-valid. | **TODO (this plan)** |
| A4 | Lockfile fallback (`fcntl.flock` on Linux, `msvcrt`/`portalocker` portable) for multi-host/project remote-fs usage. | DEFERRED — out of scope for current single-host deployment; tracked in R11/R12 register only. |

### 7. Success criterion (verifiable)
- `pytest 03-development/tests/test_storage_task_store_concurrent_writers.py`
  (new file) — N=32 threads × N=16 subprocesses — passes.
- `make verify-storage` exit 0 with output `verify-storage: PASS`.
- Gate 5 final re-run reports `task_coverage` 100% unchanged and `mutation_testing`
  score ≥ 70 (no regression introduced by the new test).

### 8. Verification artefact
- `03-development/tests/test_storage_task_store_concurrent_writers.py` (new).
- Update `TEST_INVENTORY.yaml` to register the new test.
- Update `.methodology/gate_results/gate5_*.json` trace after Gate 5.

### 9. Status update cadence
Owner reports to `RISK_STATUS_REPORT.md` after every Gate 5 milestone.

---

## R2 — Subprocess hangs / zombie tasks (S=9)

### 1. Risk
A user-submitted command blocks indefinitely (e.g. `cat` on a pipe that never
produces output, a runaway shell-script loop, a network call without
deadline). The executor thread blocks forever, the CLI never returns, and the
task stays in `running` forever.

### 2. Why HIGH (L=3, I=3)
- **L=3**: Easy to trigger with benign-looking commands.
- **I=3**: Worker thread blocks (significant) but not data-loss.

### 3. Current controls
- `taskq_plus.service.executor.run_task` calls
  `subprocess.run(..., capture_output=True, text=True, timeout=TASKQ_TASK_TIMEOUT)`.
  On `TimeoutExpired` the state machine transitions to `timeout` (FR-02).
- `TASKQ_TASK_TIMEOUT` defaults to 60s (configurable).
- FR-02 tests cover: success, non-zero exit, `TimeoutExpired` → state `timeout`.

### 4. Owner
`executor-lead`.

### 5. Deadline
Already mitigated at Gate 3. **Residual action** (timeout-budget enforcement at
CLI orchestrator) due before Phase 8 close-out: **P8-exit**.

### 6. Action items
| # | Action | Status |
|---|--------|--------|
| B1 | `subprocess.run(..., timeout=…)` mandatory; no path uses `shell=True` (SPEC §2). | DONE; bandit reports 0 HIGH/MEDIUM. |
| B2 | `TimeoutExpired` → state `timeout` + persist stdout/stderr tails. | DONE; covered by `test_fr02_inprocess*.py`. |
| B3 | Wall-clock budget on the CLI orchestrator (`taskq run --all`) so a hung DAG cannot stretch beyond `TASKQ_RUN_BUDGET × num_tasks`. | DEFERRED — tracked here as residual. |

### 7. Success criterion (verifiable)
- A test that spawns `cat | head` on a closed stdin pipe (forces `subprocess`
  to block forever) returns within `TASKQ_TASK_TIMEOUT + ε` seconds; the task
  state ends as `timeout`.
- `pytest 03-development/tests/test_fr02_timeout.py -k timeout_writes_state`
  passes deterministically (no flakiness > 1%).

### 8. Verification artefact
- `03-development/tests/test_fr02.py::test_fr02_*timeout*` (existing).
- New `03-development/tests/test_subprocess_hang_kill.py` (zombie-kill path).

### 9. Status update cadence
Owner reports to `RISK_STATUS_REPORT.md` after every Gate 5 milestone.

---

## R5 — Secrets leak to disk (S=15)

> **Status of this risk class**: the two NFR-04 high-severity findings from
> `bug_hunt_report.json` (`tasks_cache_audit_redact#1`,
> `plugins_audit_redact#1`) are **resolved** via fix_commit `affd223d`. The
> mitigation plan below is the **forward-defence plan** that keeps R5 closed
> against regressions.

### 1. Risk
A user submits a task whose command, stdout, stderr, or plugin-emitted
exception contains a secret (e.g. `sk-…`, `token=…`, `Bearer …`). Without
redaction, the secret lands verbatim in:
- `tasks.json` (via `TaskStore.update`)
- `cache.json` (via `cache_record`)
- `audit.jsonl` (via `audit.append_event` **and** the old
  `plugins.append_audit_event` write path)

### 2. Why HIGH (L=3, I=5)
- **L=3**: Trivial to trigger with a one-line command that echoes an env var.
- **I=5**: Catastrophic — long-lived disk-resident credential leak;
  `gitleaks` and `bandit` **cannot** see application-internal disk state.

### 3. Current controls
- Single redaction function `_REDACTION_RE.sub` lives in
  `taskq_plus.observability.audit._redact`.
- `audit.append_event` applies redaction on every event dict.
- After `affd223d`: `commands._persist_result`, the cache-hit branch, and
  `cache_record` also wrap `stdout_tail` / `stderr_tail` through `_redact`.
- After `affd223d`: `plugins.append_audit_event` applies `_redact` to every
  event value before serialising.
- `test_hunt_nfr04_write_through.py` (new) drives each production write path
  with a known secret and asserts zero matches on `grep -E 'sk-[A-Za-z0-9_-]{8,}|token=\S+|Bearer\s+\S+'`.

### 4. Owner
`security-lead`.

### 5. Deadline
**Closed** as of `affd223d`. Reopens automatically on any regression in
`test_nfr04_*` or `test_hunt_nfr04_*`. Next re-verification at Gate 5.

### 6. Action items
| # | Action | Status |
|---|--------|--------|
| C1 | Centralise redaction in `audit._redact`; only export through `audit`. | DONE |
| C2 | Wrap `stdout_tail` / `stderr_tail` in every `_persist_result` and `cache_record` call site. | DONE (affd223d) |
| C3 | Apply `_redact` to every value of the event dict in `plugins.append_audit_event`. | DONE (affd223d) |
| C4 | Grep gate: re-run `grep -c "sk-" $TASKQ_HOME/audit.jsonl` after the secret-write test; assert 0. | DONE |
| C5 | CI hook: re-run `grep -E '_redact' src/.../commands.py src/.../plugins.py src/.../cache.py` after every PR touching those files. | **TODO — added by this plan.** |

### 7. Success criterion (verifiable)
- `test_hunt_nfr04_write_through.py` passes (commands, cache, plugins).
- `grep -c "sk-" $TASKQ_HOME/audit.jsonl` returns 0 after running a fixture
  task whose stdout is `sk-ABCD1234efgh5678`.
- Gate 5 final verdict for `security` dim ≥ 95 (current 98).
- Gate 5 final verdict for `secrets_scanning` dim = 100 (current 100).

### 8. Verification artefact
- `03-development/tests/test_hunt_nfr04_write_through.py` (existing).
- New `03-development/tests/test_nfr04_*_regression_ci.py` (CI hook test).

### 9. Status update cadence
Owner reports on every PR touching `commands.py`, `plugins.py`, `cache.py`,
or `audit.py`. Auto-reopen on failure of `test_nfr04_*`.

---

## R6 — Plugin loader becomes arbitrary-code-execution entrypoint (S=15)

### 1. Risk
`service.plugins` loads user-named modules (FR-07). If the loader ever accepts
a path, accepts `eval`/`exec`, or auto-discovers from cwd, an attacker (or
accidentally committed file) installs remote code execution on every `taskq`
run.

### 2. Why HIGH (L=3, I=5)
- **L=3**: The plugin surface is small and audited; default config is safe.
  But a single misapplied patch (e.g. `importlib.import_module(name)` instead
  of `importlib.import_module("taskq_plus.plugins." + name)`) opens the door.
- **I=5**: Catastrophic — silent remote code execution with the user's
  privileges.

### 3. Current controls
- `Plugins` registry uses an **allowlist** (a frozen set of names baked into
  the build).
- Each name passes through a **regex whitelist** before resolution.
- `subprocess.run(..., shell=True)` is **forbidden** in plugin code paths
  (SPEC §2, NFR-02).
- `importlib.import_module` is restricted to a fixed prefix
  (`taskq_plus.plugins.`).
- Bandit tripwire: 0 HIGH, 0 MEDIUM (Gate 4); 2 LOW (B404/B603 in
  `service/executor.py`) — both explicitly justified.

### 4. Owner
`plugin-lead`.

### 5. Deadline
Already mitigated at Gate 3. **Residual action** (lock-down CI hook on
plugin-shape drift) due before Phase 8 close-out: **P8-exit**.

### 6. Action items
| # | Action | Status |
|---|--------|--------|
| D1 | Allowlist + regex whitelist + banned-prefix check on every `importlib.import_module` call site. | DONE |
| D2 | Static-analysis unit test asserting every plugin import path begins with `taskq_plus.plugins.`. | DONE (`test_fr07_*`) |
| D3 | CI hook: re-run `ast` scan on every `service/plugins.py` edit; reject commits introducing `eval(`, `exec(`, or `__import__(`. | **TODO — added by this plan.** |

### 7. Success criterion (verifiable)
- `grep -RE "eval\(|exec\(|__import__\(" src/.../service/plugins.py` returns
  nothing.
- `test_fr07_*` all pass.
- Bandit `B102` (use of `exec`) and `B307` (use of `eval`) — 0 hits.
- Gate 5 `security` dim ≥ 95.

### 8. Verification artefact
- Existing `03-development/tests/test_fr07.py`.
- New `03-development/tests/test_plugin_loader_lockdown.py` (forbidden-API
  regex tripwire test).

### 9. Status update cadence
Owner reports on every PR touching `service/plugins.py`.

---

## R8 — Plugin exception breaks main flow (S=9)

### 1. Risk
A plugin hook throws an uncaught exception. If the exception propagates out of
the hook context back into `taskq run`, the executor aborts mid-DAG and the
rest of the user's queue is left running with stale state.

### 2. Why HIGH (L=3, I=3)
- **L=3**: Plugins are third-party by definition; failure modes are unbounded.
- **I=3**: Mid-DAG abort is recoverable but disruptive.

### 3. Current controls
- Each hook call is wrapped in a `try/except Exception` that records the
  failure in audit and emits a `BreakerError`-style typed exception at the
  boundary (FR-07 contract).
- Consecutive-failure counter on the plugin — when the counter exceeds the
  threshold, the plugin is auto-disabled for the remainder of the session
  (SPEC §3 FR-07).
- Plugin exceptions are **isolated**; the executor never sees raw plugin
  exceptions.

### 4. Owner
`plugin-lead`.

### 5. Deadline
Already mitigated at Gate 3. **Residual**: confirm auto-disable threshold is
sane at scale (P8-exit).

### 6. Action items
| # | Action | Status |
|---|--------|--------|
| E1 | `try/except Exception` around every hook call site. | DONE |
| E2 | Consecutive-failure counter; auto-disable on threshold. | DONE |
| E3 | Auto-disable threshold review under a 1000-task synthetic load. | **TODO — added by this plan.** |

### 7. Success criterion (verifiable)
- Synthetic load: a plugin that raises on every hook does not abort the
  executor past its first 3 invocations (threshold).
- After auto-disable, `taskq run` continues processing the remaining tasks
  to completion; audit.jsonl records the auto-disable event.
- `pytest 03-development/tests/test_fr07_plugin_auto_disable.py` passes.

### 8. Verification artefact
- New `03-development/tests/test_fr07_plugin_auto_disable.py`.

### 9. Status update cadence
Owner reports on every release; re-evaluate threshold if the executor changes.

---

## Cross-cutting controls (apply to all HIGH risks)

| Control | Covers | Artefact |
|---------|--------|----------|
| Bandit HIGH/MEDIUM = 0 gate | R5, R6, R8 | `gate4_result.json::security` |
| gitleaks 0 leaks gate | R5 | `gate4_result.json::secrets_scanning` |
| Mutation score ≥ 70 gate | R1, R2, R5, R6, R8 | `gate4_result.json::mutation_testing` |
| FR-02 sentinel + FR-07 sentinel | R2, R6, R8 | `.sessi-work/sentinels/` |
| Test-coverage 100% on `taskq_plus.service.executor`, `service.plugins`, `storage.task_store` | R1, R2, R5, R6, R8 | `gate4_result.json::test_coverage` |

---

## Why no other plans

- **R3 (LOW)**, **R7 (LOW)**, **R9 (LOW)**, **R10 (MEDIUM, accepted limitation)**,
  **R11–R15 (MEDIUM/LOW)** are explicitly tracked in `RISK_REGISTER.md` and
  surfaced in `RISK_STATUS_REPORT.md` with their assigned owner and target
  date; they do not warrant a formal mitigation plan until reclassified.

---

*End of RISK_MITIGATION_PLANS.md*
