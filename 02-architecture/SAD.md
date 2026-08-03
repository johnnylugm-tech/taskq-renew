# Software Architecture Document (SAD) — taskq

> Phase 2 architectural design for the `taskq` round-1 validation bed.
> Source of truth for module boundaries, data flows, NFR handling, and
> the security design (STRIDE-lite) blocks. The SAB block is a
> placeholder to be filled in during the SAB Generation phase.

## 1. Architecture Overview

`taskq` is a local-first Python CLI that submits shell commands as tasks,
executes them under a controlled runtime (timeout, retry, circuit breaker,
result cache, dependency DAG), exposes plugin hooks, and writes structured
audit logs. It is a single-user, single-process tool — no network surface,
no remote service.

The architecture is a strict five-layer hierarchy (top down):

```
cli  >  observability  >  service  >  storage  >  models
                         (also: config — independence, any layer may import)
```

The hierarchy is **enforced** by `import-linter` (`.importlinter`, NFR-06).
Lower layers MUST NOT import upper layers. The `config` module sits
outside the hierarchy and is importable from any layer.

Key architectural choices:

- **Strict layering** — eliminates circular dependencies and concentrates
  risk in well-defined seams (see §6 STRIDE-lite).
- **Five CRG communities** — one per directory (`cli`, `observability`,
  `service`, `storage`, `models`). Each layer has a hub file that earns
  internal edges from its siblings (see §2.1 CRG design notes).
- **Independence module (`config`)** — read-only side-channel for env
  vars; never imports layers, so it cannot be used to bypass the
  hierarchical contract.
- **Subprocess execution, never `shell=True`** — see NFR-02; this is the
  single most important security invariant in the system, called out
  explicitly in §6.
- **Plugin allowlist** — modules are loaded by name only via
  `importlib.import_module`, never from a path or URL (NFR-02, FR-07).

### 1.1 System Verification Target

> **Phase 3 Gate 2 Requirement**: The harness executes `make verify-system`
> at Gate 2. Add a `verify-system` target to your project `Makefile` that
> assembles and exercises the system end-to-end. Target name is fixed.

**Makefile target**: `verify-system` (per SPEC §5.3 / NFR-12)

## 2. Module Design

### 2.1 Directory Structure Design Principles

The five-layer structure (`cli > observability > service > storage > models`
plus the independence `config`) is taken verbatim from SPEC §6 and
enforced by `import-linter`. Each layer becomes one CRG community —
this is the explicit design intent to prevent the flat-package collapse
that broke the previous test bed (SPEC §10 CRG 校準鐵律).

For each layer with ≥2 sibling files, the layer contains a hub module
that ≥70% of siblings import and call via standalone function calls
(`result = hub.fn(...)`). This produces per-(caller, callee) edges that
push internal edge density above the 0.3 CRG threshold.

Per CRG principles and given the projected node count per layer
(each layer holds 2–5 files × roughly 4–8 functions), every community
fits comfortably under the 50-node cap and the 15-file-per-directory cap
(NFR-11). Layer file counts:

| Layer | Files | Hub | Notes |
|-------|-------|-----|-------|
| `models` | 2 | `models.task` | `errors` is the only sibling; hub-and-spoke |
| `storage` | 4 | `storage.atomic` | All stores call `atomic_write_json` |
| `service` | 5 | `service.executor` | Executor touches every flow |
| `observability` | 2 | `observability.audit` | `audit` + `export` (audit is the event sink) |
| `cli` | 2 | `cli.main` | Both call into `main` group |
| `config` | 1 | (itself) | Independence module |

### 2.2 Module Index

The full module inventory, with FR ownership, follows §2.3–§2.7.

#### 2.2.1 `taskq_plus.config` (Independence)

| Attribute | Value |
|-----------|-------|
| Responsibility | Read `TASKQ_*` env vars with defaults; expose typed constants. |
| External Interface | Module-level constants (e.g. `TASKQ_HOME`, `TASKQ_MAX_WORKERS`, `TASKQ_TASK_TIMEOUT`, …). |
| Dependencies | stdlib only (`os`, `pathlib`). Imported by every layer. |
| FR / NFR owned | (cross-cutting; touched by all FRs for env-resolution); NFR-07 (allowlist-aware: no license-bearing deps here). |

**Logical constraints:**
- MUST NOT import anything from `taskq_plus` (independence).
- MUST be importable from any layer without creating a cycle.

#### 2.2.2 Models layer (`taskq_plus.models`)

| Attribute | Value |
|-----------|-------|
| Responsibility | Pydantic schemas for task state and domain exceptions. No side effects. |
| External Interface | `TaskSubmission`, `TaskResult`, `Status` enum, `Task`. |
| Dependencies | `pydantic` v2 only. |
| FR / NFR owned | FR-01 (validation), FR-02 (result shape), FR-03 (state machine), FR-04 (cache entry), FR-06 (deps list), FR-08 (audit event shape). |

**Logical constraints:**
- L1 — zero internal `taskq_plus` imports.
- Every public class/function has a docstring including `[FR-XX]` or `[NFR-XX]` (NFR-05).

