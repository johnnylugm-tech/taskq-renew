# Architecture Decision Records (ADR) — taskq

> Collection of decision records for the `taskq` Phase 2 architecture.
> Each decision below is derived from `SPEC.md`, `01-requirements/SRS.md`,
> and `02-architecture/SAD.md`. Every entry states Context, Decision,
> Rationale, Consequences, and Alternatives Considered.
>
> Runtime baseline: **CPython 3.11.15** (verified: `.venv/bin/python --version`).

| ADR | Title | Status | Drives FR/NFR |
|-----|-------|--------|--------|
| ADR-001 | Runtime baseline: CPython 3.11 + stdlib-first with two pinned third-party deps | Accepted | NFR-07, NFR-11, NFR-05, NFR-08, NFR-09 |
| ADR-002 | Strict five-layer package hierarchy enforced by `import-linter` | Accepted | NFR-06, NFR-05, NFR-10, NFR-11, NFR-12 |
| ADR-003 | `config` as an independence module | Accepted | NFR-06, NFR-05, NFR-09 |
| ADR-004 | JSON files under `$TASKQ_HOME` as the persistence substrate | Accepted | FR-01, NFR-01, NFR-05, NFR-08, NFR-10 |
| ADR-005 | Atomic write via `tmp + os.replace`; audit via append + `fsync` | Accepted | NFR-03, NFR-05, NFR-08, NFR-09 |
| ADR-006 | `subprocess.run` with `shlex.split` argv; `shell=True` forbidden everywhere | Accepted | NFR-02, FR-02, NFR-05, NFR-09, NFR-10 |
| ADR-007 | `ThreadPoolExecutor` for `run --all` concurrency | Accepted | FR-02, FR-06, NFR-08, NFR-10 |
| ADR-008 | Retry with exponential backoff and an injectable `sleep` | Accepted | FR-03, NFR-05, NFR-09 |
| ADR-009 | Circuit breaker as a persisted global state machine | Accepted | FR-03, NFR-05, NFR-08, NFR-09 |
| ADR-010 | Result cache keyed by `sha256(command)` with TTL | Accepted | FR-04, NFR-05, NFR-08 |
| ADR-011 | Kahn topological sort with cycle detection and a depth cap | Accepted | FR-06, NFR-05, NFR-10 |
| ADR-012 | Plugin loading restricted to an allowlist of module names | Accepted | FR-07, NFR-02, NFR-05, NFR-09 |
| ADR-013 | Redaction at the single audit-write chokepoint | Accepted | NFR-04, NFR-05, NFR-09, NFR-12 |
| ADR-014 | Domain exception hierarchy mapped to documented exit codes | Accepted | NFR-03, §7, NFR-05, NFR-08, NFR-09, NFR-10, NFR-12 |
| ADR-015 | One hub module per layer to satisfy CRG community cohesion | Accepted | NFR-11, NFR-05, NFR-08, NFR-10, NFR-12 |

---

## ADR-001: Runtime baseline — CPython 3.11 + stdlib-first with two pinned third-party dependencies

### Status
Accepted

### Context
The project venv resolves to **CPython 3.11.15**. SPEC.md §1 records that the
previous round shipped with **zero** runtime dependencies, which made the
`license_compliance` quality dimension vacuous — the scanner reported
"19 source files scanned" and returned a perfect score with no signal.
SPEC.md §4 NFR-07 therefore mandates the opposite for this round: introduce
pinned third-party dependencies and require the license scan to cover the
*installed dependency tree*, not only first-party source.

### Decision
Target CPython 3.11 and use the standard library for everything the standard
library already does well — `subprocess`, `concurrent.futures`, `hashlib`,
`importlib`, `threading`, `json`, `os`, `pathlib`, `shlex`. Introduce exactly
**two** third-party runtime dependencies, both pinned with `==` in
`requirements.txt`:

- `click` — CLI command groups (SPEC §2, FR-05)
- `pydantic` v2 — declarative input validation (SPEC §2, FR-01)

Every dependency's license must be in the allowlist
{MIT, BSD-2-Clause, BSD-3-Clause, Apache-2.0}; the SBOM is emitted to
`08-config/SBOM.json`.

### Rationale
`click` and `pydantic` are the two places where hand-rolled stdlib code would
carry real defect risk: argument parsing/exit-code plumbing, and field-level
validation with precise error messages. Everywhere else the stdlib is
sufficient, so additional dependencies would only enlarge the license and
supply-chain surface that NFR-07 is meant to police. Python 3.11 supplies
`ExceptionGroup`, `tomllib`, and the mature `concurrent.futures` used by
ADR-007 without any backport shim.

> **Deviation note (explicit):** an upstream task brief described this system
> as "Python stdlib-only". That contradicts SPEC.md §1/§4 NFR-07 and SAD.md
> §2.2.2/§2.6, which mandate pinned third-party dependencies as a deliberate
> objective of this round. SPEC.md is the source of truth and wins; this ADR
> records the divergence rather than silently resolving it.

### Consequences
- Positive: license scanning has real subjects, so the NFR-07 gate produces
  a genuine signal instead of a vacuous pass.
- Positive: stdlib-first keeps the dependency tree shallow and auditable.
- Negative: dependency pinning must be maintained; a `pydantic` or `click`
  major bump becomes a deliberate migration, not a passive upgrade.
- Negative: `pydantic` v2 pulls a compiled `pydantic-core` wheel, adding a
  platform-specific artifact to the SBOM.

### Alternatives Considered
- **Zero dependencies (`argparse` + hand-written validators).** Rejected: it
  reproduces exactly the previous round's vacuous license dimension, the
  documented failure this round exists to fix.
