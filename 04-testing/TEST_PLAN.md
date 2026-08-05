# TEST_PLAN.md — taskq-renew

> **Phase**: 4 — Testing
> **Generated**: 2026-08-05
> **Source spec**: `01-requirements/SRS.md` (FR-01 … FR-08, NFR-01 … NFR-12)
> **Quality manifest**: `.methodology/quality_manifest.json` (`fr_ids`: FR-01..FR-08; `nfr_dimension_mapping`: NFR-01..NFR-12)
> **Scope**: Per-FR test-case plan (positive / negative / boundary / edge). Each row maps 1:1 to a `SRS.md` acceptance criterion (AC) and a function in `03-development/tests/test_frNN.py` or `test_nfrNN.py`.
> **Run-once**: This file is generated **once** before per-FR testing begins (CHECKPOINT-0). Per-FR TDD execution does **not** modify this plan; it adds new test cases under each `TP-FR-NN-NN` ID if a regression forces it.

---

## 0. Coverage Matrix (manifest ↔ plan)

| Manifest FR / NFR | Dimension | # ACs in SRS | Test cases below | Status |
|-------------------|-----------|--------------|-------------------|--------|
| FR-01 | — | 5 | TP-FR-01-01 … TP-FR-01-08 (8) | covered |
| FR-02 | — | 4 | TP-FR-02-01 … TP-FR-02-08 (8) | covered |
| FR-03 | — | 3 | TP-FR-03-01 … TP-FR-03-06 (6) | covered |
| FR-04 | — | 2 | TP-FR-04-01 … TP-FR-04-05 (5) | covered |
| FR-05 | — | 3 | TP-FR-05-01 … TP-FR-05-10 (10) | covered |
| FR-06 | — | 3 | TP-FR-06-01 … TP-FR-06-06 (6) | covered |
| FR-07 | — | 3 | TP-FR-07-01 … TP-FR-07-05 (5) | covered |
| FR-08 | — | 2 | TP-FR-08-01 … TP-FR-08-05 (5) | covered |
| NFR-01 | performance | 2 | TP-NFR-01-01, TP-NFR-01-02 | covered |
| NFR-02 | security | 4 | TP-NFR-02-01 … TP-NFR-02-05 | covered |
| NFR-03 | error_handling | 3 | TP-NFR-03-01 … TP-NFR-03-03 | covered |
| NFR-04 | security | 3 | TP-NFR-04-01 … TP-NFR-04-04 | covered |
| NFR-05 | documentation | 2 | TP-NFR-05-01, TP-NFR-05-02 | covered |
| NFR-06 | architecture_constraints | 3 | TP-NFR-06-01 … TP-NFR-06-03 | covered |
| NFR-07 | license_compliance | 3 | TP-NFR-07-01 … TP-NFR-07-03 | covered |
| NFR-08 | mutation_testing (WAIVED) | 3 | TP-NFR-08-01 … TP-NFR-08-03 (config / scope only; score AC WAIVED) | covered |
| NFR-09 | test_assertion_quality | 5 | TP-NFR-09-01 … TP-NFR-09-05 | covered |
| NFR-10 | integration_coverage | 2 | TP-NFR-10-01, TP-NFR-10-02 | covered |
| NFR-11 | readability | 4 | TP-NFR-11-01 … TP-NFR-11-04 | covered |
| NFR-12 | execute_verification_target | 2 | TP-NFR-12-01, TP-NFR-12-02 | covered |

**Priority legend.** `P0` = blocks Gate 3 (must pass); `P1` = blocks Gate 1 per-FR (must pass for the FR to be COMPLETE); `P2` = negative / regression; `P3` = nice-to-have / future. Categories: `POS` positive, `NEG` negative, `BOUND` boundary, `EDGE` edge-case.

**Test runner.** `pytest -q` invoked from repo root. Subprocess tests use `python -m taskq_plus …`; in-process tests import `taskq_plus.cli.commands` / `taskq_plus.service.*` modules.

**NFR-08 status.** AC-NFR-08-2 (mutation score ≥ 70) is **WAIVED** per `SRS.md` §4 NFR-08 (harness-side `tempfile.mkdtemp` bootstrap defect — `mutation_enforcer.py:1113` does not seed the workdir). TP-NFR-08-01 / -03 (config / scope annotation) still run as dedicated assertions; TP-NFR-08-02 (score ≥ 70) is recorded but **not** blocking until the framework bootstrap is repaired.

---

## 1. FR-01 — 任務提交與驗證 (Task Submission & Validation)

> Modules: `taskq_plus.models.task`, `taskq_plus.storage.task_store`, `taskq_plus.cli.commands`.
> Source file: `03-development/tests/test_fr01.py`.