#### 2.2.3 `taskq_plus.models.task`

| Attribute | Value |
|-----------|-------|
| Responsibility | Pydantic models `Task`, `TaskSubmission`, `TaskResult`, `Status`, `CacheEntry`. |
| External Interface | Listed above. |
| Dependencies | `pydantic`, `taskq_plus.config` (none — config is only used by upper layers). |
| FR / NFR owned | FR-01 (`TaskSubmission`), FR-02 (`TaskResult`, `Status`), FR-04 (`CacheEntry`). |

#### 2.2.4 `taskq_plus.models.errors`

| Attribute | Value |
|-----------|-------|
| Responsibility | Domain exceptions: `ValidationError`, `UnknownTaskError`, `UnknownDependencyError`, `BreakerOpenError`, `DependencyCycleError`, `DepthExceededError`, `PluginLoadError`, `StoreCorruptedError`. |
| External Interface | Exception classes (each maps to a SPEC §7 exit code). |
| Dependencies | stdlib only. |
| FR / NFR owned | FR-01, FR-03, FR-06, FR-07; NFR-03 (NO bare `except`). |

### 2.3 Storage layer (`taskq_plus.storage`)

| Attribute | Value |
|-----------|-------|
| Responsibility | All disk I/O. Atomic writes. Thread-safe access (shared `threading.Lock`). |
| External Interface | `atomic_write_json(path, payload)`, `atomic_append_jsonl(path, record)`, `TaskStore`, `BreakerStore`, `CacheStore`. |
| Dependencies | `taskq_plus.models`, `taskq_plus.config`, json/os/pathlib/threading. |
| FR / NFR owned | FR-01 (task_store), FR-03 (breaker_store), FR-04 (cache_store); NFR-03 (atomic writes), NFR-04 (audit redaction), NFR-08 (mutation scope). |

**Logical constraints:**
- L2 — may import `models`; MUST NOT import `service`, `observability`, or `cli`.
- All writes use `tmp + os.replace` (atomic) except `audit.jsonl` which is append + fsync.
- All shared state is guarded by a `threading.Lock`.

#### 2.3.1 `taskq_plus.storage.atomic`

| Attribute | Value |
|-----------|-------|
| Responsibility | `atomic_write_json(path, payload)` — tmp + `os.replace`; `atomic_append_jsonl(path, record)` — open append + fsync. |
| External Interface | Two functions. |
| Dependencies | stdlib only. |
| FR / NFR owned | NFR-03 (atomicity), NFR-04 (write-before-redaction invariant). |

#### 2.3.2 `taskq_plus.storage.task_store`

| Attribute | Value |
|-----------|-------|
| Responsibility | Load/save `tasks.json`; thread-safe CRUD on tasks by id. |
| External Interface | `TaskStore.add`, `TaskStore.get`, `TaskStore.update`, `TaskStore.list`. |
| Dependencies | `atomic`, `models.task`, `config`. |
| FR / NFR owned | FR-01 (add), FR-02 (update), FR-06 (depends_on). |

#### 2.3.3 `taskq_plus.storage.breaker_store`

| Attribute | Value |
|-----------|-------|
| Responsibility | Persist `breaker.json` (state, failure_count, opened_at). |
| External Interface | `BreakerStore.load`, `BreakerStore.save`. |
| Dependencies | `atomic`, `models.task` (state enum), `config`. |
| FR / NFR owned | FR-03. |

#### 2.3.4 `taskq_plus.storage.cache_store`

| Attribute | Value |
|-----------|-------|
| Responsibility | Persist `cache.json`; keyed by `sha256(command)`. |
| External Interface | `CacheStore.get(signature)`, `CacheStore.put(signature, result)`. |
| Dependencies | `atomic`, `models.task`, `config`. |
| FR / NFR owned | FR-04. |

### 2.4 Service layer (`taskq_plus.service`)

| Attribute | Value |
|-----------|-------|
| Responsibility | Business logic — subprocess execution, retry/backoff, circuit breaker, TTL cache, DAG scheduling, plugin hooks. |
| External Interface | `Executor`, `Breaker`, `Cache`, `DAG`, `PluginRegistry`. |
| Dependencies | `taskq_plus.storage`, `taskq_plus.models`, `taskq_plus.config`; stdlib subprocess, concurrent.futures, hashlib, importlib. |
| FR / NFR owned | FR-02 (executor), FR-03 (breaker), FR-04 (cache), FR-06 (dag), FR-07 (plugins); NFR-08 (mutation scope). |

**Logical constraints:**
- L3 — may import `storage` and `models`. MUST NOT import `observability` or `cli`.
- Subprocess call sites MUST use `subprocess.run([...], shell=False)` (NFR-02).

#### 2.4.1 `taskq_plus.service.executor`

