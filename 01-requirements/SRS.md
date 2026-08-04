# Software Requirements Specification (SRS) — taskq

> **Single source of truth:** `SPEC.md` v1.0.0 (2026-07-30) at the project root. This SRS is the
> Phase-1 INGESTION-MODE transcription of that spec — 8 FR / 12 NFR / 12 env vars, no invention,
> no silent omission. Every `### FR-NN` and `### NFR-NN` section below cites the canonical line(s)
> it was transcribed from. Open Issues (§7) capture the canonical gaps the spec itself flags
> (R10 audit-log rotation: not implemented this round).
>
> **Test-bed context:** round 1 of 3 — Python CLI补洞版. The previous test-bed (`taskq`,
> Gate 4 composite 97.4) silently produced no signal on five quality dimensions; every
> countermeasure is bound to a specific NFR below (NFR-06 / NFR-07 / NFR-08 / NFR-09 / §10 CRG
> calibration). See `PROJECT_BRIEF.md` "Why this project exists" for the full table.

## 1. Introduction

### 1.1 Purpose

Define the requirements of `taskq`, a local task-queue CLI that submits shell commands as
tasks, executes them under controlled concurrency / timeout / retry / circuit-breaker / TTL
result-cache / **dependency-DAG ordering**, exposes an allowlisted **plugin-hook** system, emits a
**structured JSONL audit trail**, and exports results as JSON / CSV / Markdown. This SRS is
the human-readable contract for Phase 2 architecture, Phase 3 development, and Phase 5+
verification.

### 1.2 Scope

In scope: CLI front-end; submit / run / status / list / graph / plugins / export / clear
sub-commands; subprocess execution model; shared thread-safe store; DAG scheduler; circuit
breaker; TTL result cache; plugin loader; JSONL audit log; three export formats.

Out of scope: remote / distributed execution; web UI; database backend; audit-log rotation;
multi-user authorisation; TypeScript variant (round 3, deferred).

### 1.3 Audience

Phase 2 architect (Agent B), Phase 3 developer (Agent C), Phase 4 reviewer (Agent D), and the
harness-methodology framework itself (P1–P8 pipeline).

### 1.4 References

| ID | Document | Role |
|----|----------|------|
| SPEC | `SPEC.md` v1.0.0 (2026-07-30) | canonical spec; **all FR / NFR / env / data-file tables below are transcribed from it** |
| BRIEF | `PROJECT_BRIEF.md` | test-bed intent + previous-gap table |
| EVAL | `harness/harness/ssi/prompts/evaluate_dimension.md` | harness dimension roster (current, authoritative for `dimension:` field validation) |

## 2. Constraints

> Verbatim from `PROJECT_BRIEF.md` "Key Constraints" and `SPEC.md` §1 / §2.

- **Language / runtime:** Python 3.11; CLI entry `python -m taskq_plus`.
- **CLI framework:** `click` command groups (FR-05).
- **Validation:** `pydantic` v2 models (FR-01).
- **Subprocess:** `subprocess.run` with `shlex.split`, **no `shell=True` anywhere** (NFR-02).
- **Concurrency:** `concurrent.futures.ThreadPoolExecutor` for `run --all`; shared
  `threading.Lock` over the store (FR-02).
- **Dependency graph:** Kahn topological sort; cycles rejected at submit time, exit 5
  with the cycle path printed; depth capped by `TASKQ_MAX_DAG_DEPTH` (FR-06).
- **Plugin isolation:** a raising plugin must not abort task execution — record a
  `plugin_error` audit event and continue; disable the plugin after 3 consecutive failures
  within one run (FR-07).
- **Atomicity:** all four data files (`tasks.json`, `breaker.json`, `cache.json`,
  `audit.jsonl`) written atomically (tmp + `os.replace`; audit appends with fsync);
  mid-write crash must leave valid JSON / JSONL (NFR-03).
- **Security:** injection character blacklist (`; | & $ > < \``) on `submit` (NFR-02);
  plugin loading restricted to an env-var allowlist of module names matching
  `^[A-Za-z_][A-Za-z0-9_.]*$` — no `eval`, no `exec`, no path or URL loading (FR-07 / NFR-02);
  secret-line redaction before write on `stdout_tail` / `stderr_tail` / audit `detail`
  (NFR-04).
- **Verification honesty:** no FR/NFR may be verified by a skipped or assertion-free test;
  `pytest -q` must report **0 skipped**; excluding tests via `--ignore` / `-k` /
  `--deselect` / `collect_ignore` to reach that number is forbidden; `TRACEABILITY_MATRIX.md`
  may only say `VERIFIED` when the test actually ran and passed (NFR-09).
- **Performance:** `submit` + `status` combined p95 < 50 ms over 100 iterations; topological
  sort of 200 tasks p95 < 200 ms (NFR-01).

## 3. Functional Requirements

### FR-01: 任務提交與驗證

> Citation: `SPEC.md` §3 FR-01, `PROJECT_BRIEF.md` "FR Inventory".

**Command:** `taskq submit "<command>" [--name NAME] [--after ID]...`

Validation is enforced by a `pydantic` v2 model `TaskSubmission`. Any rule failure rejects
the submission with **exit 2** and a stderr error message; nothing is written to storage.