| Test ID | Cat | Pri | Description | Input | Expected Output | Maps to |
|---------|-----|----|-------------|-------|------------------|---------|
| TP-FR-01-01 | POS | P0 | Happy path — submit a valid command via CLI subprocess; verify exit 0, stdout is 8-hex id, `$TASKQ_HOME/tasks.json` contains one `pending` task with `command`, `name`, `created_at`, `depends_on` fields, and one `submit` audit event was emitted. | `python -m taskq_plus submit "echo hi"` | stdout = `[0-9a-f]{8}`, exit 0; tasks.json has one task id matching stdout; audit.jsonl contains `{"event":"submit","task_id":<id>,…}` | AC-01-1 |
| TP-FR-01-02 | NEG | P0 | Reject empty command — exit 2, no storage write, no audit event. | `python -m taskq_plus submit ""` | exit 2, stderr error, tasks.json unchanged | AC-01-2 |
| TP-FR-01-03 | NEG | P0 | Reject whitespace-only command (boundary of "non-empty" rule). | `python -m taskq_plus submit "   "` | exit 2, stderr error | AC-01-2 (boundary) |
| TP-FR-01-04 | NEG | P0 | Reject command > 1000 chars (length boundary). | `python -m taskq_plus submit "<1001 'a' chars>"` | exit 2, stderr error mentions length | FR-01 length rule |
| TP-FR-01-05 | BOUND | P0 | Accept command exactly 1000 chars (boundary). | `python -m taskq_plus submit "<1000 'a' chars>"` | exit 0 | FR-01 length rule boundary |
| TP-FR-01-06 | NEG | P0 | Reject injection character `;` (canonical SPEC §8 #6). | `python -m taskq_plus submit "echo hi; rm x"` | exit 2, stderr mentions injection char | AC-01-3 / NFR-02 AC-02-2 |
| TP-FR-01-07 | NEG | P0 | Reject injection character `\|` (canonical pipe — separate from `;` per FR-01 blacklist). | `python -m taskq_plus submit "cat a \| head"` | exit 2 | NFR-02 AC-02-2 (per-character coverage) |
| TP-FR-01-08 | NEG | P0 | Reject injection characters `&`, `$`, `>`, `<`, `` ` `` (one test per character — 5 cases). | `submit "echo & echo"`, `submit "echo $foo"`, `submit "echo > x"`, `submit "echo < x"`, `` submit "echo `pwd`" `` | all five exit 2 | NFR-02 AC-02-2 |
| TP-FR-01-09 | NEG | P0 | Reject `--name` collision with an existing pending task. | submit "echo a" --name X; submit "echo b" --name X | first exit 0, second exit 2 with stderr naming X | AC-01-4 |
| TP-FR-01-10 | NEG | P0 | Reject `--name` collision when first task has reached `done` (positive edge: name uniqueness only enforces on `pending`/`running`). | submit "echo a" --name X; run <a>; submit "echo b" --name X | both succeed (a=pending→done, b=pending) | FR-01 name-uniqueness rule edge |
| TP-FR-01-11 | NEG | P0 | Reject `--after <unknown-id>` — exit 2 with stderr `unknown dependency: <id>`. | `python -m taskq_plus submit "echo b" --after deadbeef` | exit 2, stderr `unknown dependency: deadbeef` | AC-01-5 |
| TP-FR-01-12 | POS | P1 | `--json` mode prints single-line JSON `{"id":…,"status":"pending"}` (no other key on stdout). | `python -m taskq_plus submit --json "echo hi"` | exit 0; stdout parses as one JSON object with `id` (8-hex) and `status="pending"`; nothing else on stdout | FR-01 `--json` clause |
| TP-FR-01-13 | EDGE | P2 | Submit command containing only safe characters but mixed unicode (e.g. `echo 你好`) — should be accepted. | `python -m taskq_plus submit "echo 你好"` | exit 0, id 8-hex | FR-01 unicode boundary |
| TP-FR-01-14 | EDGE | P1 | Submit with multiple `--after` flags — all must exist (positive multi-dep edge). | submit "echo c" --after a --after b (a, b both pending) | exit 0; task.depends_on == [a, b] | FR-01 multi-dep edge |
| TP-FR-01-15 | NEG | P1 | Submit with multiple `--after` flags where one is unknown — exit 2. | submit "echo c" --after a --after deadbeef | exit 2, stderr names deadbeef | FR-01 multi-dep negative |
| TP-FR-01-16 | POS | P1 | Atomic write — `tasks.json` is either the previous valid state or the new valid state after a submit (no truncation to empty / partial JSON). | kill -9 mid-submit (via injected fault); next read of tasks.json parses as JSON. | next `status <id>` works or returns exit 2 cleanly; no JSON parse error | NFR-03 AC-03-1 (cross-cut) |

**Coverage summary for FR-01:** 16 test cases (1+2+3+4+5+6+7+8 → TP-FR-01-01..16; mapped to AC-01-1..5 + NFR-02 + NFR-03). Categories: POS (3), NEG (8), BOUND (2), EDGE (3).

---

## 2. FR-02 — 任務執行器 (Task Executor)

> Modules: `taskq_plus.service.executor`, `taskq_plus.cli.commands`.
> Source file: `03-development/tests/test_fr02.py`.

| Test ID | Cat | Pri | Description | Input | Expected Output | Maps to |
|---------|-----|----|-------------|-------|------------------|---------|
| TP-FR-02-01 | POS | P0 | `run <id>` of a successful command — exit 0, status `done`, exit_code 0, stdout_tail last 2000 chars of `echo hi`. | submit "echo hi"; run <id> | status=done, exit_code=0, stdout_tail ends with "hi\n" | AC-02-1 |
| TP-FR-02-02 | POS | P1 | `run <id>` is **idempotent on replay**: same command produces same `exit_code` / `stdout_tail`. | submit "echo X"; run <id>; submit "echo X" (new id); run <new-id> | both tasks have exit_code=0 and identical stdout_tail | AC-02-1 |
| TP-FR-02-03 | NEG | P0 | `run <id>` on a failing command — status `failed`, exit_code ≠ 0. | submit "false"; run <id> | status=failed, exit_code=1, no timeout | FR-02 state machine |
| TP-FR-02-04 | BOUND | P0 | `TASKQ_TASK_TIMEOUT=1 run <sleep-5-id>` → status `timeout`, exit 4. | submit "sleep 5"; TASKQ_TASK_TIMEOUT=1 run <id> | exit 4, status=timeout | AC-02-2 |
| TP-FR-02-05 | NEG | P0 | No subprocess leak — `subprocess.run` is invoked without `shell=True`. | `grep -rn "shell=True" 03-development/src/` | 0 matches | AC-02-3 / NFR-02 AC-02-1 |
| TP-FR-02-06 | EDGE | P0 | Two parallel `run --all` invocations on independent commands leave `tasks.json` valid JSON (concurrent write safety). | 2× concurrent `run --all` with disjoint task sets | final tasks.json parses; all tasks in a coherent state (no torn fields) | AC-02-4 |
| TP-FR-02-07 | EDGE | P0 | Mid-write `kill -9` of `tasks.json` write leaves the file valid JSON on next start (atomicity). | kill -9 mid-write (via injected fault on os.replace); restart | tasks.json parses as JSON; no `json.JSONDecodeError` | AC-02-4 / NFR-03 AC-03-1 |
| TP-FR-02-08 | POS | P1 | Result fields populated: `exit_code`, `stdout_tail` (last 2000 chars), `stderr_tail` (last 2000 chars), `duration_ms`, `finished_at`. | submit "printf '<5000 a>'; echo err 1>&2"; run <id> | all 5 fields present; stdout_tail/stderr_tail truncated to ≤ 2000 chars | FR-02 result schema |
| TP-FR-02-09 | BOUND | P1 | stdout_tail boundary: command emitting 2000 chars exactly → stored verbatim; 2001 chars → truncated to last 2000. | submit "python -c 'print(\"a\"*2000)'"; run; submit "python -c 'print(\"a\"*2001)'"; run | first stdout_tail.len == 2000; second == 2000 | FR-02 truncation rule |
| TP-FR-02-10 | POS | P1 | `run --all` with no pending tasks — exit 0, no work. | `python -m taskq_plus run --all` (no pending) | exit 0 | FR-02 `--all` positive |
| TP-FR-02-11 | NEG | P1 | `run <unknown-id>` — exit 2 (input validation). | `python -m taskq_plus run deadbeef` | exit 2, stderr unknown id | FR-05 exit-code table |
| TP-FR-02-12 | EDGE | P1 | Subprocess emitting 0 bytes (e.g. `true`) — stdout_tail empty, exit_code 0. | submit "true"; run <id> | exit_code=0, stdout_tail="" or empty | FR-02 edge |

**Coverage summary for FR-02:** 12 test cases. Categories: POS (5), NEG (3), BOUND (2), EDGE (2). ACs covered: AC-02-1, AC-02-2, AC-02-3, AC-02-4.

---

## 3. FR-03 — 重試與斷路器 (Retry & Circuit Breaker)

> Modules: `taskq_plus.service.breaker`, `taskq_plus.service.executor`, `taskq_plus.storage.breaker_store`.
> Source file: `03-development/tests/test_fr03.py`.

| Test ID | Cat | Pri | Description | Input | Expected Output | Maps to |
|---------|-----|----|-------------|-------|------------------|---------|
| TP-FR-03-01 | POS | P0 | Retry exponential backoff: a `failed` task with `TASKQ_RETRY_LIMIT=3` is re-run up to 3 times; backoff sleeps `TASKQ_BACKOFF_BASE × 2^n` seconds before the n-th retry; sleep function is injectable (monkey-patched). | submit "false"; monkey-patch sleep; run <id> with retry_limit=3 | sleep called with base × [1, 2, 4]; final state failed; 4 total executions | FR-03 retry rule |
| TP-FR-03-02 | NEG | P0 | After 3 consecutive final failures, next `run <id>` exits 3 with stderr `breaker open`; no subprocess is launched (assert via patched subprocess). | induce 3 consecutive failures; run <id> | exit 3, stderr `breaker open`, subprocess.run never invoked | AC-03-1 |
| TP-FR-03-03 | POS | P0 | After `TASKQ_BREAKER_COOLDOWN` elapses (clock injected), next `run <id>` succeeds → breaker `CLOSED`, failure count = 0. | 3 failures; advance clock by cooldown; run <id> (succeeds) | exit 0; breaker.json shows state=CLOSED, failures=0 | AC-03-2 |
| TP-FR-03-04 | BOUND | P0 | `OPEN → CLOSED` recovery time ≤ `TASKQ_BREAKER_COOLDOWN` + 1 s (timing bound). | 3 failures; measure wall time from OPEN→CLOSED | measured ≤ cooldown + 1 s | AC-03-3 |
| TP-FR-03-05 | EDGE | P0 | HALF_OPEN → exactly one task through (concurrency control). | OPEN; cooldown elapses; concurrent 2× run <a>, <b> | exactly one spawns subprocess; the other rejected (transition re-OPEN on failure, CLOSED on success) | FR-03 HALF_OPEN rule |
| TP-FR-03-06 | POS | P1 | Breaker state persists across processes via `$TASKQ_HOME/breaker.json` (atomic write). | process 1: 3 failures → OPEN; exit; process 2: run <id> | process 2 sees OPEN, exits 3 immediately (no subprocess) | FR-03 cross-process persistence |
| TP-FR-03-07 | EDGE | P1 | A single task that succeeds on retry (transient failure) does not increment the breaker failure counter — only **final** failures count. | submit "sh -c 'exit 1; exit 0'" with patched retrier; run | task eventually done; breaker failures=0 | FR-03 final-failure semantics |
| TP-FR-03-08 | POS | P1 | Atomic write of `breaker.json` survives mid-write `kill -9`. | kill -9 mid-write; restart | breaker.json parses as JSON; state coherent | NFR-03 AC-03-1 (cross-cut) |

**Coverage summary for FR-03:** 8 test cases. Categories: POS (4), NEG (1), BOUND (1), EDGE (2). ACs covered: AC-03-1, AC-03-2, AC-03-3.

---

## 4. FR-04 — 結果 TTL 快取 (TTL Result Cache)

> Modules: `taskq_plus.service.cache`, `taskq_plus.storage.cache_store`.
> Source file: `03-development/tests/test_fr04.py`.

| Test ID | Cat | Pri | Description | Input | Expected Output | Maps to |
|---------|-----|----|-------------|-------|------------------|---------|
| TP-FR-04-01 | POS | P0 | Within TTL, `run <id> --cached` returns prior `exit_code` / `stdout_tail`, sets `cached: true`, and does **not** spawn a subprocess (assert via patched `subprocess.run`). | submit "echo hi"; run <id>; submit "echo hi" (new id); TASKQ_CACHE_TTL=60 run <new-id> --cached | exit_code=0, stdout_tail matches first run, task.cached=true, subprocess.run never called | AC-04-1 |
| TP-FR-04-02 | NEG | P0 | Cache miss: signature not in `cache.json` → run normally; on `done`, write entry to `cache.json` atomically. | delete cache.json; submit "echo X"; run <id> --cached | normal execution; cache.json now contains sha256("echo X") key with result | FR-04 cache-miss rule |
| TP-FR-04-03 | BOUND | P0 | After TTL elapses (clock injected), `run <id> --cached` re-executes. | submit "echo X"; run; advance clock > TTL; run --cached | subprocess.run invoked; cache entry refreshed | AC-04-2 |
| TP-FR-04-04 | EDGE | P0 | Cache key = `sha256(command)` — two syntactically-equivalent commands (e.g. extra whitespace) are **not** cached under the same key. | submit "echo X"; run; submit "echo  X" (2 spaces); run --cached | cache miss on second (different signature); no false replay | FR-04 cache-key rule |
| TP-FR-04-05 | EDGE | P1 | Cache only stores `done` results — a `failed` / `timeout` task does not poison the cache. | submit "false"; run; submit "false" (new id); run --cached | no cache entry written for failed run; second run also fails (no replay) | FR-04 cache-only-done rule |
| TP-FR-04-06 | POS | P1 | Cache write atomic: mid-write `kill -9` of `cache.json` leaves valid JSON. | kill -9 mid-write; restart | cache.json parses; no data loss for prior entries | NFR-03 AC-03-1 (cross-cut) |
| TP-FR-04-07 | POS | P1 | Cache TTL boundary: exactly at TTL edge (TTL-1ms → hit; TTL+1ms → miss). | inject clock; submit; run; advance clock TTL-1ms; run --cached (hit); advance clock TTL+1ms; run --cached (miss) | first hit, second miss | FR-04 TTL boundary |

**Coverage summary for FR-04:** 7 test cases. Categories: POS (3), NEG (1), BOUND (1), EDGE (2). ACs covered: AC-04-1, AC-04-2.

---

## 5. FR-05 — CLI 整合 (CLI Integration)

> Modules: `taskq_plus.cli.main`, `taskq_plus.cli.commands`.
> Source file: `03-development/tests/test_fr05.py`.

| Test ID | Cat | Pri | Description | Input | Expected Output | Maps to |
|---------|-----|----|-------------|-------|------------------|---------|
| TP-FR-05-01 | POS | P0 | `status <id> --json` prints one parseable JSON object containing every field stored at submit time. | submit "echo hi" --name N --after <a>; status <id> --json | stdout is single-line JSON; keys include id, command, name, status, depends_on, created_at | AC-05-1 |
| TP-FR-05-02 | POS | P0 | `clear` removes all four data files from `$TASKQ_HOME`, then exits 0. | populate tasks.json, breaker.json, cache.json, audit.jsonl; run `python -m taskq_plus clear` | exit 0; all four files absent afterward | AC-05-2 |
| TP-FR-05-03 | NEG | P0 | `clear` on empty `$TASKQ_HOME` exits 0 (idempotent — boundary). | empty TASKQ_HOME; `python -m taskq_plus clear` | exit 0 | FR-05 clear positive edge |
| TP-FR-05-04 | NEG | P0 | Exit code 0 (success path) — verified by happy `submit` + `status`. | submit "echo hi"; status <id> | exit 0 | AC-05-3 (code 0) |
| TP-FR-05-05 | NEG | P0 | Exit code 2 — input validation (e.g. unknown task id on `status`). | `python -m taskq_plus status deadbeef` | exit 2 | AC-05-3 (code 2) |
| TP-FR-05-06 | NEG | P0 | Exit code 3 — breaker open path. | 3 consecutive failures; run <id> | exit 3 | AC-05-3 (code 3) |
| TP-FR-05-07 | NEG | P0 | Exit code 4 — task timeout (single-task mode). | TASKQ_TASK_TIMEOUT=1 run <sleep-5-id> | exit 4 | AC-05-3 (code 4) |
| TP-FR-05-08 | NEG | P0 | Exit code 5 — dependency cycle. | submit A → B → A | exit 5 | AC-05-3 (code 5) |
| TP-FR-05-09 | NEG | P0 | Exit code 6 — plugin load failure. | TASKQ_PLUGINS="../evil.py" python -m taskq_plus plugins list | exit 6 | AC-05-3 (code 6) |
| TP-FR-05-10 | NEG | P1 | Exit code 1 — generic internal error (induced via stubbed side-effect). | patch a service fn to raise RuntimeError; trigger | exit 1 | AC-05-3 (code 1) |
| TP-FR-05-11 | POS | P1 | `list --status <S>` filters tasks by status (positive). | submit 3 tasks; run one to done; `list --status pending` | output contains only pending tasks | FR-05 list clause |
| TP-FR-05-12 | POS | P1 | `list --status <bad>` exits 2 (invalid filter value). | `python -m taskq_plus list --status nonsense` | exit 2 | FR-05 list validation |
| TP-FR-05-13 | BOUND | P1 | `--json` flag on every CLI subcommand produces single-line JSON parseable by `json.loads`. | smoke-test: submit, status, list, graph, plugins list, export (each with --json) | each stdout is valid single-line JSON | FR-05 `--json` global flag |

**Coverage summary for FR-05:** 13 test cases. Categories: POS (4), NEG (7), BOUND (1), EDGE (1). ACs covered: AC-05-1, AC-05-2, AC-05-3 (all 7 exit codes).

---

## 6. FR-06 — 任務相依 DAG (Dependency DAG)

> Modules: `taskq_plus.service.dag`, `taskq_plus.models.task`.
> Source file: `03-development/tests/test_fr06.py`.

| Test ID | Cat | Pri | Description | Input | Expected Output | Maps to |
|---------|-----|----|-------------|-------|------------------|---------|
| TP-FR-06-01 | POS | P0 | `submit "echo b" --after <a>` followed by `run --all` runs `b` only after `a` is `done` (Kahn topological order). | submit a; submit "echo b" --after <a>; run --all | a runs first; b runs after a is done | AC-06-1 |
| TP-FR-06-02 | NEG | P0 | If a dependency ends in a non-`done` state (e.g. `failed` / `timeout`), the downstream task is `blocked` (not executed; not counted in breaker failure counter). | submit a "false"; submit "echo b" --after <a>; run --all | a=failed; b=blocked; breaker failures unchanged | AC-06-1 negative |
| TP-FR-06-03 | NEG | P0 | Cycle A → B → A is rejected at the second submit with exit 5 and stderr containing the cycle path. | submit A; submit B --after A; submit A2 --after B (where A2 refers back to A) | the cycle-closing submit exits 5; stderr contains the cycle path (e.g. `A → B → A`) | AC-06-2 |
| TP-FR-06-04 | NEG | P0 | Depth cap: chain depth > `TASKQ_MAX_DAG_DEPTH` rejected with exit 5; stderr `dependency chain too deep: <n> > <max>`. | build chain of length > MAX_DAG_DEPTH | exit 5; stderr matches `dependency chain too deep: \d+ > \d+` | AC-06-3 |
| TP-FR-06-05 | BOUND | P0 | Depth cap boundary: chain length exactly `TASKQ_MAX_DAG_DEPTH` accepted; `TASKQ_MAX_DAG_DEPTH + 1` rejected. | chain length = max (accepted); chain length = max+1 (rejected) | accepted exit 0; rejected exit 5 | AC-06-3 boundary |
| TP-FR-06-06 | EDGE | P0 | `graph --format dot` emits Graphviz DOT; `graph --format text` emits indented tree. | submit 4-task diamond DAG; `graph --format dot`; `graph --format text` | DOT output starts with `digraph {`, contains `->` edges; text output is a hierarchical tree | FR-06 graph clause |
| TP-FR-06-07 | POS | P1 | Self-loop (task depending on itself) is rejected with exit 5 (cycle of length 1). | submit T; submit T2 --after T (where T2 == T id — only possible if reuse allowed) | exit 5 | FR-06 cycle edge |
| TP-FR-06-08 | POS | P1 | Diamond DAG (A → B, A → C, B → D, C → D) executes A first; B and C concurrently; D after both. | submit diamond DAG; run --all | execution order A → {B,C} → D; B,C may overlap in time | FR-06 Kahn layer semantics |

**Coverage summary for FR-06:** 8 test cases. Categories: POS (4), NEG (3), BOUND (1), EDGE (1). ACs covered: AC-06-1, AC-06-2, AC-06-3.

---

## 7. FR-07 — Plugin Hook 系統 (Plugin Hook System)

> Modules: `taskq_plus.service.plugins`, `taskq_plus.cli.commands`.
> Source file: `03-development/tests/test_fr07.py`.

| Test ID | Cat | Pri | Description | Input | Expected Output | Maps to |
|---------|-----|----|-------------|-------|------------------|---------|
| TP-FR-07-01 | NEG | P0 | Path-form plugin name rejected: `TASKQ_PLUGINS="../evil.py" plugins list` → exit 6. | `TASKQ_PLUGINS="../evil.py" python -m taskq_plus plugins list` | exit 6; stderr names the rejected module | AC-07-1 / NFR-02 AC-02-4 |
| TP-FR-07-02 | NEG | P0 | URL-form plugin rejected: `TASKQ_PLUGINS="https://evil/x.py" plugins list` → exit 6. | `TASKQ_PLUGINS="https://evil/x.py" python -m taskq_plus plugins list` | exit 6 | FR-07 / NFR-02 |
| TP-FR-07-03 | NEG | P1 | Plugin name violating regex (e.g. contains `-` or starts with digit) → rejected with exit 6. | `TASKQ_PLUGINS="my-plugin" python -m taskq_plus plugins list` | exit 6 | FR-07 regex clause |
| TP-FR-07-04 | POS | P0 | A plugin whose `pre_run` raises an exception: the underlying task still completes, and `audit.jsonl` contains a `plugin_error` event. | load a plugin stub that raises in pre_run; submit + run a task | task reaches final status; audit.jsonl has `{"event":"plugin_error",…}` line | AC-07-2 |
| TP-FR-07-05 | POS | P0 | A plugin that fails 3 consecutive `pre_run` invocations in one run is disabled for the remainder of that run; the audit trail records the disablement. | load raising plugin; run 4 tasks in --all | first 3 tasks each emit `plugin_error`; 4th task emits neither pre_run call nor `plugin_error`; audit contains `plugin_disabled` event | AC-07-3 |
| TP-FR-07-06 | POS | P1 | `plugins list` prints each plugin's module name, registered hooks, load status. | load a valid plugin with pre_run + post_run; `plugins list` | output contains module name, both hook names, status "loaded" | FR-07 plugins list clause |
| TP-FR-07-07 | EDGE | P1 | Plugin loaded successfully but exposes neither hook → load succeeds but `plugins list` shows no hooks. | stub plugin with no pre_run/post_run; `plugins list` | exit 0; module listed with hooks=[] | FR-07 permissive load edge |

**Coverage summary for FR-07:** 7 test cases. Categories: POS (3), NEG (3), EDGE (1). ACs covered: AC-07-1, AC-07-2, AC-07-3.

---

## 8. FR-08 — 結構化稽核日誌與匯出 (Audit Log & Export)

> Modules: `taskq_plus.observability.audit`, `taskq_plus.observability.export`, `taskq_plus.cli.commands`.
> Source file: `03-development/tests/test_fr08.py`.

| Test ID | Cat | Pri | Description | Input | Expected Output | Maps to |
|---------|-----|----|-------------|-------|------------------|---------|
| TP-FR-08-01 | POS | P0 | A successful `submit → run` produces `audit.jsonl` lines for `submit`, `run_start`, `run_end`, all sharing one `correlation_id`. | submit + run <id> (single CLI process) | 3 lines; `submit`/`run_start`/`run_end` events; same `correlation_id` field on all 3 | AC-08-1 |
| TP-FR-08-02 | POS | P0 | Each audit record carries `ts` (ISO-8601 UTC), `event`, `task_id`, `correlation_id`, `detail` fields. | submit + run; read audit.jsonl | every line is valid JSON; all 5 keys present; `ts` matches `^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$` | FR-08 schema |
| TP-FR-08-03 | POS | P0 | `export --format json` produces a single JSON array with fields mirroring `status`. | populate tasks; `export --format json` | stdout is parseable JSON array; each entry has the same field set as `status <id>` | AC-08-2 / FR-08 export |
| TP-FR-08-04 | POS | P0 | `export --format csv` produces a header row + one row per task; fields with `,` or `"` are correctly escaped (CSV quoting). | populate task with command containing both `,` and `"`; `export --format csv` | first row is header; per-task row count matches json array length; commas/quotes escaped per RFC 4180 | AC-08-2 / FR-08 CSV |
| TP-FR-08-05 | POS | P0 | `export --format md` produces a Markdown table; task count and field set agree with `json` and `csv`. | populate tasks; export md | markdown table; row count == json array length; same field set | AC-08-2 / FR-08 MD |
| TP-FR-08-06 | POS | P1 | Audit log uses append + fsync (atomic append). | submit 10 tasks in sequence | audit.jsonl has 10 lines; tail -f safe to read concurrently | NFR-03 AC-03-1 (cross-cut) |
| TP-FR-08-07 | NEG | P1 | Reject export format other than `json|csv|md` — exit 2. | `python -m taskq_plus export --format yaml` | exit 2 | FR-05 exit-code table |

**Coverage summary for FR-08:** 7 test cases. Categories: POS (6), NEG (1). ACs covered: AC-08-1, AC-08-2.

---

## 9. NFR-01 — 效能預算 (Performance)

> Source: `03-development/tests/test_perf.py` (pytest-benchmark).

| Test ID | Cat | Pri | Description | Input | Expected Output | Maps to |
|---------|-----|----|-------------|-------|------------------|---------|
| TP-NFR-01-01 | BOUND | P0 | `pytest-benchmark` on `submit + status` 100 iterations reports **p95 < 50 ms**. | benchmark 100× submit("echo hi") + status(<id>) | p95 < 50 ms | AC-01-1 |
| TP-NFR-01-02 | BOUND | P0 | `pytest-benchmark` on 200-task topological-sort phase of `run --all` reports **p95 < 200 ms** (excluding subprocess execution). | benchmark topo-sort over 200-node DAG | p95 < 200 ms | AC-01-2 |

**Coverage summary for NFR-01:** 2 test cases (both boundary). ACs covered: AC-01-1, AC-01-2.

---

## 10. NFR-02 — 執行與載入安全 (Security)

> Source: `03-development/tests/test_nfr02.py` + static grep / bandit gates.

| Test ID | Cat | Pri | Description | Input | Expected Output | Maps to |
|---------|-----|----|-------------|-------|------------------|---------|
| TP-NFR-02-01 | NEG | P0 | `grep -rn "shell=True\|eval(\|exec(" 03-development/src/` → **0 matches**. | grep as written | 0 matches, exit 0 | AC-02-1 |
| TP-NFR-02-02 | NEG | P0 | Per-character FR-01 blacklist coverage: 7 unit tests (one per char `; \| & $ > < \``) each asserting the submission is rejected. | submit "<cmd with char>" for each of 7 chars | all 7 → exit 2 | AC-02-2 |
| TP-NFR-02-03 | NEG | P0 | `bandit -r 03-development/src/` → exit 0; JSON `metrics._totals.HIGH == 0` and `MEDIUM == 0`. | bandit invocation | both counters 0 | AC-02-3 |
| TP-NFR-02-04 | NEG | P0 | `TASKQ_PLUGINS="../evil.py" python -m taskq_plus plugins list` exits 6 (path-form rejected — cross-cuts FR-07 AC-07-1). | as written | exit 6; stderr names rejected module | AC-02-4 |
| TP-NFR-02-05 | NEG | P1 | Plugin name regex `^[A-Za-z_][A-Za-z0-9_.]*$` enforced — names with `-`, leading digit, or `__import__` are rejected. | 3 separate plugin-name cases | all 3 → exit 6 | FR-07 regex clause (cross-cut) |

**Coverage summary for NFR-02:** 5 test cases. Categories: NEG (5). ACs covered: AC-02-1, AC-02-2, AC-02-3, AC-02-4.

---

## 11. NFR-03 — 錯誤處理與原子性 (Error Handling & Atomicity)

> Source: `03-development/tests/test_nfr03.py` + static scan.

| Test ID | Cat | Pri | Description | Input | Expected Output | Maps to |
|---------|-----|----|-------------|-------|------------------|---------|
| TP-NFR-03-01 | EDGE | P0 | Kill `-9` mid-write of each of `tasks.json` / `breaker.json` / `cache.json` / `audit.jsonl`; on next start, every file parses as valid JSON / JSONL. | inject fault on `os.replace` for each file in turn | next `status <id>` (or read of file) parses without error | AC-03-1 |
| TP-NFR-03-02 | NEG | P0 | `ast-error-handling` scan of `03-development/src/` reports `broad_swallow == 0`, `bare_except == 0`, and zero `except BaseException`. | run scan | all three counters == 0 | AC-03-2 |
| TP-NFR-03-03 | BOUND | P0 | After 3 consecutive failures and `TASKQ_BREAKER_COOLDOWN + 1 s` elapsed, `run <id>` succeeds and breaker is `CLOSED`. | induce 3 failures; advance clock cooldown+1s; run <id> | exit 0; breaker.json state=CLOSED | AC-03-3 |

**Coverage summary for NFR-03:** 3 test cases. Categories: NEG (1), BOUND (1), EDGE (1). ACs covered: AC-03-1, AC-03-2, AC-03-3.

---

## 12. NFR-04 — 敏感資料遮蔽 (Secret Redaction)

> Source: `03-development/tests/test_nfr04.py`.

| Test ID | Cat | Pri | Description | Input | Expected Output | Maps to |
|---------|-----|----|-------------|-------|------------------|---------|
| TP-NFR-04-01 | POS | P0 | Submit a command whose stdout contains `sk-XXXXXXXX` (≥ 8 chars after `sk-`); after `run`, `grep -c "sk-" $TASKQ_HOME/audit.jsonl` → 0. | submit "echo sk-ABCDEFGH1234"; run <id>; grep | 0 | AC-04-1 |
| TP-NFR-04-02 | POS | P0 | Same setup; `grep -c "sk-" $TASKQ_HOME/tasks.json` → 0 (redaction applies to stored result, not only audit). | as above; grep tasks.json | 0 | AC-04-2 |
| TP-NFR-04-03 | POS | P0 | `token=…` pattern redacted (positive unit test). | submit "echo token=abc123def"; run; grep | 0 matches for `token=` in audit.jsonl + tasks.json | AC-04-3 |
| TP-NFR-04-04 | POS | P0 | `Bearer …` pattern redacted (positive unit test). | submit "echo Bearer eyJabc"; run; grep | 0 matches for `Bearer` | AC-04-3 |

**Coverage summary for NFR-04:** 4 test cases. Categories: POS (4). ACs covered: AC-04-1, AC-04-2, AC-04-3.

---

## 13. NFR-05 — 文件覆蓋 (Documentation)

> Source: static scan (`ast-docstrings`).

| Test ID | Cat | Pri | Description | Input | Expected Output | Maps to |
|---------|-----|----|-------------|-------|------------------|---------|
| TP-NFR-05-01 | POS | P0 | `ast-docstrings` reports `public_with_docstring / total_public == 1.0`. | run scan over `03-development/src/taskq_plus` | ratio == 1.0 | AC-05-1 |
| TP-NFR-05-02 | POS | P0 | Regex sweep over `03-development/src/taskq_plus/**/*.py` finds `[FR-` or `[NFR-` tag in every public docstring; missing tag → test failure. | run sweep | 0 missing tags | AC-05-2 |

**Coverage summary for NFR-05:** 2 test cases. Categories: POS (2). ACs covered: AC-05-1, AC-05-2.

---

## 14. NFR-06 — 架構分層契約 (Architecture Layering)

> Source: static gate (`lint-imports`).

| Test ID | Cat | Pri | Description | Input | Expected Output | Maps to |
|---------|-----|----|-------------|-------|------------------|---------|
| TP-NFR-06-01 | POS | P0 | `lint-imports` exits 0. | run lint-imports | exit 0 | AC-06-1 |
| TP-NFR-06-02 | POS | P0 | `.importlinter` exists at repo root before Gate 1, declares the `cli > observability > service > storage > models` layers contract, and references `config` as an independence module. | read .importlinter; parse contract | present + correct topology | AC-06-2 |
| TP-NFR-06-03 | POS | P0 | `.methodology/harness_config.json` keeps `crg_cohesion_healthy` at its default value (calibration pin). | read config; compare to default | unchanged | AC-06-3 |

**Coverage summary for NFR-06:** 3 test cases. Categories: POS (3). ACs covered: AC-06-1, AC-06-2, AC-06-3.

---

## 15. NFR-07 — 依賴與授權合規 (Dependency & License Compliance)

> Source: static scans + `pip-licenses`.

| Test ID | Cat | Pri | Description | Input | Expected Output | Maps to |
|---------|-----|----|-------------|-------|------------------|---------|
| TP-NFR-07-01 | NEG | P0 | `grep -E "^[a-zA-Z0-9_-]+(~=|>=| *$)" requirements.txt` → 0 matches (all runtime deps pinned with `==`). | grep as written | 0 matches | AC-07-1 |
| TP-NFR-07-02 | POS | P0 | `pip-licenses --format=json --with-urls` (or scancode equivalent) reports every dep's license ∈ {MIT, BSD-2-Clause, BSD-3-Clause, Apache-2.0}. | run pip-licenses | all entries in allowlist | AC-07-2 |
| TP-NFR-07-03 | POS | P0 | `08-config/SBOM.json` exists, parses as JSON, and every entry contains `name`, `version`, `license` keys. | read SBOM | parses; all 3 keys present | AC-07-3 |

**Coverage summary for NFR-07:** 3 test cases. Categories: POS (2), NEG (1). ACs covered: AC-07-1, AC-07-2, AC-07-3.

---

## 16. NFR-08 — 變異測試 (Mutation Testing — WAIVED for score)

> Source: `harness_cli.py mutation-test-score` + config assertions.
> **WAIVER NOTE (2026-08-04, per SRS.md §4 NFR-08):** the framework-owned
> `compute_mutation_score` (`harness/core/quality_gate/mutation_enforcer.py:1113`)
> creates `tempfile.mkdtemp(prefix='_mutmut_score.', dir='/tmp')` without seeding
> it with the project source tree; mutmut then raises
> `RuntimeError: Tests don't run cleanly without mutations` against the empty
> workdir. The bootstrap is project-side unpatchable (HR-17 forbids editing
> `harness/`). AC-NFR-08-2 (score ≥ 70) is **WAIVED** until upstream repair;
> the configuration ACs below still run as dedicated CI assertions.

| Test ID | Cat | Pri | Description | Input | Expected Output | Maps to |
|---------|-----|----|-------------|-------|------------------|---------|
| TP-NFR-08-01 | POS | P0 | `.methodology/harness_config.json` parses and `features.mutation_testing == true`. | read config | true | AC-08-1 |
| TP-NFR-08-02 | POS | P2 | `harness_cli.py mutation-test-score --project .` exits 0 and reports `score >= 70` in `.methodology/mutation_score.json` — **WAIVED** until framework bootstrap repaired; recorded for re-enable. | run tool | exit 0, score ≥ 70 (currently blocked) | AC-08-2 (WAIVED) |
| TP-NFR-08-03 | POS | P0 | `harness_config.json` annotates scope limitation to `service/` + `storage/` with an "execution-time budget" rationale. | read config | scope + rationale present | AC-08-3 |

**Coverage summary for NFR-08:** 3 test cases. ACs covered: AC-08-1, AC-08-3 (config only); AC-08-2 WAIVED.

---

## 17. NFR-09 — 驗證真實性 / 零 skip 鐵律 (Verification Honesty)

> Source: `03-development/tests/test_nfr_static_anchors.py` + project-level scans.

| Test ID | Cat | Pri | Description | Input | Expected Output | Maps to |
|---------|-----|----|-------------|-------|------------------|---------|
| TP-NFR-09-01 | NEG | P0 | `pytest 03-development/tests -q` reports `... <n> passed in ...` with **no `skipped` count line**. | run pytest -q | `skipped` not present | AC-09-1 |
| TP-NFR-09-02 | NEG | P0 | `ast-assertions` reports `zero_assert == 0` (every test has ≥ 1 assert). | run scan | 0 | AC-09-2 |
| TP-NFR-09-03 | NEG | P0 | Static scan over `03-development/tests/` finds zero functional uses of `pytest.skip(`, `@pytest.mark.skip`, `skipif`, or `xfail` (string literals / docstrings excluded). | run scan | 0 matches | AC-09-3 |
| TP-NFR-09-04 | NEG | P0 | No harness-invoked command line contains `--ignore`, `-k <pattern excluding tests>`, `--deselect`, or `collect_ignore` entries that remove passing tests. | inspect harness invocations | none present | AC-09-4 |
| TP-NFR-09-05 | POS | P0 | Every `VERIFIED` row in `TRACEABILITY_MATRIX.md` is paired with a `pytest 03-development/tests::<test_id> -q` invocation that exits 0; rows without a paired, passing test are `NOT_VERIFIED`. | parse matrix; spot-check each VERIFIED row | all VERIFIED rows map to a passing test | AC-09-5 |

**Coverage summary for NFR-09:** 5 test cases. Categories: POS (1), NEG (4). ACs covered: AC-09-1, AC-09-2, AC-09-3, AC-09-4, AC-09-5.

---

## 18. NFR-10 — 整合覆蓋 (Integration Coverage)

> Source: `03-development/tests/integration/` + `pytest --cov`.

| Test ID | Cat | Pri | Description | Input | Expected Output | Maps to |
|---------|-----|----|-------------|-------|------------------|---------|
| TP-NFR-10-01 | BOUND | P0 | `pytest 03-development/tests/integration --cov=03-development/src --cov-report=term -q` reports **TOTAL line coverage ≥ 80 %**. | run pytest --cov | TOTAL ≥ 80 % | AC-10-1 |
| TP-NFR-10-02 | POS | P0 | Every integration test invokes the CLI through `CliRunner` or `subprocess.run(["python", "-m", "taskq_plus", ...])`, never by directly importing internal functions; the suite covers submit→run→status, multi-layer DAG, breaker open/close, cache hit, plugin hook, and all three export formats. | static scan + test list review | all 6 scenarios covered; entry-point invariant enforced | AC-10-2 |

**Coverage summary for NFR-10:** 2 test cases. Categories: POS (1), BOUND (1). ACs covered: AC-10-1, AC-10-2.

---

## 19. NFR-11 — 可讀性 (Readability)

> Source: static scans (`radon`, `wc`, `find`).

| Test ID | Cat | Pri | Description | Input | Expected Output | Maps to |
|---------|-----|----|-------------|-------|------------------|---------|
| TP-NFR-11-01 | BOUND | P0 | `radon mi -j 03-development/src` reports `mean(mi) >= 80`. | run radon mi | mean(mi) ≥ 80 | AC-11-1 |
| TP-NFR-11-02 | NEG | P0 | `radon cc -s -a 03-development/src` reports every function with rank A or B (CC ≤ 10); no C/D/E/F functions. | run radon cc | no rank > B | AC-11-2 |
| TP-NFR-11-03 | BOUND | P0 | `find 03-development/src -name "*.py" -exec wc -l {} +` reports no line count > 400. | run wc | max ≤ 400 | AC-11-3 |
| TP-NFR-11-04 | NEG | P0 | Every directory under `03-development/src/taskq_plus` contains ≤ 15 files. | enumerate dirs | all ≤ 15 | AC-11-4 |

**Coverage summary for NFR-11:** 4 test cases. Categories: POS (0), NEG (2), BOUND (2). ACs covered: AC-11-1, AC-11-2, AC-11-3, AC-11-4.

---

## 20. NFR-12 — 系統驗證目標 (System Verification Target)

> Source: `Makefile` (`verify-system` target).

| Test ID | Cat | Pri | Description | Input | Expected Output | Maps to |
|---------|-----|----|-------------|-------|------------------|---------|
| TP-NFR-12-01 | POS | P0 | `make verify-system` exits 0. | run make | exit 0 | AC-12-1 |
| TP-NFR-12-02 | POS | P0 | `make verify-system` stdout contains the literal substring `verify-system: PASS`. | run make | stdout contains substring | AC-12-2 |

**Coverage summary for NFR-12:** 2 test cases. Categories: POS (2). ACs covered: AC-12-1, AC-12-2.

---

## 21. Aggregate Summary

| Bucket | Test cases | POS | NEG | BOUND | EDGE |
|--------|------------|-----|-----|-------|------|
| FR-01 | 16 | 3 | 8 | 2 | 3 |
| FR-02 | 12 | 5 | 3 | 2 | 2 |
| FR-03 | 8  | 4 | 1 | 1 | 2 |
| FR-04 | 7  | 3 | 1 | 1 | 2 |
| FR-05 | 13 | 4 | 7 | 1 | 1 |
| FR-06 | 8  | 4 | 3 | 1 | 1 (one row is self-loop) |
| FR-07 | 7  | 3 | 3 | 0 | 1 |
| FR-08 | 7  | 6 | 1 | 0 | 0 |
| NFR-01 | 2 | 0 | 0 | 2 | 0 |
| NFR-02 | 5 | 0 | 5 | 0 | 0 |
| NFR-03 | 3 | 0 | 1 | 1 | 1 |
| NFR-04 | 4 | 4 | 0 | 0 | 0 |
| NFR-05 | 2 | 2 | 0 | 0 | 0 |
| NFR-06 | 3 | 3 | 0 | 0 | 0 |
| NFR-07 | 3 | 2 | 1 | 0 | 0 |
| NFR-08 | 3 | 3 | 0 | 0 | 0 (1 WAIVED) |
| NFR-09 | 5 | 1 | 4 | 0 | 0 |
| NFR-10 | 2 | 1 | 0 | 1 | 0 |
| NFR-11 | 4 | 0 | 2 | 2 | 0 |
| NFR-12 | 2 | 2 | 0 | 0 | 0 |
| **Total** | **117** | **48** | **39** | **13** | **13** |

**FR coverage check (against `quality_manifest.json` `fr_ids`):**

| FR | Has cases? | AC count in SRS | Cases mapped |
|----|-----------|------------------|--------------|
| FR-01 | yes (16) | 5 | AC-01-1..5 + NFR-02 + NFR-03 |
| FR-02 | yes (12) | 4 | AC-02-1..4 |
| FR-03 | yes (8)  | 3 | AC-03-1..3 |
| FR-04 | yes (7)  | 2 | AC-04-1..2 |
| FR-05 | yes (13) | 3 | AC-05-1..3 (covers all 7 exit codes) |
| FR-06 | yes (8)  | 3 | AC-06-1..3 |
| FR-07 | yes (7)  | 3 | AC-07-1..3 |
| FR-08 | yes (7)  | 2 | AC-08-1..2 |

All 8 manifest FRs have ≥ 1 test case. All 12 manifest NFRs have ≥ 1 test case. Plan PASSES the coverage check.

---

## 22. Test ID Naming Convention (for per-FR TDD)

- Format: `TP-{FR|NFR}-{NN}-{NN}` — e.g. `TP-FR-01-01`, `TP-NFR-02-03`.
- The first NN matches the manifest FR / NFR index.
- The second NN is a per-FR / per-NFR sequence (01 = canonical happy path; 02… = extensions).
- Python test functions are named after this ID where possible: `test_tp_fr_01_01_happy_path`, etc. Existing `test_fr01.py` functions already follow a similar `test_fr01_*` convention — do **not** rename them (they are the canonical names `spec-coverage-check` looks up); the TP ID is the **plan-side** handle, the `test_frNN_xxx` name is the **implementation-side** handle.
- Audit-pipeline aliases (C5 / phase-auditor Document-Content-Depth check) use a lightweight `TC-N` index that maps 1:1 to a stable subset of `TP-*` IDs:
  - TC-1 — alias for `TP-FR-01-01` (FR-01 canonical happy path).
  - TC-2 — alias for `TP-FR-02-01` (FR-02 canonical happy path).
  - TC-3 — alias for `TP-FR-05-01` (FR-05 canonical happy path).

---

## 23. Out-of-Scope (per SRS.md §6)

- Audit-log rotation (R10 / NFR-99) — explicitly not implemented this round; no test plan entry.
- Remote / distributed execution, web UI, DB backend, multi-user auth, TypeScript variant — out of project scope.

---

## 24. References

- `01-requirements/SRS.md` — canonical SRS (single source of truth for FR / NFR / AC text)
- `01-requirements/TRACEABILITY_MATRIX.md` — VERIFIED pairing table (populated during P5 verification)
- `.methodology/quality_manifest.json` — manifest driving this plan's coverage matrix
- `.methodology/phase4_plan.md` — Phase 4 execution plan (CHECKPOINT-0 references this file)
- `harness/harness/ssi/prompts/evaluate_dimension.md` — dimension roster (NFR dimension tags)
- `03-development/tests/` — per-FR + per-NFR test files (consumers of this plan)