| Attribute | Value |
|-----------|-------|
| Responsibility | Run one task via `subprocess.run`; capture stdout_tail (2000 chars), stderr_tail (2000 chars), exit_code, duration_ms; apply retry/backoff. |
| External Interface | `Executor.run(task) -> TaskResult`. `sleep` injectable for testing. |
| Dependencies | `storage.task_store`, `models.task`, `config`. |
| FR / NFR owned | FR-02, FR-03 (retry), FR-08 (run_start / run_end audit). |

#### 2.4.2 `taskq_plus.service.breaker`

| Attribute | Value |
|-----------|-------|
| Responsibility | State machine `CLOSED → OPEN → HALF_OPEN → CLOSED/OPEN`. Persisted via `breaker_store`. |
| External Interface | `Breaker.allow() -> bool`, `Breaker.record_success()`, `Breaker.record_failure()`. |
| Dependencies | `storage.breaker_store`, `config`. |
| FR / NFR owned | FR-03. |

#### 2.4.3 `taskq_plus.service.cache`

| Attribute | Value |
|-----------|-------|
| Responsibility | Compute `sha256(command)`; consult/populate `cache_store` under TTL. |
| External Interface | `Cache.lookup(command) -> TaskResult | None`, `Cache.store(command, result)`. |
| Dependencies | `storage.cache_store`, `config`. |
| FR / NFR owned | FR-04. |

#### 2.4.4 `taskq_plus.service.dag`

| Attribute | Value |
|-----------|-------|
| Responsibility | Kahn topological sort; cycle detection; depth limit. |
| External Interface | `DAG.schedule(tasks) -> list[list[str]]` (layers of runnable ids). |
| Dependencies | `models.task`, `config`. |
| FR / NFR owned | FR-06. |

#### 2.4.5 `taskq_plus.service.plugins`

| Attribute | Value |
|-----------|-------|
| Responsibility | Load plugins from `TASKQ_PLUGINS` allowlist (module names only). Validate name regex. Wrap `pre_run`/`post_run` calls in try/except. Disable after 3 consecutive failures. |
| External Interface | `PluginRegistry.load()`, `PluginRegistry.run_pre(task)`, `PluginRegistry.run_post(task, result)`. |
| Dependencies | `models.errors`, `config`. |
| FR / NFR owned | FR-07; NFR-02 (plugin loading surface). |

### 2.5 Observability layer (`taskq_plus.observability`)

| Attribute | Value |
|-----------|-------|
| Responsibility | Audit log emission (JSONL, redacted) and export formatters. |
| External Interface | `AuditLogger.emit(event, ...)`, `Exporter.to_json(tasks)`, `Exporter.to_csv(tasks)`, `Exporter.to_md(tasks)`. |
| Dependencies | `taskq_plus.service`, `taskq_plus.models`, `taskq_plus.storage`, `taskq_plus.config`. |
| FR / NFR owned | FR-08, NFR-04 (redaction), NFR-05 (docstring coverage). |

**Logical constraints:**
- L4 — may import `service`, `storage`, `models`. MUST NOT import `cli`.
- Audit log writes go through `storage.atomic` (append + fsync).

#### 2.5.1 `taskq_plus.observability.audit`

| Attribute | Value |
|-----------|-------|
| Responsibility | Emit JSONL events with `correlation_id`; apply NFR-04 redaction before write. |
| External Interface | `AuditLogger.emit(event, task_id, correlation_id, detail)`; `with_correlation_id` context. |
| Dependencies | `storage.atomic`, `config`, `models.task`. |
| FR / NFR owned | FR-08, NFR-04. |

#### 2.5.2 `taskq_plus.observability.export`

| Attribute | Value |
|-----------|-------|
| Responsibility | JSON / CSV / MD serializers; identical task set across formats. |
| External Interface | `Exporter.to_json(tasks) -> str`, `Exporter.to_csv(tasks) -> str`, `Exporter.to_md(tasks) -> str`. |
| Dependencies | `models.task`. |
| FR / NFR owned | FR-08. |

### 2.6 CLI layer (`taskq_plus.cli`)

| Attribute | Value |
|-----------|-------|
| Responsibility | Click group entry point. Per-command handlers. |
| External Interface | `python -m taskq_plus <subcommand>` (see SPEC §3 FR-05). |
| Dependencies | `taskq_plus.observability`, `taskq_plus.service`, `taskq_plus.storage`, `taskq_plus.models`, `taskq_plus.config`. |
| FR / NFR owned | FR-05 (command surface), FR-01–08 (each subcommand delegates to its service module). |

**Logical constraints:**
- L5 — may import any layer. The only place a correlation id is minted (per CLI invocation).

#### 2.6.1 `taskq_plus.cli.main`

| Attribute | Value |
|-----------|-------|
| Responsibility | `click` group definition; wires subcommands; mints `correlation_id`. |
| External Interface | `cli` group, `submit`, `run`, `status`, `list`, `graph`, `plugins`, `export`, `clear`. |
| Dependencies | `cli.commands`, all layers. |
| FR / NFR owned | FR-05. |

#### 2.6.2 `taskq_plus.cli.commands`