- **`typer` / `attrs` / `marshmallow` instead.** Rejected: no capability gain
  over `click` + `pydantic`, and each adds transitive licenses to police.
- **Python 3.12/3.13.** Rejected: no feature in this design requires them, and
  the installed interpreter is 3.11.15 — pinning above the actual toolchain
  would make CI unreproducible.

---

## ADR-002: Strict five-layer package hierarchy enforced by `import-linter`

### Status
Accepted

### Context
SPEC.md §6 prescribes the package layout, and NFR-06 requires
`lint-imports` to exit 0. SPEC.md §4 NFR-06 also records the previous-round
gap: `harness/tool_runners.py` returned exit 0 whenever `.importlinter` was
absent, so a 0.25-weight Gate 1 dimension was an unconditional full score.

### Decision
Adopt the strict layered contract
`cli > observability > service > storage > models`, one Python package per
layer, and declare it in `.importlinter` as a `layers` contract. Lower layers
MUST NOT import upper layers. `.importlinter` must exist and `lint-imports`
must exit 0 in CI.

### Rationale
A declared, machine-checked layering makes cycles impossible by construction
and confines each FR to a predictable owner (SAD §2.7). Because the file's
mere presence is what activates the gate, committing `.importlinter` is itself
the fix for the recorded harness gap.

### Consequences
- Positive: no circular imports; module ownership per FR is unambiguous.
- Positive: the 0.25-weight architecture dimension becomes a real check.
- Negative: legitimate upward needs must be solved by dependency injection or
  callbacks rather than a direct import — more ceremony in a few call sites.
- Negative: adding a layer requires editing both `.importlinter` and the SAB
  block in SAD §5.

### Alternatives Considered
- **Flat package with convention-only discipline.** Rejected: this is the
  precise failure mode SAD §2.1 calls out — the flat-package collapse that
  broke the previous test bed.
- **Hexagonal / ports-and-adapters.** Rejected: the port/adapter indirection
  buys nothing for a single-user local CLI with one storage backend, and would
  violate the Simplicity-First constraint.
- **Runtime import guards instead of a static linter.** Rejected: detects
  violations only on executed paths, and adds production code for a build-time
  concern.

---

## ADR-003: `config` as an independence module

### Status
Accepted

### Context
Twelve `TASKQ_*` environment variables (SPEC §5.1) are read by every layer,
including `models` (L1), which by ADR-002 may import nothing internal.

### Decision
Place `taskq_plus/config.py` outside the layer ordering as an *independence*
module: it imports nothing from `taskq_plus`, and any layer may import it. It
exposes typed module-level constants resolved from `os.environ` with the
SPEC §5.1 defaults.

### Rationale
Configuration is a leaf, not a layer. Declaring it independent lets L1 read
env defaults without creating an upward edge, and because it imports no layer
it cannot be used as a back channel to smuggle a dependency past the
`import-linter` contract.

### Consequences
- Positive: one place resolves and documents all twelve env vars; `.env.example`
  has a single counterpart in code.
- Positive: no cycle is reachable through configuration.
- Negative: module-level constants are read at import time, so tests that vary
  env must reload the module or inject values explicitly.

### Alternatives Considered
- **Read `os.environ` inline at each use site.** Rejected: defaults would drift
  and NFR-01/NFR-03 behaviour would become untestable per-variable.
- **Config object threaded through every constructor.** Rejected: constructor
  churn across five layers for a single-process, single-user tool.
- **Make `config` part of L1 `models`.** Rejected: `models` is defined as
  side-effect-free; env reads are a side effect.

---

## ADR-004: JSON files under `$TASKQ_HOME` as the persistence substrate

### Status
Accepted

### Context
The tool stores four artefacts (SPEC §5.2): `tasks.json`, `breaker.json`,
`cache.json`, `audit.jsonl`. NFR-01 budgets p95 < 50 ms for `submit`+`status`
over 100 iterations and < 200 ms for a 200-task topological sort.

### Decision
Persist state as plain JSON documents (and JSON Lines for the audit log) in
`$TASKQ_HOME`, loaded into in-process dicts, written back through
`storage.atomic` (ADR-005). No database engine, no ORM.

### Rationale
Working-set size is bounded by the NFR-01 scenario (200 tasks); whole-file
read/modify/write of a small JSON document is far inside the latency budget.
JSON files are also directly inspectable, which makes the corruption path in
T-06 and the redaction assertion in NFR-04 easy to test by reading bytes.

### Consequences
- Positive: latency budget is met by the storage choice itself, not by
  optimisation work.
- Positive: `clear` is a file deletion; test fixtures are `tmp_path` directories.
- Negative: whole-file rewrite is O(n) per mutation — acceptable at 200 tasks,
  not at 10⁵.
- Negative: cross-process concurrency relies on `os.replace` atomicity only;
  there is no multi-writer transaction, matching the single-user scope.

### Alternatives Considered
- **SQLite.** Rejected: it would solve a concurrency problem the single-user
  scope does not have, while making atomic-write verification (NFR-03) an
  assertion about someone else's WAL rather than about our own code.
- **One file per task.** Rejected: turns `list`/`status` into a directory scan
  and risks breaching the NFR-11 15-files-per-directory rule for data too.
- **`pickle` / `shelve`.** Rejected: opaque on disk, and unpickling untrusted
  files is a code-execution vector directly at odds with NFR-02.

---

## ADR-005: Atomic write via `tmp + os.replace`; audit via append + `fsync`

### Status
Accepted

### Context
NFR-03 requires all four data files to remain valid JSON/JSONL even if the
process is interrupted mid-write. Threat T-06 additionally requires that a
corrupted store be detected rather than silently rebuilt.