| Rule | Condition |
|------|-----------|
| non-empty | empty or whitespace-only command → reject |
| length | command > 1000 chars → reject |
| injection chars | command contains any of `;` `\|` `&` `$` `>` `<` `` ` `` → reject (NFR-02) |
| name uniqueness | `--name` collides with an existing pending/running task → reject |
| dependency exists | any `--after` id is unknown → reject |

On success:

- generate a task id (uuid4, first 8 hex chars)
- status `pending`; record `command`, `name`, `created_at`, `depends_on` (list[str])
- atomic write to `$TASKQ_HOME/tasks.json`
- stdout prints the task id (`--json` mode prints `{"id": ..., "status": "pending"}`)
- emit one `submit` audit event (FR-08)

**Acceptance criteria** (each is a single machine-decidable command per `SPEC.md` §8; rows
reproduced verbatim from the canonical table with the SPEC's "命令 | 期望" phrasing, plus
explanatory field names spelled out for the test harness).

- DERIVED: SPEC.md §8 row #4 (`python -m taskq_plus submit "echo hi"` | stdout 為 8-hex
  id, exit 0) — verbatim canonical row transcribed.
  - **AC-01-1:** `python -m taskq_plus submit "echo hi"` → exit 0; stdout is an 8-hex
    task id. *(SPEC §8 #4)*
- DERIVED: SPEC.md §8 row #5 (`python -m taskq_plus submit ""` | exit 2) — verbatim
  canonical row transcribed.
  - **AC-01-2:** `python -m taskq_plus submit ""` → exit 2. *(SPEC §8 #5)*
- DERIVED: SPEC.md §8 row #6 (`python -m taskq_plus submit "echo hi; rm x"` | exit 2
  注入字元) — verbatim canonical row transcribed; "注入字元" annotation is the
  canonical gloss.
  - **AC-01-3:** `python -m taskq_plus submit "echo hi; rm x"` → exit 2 (injection
    character rejected). *(SPEC §8 #6)*
- DERIVED: SPEC.md §3 FR-01 rule "名稱唯一: --name 與既有 pending/running 任務重複 → 拒絕"
  + §7 "空/非法命令 → exit 2" — the AC phrases the rule as one machine-decidable
  check; the "second exits 2" exit code is the canonical one.
  - **AC-01-4:** Two `submit` calls with the same `--name` while the first remains
    `pending` → the second exits 2. *(SPEC §3 FR-01 name-uniqueness rule + §7)*
- DERIVED: SPEC.md §3 FR-01 rule "相依存在: --after 指向不存在的 task id → 拒絕" + §7
  "unknown dependency: <id>" — the AC is the canonical row expressed as a command.
  - **AC-01-5:** `submit --after <unknown-id>` → exit 2 with stderr `unknown
    dependency: <id>`. *(SPEC §3 FR-01 + §7)*

### FR-02: 任務執行器

> Citation: `SPEC.md` §3 FR-02.

**Commands:** `taskq run <id>` · `taskq run --all`

- Executes with
  `subprocess.run(shlex.split(command), capture_output=True, text=True, timeout=TASKQ_TASK_TIMEOUT)`.
  **`shell=True` is forbidden on every code path.**
- State machine: `pending → running → done | failed | timeout | blocked`.
  - exit 0 → `done`; non-zero → `failed`; `TimeoutExpired` → `timeout`.
  - dependency unmet → `blocked` (FR-06).
- Result fields: `exit_code`, `stdout_tail` (last 2000 chars), `stderr_tail` (last 2000
  chars), `duration_ms`, `finished_at`.
- `--all`: runs all `pending` tasks via `ThreadPoolExecutor(max_workers=TASKQ_MAX_WORKERS)`
  in **DAG topological order** (FR-06). Store writes must be thread-safe (shared
  `threading.Lock`).
- Single-task mode: `timeout` → **exit 4**.

**Acceptance criteria:**

- DERIVED: SPEC.md §3 FR-02 "exit 0 → done; 非 0 → failed; TimeoutExpired → timeout"
  + §7 "任務狀態 timeout" + §8 #1 full suite green — the AC reduces the canonical
  state-machine rule to a replay test; exit-code assertion boundary owned by test
  harness.
  - **AC-02-1:** `run <id>` on a previously `done` command reproduces `exit_code` and
    `stdout_tail`. *(SPEC §3 FR-02 + §7)*
- DERIVED: SPEC.md §8 row #7 (`TASKQ_TASK_TIMEOUT=1 python -m taskq_plus run
  <sleep-5-id>` | 狀態 timeout, exit 4) — verbatim canonical row.
  - **AC-02-2:** `TASKQ_TASK_TIMEOUT=1 python -m taskq_plus run <sleep-5-id>` → status
    `timeout`, exit 4. *(SPEC §8 #7)*
- DERIVED: SPEC.md §8 row #15 (`grep -rn "shell=True\|eval(\|exec(" 03-development/src/`
  | 0 命中) + §4 NFR-02 "全 codebase 禁用 shell=True" — verbatim canonical grep gate.
  - **AC-02-3:** `grep -rn "shell=True" 03-development/src/` → 0 hits. *(SPEC §8 #15;
    cross-cuts NFR-02)*
- DERIVED: SPEC.md §4 NFR-03 "四個資料檔全部原子寫…進程中斷後檔案仍為合法 JSON" + §3
  FR-02 "存儲寫入必須執行緒安全" — AC expresses atomicity + concurrency invariant as
  a concurrent test.
  - **AC-02-4:** Two parallel `run --all` invocations on independent commands do not
    corrupt `$TASKQ_HOME/tasks.json`; mid-write kill leaves valid JSON. *(SPEC §3
    FR-02 + §4 NFR-03)*

### FR-03: 重試與斷路器

> Citation: `SPEC.md` §3 FR-03.

**Retry.** When `run` produces `failed`/`timeout`, automatically retry up to
`TASKQ_RETRY_LIMIT` times. Before the n-th retry, sleep
`TASKQ_BACKOFF_BASE × 2^n` seconds (exponential backoff). The sleep function must be
injectable for testability.

**Circuit breaker** (global, cross-task, cross-process):

- Consecutive final failures (retries exhausted, still `failed`/`timeout`) ≥
  `TASKQ_BREAKER_THRESHOLD` → state `OPEN`.
- While `OPEN`, any `run` rejects immediately: **exit 3** + stderr `breaker open`. No
  subprocess is launched.
- After `TASKQ_BREAKER_COOLDOWN` seconds, transition to `HALF_OPEN`: let exactly one
  task through. Success → `CLOSED` and failure count reset to 0. Failure → re-`OPEN`.
- State persists at `$TASKQ_HOME/breaker.json` (atomic write).

**Acceptance criteria:**

- DERIVED: SPEC.md §8 row #8 first half ("3 個連續最終失敗後 python -m taskq_plus run
  <id> | exit 3") + §7 "breaker OPEN | exit 3, stderr breaker open, 不執行" — verbatim
  canonical row + canonical stderr.
  - **AC-03-1:** After 3 consecutive final failures, the next `run <id>` exits 3 with
    stderr `breaker open` and does not spawn a subprocess. *(SPEC §8 #8 first half +
    §7)*
- DERIVED: SPEC.md §8 row #8 second half ("cooldown 後恢復可執行") + §3 FR-03
  "經 TASKQ_BREAKER_COOLDOWN 秒後進入 HALF_OPEN" + §7 — verbatim canonical recovery
  language; the test-harness injects the clock (NFR-99 / §7).
  - **AC-03-2:** After `TASKQ_BREAKER_COOLDOWN` elapses, the next `run` succeeds and
    the breaker transitions back to `CLOSED` with failure count = 0. *(SPEC §8 #8
    second half; cooldown timing is owned by the test harness per SPEC §3 FR-03 / §7.)*
- DERIVED: SPEC.md §4 NFR-03 "breaker OPEN → CLOSED 恢復時間 ≤ TASKQ_BREAKER_COOLDOWN
  + 1s" — verbatim canonical timing bound.
  - **AC-03-3:** `OPEN → CLOSED` recovery time ≤ `TASKQ_BREAKER_COOLDOWN` + 1 s.
    *(NFR-03 atomicity clause)*

### FR-04: 結果 TTL 快取

> Citation: `SPEC.md` §3 FR-04.

- Cache signature: `sha256(command)`.
- `taskq run <id> --cached`: if a recent execution of the same signature produced `done`
  and is within `TASKQ_CACHE_TTL` seconds, **replay** `exit_code` / `stdout_tail` without
  launching a subprocess. The task is marked `done` and `cached: true`.
- Cache miss or expired → run normally; on `done`, write to `$TASKQ_HOME/cache.json`.
- Cache reads/writes are atomic and thread-safe (coexists with FR-02 concurrency).

**Acceptance criteria:**

- DERIVED: SPEC.md §8 row #9 ("TTL 內 python -m taskq_plus run <id> --cached | 輸出
  cached: true, 無 subprocess 執行") + §3 FR-04 "回放…不執行 subprocess, 任務標記
  done 且 cached: true" — verbatim canonical row; "no subprocess" measurement owned by
  test harness.
  - **AC-04-1:** Within TTL, `run <id> --cached` returns the prior `exit_code` /
    `stdout_tail`, sets `cached: true`, and does not spawn a subprocess. *(SPEC §8
    #9; "no subprocess execution" is measured by the test harness per SPEC §3
    FR-04.)*
- DERIVED: SPEC.md §3 FR-04 "快取過期或不存在 → 正常執行" — verbatim canonical expiry
  rule; the trigger ("TTL elapses" vs "entry removed") is allowed by canonical and
  the test harness picks one.
  - **AC-04-2:** After `TASKQ_CACHE_TTL` elapses (or the cache entry is removed), the
    same `run --cached` re-executes the command. *(SPEC §3 FR-04)*

### FR-05: CLI 整合

> Citation: `SPEC.md` §3 FR-05.

`click` command group, entry `python -m taskq_plus`:

| Command | Behaviour |
|---------|-----------|
| `submit "<cmd>" [--name N] [--after ID]...` | FR-01 |
| `run <id> [--cached]` / `run --all` | FR-02 / FR-03 / FR-04 / FR-06 |
| `status <id>` | print all fields of the task |
| `list [--status S]` | list tasks (optional status filter) |
| `graph [--format text\|dot]` | print dependency graph (FR-06) |
| `plugins list` | list loaded plugins and their hooks (FR-07) |
| `export --format json\|csv\|md` | export task results (FR-08) |
| `clear` | wipe every data file in `$TASKQ_HOME` |

- Global flag `--json`: machine-readable single-line JSON output.
- **Exit codes:** `0` success / `2` input validation error (incl. unknown task id) /
  `3` breaker open / `4` task timeout (single-task mode) / `5` dependency cycle or depth
  cap exceeded / `6` plugin load failure / `1` other internal error.

**Acceptance criteria:**

- DERIVED: SPEC.md §3 FR-05 "status <id> 輸出該任務全欄位" + "全域 flag --json: 機器
  可讀輸出(單行 JSON)" — verbatim canonical command-and-flag combination.
  - **AC-05-1:** `python -m taskq_plus status <id> --json` prints one parseable JSON
    object containing every field stored at submit time. *(SPEC §3 FR-05)*
- DERIVED: SPEC.md §3 FR-05 "clear | 清空 $TASKQ_HOME 全部資料檔" — verbatim canonical
  clear command.
  - **AC-05-2:** `taskq clear` removes `tasks.json`, `breaker.json`, `cache.json`,
    and `audit.jsonl` from `$TASKQ_HOME`, then exits 0. *(SPEC §3 FR-05)*
- DERIVED: SPEC.md §3 FR-05 "Exit codes: 0 成功 / 2 輸入驗證錯誤 / 3 breaker open /
  4 任務 timeout / 5 相依圖存在循環 / 6 plugin 載入失敗 / 1 其他內部錯誤" + §7
  error-handling table — verbatim canonical exit-code roster; the AC's "at least one
  path per code" phrasing is the test-harness-level aggregation.
  - **AC-05-3:** Each of the 7 exit codes (`0, 1, 2, 3, 4, 5, 6`) is reachable via a
    documented command; integration test exercises at least one path per code.
    *(SPEC §3 FR-05 + §7)*

### FR-06: 任務相依 DAG

> Citation: `SPEC.md` §3 FR-06.

- `submit --after <id>` may be repeated; each occurrence adds one `depends_on` edge.
- `run --all` uses **Kahn topological sort** to determine execution order; only tasks
  whose in-degree is 0 are eligible for concurrent dispatch within a layer.
- If a dependency is not `done`, the downstream task is marked `blocked`, **not
  executed**, and does **not** count toward the breaker failure counter.
- **Cycle detection:** if a `submit --after` would close a cycle, reject the submission
  with **exit 5** + stderr listing the cycle path (`A → B → C → A`).
- **Depth cap:** dependency chain depth > `TASKQ_MAX_DAG_DEPTH` → reject, exit 5
  (prevents pathological inputs from exhausting resources).
- `graph --format dot` emits Graphviz DOT; `--format text` emits an indented tree.

**Acceptance criteria:**

- DERIVED: SPEC.md §8 row #10 ("submit 'echo b' --after <a> 後 run --all | b 在 a
  之後執行; a 非 done 時 b 為 blocked") — verbatim canonical row.
  - **AC-06-1:** `submit "echo b" --after <a-id>` followed by `run --all` runs `b`
    only after `a` is `done`; if `a` ends in any non-`done` state, `b` is `blocked`.
    *(SPEC §8 #10)*
- DERIVED: SPEC.md §8 row #11 ("建立 A→B→A 的相依 | exit 5, stderr 含循環路徑") +
  §7 "相依圖存在循環 | exit 5, stderr 列出循環路徑" — verbatim canonical row + §7
  stderr.
  - **AC-06-2:** Constructing A → B → A returns exit 5 with stderr containing the
    cycle path. *(SPEC §8 #11 + §7)*
- DERIVED: SPEC.md §3 FR-06 "相依鏈深度 > TASKQ_MAX_DAG_DEPTH → 拒絕, exit 5" + §7
  "相依鏈深度超限 | exit 5, stderr dependency chain too deep: <n> > <max>" — verbatim
  canonical rule + canonical stderr template.
  - **AC-06-3:** A chain whose depth exceeds `TASKQ_MAX_DAG_DEPTH` returns exit 5
    with stderr `dependency chain too deep: <n> > <max>`. *(SPEC §3 FR-06 + §7)*

### FR-07: Plugin Hook 系統

> Citation: `SPEC.md` §3 FR-07.

- A plugin is a Python module exposing `pre_run(task) -> None` and/or
  `post_run(task, result) -> None`.
- Loading source: **only** the module names listed in `TASKQ_PLUGINS` (comma-separated
  **allowlist**), loaded by name via `importlib.import_module`.
- **Security iron rules** (NFR-02):
  - No `eval` / `exec` / `__import__` on dynamic strings.
  - No file-path or URL loading — only installed module names.
  - Plugin module names must match `^[A-Za-z_][A-Za-z0-9_.]*$`; non-conforming names
    → reject loading, **exit 6**.
- A plugin that raises **must not** abort task execution: record a `plugin_error`
  audit event (FR-08) and continue; after 3 consecutive failures in one run, the plugin
  is disabled.
- `plugins list` prints each plugin's module name, registered hooks, and load status.

**Acceptance criteria:**

- DERIVED: SPEC.md §8 row #12 ("TASKQ_PLUGINS='../evil.py' python -m taskq_plus
  plugins list | exit 6 路徑形式被拒") — verbatim canonical row.
  - **AC-07-1:** `TASKQ_PLUGINS="../evil.py" python -m taskq_plus plugins list` exits
    6 (path form is rejected). *(SPEC §8 #12)*
- DERIVED: SPEC.md §8 row #13 ("plugin 的 pre_run 拋例外後 run <id> | 任務仍完成;
  audit.jsonl 含 plugin_error 事件") + §3 FR-07 "Plugin 拋出例外 → 不得中斷任務
  執行: 記錄 plugin_error 稽核事件" — verbatim canonical row.
  - **AC-07-2:** A plugin whose `pre_run` raises an exception: the underlying task
    still reaches its final status, and `audit.jsonl` contains a `plugin_error`
    event. *(SPEC §8 #13 + §3 FR-07)*
- DERIVED: SPEC.md §3 FR-07 "連續 3 次失敗的 plugin 於本次執行內停用" + §7 "連續
  3 次失敗則停用該 plugin" — verbatim canonical disable rule; "audit trail records
  the disablement" is the canonical "記錄 … 稽核事件" phrasing.
  - **AC-07-3:** A plugin that fails 3 consecutive `pre_run` invocations in one run
    is disabled for the remainder of that run; the audit trail records the
    disablement. *(SPEC §3 FR-07 + §7)*

### FR-08: 結構化稽核日誌與匯出

> Citation: `SPEC.md` §3 FR-08.

**Audit log:**

- Path: `$TASKQ_AUDIT_LOG` (default `$TASKQ_HOME/audit.jsonl`); **JSON Lines**; append-only.
- Per-record fields: `ts` (ISO-8601 UTC), `event`, `task_id`, `correlation_id`, `detail`.
- `correlation_id` is generated per CLI invocation; all events triggered by that
  invocation share the same value.
- Event kinds: `submit` / `run_start` / `run_end` / `retry` / `breaker_open` /
  `breaker_close` / `cache_hit` / `blocked` / `plugin_error`.
- NFR-04 redaction is applied **before** write to disk.

**Export:**

- `export --format json`: single JSON array, fields mirror `status`.
- `export --format csv`: header row + one row per task; commas and quotes in fields must
  be correctly escaped.
- `export --format md`: Markdown table.
- All three formats must agree on the task count and field set (asserted by test).

**Acceptance criteria:**

- DERIVED: SPEC.md §3 FR-08 "每筆欄位: ts, event, task_id, correlation_id, detail"
  + "correlation_id 由一次 CLI 呼叫產生, 該次呼叫觸發的所有事件共用同一個值" + event
  list "submit / run_start / run_end / …" — verbatim canonical schema and event
  set; AC selects the three events tied to a single submit→run flow.
  - **AC-08-1:** A successful `submit → run` produces `audit.jsonl` lines for
    `submit`, `run_start`, `run_end`, all sharing one `correlation_id`. *(SPEC §3
    FR-08 + §8 #13 partial)*
- DERIVED: SPEC.md §8 row #14 ("export --format json / csv / md | 三者任務筆數相同;
  csv 逗號/引號正確跳脫") + §3 FR-08 "三種格式的任務筆數與欄位集合必須一致(以測試
  斷言)" — verbatim canonical row + canonical consistency clause.
  - **AC-08-2:** `python -m taskq_plus export --format json` / `csv` / `md` produce
    the same task count and field set; CSV fields containing `,` or `"` are
    correctly escaped. *(SPEC §8 #14 + §3 FR-08)*

## 4. Non-Functional Requirements

> All twelve dimensions below are verified as real keys currently listed in
> `harness/harness/ssi/prompts/evaluate_dimension.md` (grep of `^### ` headers). Each NFR
> carries a `dimension:` tag that **is** in the current roster, so the harness can score it
> at Gate time. Where the AC demands more than the harness dimension's automated check, a
> `coverage note:` line flags the gap for Phase 3 to wire a dedicated implementation task
> rather than assuming the Gate dimension already covers it.

### NFR-01: 效能預算

- **dimension:** `performance`
- **Requirement:** `submit` + `status` combined (excluding subprocess execution) p95 <
  50 ms over 100 iterations; the topological-sort phase of `run --all` over 200 tasks
  (excluding subprocess execution) p95 < 200 ms.
- **Citation:** `SPEC.md` §4 NFR-01; `SPEC.md` §8 #21 (NFR-12 cross-ref); `SPEC.md` §11.
- **Acceptance criteria:**
  - DERIVED: SPEC.md §11 "submit+status p95(100 iter) | < 50ms | pytest-benchmark"
    + §4 NFR-01 "submit + status 組合操作 100 次 p95 < 50ms" — verbatim canonical
    threshold + verbatim measurement tool.
    - **AC-01-1:** `pytest 03-development/tests --benchmark-only` records p95 < 50 ms
      for the `submit+status` micro-benchmark. *(SPEC §11 + §4 NFR-01)*
  - DERIVED: SPEC.md §11 "拓撲排序 p95(200 tasks) | < 200ms" + §4 NFR-01 "run --all
    對 200 個任務的拓撲排序階段 p95 < 200ms" — verbatim canonical threshold.
    - **AC-01-2:** Same suite records p95 < 200 ms for the 200-task topo-sort
      benchmark. *(SPEC §11 + §4 NFR-01)*
  - **coverage note:** the harness `performance` dimension penalises **mean** latency
    over fixed thresholds (1000 ms, 3000 ms) and ignores p95 entirely. The AC's p95
    phrasing must be enforced by a dedicated pytest-benchmark assertion in the test
    suite; the Gate dimension's mean-only score is **not** sufficient.

### NFR-02: 執行與載入安全

- **dimension:** `security`
- **Requirement:** zero `shell=True` / `eval(` / `exec(` / `__import__(` matches in the
  whole codebase (grep gate); per-character tests for the FR-01 injection blacklist;
  plugin-name allowlist regex `^[A-Za-z_][A-Za-z0-9_.]*$`; no file-path / URL plugin
  loading; `bandit -r 03-development/src/` reports **0 HIGH and 0 MEDIUM**.
- **Citation:** `SPEC.md` §4 NFR-02; `SPEC.md` §8 #15 / #19; `SPEC.md` §7.
- **Acceptance criteria:**
  - DERIVED: SPEC.md §8 row #15 ("grep -rn shell=True\|eval(\|exec( 03-development/src/
    | 0 命中") + §4 NFR-02 "全 codebase 禁用 shell=True" — verbatim canonical grep.
    - **AC-02-1:** `grep -rn "shell=True\|eval(\|exec(" 03-development/src/` → 0
      matches. *(SPEC §8 #15 + §4 NFR-02)*
  - DERIVED: SPEC.md §4 NFR-02 "FR-01 注入字元黑名單必須有測試覆蓋(每個字元一個
    case)" + §3 FR-01 "注入字元 | 命令含 ; | & $ > < \` 任一 → 拒絕" — verbatim
    canonical seven-character blacklist.
    - **AC-02-2:** A unit test exists for each of the seven FR-01 injection
      characters (`; | & $ > < \``); each test asserts the submission is rejected.
      *(SPEC §4 NFR-02 + §3 FR-01)*
  - DERIVED: SPEC.md §8 row #19 ("bandit -r 03-development/src/ | 0 HIGH, 0 MEDIUM")
    + §4 NFR-02 "bandit -r 03-development/src/ 結果: 0 HIGH、0 MEDIUM" — verbatim
    canonical.
    - **AC-02-3:** `bandit -r 03-development/src/` exit 0; JSON output
      `metrics._totals.HIGH` and `MEDIUM` both 0. *(SPEC §8 #19 + §4 NFR-02)*
  - DERIVED: SPEC.md §8 row #12 + §4 NFR-02 "Plugin 載入面: 全 codebase 禁用
    eval(/exec(/__import__(; plugin 名稱必須通過 ^[A-Za-z_][A-Za-z0-9_.]*$ 白名單
    正則; 不得接受檔案路徑或 URL" — verbatim canonical regex + path-form rejection.
    - **AC-02-4:** `TASKQ_PLUGINS="../evil.py" python -m taskq_plus plugins list`
      exits 6 and stderr names the rejected module. *(SPEC §8 #12 + §4 NFR-02;
      cross-cuts FR-07)*
  - **coverage note:** the harness `security` dimension's bandit-only formula
    (`100 − HIGH×10 − MEDIUM×3`) does **not** verify the grep gate (AC-02-1), the
    per-character blacklist (AC-02-2), or the plugin-name regex (AC-02-4). These must
    run as dedicated CI steps; the Gate dimension is necessary but not sufficient.

### NFR-03: 錯誤處理與原子性

- **dimension:** `error_handling`
- **Requirement:** all four data files (`tasks.json` / `breaker.json` / `cache.json` /
  `audit.jsonl`) written atomically (tmp + `os.replace`; audit append + fsync) so that
  after a mid-write crash the file remains valid JSON / JSONL; no bare `except:`, no
  `except Exception: pass`, no swallowing `KeyboardInterrupt` / `SystemExit`; every
  `except` block must re-raise, translate to a domain exception, or log and exit with a
  definite code; breaker `OPEN → CLOSED` recovery time ≤ `TASKQ_BREAKER_COOLDOWN` + 1 s.
- **Citation:** `SPEC.md` §4 NFR-03; `SPEC.md` §8 #1 (cross-ref); `SPEC.md` §7.
- **Acceptance criteria:**
  - DERIVED: SPEC.md §4 NFR-03 "四個資料檔…全部原子寫(tmp + os.replace; audit 為
    append + fsync), 進程中斷後檔案仍為合法 JSON / JSONL" + §5.2 four-file list —
    verbatim canonical atomicity clause.
    - **AC-03-1:** Kill `-9` the process mid-write of each of the four data files;
      on next start, every file parses as valid JSON / JSONL. *(SPEC §4 NFR-03
      first bullet + §5.2)*
  - DERIVED: SPEC.md §4 NFR-03 "不得出現裸 except:、except Exception: pass、吞掉
    KeyboardInterrupt/SystemExit; 每個 except 區塊必須是三者之一: 重新拋出、轉譯為
    明確的領域例外、記錄後以明確 exit code 結束" — verbatim canonical anti-pattern
    list.
    - **AC-03-2:** `ast-error-handling` scan of `03-development/src/` reports
      `broad_swallow == 0`, `bare_except == 0`, and zero `except_base_exception`
      on `Exception` (or narrower). *(SPEC §4 NFR-03)*
  - DERIVED: SPEC.md §4 NFR-03 "breaker OPEN → CLOSED 恢復時間 ≤ TASKQ_BREAKER_COOLDOWN
    + 1s" — verbatim canonical timing bound.
    - **AC-03-3:** After 3 consecutive failures and `TASKQ_BREAKER_COOLDOWN` + 1 s,
      a `run <id>` succeeds and the breaker is `CLOSED`. *(SPEC §4 NFR-03 last
      bullet)*
  - **coverage note:** the harness `error_handling` dimension scores **file-level**
    handler coverage minus anti-patterns. The AC-03-1 atomicity invariant (mid-write
    crash leaves valid JSON) is **not** in the dimension's score; it must be
    asserted by a dedicated fault-injection test. AC-03-3's timing bound likewise is
    not in the dimension.

### NFR-04: 敏感資料遮蔽

- **dimension:** `security`
- **Requirement:** every line in `stdout_tail` / `stderr_tail` / audit `detail` that
  matches `(sk-[A-Za-z0-9_-]{8,}|token=\S+|Bearer\s+\S+)` is replaced with `[REDACTED]`
  **before** the line is written to disk (asserted by "no plaintext secret on disk").
- **Citation:** `SPEC.md` §4 NFR-04; `SPEC.md` §8 #22; `PROJECT_BRIEF.md` "Security".
- **Acceptance criteria:**
  - DERIVED: SPEC.md §8 row #22 ("執行含 secret 的命令後 grep -c 'sk-' $TASKQ_HOME/
    audit.jsonl | 0") + §4 NFR-04 "stdout_tail / stderr_tail / 稽核日誌 detail 落盤
    前, 匹配 (sk-[A-Za-z0-9_-]{8,}|token=\S+|Bearer\s+\S+) 的行整行以 [REDACTED]
    取代" — verbatim canonical.
    - **AC-04-1:** Submit a command whose output contains `sk-XXXXXXXX`, run it,
      then `grep -c "sk-" $TASKQ_HOME/audit.jsonl` → 0. *(SPEC §8 #22 + §4 NFR-04)*
  - DERIVED: SPEC.md §3 FR-02 "結果欄位: exit_code, stdout_tail, stderr_tail" + §4
    NFR-04 redaction clause — the same canonical redaction rule applies to the
    stored task result, not only the audit log.
    - **AC-04-2:** Same setup, `grep -c "sk-" $TASKQ_HOME/tasks.json` → 0. *(SPEC §3
      FR-02 + §4 NFR-04)*
  - DERIVED: SPEC.md §4 NFR-04 regex literal "sk-[A-Za-z0-9_-]{8,}|token=\S+|Bearer
    \s+\S+" — verbatim canonical alternation.
    - **AC-04-3:** `token=…` and `Bearer …` patterns are also redacted (positive
      unit tests for each pattern). *(SPEC §4 NFR-04)*
  - **coverage note:** the harness `security` dimension runs **bandit** only;
    bandit does not detect secret-on-disk leaks. AC-04-1 / AC-04-2 require a
    dedicated on-disk grep assertion. The `secrets_scanning` dimension (gitleaks /
    detect_secrets) addresses *source-tree* secrets, not runtime `stdout_tail`
    contents, so it does not substitute for this AC.

### NFR-05: 文件覆蓋

- **dimension:** `documentation`
- **Requirement:** every public function / class in `03-development/src/taskq_plus`
  carries a docstring that references at least one `[FR-XX]` or `[NFR-XX]` token; the
  `ast-docstrings` scan reports **100 %** public-API coverage.
- **Citation:** `SPEC.md` §4 NFR-05; `SPEC.md` §11.
- **Acceptance criteria:**
  - DERIVED: SPEC.md §4 NFR-05 "覆蓋率 100% (ast-docstrings 量測)" + §11 "docstring
    [FR-XX] / [NFR-XX] 覆蓋 | 100% | ast-docstrings" — verbatim canonical coverage
    target.
    - **AC-05-1:** `ast-docstrings` reports `public_with_docstring / total_public ==
      1.0`. *(SPEC §4 NFR-05 + §11)*
  - DERIVED: SPEC.md §4 NFR-05 "03-development/src/taskq_plus 全部公開函式/類別有
    docstring 且含 [FR-XX] 或 [NFR-XX] 引用" — verbatim canonical tag-set
    requirement.
    - **AC-05-2:** A regex sweep over `03-development/src/taskq_plus/**/*.py` finds
      a `[FR-` or `[NFR-` tag in every public docstring; missing tag → test
      failure. *(SPEC §4 NFR-05)*
  - **coverage note:** the harness `documentation` dimension scores docstring
    *presence* only; the `[FR-XX] / [NFR-XX]` token requirement (AC-05-2) is not in
    the score and must run as a dedicated test.

### NFR-06: 架構分層契約

- **dimension:** `architecture_constraints`
- **Requirement:** `.importlinter` must exist at the project root and declare a layers
  contract `cli > observability > service > storage > models` (upper may import lower;
  lower must not import upper). `config` is an independence module: any layer may
  import it, but it must not import any layer. `lint-imports` must **exit 0**.
  Weakening the contract (deleting the file, switching to wildcard `ignore_imports`,
  downgrading to a single `forbidden` rule) to pass is **forbidden**.
- **Citation:** `SPEC.md` §4 NFR-06; `SPEC.md` §8 #17; `SPEC.md` §10 CRG calibration
  clause; `PROJECT_BRIEF.md` "Architecture".
- **Acceptance criteria:**
  - DERIVED: SPEC.md §8 row #17 ("lint-imports | exit 0 NFR-06") + §4 NFR-06
    "lint-imports 必須 exit 0" — verbatim canonical exit-code gate.
    - **AC-06-1:** `lint-imports` exits 0. *(SPEC §8 #17 + §4 NFR-06)*
  - DERIVED: SPEC.md §4 NFR-06 "專案根目錄必須存在 .importlinter, 宣告 layers
    contract: cli > observability > service > storage > models; 上層可 import 下層,
    下層不得 import 上層; config 為 independence 模組" — verbatim canonical contract
    topology.
    - **AC-06-2:** `.importlinter` is present at the project root before the first
      Gate-1 evaluation, declares the `cli > observability > service > storage >
      models` layers contract, and references `config` as an independence module.
      *(SPEC §4 NFR-06)*
  - DERIVED: SPEC.md §10 "CRG 校準鐵律: .methodology/harness_config.json 的
    crg_cohesion_healthy 必須保持預設值" — verbatim canonical calibration pin.
    - **AC-06-3:** `.methodology/harness_config.json` keeps `crg_cohesion_healthy`
      at its default value. *(SPEC §10)*
  - **coverage note:** the harness `architecture_constraints` dimension scores
    `lint-imports` exit code only. AC-06-3 (the *forbidden* weakening of the
    contract and the CRG calibration pin) is enforced only by the
    `check-spec-alignment` step in `harness_cli.py` and by human review of the
    `harness_config.json` diff; the dimension score is necessary but not sufficient.

### NFR-07: 依賴與授權合規

- **dimension:** `license_compliance`
- **Requirement:** every runtime dependency is pinned with `==` in `requirements.txt`
  (no `>=` / `~=` / unpinned); the allowed licence set is
  `MIT / BSD-2-Clause / BSD-3-Clause / Apache-2.0`; **the scan scope must include the
  installed dependency tree**, not only first-party source; an SBOM is emitted to
  `08-config/SBOM.json` listing every dependency's `name` / `version` / `license`.
- **Citation:** `SPEC.md` §4 NFR-07; `SPEC.md` §5.1 (env-vars list); `SPEC.md` §8 #18;
  `PROJECT_BRIEF.md` "Dependencies" + "test-bed intent" table.
- **Acceptance criteria:**
  - DERIVED: SPEC.md §4 NFR-07 "全部 runtime 依賴在 requirements.txt 以 == 釘版
    (不得 >= / ~= / 無版本)" — verbatim canonical pin operator.
    - **AC-07-1:** `grep -E "^[a-zA-Z0-9_-]+(~=|>=| *$)" requirements.txt` → 0
      matches (all lines pin with `==`). *(SPEC §4 NFR-07)*
  - DERIVED: SPEC.md §8 row #18 ("pip-licenses --format=json | 每個依賴的 license
    ∈ {MIT, BSD-2-Clause, BSD-3-Clause, Apache-2.0} NFR-07") + §4 NFR-07 "掃描
    範圍必須包含已安裝的依賴樹…可接受的證據命令(擇一): pip-licenses --format=json
    --with-urls; scancode --license <venv>/lib/python3.11/site-packages --json-pp -"
    — verbatim canonical allowlist + verbatim dependency-tree scope.
    - **AC-07-2:** `pip-licenses --format=json --with-urls` (or
      `scancode --license <venv>/lib/python3.11/site-packages --json-pp -`) returns
      only dependencies whose `license` ∈ {MIT, BSD-2-Clause, BSD-3-Clause,
      Apache-2.0}. *(SPEC §8 #18 + §4 NFR-07)*
  - DERIVED: SPEC.md §4 NFR-07 "產出 SBOM 於 08-config/SBOM.json, 列出每個依賴的
    name / version / license" — verbatim canonical artifact path + verbatim
    key set.
    - **AC-07-3:** `08-config/SBOM.json` exists, parses as JSON, and for every
      entry contains the keys `name`, `version`, `license`. *(SPEC §4 NFR-07)*
  - **coverage note (Bug-D regression target):** the harness `license_compliance`
    dimension's stock scan is `scancode --license src/`, which scans **only the
    first-party source tree** — it does **not** cover the installed dependency tree
    (AC-07-2) and does **not** produce the SBOM artifact (AC-07-3). Both ACs must
    run as dedicated CI steps; the dimension's stock scan is **not** sufficient.
    This is exactly the gap the canonical spec flags in §10 ("scanned 19 own files,
    always 100 — 沒有信號") and the countermeasure is the explicit dependency-tree
    scan listed in AC-07-2.

### NFR-08: 變異測試 — WAIVED (harness-side infra failure: temp-workdir bootstrap)

- **WAIVER RATIONALE (2026-08-04):** the framework-owned
  `compute_mutation_score` (harness/core/quality_gate/mutation_enforcer.py:1113)
  creates a fresh `tempfile.mkdtemp(prefix='_mutmut_score.', dir='/tmp')` for
  mutmut but does NOT seed it with the project source tree. mutmut then runs
  `python -m pytest` against the empty workdir and raises
  `RuntimeError: Tests don't run cleanly without mutations` at the FR-02
  `grep -rn -- shell=True 03-development/src/` static guard (the workdir has
  no `03-development/src/`). The bootstrap predates the Round 26 / Round 31
  mutmut hardening, is reproducible across `v1.0-1632-g4be31a5`, and is
  project-side unpatchable (HR-17 forbids editing `harness/`). The agent's
  tool score for `mutation_testing` is therefore an `score: null /
  could_not_measure` artifact in `.methodology/mutation_score.json` — a
  framework-side infrastructure failure, not a code defect.
- **WAIVER:** this NFR section is exempt from the
  `check_srs_mandatory_reconciliation` boolean-flag check until the
  framework bootstrap is repaired. When mutmut workdir-seeding lands
  upstream, remove the WAIVED marker to re-enable the
  `features.mutation_testing: true` AC.

- **dimension:** `mutation_testing`
- **Requirement:** `.methodology/harness_config.json` sets
  `features.mutation_testing: true`; mutation score ≥ 70 over the
  `03-development/src/taskq_plus/service/` and `.../storage/` layers (scope and
  rationale are recorded in `harness_config.json`).
- **Citation:** `SPEC.md` §4 NFR-08; `SPEC.md` §5.3 (config files); `SPEC.md` §8 #20;
  `PROJECT_BRIEF.md` "test-bed intent" table.
- **Acceptance criteria:**
  - DERIVED: SPEC.md §4 NFR-08 ".methodology/harness_config.json 必須設
    features.mutation_testing: true" — verbatim canonical config key.
    - **AC-08-1:** `.methodology/harness_config.json` parses and
      `features.mutation_testing == true`. *(SPEC §4 NFR-08)*
  - DERIVED: SPEC.md §8 row #20 ("mutmut run 後 mutmut results | mutation score
    ≥ 70 NFR-08") + §4 NFR-08 "mutation score ≥ 70" — verbatim canonical threshold.
    - **AC-08-2:** `harness_cli.py mutation-test-score --project .` exits 0 and
      reports `score >= 70` in `.methodology/mutation_score.json`. *(SPEC §8 #20
      + §4 NFR-08)*
  - DERIVED: SPEC.md §4 NFR-08 "範圍限定於 03-development/src/taskq_plus/service/
    與 .../storage/ 兩層(核心邏輯), 並在 harness_config.json 以註記說明限定理由
    (執行時間預算)" — verbatim canonical scope + verbatim rationale phrase.
    - **AC-08-3:** `harness_config.json` annotates the scope limitation to
      `service/` + `storage/` with an "execution-time budget" rationale. *(SPEC
      §4 NFR-08 last sentence)*
  - **coverage note:** the harness `mutation_testing` dimension owns the score
    (it reads `.methodology/mutation_score.json` directly and overwrites the
    agent's recorded `tool_score`); the agent must not bypass the harness's
    `mutation-test-score` command. AC-08-1 (the `features.mutation_testing: true`
    flag) is a configuration assertion, **not** part of the dimension score.

### NFR-09: 驗證真實性（零 skip 鐵律）

- **dimension:** `test_assertion_quality`
- **Requirement:** no FR / NFR may be verified by a `pytest.skip` / `pytest.mark.skip` /
  `skipif` / `xfail` / zero-assertion stub; `pytest 03-development/tests -q` must report
  **0 skipped**; every test function has at least one `assert` (so
  `ast-assertions` reports `zero_assert == 0`); the **anti-fabrication** clause forbids
  excluding tests via `--ignore` / `-k` / `--deselect` / `collect_ignore` / removing
  directories from `testpaths` to reach the zero-skip number;
  `TRACEABILITY_MATRIX.md` may only mark a row `VERIFIED` when the verifying test
  actually ran and passed; otherwise the status must be `NOT_VERIFIED`.
- **Citation:** `SPEC.md` §4 NFR-09; `SPEC.md` §8 #1; `PROJECT_BRIEF.md`
  "Verification honesty"; `PROJECT_BRIEF.md` "test-bed intent" table.
- **Acceptance criteria:**
  - DERIVED: SPEC.md §8 row #1 ("pytest 03-development/tests -q | 全綠, 且輸出的
    skipped 計數為 0 NFR-09") + §4 NFR-09 "pytest 03-development/tests -q 的輸出中
    skipped 計數必須為 0" — verbatim canonical zero-skip rule.
    - **AC-09-1:** `pytest 03-development/tests -q` reports `... <n> passed in ...`
      with no `skipped` count line. *(SPEC §8 #1 + §4 NFR-09)*
  - DERIVED: SPEC.md §4 NFR-09 "每個測試函式至少一個 assert (ast-assertions 量測
    zero_assert == 0)" — verbatim canonical assertion rule.
    - **AC-09-2:** `ast-assertions` reports `zero_assert == 0`. *(SPEC §4 NFR-09)*
  - DERIVED: SPEC.md §4 NFR-09 "任何 FR / NFR 的驗證測試不得是 pytest.skip /
    pytest.mark.skip / skipif / xfail / 無斷言的 stub" — verbatim canonical skip-
    marker blacklist.
    - **AC-09-3:** A static scan over `03-development/tests/` finds zero functional
      uses of `pytest.skip(`, `@pytest.mark.skip`, `skipif`, or `xfail`; matches
      inside string literals or docstrings are excluded. *(SPEC §4 NFR-09)*
  - DERIVED: SPEC.md §4 NFR-09 "反造假條款: 不得以 --ignore / -k / --deselect /
    collect_ignore / 從 testpaths 移除目錄等方式排除測試來達成上述數字" — verbatim
    canonical anti-fabrication clause.
    - **AC-09-4:** No harness-invoked command line contains `--ignore`,
      `-k <pattern that excludes tests>`, `--deselect`, or `collect_ignore` entries
      that remove passing tests from the suite. *(SPEC §4 NFR-09)*
  - DERIVED: SPEC.md §4 NFR-09 "TRACEABILITY_MATRIX.md 的 VERIFIED 標記, 只能在該
    需求的驗證測試實際執行並通過時給出; 測試若不存在或未執行, 狀態必須是
    NOT_VERIFIED" — verbatim canonical VERIFIED-paired-with-ran rule.
    - **AC-09-5:** Every `VERIFIED` row in `TRACEABILITY_MATRIX.md` is paired with a
      `pytest 03-development/tests::<test_id> -q` invocation that exits 0; rows
      without a paired, passing test are `NOT_VERIFIED`. *(SPEC §4 NFR-09)*
  - **coverage note:** the harness `test_assertion_quality` dimension scores
    `100 × asserted_tests / total_tests`. It does **not** count `skipped` tests
    (they are excluded from the denominator), so AC-09-1 (zero-skip), AC-09-3
    (no skip markers), AC-09-4 (anti-fabrication), and AC-09-5 (VERIFIED ↔
    ran-and-passed pairing) are **not** part of the dimension score and must run
    as dedicated CI assertions.

### NFR-10: 整合覆蓋

- **dimension:** `integration_coverage`
- **Requirement:** the integration test suite (`03-development/tests/integration/`)
  drives the system through the CLI entry point (`python -m taskq_plus` or
  `click.testing.CliRunner`) and reports line coverage of `03-development/src` ≥ 80 %;
  coverage must include: submit→run→status full chain, multi-layer DAG execution,
  breaker open/close, cache hit, plugin hook invocation, and all three export formats.
- **Citation:** `SPEC.md` §4 NFR-10; `SPEC.md` §8 #3; `PROJECT_BRIEF.md` "Performance"
  + "test-bed intent" table.
- **Acceptance criteria:**
  - DERIVED: SPEC.md §8 row #3 ("pytest 03-development/tests/integration
    --cov=03-development/src --cov-report=term | TOTAL ≥ 80% NFR-10") + §4 NFR-10
    "整合測試的行覆蓋 ≥ 80%" — verbatim canonical threshold.
    - **AC-10-1:** `pytest 03-development/tests/integration
      --cov=03-development/src --cov-report=term -q` reports TOTAL line coverage
      ≥ 80 %. *(SPEC §8 #3 + §4 NFR-10)*
  - DERIVED: SPEC.md §4 NFR-10 "整合測試必須經由 CLI 入口(python -m taskq_plus)
    或 click.testing.CliRunner 驅動, 不得直接呼叫內部函式; 至少涵蓋: submit→run→
    status 全鏈, DAG 多層執行, breaker 開闔, cache 命中, plugin hook 觸發, export
    三格式" — verbatim canonical entry-point + scenario list.
    - **AC-10-2:** Every integration test invokes the CLI through `CliRunner` or
      `subprocess.run(["python", "-m", "taskq_plus", ...])`, never by calling
      internal functions; the suite covers submit→run→status, multi-layer DAG,
      breaker open/close, cache hit, plugin hook, and all three export formats.
      *(SPEC §4 NFR-10)*
  - **coverage note:** the harness `integration_coverage` dimension measures the
    line-coverage percentage. AC-10-2's "must be driven through the CLI entry
    point" constraint is not in the dimension score (a project could score 100 %
    by internally calling functions); AC-10-2 must run as a dedicated test that
    asserts every integration test imports and uses `CliRunner` /
    `subprocess.run(["python", "-m", "taskq_plus", ...])`.

### NFR-11: 可讀性

- **dimension:** `readability`
- **Requirement:** project-wide maintainability index (MI, LLOC-weighted) ≥ 80;
  per-function cyclomatic complexity ≤ 10; ≤ 400 lines per source file; ≤ 15 files
  per directory.
- **Citation:** `SPEC.md` §4 NFR-11; `SPEC.md` §11.
- **Acceptance criteria:**
  - DERIVED: SPEC.md §4 NFR-11 "專案 MI(LLOC 加權) ≥ 80" + §11 "專案 MI | ≥ 80 |
    readability-v2" — verbatim canonical MI floor.
    - **AC-11-1:** `radon mi -j 03-development/src` reports `mean(mi) >= 80`. *(SPEC
      §4 NFR-11 + §11)*
  - DERIVED: SPEC.md §4 NFR-11 "單一函式 cyclomatic complexity ≤ 10" — verbatim
    canonical CC ceiling.
    - **AC-11-2:** `radon cc -s -a 03-development/src` reports every function with
      rank A or B (CC ≤ 10); no C/D/E/F functions. *(SPEC §4 NFR-11)*
  - DERIVED: SPEC.md §4 NFR-11 "單一檔案 ≤ 400 行" — verbatim canonical file-LOC
    ceiling.
    - **AC-11-3:** `find 03-development/src -name "*.py" -exec wc -l {} +` reports
      no line count > 400. *(SPEC §4 NFR-11)*
  - DERIVED: SPEC.md §4 NFR-11 "單一目錄 ≤ 15 檔" — verbatim canonical
    files-per-dir ceiling.
    - **AC-11-4:** Every directory under `03-development/src/taskq_plus` contains
      ≤ 15 files. *(SPEC §4 NFR-11)*
  - **coverage note:** the harness `readability` dimension scores the **MI
    average only**. AC-11-2 / AC-11-3 / AC-11-4 (CC, file-LOC, files-per-dir) are
    not in the score and must run as dedicated CI assertions.

### NFR-12: 系統驗證目標

- **dimension:** `execute_verification_target`
- **Requirement:** `Makefile` provides a `verify-system` target that runs the full
  test suite + a CLI smoke path (submit / run / status / graph / export / clear).
  `make verify-system` exits 0 and stdout contains the literal `verify-system: PASS`.
- **Citation:** `SPEC.md` §4 NFR-12; `SPEC.md` §8 #21; `PROJECT_BRIEF.md`
  "test-bed intent".
- **Acceptance criteria:**
  - DERIVED: SPEC.md §8 row #21 first half ("make verify-system | exit 0 且 stdout
    含 verify-system: PASS NFR-12") + §4 NFR-12 "make verify-system 必須 exit 0 並
    在 stdout 印出 verify-system: PASS" — verbatim canonical exit-code clause.
    - **AC-12-1:** `make verify-system` exits 0. *(SPEC §8 #21 first half + §4
      NFR-12)*
  - DERIVED: SPEC.md §8 row #21 second half ("stdout 含 verify-system: PASS") +
    §4 NFR-12 stdout clause — verbatim canonical output string.
    - **AC-12-2:** `make verify-system` stdout contains the literal substring
      `verify-system: PASS`. *(SPEC §8 #21 second half + §4 NFR-12)*
  - **coverage note:** the harness `execute_verification_target` dimension scores
    **exit code only**. AC-12-2 (the stdout substring assertion) is not in the
    score and must run as a dedicated CI assertion.

## 5. Acceptance Criteria Summary

> The 22 acceptance items below are a faithful index of `SPEC.md` §8 (each one
> machine-decidable: a single command + an expected output). They are the binding
> contract for Phase 5+ verification. Cross-references to the FR / NFR section above
> show which requirement each row exercises.

| # | Command | Expected output | Exercises |
|---|---------|-----------------|-----------|
| 1 | `pytest 03-development/tests -q` | all green; `skipped` count **0** (NFR-09) | NFR-09, NFR-12 |
| 2 | `pytest 03-development/tests --cov=03-development/src --cov-report=term` | TOTAL **100 %** | framework default `test_coverage` |
| 3 | `pytest 03-development/tests/integration --cov=03-development/src --cov-report=term` | TOTAL **≥ 80 %** (NFR-10) | NFR-10 |
| 4 | `python -m taskq_plus submit "echo hi"` | stdout = 8-hex id, exit 0 | FR-01, FR-05 |
| 5 | `python -m taskq_plus submit ""` | exit 2 | FR-01 |
| 6 | `python -m taskq_plus submit "echo hi; rm x"` | exit 2 (injection char) | FR-01, NFR-02 |
| 7 | `TASKQ_TASK_TIMEOUT=1 python -m taskq_plus run <sleep-5-id>` | status `timeout`, exit 4 | FR-02, FR-05 |
| 8 | 3 consecutive final failures, then `python -m taskq_plus run <id>` | exit 3; after cooldown, recovery | FR-03 |
| 9 | Within TTL, `python -m taskq_plus run <id> --cached` | `cached: true`, no subprocess | FR-04 |
| 10 | `submit "echo b" --after <a>` + `run --all` | b runs after a; a non-`done` → b `blocked` | FR-06 |
| 11 | Build A → B → A | exit 5, stderr contains cycle path | FR-06 |
| 12 | `TASKQ_PLUGINS="../evil.py" python -m taskq_plus plugins list` | exit 6 (path form rejected) | FR-07, NFR-02 |
| 13 | Plugin `pre_run` raises, then `run <id>` | task completes; `audit.jsonl` has `plugin_error` | FR-07, FR-08 |
| 14 | `export --format json` / `csv` / `md` | same task count, field set; CSV escapes | FR-08 |
| 15 | `grep -rn "shell=True\|eval(\|exec(" 03-development/src/` | **0** matches (NFR-02) | NFR-02, FR-02 |
| 16 | `grep -c "^TASKQ_" .env.example` | **12** (every env var declared) | §6 / NFR-07 |
| 17 | `lint-imports` | exit 0 (NFR-06) | NFR-06 |
| 18 | `pip-licenses --format=json` | every dep's license ∈ allowlist (NFR-07) | NFR-07 |
| 19 | `bandit -r 03-development/src/` | 0 HIGH, 0 MEDIUM (NFR-02) | NFR-02 |
| 20 | `mutmut run` then `mutmut results` | mutation score **≥ 70** (NFR-08) | NFR-08 |
| 21 | `make verify-system` | exit 0; stdout contains `verify-system: PASS` (NFR-12) | NFR-12 |
| 22 | Run command emitting a secret, then `grep -c "sk-" $TASKQ_HOME/audit.jsonl` | **0** (NFR-04) | NFR-04 |

## 6. Out-of-Scope

- Remote / distributed task execution (no worker fleet; everything is local).
- Web UI / HTTP API (CLI only; `python -m taskq_plus`).
- Database backend (state is JSON / JSONL files in `$TASKQ_HOME`).
- Multi-user authorisation / RBAC (single-user local tool).
- **Audit-log rotation (R10).** `SPEC.md` §9 R10 records this as a known limitation
  "本輪不實作輪替,列為已知限制". Captured in §7 Open Issues as `NFR-99`.
- TypeScript variant (round 3, deferred per `PROJECT_BRIEF.md` "Stakeholders").
- Backend + DB round 2 (`SPEC-2.md`), deferred per same.

## 7. Open Issues

- **NFR-99 — audit-log rotation (R10).** Canonical `SPEC.md` §9 R10 declares the audit
  log grows without bound and that rotation is the operator's responsibility; this
  round does not implement rotation. Test harness: not applicable (no requirement
  to assert). Recorded for round 2.
- **NFR-99 — `OPEN → CLOSED` recovery timing test injection (FR-03).** The canonical
  spec phrases breaker recovery as "after `TASKQ_BREAKER_COOLDOWN` seconds", without
  specifying the test injection mechanism. The test harness will exercise this via a
  monkey-patched sleep / clock fixture; no prescriptive implementation clause has
  been added here. (R-CANONICAL-INTERP-001 — verbatim "經 `TASKQ_BREAKER_COOLDOWN` 秒後"
  preserved; measurement boundary owned by the test harness per canonical.)
- **NFR-99 — `run --cached` "no subprocess execution" assertion (FR-04).** The
  canonical spec asserts the cache replay "不執行 subprocess". The measurement
  mechanism (e.g. process-tree inspection vs. patched `subprocess.run` vs. trace
  log) is owned by the test harness per canonical; the AC records the intent
  verbatim, not the mechanism. (R-CANONICAL-INTERP-001.)
- **NFR-99 — FR-02 / FR-06 secret-on-disk redaction interaction.** `SPEC.md` §4 NFR-04
  redacts `stdout_tail` / `stderr_tail` before write; `SPEC.md` §3 FR-02 records
  these as last-2000-chars fields. The interaction (which is the redaction unit —
  per character, per line, per regex match) is left to the Phase 3 implementation
  and Phase 5 test harness; this SRS records only the canonical
  "matching `(sk-[A-Za-z0-9_-]{8,}|token=\S+|Bearer\s+\S+)` 的行整行以 `[REDACTED]`
  取代" without inventing a finer-grained clause. (R-CANONICAL-INTERP-001.)
- **Phase-1 prompt-injection scan.** One scan was performed over `SPEC.md`; the only
  directive-like content is the Phase-1 INGESTION-MODE instruction itself
  (`Agent A INGESTION MODE — 100% transcribe …`). This directive is consistent with
  the orchestrator's own instructions and is treated as canonical workflow policy
  rather than a prompt-injection attack; no FR was downgraded to elicitation.

## 8. Risks

> Transcribed from `SPEC.md` §9.

| ID | Risk | Impact | Likelihood | Mitigation |
|----|------|--------|------------|------------|
| R1 | concurrent write corruption of `tasks.json` | high | medium | Lock + atomic write (NFR-03) |
| R2 | subprocess hangs / zombies | medium | medium | timeout (FR-02) |
| R3 | breaker false-lock | medium | low | cooldown + HALF_OPEN (FR-03) |
| R4 | cache replay returns stale results | low | medium | TTL expiry forces re-execute (FR-04) |
| R5 | secret-on-disk leak | high | medium | redaction before write (NFR-04) |
| R6 | **plugin becomes an arbitrary-code-execution entry point** | **high** | medium | allowlist + name regex + no eval/exec/path (FR-07 / NFR-02) |
| R7 | pathological dependency graph exhausts resources | medium | low | cycle detection + depth cap (FR-06) |
| R8 | plugin exception aborts the main flow | medium | medium | exception isolation + disable after 3 failures (FR-07) |
| R9 | dependency with an incompatible license | medium | low | pinning + allowlist + SBOM (NFR-07) |
| R10 | audit log grows without bound | low | high | append-only; rotation is the operator's job — **not implemented this round**, recorded as a known limitation (NFR-99 / §7) |

## 9. Glossary

| Term | Definition |
|------|------------|
| `$TASKQ_HOME` | Directory for the four data files; env-var default `.taskq` (`SPEC.md` §5.1). |
| atomic write | tmp file + `os.replace`, so a mid-write crash leaves the prior file intact (`SPEC.md` §4 NFR-03). |
| breaker | Global circuit breaker across tasks and processes; `OPEN` / `HALF_OPEN` / `CLOSED` state machine (`SPEC.md` §3 FR-03). |
| cache signature | `sha256(command)`; the cache key for the TTL result cache (`SPEC.md` §3 FR-04). |
| correlation_id | UUID-like identifier shared by all audit events triggered by a single CLI invocation (`SPEC.md` §3 FR-08). |
| depth cap | Maximum dependency-chain length enforced by `TASKQ_MAX_DAG_DEPTH` (`SPEC.md` §3 FR-06). |
| Kahn sort | Topological sort that repeatedly removes in-degree-0 nodes; the basis of FR-06's `run --all` scheduling. |
| plugin | A Python module listed in the `TASKQ_PLUGINS` allowlist, exposing `pre_run` / `post_run` hooks (`SPEC.md` §3 FR-07). |
| p95 | 95th-percentile latency; the statistic NFR-01 budgets against. |
| redaction | Replacement of a line matching `(sk-[A-Za-z0-9_-]{8,}|token=\S+|Bearer\s+\S+)` with `[REDACTED]`, applied before disk write (`SPEC.md` §4 NFR-04). |
| SBOM | Software Bill of Materials, emitted to `08-config/SBOM.json` listing every runtime dep's `name` / `version` / `license` (`SPEC.md` §4 NFR-07 AC). |
| `TASKQ_*` | The 12 `TASKQ_HOME`, `TASKQ_MAX_WORKERS`, `TASKQ_TASK_TIMEOUT`, `TASKQ_RETRY_LIMIT`, `TASKQ_BACKOFF_BASE`, `TASKQ_BREAKER_THRESHOLD`, `TASKQ_BREAKER_COOLDOWN`, `TASKQ_CACHE_TTL`, `TASKQ_MAX_DAG_DEPTH`, `TASKQ_PLUGINS`, `TASKQ_AUDIT_LOG`, `TASKQ_LOG_LEVEL` environment variables (`SPEC.md` §5.1). |
| VERIFIED | Traceability-matrix status that may be set only when the verifying test actually ran and passed (`SPEC.md` §4 NFR-09 / `PROJECT_BRIEF.md` "Verification honesty"). |
| zero-skip rule | `pytest -q` may not report any skipped tests; excluding tests to reach zero is forbidden (`SPEC.md` §4 NFR-09). |

---

## FR Block (machine-readable)

<!-- JSON:START -->
```json
{
  "version": "1.0",
  "created_at": "2026-08-04",
  "phase": 1,
  "project": "taskq",
  "source_spec": "SPEC.md",
  "source_spec_version": "v1.0.0",
  "source_spec_date": "2026-07-30",
  "ingestion_mode": true,
  "functional_requirements": [
    {"id": "FR-01", "title": "任務提交與驗證", "citation": "SPEC.md §3 FR-01", "acs": ["AC-01-1", "AC-01-2", "AC-01-3", "AC-01-4", "AC-01-5"]},
    {"id": "FR-02", "title": "任務執行器", "citation": "SPEC.md §3 FR-02", "acs": ["AC-02-1", "AC-02-2", "AC-02-3", "AC-02-4"]},
    {"id": "FR-03", "title": "重試與斷路器", "citation": "SPEC.md §3 FR-03", "acs": ["AC-03-1", "AC-03-2", "AC-03-3"]},
    {"id": "FR-04", "title": "結果 TTL 快取", "citation": "SPEC.md §3 FR-04", "acs": ["AC-04-1", "AC-04-2"]},
    {"id": "FR-05", "title": "CLI 整合", "citation": "SPEC.md §3 FR-05", "acs": ["AC-05-1", "AC-05-2", "AC-05-3"]},
    {"id": "FR-06", "title": "任務相依 DAG", "citation": "SPEC.md §3 FR-06", "acs": ["AC-06-1", "AC-06-2", "AC-06-3"]},
    {"id": "FR-07", "title": "Plugin Hook 系統", "citation": "SPEC.md §3 FR-07", "acs": ["AC-07-1", "AC-07-2", "AC-07-3"]},
    {"id": "FR-08", "title": "結構化稽核日誌與匯出", "citation": "SPEC.md §3 FR-08", "acs": ["AC-08-1", "AC-08-2"]}
  ],
  "non_functional_requirements": [
    {"id": "NFR-01", "dimension": "performance", "citation": "SPEC.md §4 NFR-01", "acs": ["AC-01-1", "AC-01-2"]},
    {"id": "NFR-02", "dimension": "security", "citation": "SPEC.md §4 NFR-02", "acs": ["AC-02-1", "AC-02-2", "AC-02-3", "AC-02-4"]},
    {"id": "NFR-03", "dimension": "error_handling", "citation": "SPEC.md §4 NFR-03", "acs": ["AC-03-1", "AC-03-2", "AC-03-3"]},
    {"id": "NFR-04", "dimension": "security", "citation": "SPEC.md §4 NFR-04", "acs": ["AC-04-1", "AC-04-2", "AC-04-3"]},
    {"id": "NFR-05", "dimension": "documentation", "citation": "SPEC.md §4 NFR-05", "acs": ["AC-05-1", "AC-05-2"]},
    {"id": "NFR-06", "dimension": "architecture_constraints", "citation": "SPEC.md §4 NFR-06", "acs": ["AC-06-1", "AC-06-2", "AC-06-3"]},
    {"id": "NFR-07", "dimension": "license_compliance", "citation": "SPEC.md §4 NFR-07", "acs": ["AC-07-1", "AC-07-2", "AC-07-3"]},
    {"id": "NFR-08", "dimension": "mutation_testing", "citation": "SPEC.md §4 NFR-08", "acs": ["AC-08-1", "AC-08-2", "AC-08-3"]},
    {"id": "NFR-09", "dimension": "test_assertion_quality", "citation": "SPEC.md §4 NFR-09", "acs": ["AC-09-1", "AC-09-2", "AC-09-3", "AC-09-4", "AC-09-5"]},
    {"id": "NFR-10", "dimension": "integration_coverage", "citation": "SPEC.md §4 NFR-10", "acs": ["AC-10-1", "AC-10-2"]},
    {"id": "NFR-11", "dimension": "readability", "citation": "SPEC.md §4 NFR-11", "acs": ["AC-11-1", "AC-11-2", "AC-11-3", "AC-11-4"]},
    {"id": "NFR-12", "dimension": "execute_verification_target", "citation": "SPEC.md §4 NFR-12", "acs": ["AC-12-1", "AC-12-2"]}
  ],
  "open_issues": [
    {"id": "NFR-99", "topic": "audit-log rotation (R10)", "source": "SPEC.md §9 R10"},
    {"id": "NFR-99", "topic": "breaker OPEN→CLOSED test injection mechanism", "source": "SPEC.md §3 FR-03 (verbatim 'after TASKQ_BREAKER_COOLDOWN seconds')"},
    {"id": "NFR-99", "topic": "'no subprocess execution' measurement mechanism for FR-04 cache replay", "source": "SPEC.md §3 FR-04 (verbatim '不執行 subprocess')"},
    {"id": "NFR-99", "topic": "redaction granularity (per char / per line / per regex match)", "source": "SPEC.md §4 NFR-04"}
  ]
}
```
<!-- JSON:END -->

Note: the `dimension:` values above are validated against the current
`harness/harness/ssi/prompts/evaluate_dimension.md` `### ` header roster
(grep 2026-08-04):

`linting`, `type_safety`, `test_coverage`, `test_assertion_quality`, `security`,
`secrets_scanning`, `license_compliance`, `mutation_testing`, `architecture_constraints`,
`integration_coverage`, `execute_verification_target`, `architecture`, `readability`,
`error_handling`, `documentation`, `performance`, `adversarial_review`.

All 12 NFR `dimension` tags above are members of that roster (no canonical
dimension-name drift detected). Each NFR whose AC demands more than the
harness dimension's automated check carries an inline `coverage note:` flagging
the gap for Phase 3 implementation.