| Attribute | Value |
|-----------|-------|
| Responsibility | Per-subcommand handler functions. Argument parsing; error→exit-code mapping per SPEC §7. |
| External Interface | Handler functions bound by `main.py` to click commands. |
| Dependencies | All layers. |
| FR / NFR owned | FR-01–08. |

### 2.7 FR → Module Mapping

| FR | Owning module(s) | Notes |
|----|------------------|-------|
| FR-01 | `models.task`, `storage.task_store`, `cli.commands.submit` | Validation in models; persistence in storage; CLI shells. |
| FR-02 | `service.executor`, `cli.commands.run` | Subprocess runner; `--all` ThreadPoolExecutor dispatcher. |
| FR-03 | `service.breaker`, `service.executor` (retry), `storage.breaker_store` | Breaker state machine + retry/backoff. |
| FR-04 | `service.cache`, `storage.cache_store` | sha256-keyed TTL replay. |
| FR-05 | `cli.main`, `cli.commands` | Click group surface; exit codes. |
| FR-06 | `service.dag`, `models.task.depends_on`, `cli.commands.graph` | Kahn sort + cycle/depth guards. |
| FR-07 | `service.plugins`, `cli.commands.plugins` | Allowlist loader + hook wrapping. |
| FR-08 | `observability.audit`, `observability.export`, `cli.commands.export` | JSONL + formatters. |

### 2.8 NFR → Module Mapping (precise ownership)

| NFR | Owning module(s) | Verification surface |
|-----|------------------|----------------------|
| NFR-01 | `cli.commands.submit`, `cli.commands.status`, `service.dag` | `pytest-benchmark` |
| NFR-02 | `service.executor`, `service.plugins`, `models.task` (validation) | `bandit`, grep CI gate |
| NFR-03 | `storage.atomic`, `models.errors` | `ast-error-handling` |
| NFR-04 | `observability.audit` | unit test on `audit.jsonl` |
| NFR-05 | every public symbol | `ast-docstrings` |
| NFR-06 | every file (contract) | `lint-imports` |
| NFR-07 | `requirements.txt`, `requirements-dev.txt` | `pip-licenses`, SBOM |
| NFR-08 | `service/*`, `storage/*` | `mutmut` |
| NFR-09 | every test | `pytest -q`, `ast-assertions` |
| NFR-10 | `tests/integration/` | `pytest-cov-integration` |
| NFR-11 | every file | `readability-v2` |
| NFR-12 | `Makefile` | `make verify-system` |

### 2.9 Cross-cutting constraints

- **No circular dependencies.** Enforced by `import-linter`
  (`cli > observability > service > storage > models`). Verified by
  `lint-imports` exit 0 (NFR-06).
- **No `shell=True` anywhere.** Grep gate; bandit CI gate; both must be 0.
- **No `eval` / `exec` / `__import__` of dynamic strings.** Grep gate.
- **All data-file writes atomic.** `tmp + os.replace`; audit append + fsync.
- **Execution-thread safety.** Single `threading.Lock` shared across
  `TaskStore`, `BreakerStore`, `CacheStore` writes.

## 3. Interfaces & Data Flows

### 3.1 Module dependency graph (enforced by `import-linter`)

```
                 ┌──────────────────────────────┐
                 │           cli                │  L5
                 │  ┌──────────┐  ┌──────────┐  │
                 │  │  main.py │←→│commands.py│  │
                 │  └────┬─────┘  └────┬─────┘  │
                 └───────┼─────────────┼────────┘
                         ▼             ▼
                 ┌──────────────────────────────┐
                 │       observability          │  L4
                 │  ┌──────────┐  ┌──────────┐  │
                 │  │  audit   │  │  export  │  │
                 │  └────┬─────┘  └────┬─────┘  │
                 └───────┼─────────────┼────────┘
                         ▼             ▼
                 ┌──────────────────────────────┐
                 │          service             │  L3
                 │ ┌────┬────┬────┬────┬────┐    │
                 │ │ex  │br  │ca  │dag │pl  │    │
                 │ │ecu │eak │che │    │ugi │    │
                 │ │tor │er  │    │    │ns  │    │
                 │ └──┬─┴─┬──┴─┬──┴─┬──┴─┬──┘    │
                 └────┼───┼────┼────┼────┼───────┘
                      ▼   ▼    ▼    ▼    ▼
                 ┌──────────────────────────────┐
                 │          storage             │  L2
                 │ ┌──────┬─────┬─────┬─────┐   │
                 │ │atom. │tasks│brea.│cach.│   │
                 │ │      │store│store│store│   │
                 │ └──────┴──┬──┴──┬──┴──┬──┘   │
                 └───────────┼─────┼─────┼──────┘
                             ▼     ▼     ▼
                 ┌──────────────────────────────┐
                 │           models             │  L1
                 │  ┌──────────┐  ┌──────────┐  │
                 │  │   task   │  │  errors  │  │
                 │  └──────────┘  └──────────┘  │
                 └──────────────────────────────┘

   config — independence; any layer may import it; it imports no layer.
```

The arrows point downward in the dependency hierarchy. Reverse arrows
(upper → lower) are the only legal direction. `config` is the side
channel and is not part of the ordering.