### Decision
Concentrate every write in `storage.atomic`, exposing exactly two functions:

- `atomic_write_json(path, payload)` — serialise to a temp file in the same
  directory, then `os.replace` onto the target (atomic rename within a
  filesystem).
- `atomic_append_jsonl(path, record)` — open in append mode, write one line,
  `flush` + `os.fsync`.

No other module opens a data file for writing. Loaders that encounter invalid
JSON raise `StoreCorruptedError` → exit 1; they never rebuild silently.

### Rationale
Full documents (`tasks.json`, `breaker.json`, `cache.json`) need
last-writer-wins replacement, which `os.replace` provides atomically. The audit
log is append-only by definition (FR-08), so rewriting it wholesale would be
both wasteful and a data-loss risk; append + `fsync` gives per-record
durability. Two functions in one module means NFR-03 has exactly one place to
audit and one place to test.

### Consequences
- Positive: a torn write is impossible for the three document files; a crash
  mid-append can lose at most the trailing line, which JSONL readers tolerate.
- Positive: `ast-error-handling` and the atomicity tests target one module.
- Negative: `fsync` per audit record costs a syscall per event — accepted for
  the audit trail's durability requirement.
- Negative: the temp file must live on the same filesystem as the target, so
  `TASKQ_HOME` cannot straddle a mount boundary.

### Alternatives Considered
- **Write in place with `open(..., "w")`.** Rejected: truncates first, so an
  interruption leaves an invalid JSON file — a direct NFR-03 violation.
- **Lock file + in-place write.** Rejected: a lock prevents concurrent writers
  but does nothing about crash-torn content, which is the stated risk.
- **`os.replace` for the audit log too.** Rejected: O(n) rewrite per event and
  a window in which previously durable events are absent from the target path.

---

## ADR-006: `subprocess.run` with `shlex.split` argv; `shell=True` forbidden everywhere

### Status
Accepted

### Context
FR-02 executes user-supplied command strings. SAD §4.2 names this the single
most load-bearing security invariant; threats T-01 and T-02 both terminate
here. SPEC §3 FR-02 fixes the call shape verbatim.

### Decision
Execute every task as:

```python
subprocess.run(
    shlex.split(command),
    capture_output=True, text=True, timeout=TASKQ_TASK_TIMEOUT,
)
```