### 3.2 Data flow — `submit` + `run` happy path

```
user ──▶ cli.main.submit ──▶ models.task.TaskSubmission (validate)
                                 │ invalid → exit 2 (no storage write)
                                 │ valid
                                 ▼
                            storage.task_store.add (atomic write tasks.json)
                                 │
                                 ▼
                            observability.audit.emit('submit', correlation_id)

user ──▶ cli.main.run <id> ──▶ service.breaker.allow()
                                 │ OPEN → exit 3
                                 │ CLOSED / HALF_OPEN
                                 ▼
                            service.cache.lookup(command)
                                 │ hit & within TTL → replay (cached: true)
                                 │ miss
                                 ▼
                            service.executor.run(task)
                                 │   subprocess.run([...], shell=False, timeout=…)
                                 │   on TimeoutExpired → status=timeout, exit 4 (single task)
                                 │   on non-zero exit after retries → breaker.record_failure()
                                 ▼
                            service.plugins.run_pre(task) / run_post(task, result)
                                 │ exception → logged; run continues
                                 ▼
                            storage.task_store.update (atomic write)
                                 │
                                 ▼
                            observability.audit.emit('run_end', correlation_id)
```

### 3.3 Data flow — `run --all` with DAG

```
cli.commands.run --all
  │
  ▼
service.dag.schedule(pending_tasks) ──▶ layers: list[list[id]]
  │   cycle   → DependencyCycleError    → exit 5
  │   depth>n → DepthExceededError      → exit 5
  │   blocked dependents keep status=blocked; not counted by breaker
  ▼
ThreadPoolExecutor(max_workers=TASKQ_MAX_WORKERS)
  │
  ▼
service.breaker.allow()  →  service.executor.run(task)  →  audit.emit
  │
  ▼
next DAG layer (until empty)
```

### 3.4 Data flow — audit + redaction

```
service / cli  ──▶ observability.audit.emit(event, task_id, correlation_id, detail)
                                                  │
                                                  ▼
                          redact(detail)  # NEVER write raw detail
                              pattern: (sk-[A-Za-z0-9_-]{8,}|token=\S+|Bearer\s+\S+)
                                                  │
                                                  ▼
                          storage.atomic.atomic_append_jsonl(audit.jsonl)
                              (open 'a' + fsync)

service.executor.run(task) ──▶ TaskResult(stdout_tail, stderr_tail, ...)
                                                  │
                                                  ▼
                          redact(stdout_tail) / redact(stderr_tail)  # SAME regex
                              pattern: (sk-[A-Za-z0-9_-]{8,}|token=\S+|Bearer\s+\S+)
                                                  │
                                                  ▼
                          storage.task_store.add / .update (tasks.json)
                              via storage.atomic.atomic_write_json
```

**Two disk-write chokepoints** call `models.redaction.redact_payload()`
**before** the bytes hit disk, so NFR-04 AC-04-1 and AC-04-2 both hold:

1. `observability.audit.emit` redacts the audit `detail` before
   `storage.atomic.atomic_append_jsonl(audit.jsonl)`. The audit logger
   is the sole writer of `audit.jsonl`; nothing else may write to it.
2. `storage.task_store.add` / `.update` redacts `stdout_tail` and
   `stderr_tail` before `storage.atomic.atomic_write_json(tasks.json)`.
   This is the chokepoint that satisfies AC-04-2
   (`grep -c "sk-" $TASKQ_HOME/tasks.json → 0`).

Both chokepoints use the identical regex compiled once in
`models.redaction` — one source of truth for the pattern.

## 4. NFR Handling

Each NFR is enumerated from SPEC §4. The table states the architectural
mechanism by which the NFR is satisfied and the verification command
that proves it.

| NFR | Mechanism | Verification |
|-----|-----------|--------------|
| **NFR-01 (performance)** | `submit`/`status` are pure-storage operations on a single in-process dict; `run --all` topology sort is a single Kahn pass over up to 200 nodes — both well within budget. Benchmarks live in `03-development/tests/perf/`. | `pytest-benchmark`; SPEC §8 #2. |
| **NFR-02 (security)** | (a) Subprocess: `subprocess.run` with explicit argv list, `shell=False` everywhere. (b) Plugin: `importlib.import_module` of allowlisted module names; name regex `^[A-Za-z_][A-Za-z0-9_.]*$`; never `eval`/`exec`/`__import__`. (c) `bandit` clean. | `grep -rn "shell=True\|eval(\|exec(" 03-development/src/` → 0; `bandit -r 03-development/src/` → 0/0. |
| **NFR-03 (error handling)** | All data writes via `storage.atomic` (tmp + `os.replace`; audit append + fsync). No bare `except:`; every `except` re-raises, translates to a domain exception, or exits with a documented code. | `ast-error-handling` gate; SPEC §8 #1. |
| **NFR-04 (sensitive data redaction)** | `models.redaction.redact_payload()` runs the regex `(sk-[A-Za-z0-9_-]{8,}|token=\S+|Bearer\s+\S+)` over the text BEFORE writing. **Two disk-write chokepoints** call it: (1) `observability.audit.emit` redacts `detail` for `audit.jsonl` (AC-04-1); (2) `storage.task_store.add` / `.update` redacts `stdout_tail` / `stderr_tail` for `tasks.json` (AC-04-2). Both chokepoints share the regex compiled once in `models.redaction`. | unit tests assert both `audit.jsonl` and `tasks.json` contain no `sk-` token after running a command that emits one; plus positive unit tests for `token=` and `Bearer ` patterns (AC-04-3). |
| **NFR-05 (documentation)** | Every public symbol in `taskq_plus` has a docstring with `[FR-XX]` or `[NFR-XX]`. | `ast-docstrings` reports 100%. |
| **NFR-06 (architecture constraints)** | Five-layer hierarchy enforced by `.importlinter`. `cli > observability > service > storage > models`; `config` is independence. | `lint-imports` exit 0; verified in CI. |
| **NFR-07 (license compliance)** | `requirements.txt` pins every dep with `==`. License allowlist {MIT, BSD-2-Clause, BSD-3-Clause, Apache-2.0}. Scan covers the installed venv. | `pip-licenses --format=json`; SBOM at `08-config/SBOM.json`. |
| **NFR-08 (mutation testing)** | `.methodology/harness_config.json` sets `features.mutation_testing: true`. Scope: `service/` + `storage/`. | `mutmut run` → `mutmut results` reports score ≥ 70. |
| **NFR-09 (test realism)** | Zero `pytest.skip` / `xfail` / zero-assert stubs. Every test has at least one `assert`. VERIFIED marks only after a real PASS. | `pytest -q` skipped count = 0; `ast-assertions` zero_assert = 0. |
| **NFR-10 (integration coverage)** | `tests/integration/` exercises CLI surface via `python -m taskq_plus` or `click.testing.CliRunner`. Covers submit→run→status, DAG multi-layer, breaker, cache, plugin hooks, export. | `pytest-cov-integration` ≥ 80% on `03-development/src`. |
| **NFR-11 (readability)** | Code style: type hints, short functions, ≤10 cyclomatic, ≤400 lines/file, ≤15 files/dir. | `readability-v2` MI ≥ 80. |
| **NFR-12 (system verification target)** | `Makefile` provides `verify-system` target chaining unit + integration + smoke (`submit`/`run`/`status`/`graph`/`export`/`clear`). | `make verify-system` → exit 0; stdout contains `verify-system: PASS`. |

### 4.1 Latency budget

| Surface | p95 budget | Source |
|---------|-----------|--------|
| `submit` + `status` (100 iter, no subprocess) | < 50 ms | NFR-01 |
| `run --all` topo sort (200 tasks, no subprocess) | < 200 ms | NFR-01 |

The performance budgets are paid by the architecture, not by code
optimization: storage is in-process JSON (`$TASKQ_HOME/tasks.json`),
the DAG scheme is one Kahn pass, and the breaker is a single in-memory
boolean plus a small JSON file.

### 4.2 Security posture

The surface is small but sharp. The two load-bearing invariants are:

1. **Subprocess argv never goes through a shell** (NFR-02). This is
   enforced by grep + bandit + an explicit test that asserts every
   `subprocess.run` call site uses a list argv.