`shell=True` is forbidden on all paths. Defence in depth: `TaskSubmission`
(pydantic) rejects commands containing any of ``; | & $ > < ` `` with a
1000-character cap; a grep gate over `03-development/src/` for
`shell=True`/`eval(`/`exec(` must return 0 matches; `bandit -r` must report
0 HIGH and 0 MEDIUM.

### Rationale
`shlex.split` produces an argv list, so the operating system never hands the
string to a shell interpreter — metacharacters cannot change the command's
structure. Validation at the model boundary rejects hostile input before it
ever reaches the executor, and the static gates make regressions
build-breaking rather than review-dependent.

### Consequences
- Positive: shell injection is structurally unreachable, not merely filtered.
- Positive: `timeout=` yields a deterministic `TimeoutExpired` → `timeout`
  status → exit 4 in single-task mode.
- Negative: pipes, redirection, and globbing are unavailable to users; that is
  the intended trade and is documented in the `submit` help text.
- Negative: `shlex.split` follows POSIX quoting rules, so Windows-style command
  strings are out of scope.

### Alternatives Considered
- **`shell=True` with escaping/allowlisting.** Rejected: correctness depends on
  an escaping routine being perfect forever; argv removes the class of bug.
- **`os.execve` in a fork.** Rejected: reimplements `subprocess` semantics
  (timeout, capture, cleanup) with no benefit.
- **Container/sandbox per task.** Rejected: out of scope for a local
  single-user CLI and would add a runtime dependency on a container engine.

---

## ADR-007: `ThreadPoolExecutor` for `run --all` concurrency

### Status
Accepted

### Context
FR-02 requires `run --all` to execute all runnable `pending` tasks
concurrently, in DAG topological order (FR-06), with `max_workers` from
`TASKQ_MAX_WORKERS` (default 4), and thread-safe storage writes.

### Decision
Use `concurrent.futures.ThreadPoolExecutor(max_workers=TASKQ_MAX_WORKERS)`.
Dispatch one DAG layer at a time: submit every task in the current layer,
join the layer, then advance. Guard all store mutations with a single shared
`threading.Lock` across `TaskStore`, `BreakerStore`, and `CacheStore`.

### Rationale
The workload is subprocess-bound, so worker threads spend their time blocked
in `subprocess.run` — the GIL is released and threads deliver real parallelism
without the pickling and start-up cost of processes. Layer-at-a-time joining
makes the FR-06 dependency ordering trivially correct: a task starts only
after every predecessor layer has fully completed. One shared lock (rather
than per-store locks) makes lock-ordering deadlock impossible by construction.

### Consequences
- Positive: matches SPEC §2 verbatim; no new dependency.
- Positive: a single global lock removes any deadlock analysis burden.
- Negative: layer-barrier scheduling under-utilises workers when layers are
  uneven — one slow task idles the pool until the layer drains.
- Negative: the shared lock serialises all persistence, so store writes are a
  contention point at high worker counts. Acceptable: writes are small
  in-memory dict updates plus one `os.replace`, and the default is 4 workers.

### Alternatives Considered
- **`ProcessPoolExecutor`.** Rejected: subprocess spawning already runs
  outside the GIL; processes add serialisation cost and make the shared lock
  unusable (it would need a `multiprocessing` manager).
- **`asyncio` + `asyncio.create_subprocess_exec`.** Rejected: would colour the
  entire service layer async for no throughput gain, and complicates the
  injectable-`sleep` seam in ADR-008.
- **Ready-set scheduling (dispatch any task whose deps are done, no barrier).**
  Rejected for round 1: higher utilisation, but the readiness bookkeeping is
  a harder correctness argument than the layer barrier. Revisit if the NFR-01
  `run --all` budget is ever missed.
- **Per-store locks.** Rejected: introduces lock ordering, hence deadlock risk,
  for no measurable gain at this scale.

---

## ADR-008: Retry with exponential backoff and an injectable `sleep`

### Status
Accepted

### Context
FR-03 requires automatic retry of `failed`/`timeout` runs up to
`TASKQ_RETRY_LIMIT` (default 2), waiting `TASKQ_BACKOFF_BASE × 2ⁿ` seconds
before retry *n*, and states that the sleep function must be injectable for
testing. NFR-01 forbids the test suite from becoming slow.

### Decision
Implement retry inside `service.executor`. `Executor` takes a `sleep` callable
(default `time.sleep`) as a constructor parameter. Only after the retry budget
is exhausted and the task is still `failed`/`timeout` does the attempt count as
a *final* failure and reach `Breaker.record_failure()` (ADR-009).

### Rationale
Constructor injection of `sleep` lets tests substitute a recorder that asserts
the exact backoff sequence (`0.1, 0.2, 0.4 …`) while running instantly — this
is what makes FR-03 verifiable rather than timing-dependent. Keeping retry
inside the executor, below the breaker, preserves the SPEC semantics that the
breaker counts *final* outcomes: without this ordering a single flaky task
could trip a 3-threshold breaker on its own.

### Consequences
- Positive: backoff timing is asserted deterministically; no `sleep` in tests.
- Positive: breaker semantics match SPEC §3 FR-03 exactly.
- Negative: worst-case wall time for one task is
  `(retry_limit + 1) × task_timeout + Σ backoff`; users must size
  `TASKQ_TASK_TIMEOUT` with that in mind.
- Negative: the injected `sleep` is a test-visible seam in production
  signatures — a deliberate, documented cost.

### Alternatives Considered
- **Monkeypatch `time.sleep` in tests.** Rejected: global patching leaks across
  concurrent tests (ADR-007 runs tasks on threads) and SPEC explicitly asks
  for an injectable function.
- **Retry at the CLI layer.** Rejected: `run --all` would then retry outside the
  DAG barrier, and the breaker (L3) could not see final outcomes.
- **Jittered backoff.** Rejected: SPEC fixes the formula; jitter would make the
  asserted sequence non-deterministic for no benefit in a local single-user tool.

---

## ADR-009: Circuit breaker as a persisted global state machine

### Status
Accepted

### Context
FR-03 requires a breaker that is global — across tasks *and across processes*
— with states `CLOSED → OPEN → HALF_OPEN → CLOSED/OPEN`, an
`OPEN` fast-fail path (exit 3, `breaker open`, no subprocess), and recovery
within `TASKQ_BREAKER_COOLDOWN` + 1 s (NFR-03).

### Decision
Model the breaker as `service.breaker.Breaker` with the three-method surface
`allow()` / `record_success()` / `record_failure()`, persisting
`{state, failure_count, opened_at}` to `$TASKQ_HOME/breaker.json` through
`storage.breaker_store` (atomic write, ADR-005). `allow()` is consulted before
cache lookup and before any subprocess spawn. The `OPEN → HALF_OPEN`
transition is computed lazily from `now - opened_at >= cooldown` at the next
`allow()` call.

### Rationale
Cross-process globality is only achievable through shared durable state, so
the JSON file is the state machine's real home; the in-memory object is a
view. Deriving `HALF_OPEN` lazily from a timestamp avoids any background timer
thread, which would otherwise have to be created, joined, and shut down per
CLI invocation — a per-process CLI has no place to host one.

### Consequences
- Positive: breaker state survives process exit, satisfying "cross-process".
- Positive: no timer thread, no shutdown path, no clock thread to test.
- Negative: state advances only when someone calls `allow()`; a breaker can sit
  nominally `OPEN` past its cooldown until the next invocation. Harmless — the
  first caller after cooldown is admitted, which is what NFR-03 measures.
- Negative: two concurrent `HALF_OPEN` probes are possible in the window
  between `allow()` calls; the shared lock (ADR-007) narrows this to
  cross-process races only, which the single-user scope tolerates.

### Alternatives Considered
- **In-memory breaker only.** Rejected: fails the explicit "cross-process"
  requirement — every CLI invocation would start `CLOSED`.
- **Background timer thread for the cooldown transition.** Rejected: a
  short-lived CLI process gives the timer nowhere to live.
- **Per-task breakers.** Rejected: SPEC specifies one global breaker;
  per-task counters would never reach the threshold in typical use.

---

## ADR-010: Result cache keyed by `sha256(command)` with TTL

### Status
Accepted

### Context
FR-04: `run <id> --cached` must replay `exit_code`/`stdout_tail` from a prior
`done` execution of the same command signature within `TASKQ_CACHE_TTL`
(default 3600 s) without spawning a subprocess, marking the task `done` with
`cached: true`.

### Decision
Define the cache key as `hashlib.sha256(command.encode()).hexdigest()`. Store
entries in `$TASKQ_HOME/cache.json` via `storage.cache_store`; only `done`
results are cached. `service.cache` owns TTL evaluation (`now - stored_at <
TTL`); expiry is evaluated on read, not swept.

### Rationale
Hashing gives a fixed-length, filesystem- and JSON-safe key from an arbitrary
command string, and equal commands are exactly the replayable unit FR-04
describes. Read-time expiry means no sweeper process and no clock thread —
consistent with ADR-009's lazy-transition reasoning. Caching only `done`
results prevents a transient failure from being replayed as an outcome.

### Consequences
- Positive: replay is a dict lookup plus one timestamp comparison — trivially
  inside the NFR-01 budget.
- Positive: cache correctness is testable by manipulating `stored_at`.
- Negative: the key ignores the environment and working directory, so the same
  command string in a different context replays a stale result. Accepted:
  `--cached` is opt-in per invocation, exactly as SPEC specifies.
- Negative: `cache.json` grows unbounded until `clear`; entries are small and
  `clear` is a documented CLI command.

### Alternatives Considered
- **Cache on task id.** Rejected: FR-04 defines the signature as the command,
  so two tasks with identical commands must share a cache entry.
- **Include cwd/env in the key.** Rejected: over-design beyond FR-04, and it
  would make cache hits nearly unreachable in practice.
- **`md5` for speed.** Rejected: no measurable gain at this volume and it puts
  a broken hash in a `bandit` scan that NFR-02 requires clean.

---

## ADR-011: Kahn topological sort with cycle detection and a depth cap

### Status
Accepted

### Context
FR-06 adds `submit --after <id>` dependencies. Cycles must be rejected at
submit time with exit 5 and a printed cycle path (`A → B → C → A`); dependency
chains deeper than `TASKQ_MAX_DAG_DEPTH` (default 32) must be rejected with
exit 5 to defend against pathological input. NFR-01 budgets < 200 ms for a
200-node sort.

### Decision
Implement `service.dag.schedule(tasks) -> list[list[str]]` as Kahn's
algorithm, emitting *layers* of concurrently runnable ids (consumed directly
by ADR-007). A non-empty residual after the sort proves a cycle; the cycle
path is then recovered by a DFS over the residual and rendered for stderr.
Depth is the layer count, checked against `TASKQ_MAX_DAG_DEPTH`.

### Rationale
Kahn is O(V+E) — roughly 200 nodes here, orders of magnitude inside budget —
and, uniquely among the options, its natural output *is* the layered schedule
the executor needs, so no second pass converts ordering into parallel batches.
Cycle *detection* is free (residual non-empty); the extra DFS runs only on the
error path, where the human-readable path is worth the cost. Depth falls out
of the layer count with no extra traversal.

### Consequences
- Positive: one pass yields ordering, concurrency batches, cycle detection, and
  depth — four FR-06 obligations from one algorithm.
- Positive: rejecting at submit time means a cycle can never be persisted.
- Negative: layered output implies the barrier scheduling whose utilisation
  cost is recorded in ADR-007.
- Negative: the cycle-path DFS is a second code path that needs its own test.

### Alternatives Considered
- **DFS with colouring (white/grey/black).** Rejected: reports the cycle path
  naturally but produces a linear order, not layers — the executor would need
  a second grouping pass.
- **`graphlib.TopologicalSorter` (stdlib).** Rejected: it raises `CycleError`
  without the full path in the form SPEC §7 requires, so the DFS would be
  needed anyway while giving up control over layer emission.
- **Detect cycles lazily at run time.** Rejected: SPEC requires rejection at
  `submit`, keeping the store free of unschedulable state.

---

## ADR-012: Plugin loading restricted to an allowlist of module names

### Status
Accepted

### Context
FR-07 loads plugins listed in the comma-separated `TASKQ_PLUGINS` env var.
Threats T-03 (path/URL masquerading as a module name) and T-04 (dynamic
`eval`/`exec`) both land here, and NFR-02 forbids both.

### Decision
`service.plugins.PluginRegistry` splits `TASKQ_PLUGINS` on commas, validates
each name against `^[A-Za-z_][A-Za-z0-9_.]*$`, and loads it with
`importlib.import_module(name)`. A name failing the regex, or a module that
does not import, raises `PluginLoadError` → exit 6 with
`plugin load failed: <name>: <reason>`. No path loader, URL loader, `eval`,
`exec`, or `__import__` of a dynamic string exists anywhere in the codebase; a
CI grep enforces this. `pre_run`/`post_run` hooks are each wrapped in
`try/except Exception`: the exception is recorded as a `plugin_error` audit
event, the run continues, and after three consecutive failures the plugin is
disabled for the remainder of the process.

### Rationale
The regex admits only dotted identifiers, so `/`, `\`, `..`, `:` and every
other path or URL character is rejected before `importlib` sees the string —
the plugin can therefore only come from an already-importable location on
`sys.path`, not from an attacker-chosen file. Isolating hook exceptions keeps
a third-party defect from failing a user's task, while the three-strike
disable stops a persistently broken plugin from flooding the audit log.

### Consequences
- Positive: T-03 and T-04 are closed by construction plus a static gate.
- Positive: a faulty plugin degrades to a logged event, not a failed run.
- Negative: plugins must be installed on `sys.path`; ad-hoc script paths are
  unsupported — the intended restriction.
- Negative: `except Exception` around hooks is a broad catch. It is compliant
  with NFR-03 because it logs and continues deliberately rather than passing
  silently, and it does not catch `KeyboardInterrupt`/`SystemExit`.

### Alternatives Considered
- **`importlib.util.spec_from_file_location` for path plugins.** Rejected:
  it *is* threat T-03; arbitrary-file execution has no place under NFR-02.
- **`entry_points` discovery.** Rejected: implicit, environment-dependent
  loading contradicts the explicit allowlist FR-07 demands.
- **Let hook exceptions propagate.** Rejected: FR-07 states plugin failures
  must not interrupt the run.

---

## ADR-013: Redaction at the single audit-write chokepoint

### Status
Accepted

### Context
NFR-04 requires that `stdout_tail`, `stderr_tail`, and audit `detail` have
lines matching `(sk-[A-Za-z0-9_-]{8,}|token=\S+|Bearer\s+\S+)` replaced with
`[REDACTED]` **before** the bytes reach disk — the acceptance assertion is
that the file's contents contain no plaintext secret (threat T-05).

### Decision
The canonical redaction regex `(sk-[A-Za-z0-9_-]{8,}|token=\S+|Bearer\s+\S+)`
is compiled once and exposed as `taskq_plus.models.redaction.redact_payload(text) -> str`.
Two disk-write chokepoints call it **before** the bytes reach disk, and they
share the pattern via the `models.redaction` helper so the rule cannot drift:

1. `observability.audit.AuditLogger.emit()` redacts the audit `detail` and
   only then calls `storage.atomic.atomic_append_jsonl(audit.jsonl)`. No
   other module writes to `audit.jsonl`. This is the chokepoint that
   satisfies NFR-04 AC-04-1.
2. `storage.task_store.add()` / `.update()` redact `stdout_tail` and
   `stderr_tail` on the `TaskResult` payload and only then call
   `storage.atomic.atomic_write_json(tasks.json)`. This is the chokepoint
   that satisfies NFR-04 AC-04-2.

Each audit event still carries a `correlation_id` minted once per CLI
invocation in `cli.main`.

### Rationale
NFR-04 is a property of bytes on disk, so it can only be guaranteed at the
last point before the write. There are two such points — the audit file
and the task-result file — so the redaction is enforced at **both**
chokepoints, sharing a single compiled regex in `models.redaction` (one
source of truth, one import path, one test that pins the pattern). The
audit logger remains the sole writer of `audit.jsonl`; the task store
remains the sole writer of `tasks.json`. Minting the correlation id at
the CLI entry is the only scope that spans an entire invocation across
the concurrent runs of ADR-007.

### Consequences
- Positive: one compiled regex in `models.redaction` is the single source of
  truth; two chokepoints (`observability.audit.emit`,
  `storage.task_store.add/update`) enforce it before bytes reach disk.
- Positive: one canonical test asserts the file (`audit.jsonl` or
  `tasks.json`) contains no `sk-` token after a secret-emitting command;
  per-chokepoint tests pin each file.
- Positive: correlation ids stitch concurrent `run --all` events into
  per-invocation traces.
- Negative: whole matching lines are replaced, so surrounding diagnostic
  context on that line is lost — the trade NFR-04 explicitly chooses.
- Negative: the regex catches only the three declared patterns; novel secret
  formats pass through. Extending the pattern set is a one-line change in
  `models.redaction` and both chokepoints pick it up automatically.

### Alternatives Considered
- **Redact at read/export time.** Rejected: plaintext would already be on disk,
  failing the NFR-04 assertion outright.
- **Redact only in `observability.audit` (original decision).** Rejected:
  satisfied AC-04-1 but left AC-04-2
  (`grep -c "sk-" $TASKQ_HOME/tasks.json → 0`) silently violated, because
  `storage.task_store` writes raw `TaskResult` with unredacted
  `stdout_tail` / `stderr_tail`.
- **Redact in the executor when capturing subprocess output.** Rejected: it
  only covers subprocess output, leaving `detail` from other emitters
  unprotected, and splits the invariant across two modules.
- **A logging `Filter` on stdlib `logging`.** Rejected: audit output is
  hand-written JSONL for schema stability, not stdlib log records.

---

## ADR-014: Domain exception hierarchy mapped to documented exit codes

### Status
Accepted

### Context
SPEC §7 fixes exit codes: 1 unexpected/corrupt store, 2 validation and unknown
id/dependency, 3 breaker open, 4 single-task timeout, 5 cycle or depth
exceeded, 6 plugin load failure. NFR-03 bans bare `except:`,
`except Exception: pass`, and swallowing `KeyboardInterrupt`/`SystemExit`, and
requires every `except` block to re-raise, translate to a domain exception, or
exit with a documented code.

### Decision
Define one exception class per failure mode in `models.errors`
(`ValidationError`, `UnknownTaskError`, `UnknownDependencyError`,
`BreakerOpenError`, `DependencyCycleError`, `DepthExceededError`,
`PluginLoadError`, `StoreCorruptedError`). Lower layers raise these and never
call `sys.exit`. A single handler in `cli.commands` maps exception type to
exit code and stderr message. Store loaders raise `StoreCorruptedError` on
invalid JSON — they never silently rebuild.

### Rationale
Exit codes are a presentation concern; putting `sys.exit` in L1–L4 would make
those layers untestable without `pytest.raises(SystemExit)` and would violate
the layering intent of ADR-002. One exception class per exit code makes the
mapping table total and reviewable, and `ast-error-handling` can then verify
that every `except` block ends in a re-raise, a translation, or that single
mapper.

### Consequences
- Positive: the SPEC §7 table has a literal one-to-one counterpart in code.
- Positive: service and storage are testable with plain `pytest.raises`.
- Negative: eight exception classes for a small tool — justified because each
  is load-bearing for a distinct documented exit code.
- Negative: exit 1 on a corrupted store is deliberately unfriendly; a silent
  rebuild would be friendlier and is explicitly forbidden by SPEC §7.

### Alternatives Considered
- **`sys.exit(n)` at each failure site.** Rejected: couples every layer to CLI
  semantics and breaks the layering contract.
- **One exception carrying an `exit_code` attribute.** Rejected: `except`
  clauses could no longer discriminate by type, so callers would branch on a
  field — weaker typing for the same line count.
- **Reuse builtin exceptions (`ValueError`, `KeyError`).** Rejected: they are
  raised incidentally by library code, so catching them would map unrelated
  bugs onto meaningful exit codes.

---

## ADR-015: One hub module per layer to satisfy CRG community cohesion

### Status
Accepted

### Context
SAD §2.1 records the 鐵律 from SPEC §10: the previous test bed collapsed into
a flat package with no distinguishable communities. This round requires five
CRG communities — one per directory — each above the 0.3 internal-edge-density
threshold, under a 50-node cap, and under 15 files per directory (NFR-11).

### Decision
For every layer with two or more sibling files, designate a hub module that
at least 70 % of its siblings import and invoke through standalone function
calls (`result = hub.fn(...)`): `models.task`, `storage.atomic`,
`service.executor`, `observability.audit`, `cli.main`. Keep layer file counts
at 2–5 as tabulated in SAD §2.1.

### Rationale
CRG derives edges from call sites, so standalone function calls against a hub
generate the per-(caller, callee) edges that lift internal density above 0.3,
whereas a layer of mutually unaware siblings scores near zero and gets merged
into a neighbour. The hubs chosen are not artificial: `storage.atomic` is
already the sole write path (ADR-005) and `observability.audit` the sole audit
writer (ADR-013), so the cohesion requirement and the architectural
chokepoints coincide.

### Consequences
- Positive: five stable communities; the CRG-driven review tooling produces
  per-layer signal instead of one undifferentiated blob.
- Positive: hub modules are the natural review focus, matching the
  `high_risk_modules` list in the SAB block.
- Negative: hubs concentrate change; edits there have the widest blast radius.
  Mitigated by NFR-08 mutation testing scoped to `service/` and `storage/`.
- Negative: file placement is now partly constrained by graph metrics as well
  as by responsibility — a documented, deliberate coupling of the two.

### Alternatives Considered
- **Ignore CRG metrics and organise purely by responsibility.** Rejected: it
  reproduces the recorded flat-package collapse the SPEC §10 rule exists to
  prevent.
- **Force cohesion with `__init__.py` re-exports.** Rejected: re-exports create
  import edges without call edges, so density would not actually improve — a
  metric-gaming change with no structural benefit.
- **Fewer, larger layers.** Rejected: would breach the NFR-11 400-line file cap
  and blur the FR ownership mapping in SAD §2.7.

---

## Architecture traceability and quality controls

This section is the ADR's traceability matrix from the canonical **SPECIFICATION**
transcribed by `SRS.md` to an owning architecture decision. The SRS is the
requirements source; `SAD.md` is the architecture specification and its §2.8
NFR-to-module mapping is the cross-check. A row is an architectural commitment,
not a claim that implementation or testing is already complete.

| Requirement (FR/NFR) | ADR decision(s) | Architectural mechanism / boundary | Specification and verification surface |
|---|---|---|---|
| FR-01 | ADR-001, ADR-003, ADR-004, ADR-005, ADR-014 | Pydantic submission validation; centralized configuration; atomic task persistence; typed validation/unknown-dependency errors | SRS §3 FR-01; SAD §§2.2.3, 2.3.2, 2.6.2; SPEC §8 submission rows |
| FR-02 | ADR-006, ADR-007, ADR-008, ADR-014 | argv-only subprocess execution; layered thread-pool scheduling; retry seam; documented timeout mapping | SRS §3 FR-02; SAD §§2.4.1, 3.2–3.3; SPEC §7 exit-code table |
| FR-03 | ADR-008, ADR-009, ADR-005 | Injectable exponential backoff; persisted global breaker state machine; atomic breaker writes | SRS §3 FR-03; SAD §§2.4.1–2.4.2, 4.3; SPEC §8 breaker rows |
| FR-04 | ADR-004, ADR-005, ADR-010 | SHA-256 command signature, read-time TTL, atomic/thread-safe cache store | SRS §3 FR-04; SAD §§2.3.4, 2.4.3, 3.2; SPEC §8 cache row |
| FR-05 | ADR-001, ADR-014 | Click command surface and one CLI exception-to-exit-code mapper | SRS §3 FR-05; SAD §2.6; SPEC §3 FR-05 and §7 |
| FR-06 | ADR-007, ADR-011 | Kahn layers, cycle-path rejection, blocked dependents, depth cap | SRS §3 FR-06; SAD §§2.4.4, 3.3; SPEC §8 DAG rows |
| FR-07 | ADR-006, ADR-012 | Regex-validated module-name allowlist; `importlib.import_module`; isolated hooks and three-strike disablement | SRS §3 FR-07; SAD §2.4.5 and §4.2; SPEC §8 plugin rows |
| FR-08 | ADR-005, ADR-013, ADR-014 | Append-only JSONL with fsync, correlation IDs, single redaction chokepoint, format exporters | SRS §3 FR-08; SAD §§2.5, 3.4, 4.4; SPEC §8 audit/export rows |
| NFR-01 | ADR-004, ADR-010, ADR-011 | Bounded JSON working set, O(V+E) topology, constant-time cache lookup | SRS §4 NFR-01; SAD §§2.8, 4.1; verification surface stated in SAD §2.8 |
| NFR-02 | ADR-006, ADR-012 | No shell interpreter, injection validation, name-only plugin loading, no dynamic code loading | SRS §4 NFR-02; SAD §§4.2, 6; grep and bandit surfaces stated in SAD |
| NFR-03 | ADR-005, ADR-009, ADR-014 | Atomic document writes, durable audit append, explicit domain errors, bounded breaker recovery | SRS §4 NFR-03; SAD §4.3; `ast-error-handling` surface stated in SAD |
| NFR-04 | ADR-013 | Redaction of stdout/stderr/detail immediately before audit bytes are written, and of stdout_tail/stderr_tail immediately before tasks.json bytes are written; both chokepoints share `models.redaction.redact_payload` | SRS §4 NFR-04 + §3 FR-02; SAD §§2.5.1, 3.4, 4.4 |
| NFR-05 | ADR-015 | Public-symbol documentation is part of the layer/module contract | SRS §4 NFR-05; SAD §2.8 verification surface |
| NFR-06 | ADR-002, ADR-003 | Import-linter five-layer contract with independent config module | SRS §4 NFR-06; SAD §§1, 2.1, 2.9; `lint-imports` surface |
| NFR-07 | ADR-001 | Exactly two pinned runtime dependencies and allowlisted licenses; SBOM location is specified by the ADR | SRS §4 NFR-07; SAD §4.4; `pip-licenses` surface stated in SAD |
| NFR-08 | ADR-007, ADR-015 | Mutation-sensitive service/storage seams and hub ownership make mutation scope explicit | SRS §4 NFR-08; SAD §2.8 and §4.4 |
| NFR-09 | ADR-014, ADR-015 | Explicit error mapping and reviewable module contracts support non-skipped, assertion-bearing verification | SRS §4 NFR-09; SAD §2.8 and §4.4 |
| NFR-10 | ADR-014, ADR-015 | CLI boundary delegates to every FR owner, preserving an integration seam across the complete command surface | SRS §4 NFR-10; SAD §§2.6, 2.8, 3.2–3.3 |
| NFR-11 | ADR-002, ADR-015 | Five bounded communities, hub modules, and documented file/line limits | SRS §4 NFR-11; SAD §§2.1, 2.8, 4.4 |
| NFR-12 | ADR-014, ADR-015 | CLI error contract and stable hubs expose the system-verification entry surface | SRS §4 NFR-12; SAD §§1.1, 2.8, 4.4 |

### Security, coverage, and maintainability decisions

Security is a design constraint, not an inferred test result. ADR-006 makes
subprocess injection structurally unreachable by passing an argv list and
forbidding `shell=True`; ADR-012 makes plugin loading name-only and rejects
paths, URLs, `eval`, `exec`, and dynamic `__import__`; ADR-013 prevents declared
secret patterns from reaching the audit file. These decisions implement the
security requirements NFR-02 and NFR-04 and the threats described in SAD §6.
The SRS and SAD remain the authoritative specification for the security checks;
this ADR does not invent additional gates or claim them passed.

Coverage is bidirectional: every FR-01 through FR-08 and every NFR-01 through
NFR-12 appears in the matrix above, while each row points back to its SRS
requirement and SAD ownership/verification surface. Where SAD §2.8 names a
verification command or tool, that is the stated verification surface; this ADR
records architecture ownership only and does not substitute for execution of
those checks.

Maintainability is enforced through the architecture's small, explicit seams:
ADR-002 limits dependency direction, ADR-003 centralizes configuration, ADR-005
concentrates persistence writes, ADR-013 concentrates redaction, ADR-014
centralizes exit-code translation, and ADR-015 bounds each community and names
its hub. The resulting module interfaces, dependency boundaries, and rejected
alternatives are documented in this ADR and cross-referenced to SAD §§2.1–2.9.

### Review vocabulary tied to architectural controls

The following terms are used here in their architectural meaning and are tied to
existing SRS/SAD obligations, rather than being implementation claims. The
security boundary validates and sanitizes command input; it does not provide
user auth, RBAC, or a permission service because SRS §1.2 explicitly excludes
multi-user authorisation. No TLS or rate limit is required for the local,
network-free scope. The design therefore uses a whitelist for plugin module
names, masks secret output with the declared redaction pattern, and rejects
path-like input. The security review also checks for token and PII leakage;
there is no encryption, HMAC, or signature requirement in the specification.
The relevant verification evidence remains the SRS acceptance criteria and the
SAD security surfaces; this ADR records the decision and its scope.

Maintainability follows named module interfaces and ordinary Python structure:
each public class and def belongs to a module with a documented interface,
docstring, type hint, and snake_case or PascalCase naming as appropriate.
Imports are constrained by the layer contract; `from` imports cannot point
upward. The hub modules are deliberately small and are not abstract base
classes (ABC) added for their own sake. A change request must preserve the
interface and dependency direction; the ADR alternatives are the regression
record for that decision.

Coverage means requirements coverage, not an assertion that a gate has run.
The traceability matrix gives test coverage ownership for every FR/NFR, while
SAD §2.8 names the unit test, integration test, pytest-benchmark, mock/injection
seam, mutation, lint, and coverage report surfaces where the specification calls
for them. The architecture permits fixtures and a test plan to exercise the
storage seams without a subprocess; an assert-bearing regression test is the
verification unit. `pytest` and the stated integration coverage surface are
referenced only as documented verification mechanisms. The audit and
completeness of evidence are determined by the later phase; no result is
pre-verified by this ADR.

### Specification boundary and maintenance note

This ADR is subordinate to the project specification: `SPEC.md` is canonical,
`SRS.md` is its requirements transcription, and `SAD.md` is the architecture
specification. If a requirement, security assessment, or verification plan
changes, the corresponding FR/NFR row and owning ADR must be updated together.
That rule preserves bidirectional traceability and makes the impact of a
maintenance change reviewable without inventing a downstream test or gate.