2. **Plugin loading is by name only**, never by path or URL (NFR-02,
   FR-07). The name regex blocks `/`, `\`, `..`, and any
   path-looking input. `importlib.import_module` is the only loader.

The full STRIDE-lite decomposition is in §6.

### 4.3 Cost posture

This is a local CLI — there is no recurring service cost. The only
"cost" axis is developer time, controlled by:

- CI run time (kept under ~5 min via the mutation scope in NFR-08).
- Disk footprint (4 data files, all small JSON / JSONL).
- Dependency surface (pinned, allowlist-licensed — small).

## 5. SAB Block (machine-readable — BINDING CONTRACT)

> **CONTRACT**: Field names, types, `sab:` root key, and `phase` as int
> must match `core/quality_gate/sab_parser.py:render_canonical_sab_template()`.
> The YAML below is the **placeholder** — replace EXAMPLE values with
> the project's real values during the SAB Generation phase.
> Validate before committing: `python3 scripts/generate_sab.py --validate --project .`

<!-- SAB:START -->
```yaml
sab:
  version: "1.0"
  created_at: "{YYYY-MM-DD}"
  phase: 2  # MUST be int, NOT a string — parser raises on 'phase: "2"'
  project: "taskq"

  layers:  # EXAMPLE — replace with the project's layers
    - name: cli
      modules:
        - name: "taskq_plus.cli.main"
        - name: "taskq_plus.cli.commands"
      allowed_dependencies: ["observability", "service", "storage", "models", "config"]

    - name: observability
      modules:
        - name: "taskq_plus.observability.audit"
        - name: "taskq_plus.observability.export"
      allowed_dependencies: ["service", "storage", "models", "config"]

    - name: service
      modules:
        - name: "taskq_plus.service.executor"
        - name: "taskq_plus.service.breaker"
        - name: "taskq_plus.service.cache"
        - name: "taskq_plus.service.dag"
        - name: "taskq_plus.service.plugins"
      allowed_dependencies: ["storage", "models", "config"]

    - name: storage
      modules:
        - name: "taskq_plus.storage.atomic"
        - name: "taskq_plus.storage.task_store"
        - name: "taskq_plus.storage.breaker_store"
        - name: "taskq_plus.storage.cache_store"
      allowed_dependencies: ["models", "config"]

    - name: models
      modules:
        - name: "taskq_plus.models.task"
        - name: "taskq_plus.models.errors"
        - name: "taskq_plus.models.redaction"
      allowed_dependencies: ["config"]

  allowed_dependencies:
    - { from: cli,            to: observability }
    - { from: cli,            to: service       }
    - { from: cli,            to: storage       }
    - { from: cli,            to: models        }
    - { from: observability,  to: service       }
    - { from: observability,  to: storage       }
    - { from: observability,  to: models        }
    - { from: service,        to: storage       }
    - { from: service,        to: models        }
    - { from: storage,        to: models        }

  quality_targets:
    max_complexity: 10   # NFR-11: function CC <= 10
    min_coverage: 100    # SPEC §8 #2: TOTAL 100%
    max_coupling: 0.3    # CRG cohesion target

  nfr_dimension_mapping: {}  # OPTIONAL — auto-derived from nfr_traceability.type

  nfr_traceability:
    # `dimension:` is copied verbatim from SRS.md's machine-readable
    # non_functional_requirements block (SRS.md §12) and OUTRANKS the
    # `type:` guess. Do not delete it: for 8 of the 12 NFRs the SRS
    # dimension differs from what `type` would auto-derive.
    NFR-01:
      type: performance
      dimension: performance
      target: "p95 < 50ms"
      module: "taskq_plus.cli.commands"
    NFR-02:
      type: security
      dimension: security
      target: "0 HIGH, 0 MEDIUM"
      module: "taskq_plus.service.executor"
    NFR-03:
      type: reliability        # `reliability` is the closest legal `type:` enum member;
      dimension: error_handling  # the SRS-declared gate dimension (SRS.md §12) wins.
      target: "no bare except"
      module: "taskq_plus.storage.atomic"
    NFR-04:
      type: security
      dimension: security
      target: "0 secrets on disk"
      module: "taskq_plus.models.redaction"
    NFR-05:
      type: documentation
      dimension: documentation
      target: "100%"
      module: "taskq_plus"
    NFR-06:
      type: layering
      dimension: architecture_constraints
      target: "lint-imports exit 0"
      module: "taskq_plus"
    NFR-07:
      type: licensing
      dimension: license_compliance
      target: "all licenses in allowlist"
      module: "requirements.txt"
    NFR-08:
      type: mutation
      dimension: mutation_testing
      target: "score >= 70"
      module: "taskq_plus.service"
      # SRS.md NFR-08 / AC-08-3 limit mutation to service/ + storage/.
      # mutmut_scope.py reads this list to emit paths_to_mutate.
      scope_layers: ["service", "storage"]
    NFR-09:
      type: testability
      dimension: test_assertion_quality
      target: "skipped == 0"
      module: "tests"
    NFR-10:
      type: integration
      dimension: integration_coverage
      target: ">= 80%"
      module: "tests/integration"
    NFR-11:
      type: maintainability
      dimension: readability
      target: "MI >= 80"
      module: "taskq_plus"
    NFR-12:
      type: verifiability
      dimension: execute_verification_target
      target: "make verify-system exit 0"
      module: "Makefile"

  advisory_only: []  # AUTO-FILLED by parser — omit or leave []

  gate_score_overrides: {}  # AUTO-DERIVED by parser — omit or leave {}

  fr_module_traceability:
    FR-01: ["taskq_plus.models.task", "taskq_plus.storage.task_store", "taskq_plus.cli.commands"]
    FR-02: ["taskq_plus.service.executor", "taskq_plus.cli.commands"]
    FR-03: ["taskq_plus.service.breaker", "taskq_plus.service.executor", "taskq_plus.storage.breaker_store"]
    FR-04: ["taskq_plus.service.cache", "taskq_plus.storage.cache_store"]
    FR-05: ["taskq_plus.cli.main", "taskq_plus.cli.commands"]
    FR-06: ["taskq_plus.service.dag", "taskq_plus.models.task"]
    FR-07: ["taskq_plus.service.plugins", "taskq_plus.cli.commands"]
    FR-08: ["taskq_plus.observability.audit", "taskq_plus.observability.export", "taskq_plus.cli.commands"]

  architecture_constraints:
    - "no_circular_dependencies"
    - "five_layer_hierarchy"
    - "config_independence"

  high_risk_modules:
    - "taskq_plus.service.executor"
    - "taskq_plus.service.plugins"
    - "taskq_plus.storage.task_store"
```
<!-- SAB:END -->

## 6. Security Design (STRIDE-lite — machine-readable, BINDING CONTRACT)

> **CONTRACT**: Field names and the `security_design:` root key are
> parsed by `core/quality_gate/security_design.py:extract_security_block()`.
> Do NOT hand-write the YAML — pasted from the canonical template via
> `render_canonical_security_template()` with EXAMPLE values replaced
> by the project's real values.
> Validate: `python3 harness_cli.py check-artifact-consistency --project .`

The system has a small but real attack surface — local CLI input and
plugin allowlist. Threats T-01 through T-06 below cover every entry
point that crosses a trust boundary. `applicability: full` is the
honest declaration.

<!-- SEC:START -->
```yaml
security_design:
  version: "1.0"
  applicability: full
  justification: ""  # not required when applicability: full
  trust_boundaries:
    - id: TB-01
      name: "CLI user → submit validator"
      description: "user-supplied command string entering TaskSubmission validation"
    - id: TB-02
      name: "validator → subprocess"
      description: "validated command string becoming argv in subprocess.run"
    - id: TB-03
      name: "CLI user → plugin loader"
      description: "TASKQ_PLUGINS env value crossing into importlib.import_module"
    - id: TB-04
      name: "subprocess → audit log"
      description: "subprocess stdout/stderr crossing into the audit.jsonl writer"
    - id: TB-05
      name: "executor → cache"
      description: "command string entering cache key (sha256) and persisted cache.json"
    - id: TB-06
      name: "filesystem → loaders"
      description: "TASKQ_HOME directory contents feeding TaskStore / BreakerStore / CacheStore"
  threats:
    - id: T-01
      boundary: TB-01
      category: tampering
      description: "shell metacharacters (;|&$><`) in submit command escape argument context"
      mitigation: "TaskSubmission rejects commands containing any of ; | & $ > < `; length cap 1000; covered by FR-01 unit tests per character"
      owner_module: "taskq_plus.models.task"
      nfr: NFR-02
      verified_by: "test_fr01_submit_rejects_shell_metacharacters"
    - id: T-02
      boundary: TB-02
      category: elevation_of_privilege
      description: "subprocess.run invoked with shell=True enables shell injection"
      mitigation: "subprocess.run always takes argv list; shell=False everywhere; grep + bandit CI gate at 0"
      owner_module: "taskq_plus.service.executor"
      nfr: NFR-02
      verified_by: "test_nfr02_no_shell_true_anywhere"
    - id: T-03
      boundary: TB-03
      category: spoofing
      description: "TASKQ_PLUGINS contains a path or URL masquerading as a module name"
      mitigation: "name validated against ^[A-Za-z_][A-Za-z0-9_.]*$ before importlib.import_module; no path or URL loader exists"
      owner_module: "taskq_plus.service.plugins"
      nfr: NFR-02
      verified_by: "test_fr07_plugin_allowlist_rejects_path"
    - id: T-04
      boundary: TB-03
      category: elevation_of_privilege
      description: "eval / exec / __import__ of dynamic strings enables arbitrary code execution"
      mitigation: "static-import only; grep CI gate forbids eval(/exec(/__import__( in src/"
      owner_module: "taskq_plus.service.plugins"
      nfr: NFR-02
      verified_by: "test_nfr02_no_eval_exec_in_src"
    - id: T-05
      boundary: TB-04
      category: information_disclosure
      description: "subprocess output contains an API key or bearer token; written to audit.jsonl"
      mitigation: "observability.audit redacts matching lines via regex (sk-[A-Za-z0-9_-]{8,}|token=\\S+|Bearer\\s+\\S+) before write"
      owner_module: "taskq_plus.observability.audit"
      nfr: NFR-04
      verified_by: "test_nfr04_secret_redacted_before_audit_write"
    - id: T-06
      boundary: TB-06
      category: denial_of_service
      description: "corrupted or hostile tasks.json / breaker.json / cache.json crashes startup or poisons state"
      mitigation: "loaders detect invalid JSON or schema mismatch and raise StoreCorruptedError → exit 1 (no silent rebuild)"
      owner_module: "taskq_plus.storage.task_store"
      nfr: NFR-03
      verified_by: "test_nfr03_store_corruption_exits_one"
    - id: T-07
      boundary: TB-05
      category: information_disclosure
      description: "cache.json persists command string and subprocess stdout; if either embeds a secret, it lands on disk unredacted"
      mitigation: "cache_store reuses the same redaction regex as observability.audit (sk-[A-Za-z0-9_-]{8,}|token=\\S+|Bearer\\s+\\S+) before persisting cache entry"
      owner_module: "taskq_plus.storage.cache_store"
      nfr: NFR-04
      verified_by: "test_nfr04_cache_entry_redacts_secrets"
```
<!-- SEC:END -->

Note: `owner_module` values name modules declared in the §5 SAB block;
`nfr` references exist in SPEC §4; `verified_by` names single tests
that prove the mitigation. Threats T-01–T-06 also seed
`bug-hunt-targets` adversarial review and force NFR-pattern test cases
in `derive_test_cases.md` Step 1c.
