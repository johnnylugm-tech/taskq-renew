# Traceability Matrix — taskq

> Requirements Traceability Matrix
> Framework: harness-methodology
> Version: v1.0

---

## Overview

Provides complete **FR -> SRS -> Code -> Test** bidirectional traceability supporting ASPICE SWE.3/SYS.4 compliance.

---

## FR <-> Spec Mapping

> Source: `01-requirements/SRS.md` §3 (FR) / §4 (NFR), canonical lines 79, 130, 172, 210, 237, 277, 310, 347 (FR) and 394, 417, 451, 486, 515, 538, 571, 609, 640, 687, 720, 748 (NFR).
> All SRS section IDs below are verbatim from the canonical `### FR-NN` / `### NFR-NN` headers.

| FR ID | Functional Requirement | SRS Section | Priority | Status |
|-------|----------------------|-------------|----------|--------|
| FR-01 | 任務提交與驗證 — `submit "<cmd>"` 由 `pydantic` `TaskSubmission` 驗證(非空/長度≤1000/注入字元黑名單/名稱唯一/相依存在),通過後產生 uuid4 前 8-hex id,寫入 `$TASKQ_HOME/tasks.json`,發 `submit` 稽核事件 | SRS §3 FR-01 (line 79) | HIGH | DRAFT |
| FR-02 | 任務執行器 — `subprocess.run(shlex.split(cmd), …, timeout=…)`(禁 `shell=True`);狀態機 `pending → running → done/failed/timeout/blocked`;結果欄位 `exit_code`/`stdout_tail`/`stderr_tail`/`duration_ms`/`finished_at`;`--all` 以 `ThreadPoolExecutor` 依 DAG 拓撲順序並發;共享 `threading.Lock`;單任務 `timeout` → exit 4 | SRS §3 FR-02 (line 130) | HIGH | DRAFT |
| FR-03 | 重試與斷路器 — 失敗/timeout 自動重試至 `TASKQ_RETRY_LIMIT` 次,指數退避 `TASKQ_BACKOFF_BASE × 2^n`;連續最終失敗 ≥ `TASKQ_BREAKER_THRESHOLD` → `OPEN`,期間 `run` 立即 exit 3;經 `TASKQ_BREAKER_COOLDOWN` 進入 `HALF_OPEN`;狀態持久化於 `breaker.json` 原子寫 | SRS §3 FR-03 (line 172) | HIGH | DRAFT |
| FR-04 | 結果 TTL 快取 — 簽名 `sha256(command)`;`run <id> --cached` 在 `TASKQ_CACHE_TTL` 內同簽名 `done` 結果直接回放(`cached: true`),不執行 subprocess;過期或不存在 → 正常執行後寫 `cache.json`;讀寫原子+執行緒安全 | SRS §3 FR-04 (line 210) | MEDIUM | DRAFT |
| FR-05 | CLI 整合 — `click` 群組化子命令,入口 `python -m taskq_plus`;命令: submit / run / status / list / graph / plugins / export / clear;全域 `--json` flag;Exit codes: 0/1/2/3/4/5/6 | SRS §3 FR-05 (line 237) | HIGH | DRAFT |
| FR-06 | 任務相依 DAG — `--after` 建邊;`run --all` 以 Kahn 拓撲排序決定執行順序;相依非 `done` → 下游 `blocked` 不執行且不計入斷路器失敗;循環偵測 → exit 5 列出循環路徑;深度 > `TASKQ_MAX_DAG_DEPTH` → exit 5;`graph --format dot|text` | SRS §3 FR-06 (line 277) | HIGH | DRAFT |
| FR-07 | Plugin Hook 系統 — Plugin 為 Python 模組提供 `pre_run`/`post_run` hook;載入來源僅限 `TASKQ_PLUGINS` 環境變數 allowlist,`importlib.import_module` 具名載入;安全鐵律:禁 `eval`/`exec`/`__import__`、禁路徑或 URL、模組名匹配 `^[A-Za-z_][A-Za-z0-9_.]*$` 否則 exit 6;plugin 拋例外不中斷任務(記 `plugin_error` 稽核事件),連續 3 次失敗停用;`plugins list` 列出載入狀態 | SRS §3 FR-07 (line 310) | HIGH | DRAFT |
| FR-08 | 結構化稽核日誌與匯出 — `$TASKQ_AUDIT_LOG` 預設 `$TASKQ_HOME/audit.jsonl`,JSON Lines,append-only;欄位 `ts`/`event`/`task_id`/`correlation_id`/`detail`,每筆 CLI 呼叫共用同一 `correlation_id`;事件種類 submit/run_start/run_end/retry/breaker_open/breaker_close/cache_hit/blocked/plugin_error;NFR-04 redaction 寫入前套用;`export --format json\|csv\|md` 三格式任務筆數與欄位集合一致,CSV 正確跳脫 | SRS §3 FR-08 (line 347) | MEDIUM | DRAFT |
| NFR-01 | 效能預算 (`performance`) — `submit+status` 100 iter p95 < 50ms;`run --all` 對 200 任務拓撲排序階段 p95 < 200ms;量測 `pytest-benchmark` | SRS §4 NFR-01 (line 394) | HIGH | DRAFT |
| NFR-02 | 執行與載入安全 (`security`) — codebase `shell=True`/`eval(`/`exec(`/`__import__(` 0 命中(grep gate);FR-01 注入字元黑名單 7 字元逐一測試覆蓋;plugin 載入面 allowlist + 白名單正則 + 禁路徑/URL;`bandit -r 03-development/src/` 0 HIGH/0 MEDIUM | SRS §4 NFR-02 (line 417) | HIGH | DRAFT |
| NFR-03 | 錯誤處理與原子性 (`error_handling`) — 四資料檔(`tasks.json`/`breaker.json`/`cache.json`/`audit.jsonl`)原子寫(tmp+`os.replace`;audit append+fsync);禁裸 `except:`、`except Exception: pass`、吞 `KeyboardInterrupt`/`SystemExit`;每 `except` 區塊必須 re-raise/轉譯/記錄後明確 exit;breaker `OPEN→CLOSED` 恢復時間 ≤ `TASKQ_BREAKER_COOLDOWN` + 1s | SRS §4 NFR-03 (line 451) | HIGH | DRAFT |
| NFR-04 | 敏感資料遮蔽 (`security`) — `stdout_tail`/`stderr_tail`/稽核日誌 `detail` 寫入前,匹配 `(sk-[A-Za-z0-9_-]{8,}|token=\S+|Bearer\s+\S+)` 的行整行以 `[REDACTED]` 取代;以「檔案內容不含明文 secret」斷言 | SRS §4 NFR-04 (line 486) | HIGH | DRAFT |
| NFR-05 | 文件覆蓋 (`documentation`) — `03-development/src/taskq_plus` 全部公開函式/類別 docstring 含 `[FR-XX]`/`[NFR-XX]` 引用;`ast-docstrings` 覆蓋 100% | SRS §4 NFR-05 (line 515) | MEDIUM | DRAFT |
| NFR-06 | 架構分層契約 (`architecture_constraints`) — 專案根 `.importlinter` 宣告 `cli > observability > service > storage > models`,上層可 import 下層,下層不得 import 上層;`config` 為 independence 模組;`lint-imports` exit 0;禁止刪除 `.importlinter`/放寬成 `ignore_imports`/降級為單條 `forbidden`;CRG 校準 `crg_cohesion_healthy` 保持預設 | SRS §4 NFR-06 (line 538) | HIGH | DRAFT |
| NFR-07 | 依賴與授權合規 (`license_compliance`) — runtime 依賴 `requirements.txt` 以 `==` 釘版(禁 `>=`/`~=`/無版本);license 限 MIT/BSD-2-Clause/BSD-3-Clause/Apache-2.0;**掃描範圍必須含已安裝依賴樹**;產出 SBOM 至 `08-config/SBOM.json`(name/version/license) | SRS §4 NFR-07 (line 571) | HIGH | DRAFT |
| NFR-08 | 變異測試 (`mutation_testing`) — `.methodology/harness_config.json` `features.mutation_testing: true`;mutation score ≥ 70;範圍限 `service/` + `storage/`,`harness_config.json` 註記「執行時間預算」理由 | SRS §4 NFR-08 (line 609) | MEDIUM | DRAFT |
| NFR-09 | 驗證真實性 (`test_assertion_quality`) — 任何 FR/NFR 驗證測試禁 `pytest.skip`/`pytest.mark.skip`/`skipif`/`xfail`/無斷言 stub;`pytest 03-development/tests -q` skipped 計數 = 0;每測試 ≥ 1 `assert`;反造假條款禁 `--ignore`/`-k`/`--deselect`/`collect_ignore`/從 `testpaths` 移除目錄;`TRACEABILITY_MATRIX.md` `VERIFIED` 必須對應實際執行並通過的測試 | SRS §4 NFR-09 (line 640) | HIGH | DRAFT |
| NFR-10 | 整合覆蓋 (`integration_coverage`) — `03-development/tests/integration/` 行覆蓋 ≥ 80%;整合測試經 CLI 入口(`python -m taskq_plus` 或 `click.testing.CliRunner`)驅動,禁直接呼叫內部函式;涵蓋 submit→run→status 全鏈、DAG 多層、breaker 開闔、cache 命中、plugin hook、三匯出格式 | SRS §4 NFR-10 (line 687) | HIGH | DRAFT |
| NFR-11 | 可讀性 (`readability`) — 專案 MI(LLOC 加權)≥ 80;單一函式 cyclomatic complexity ≤ 10;單一檔案 ≤ 400 行;單一目錄 ≤ 15 檔 | SRS §4 NFR-11 (line 720) | MEDIUM | DRAFT |
| NFR-12 | 系統驗證目標 (`execute_verification_target`) — `Makefile` 提供 `verify-system` target,串接全套測試 + CLI 冒煙(submit/run/status/graph/export/clear);`make verify-system` exit 0 且 stdout 含 `verify-system: PASS` | SRS §4 NFR-12 (line 748) | HIGH | DRAFT |

---

## Spec <-> Code Mapping

> Per `01-requirements/SPEC_TRACKING.md` row Notes ("DRAFT — code not yet authored; Owner: Phase 3 Agent C"), no FR/NFR code yet exists. The rows below declare the **target layer** per NFR-06 (`cli > observability > service > storage > models`, `config` = independence) so the Phase 3 implementation has a deterministic landing pad; `Function/Class` and `Lines` remain blank until Phase 3 author fills them. Status is `PLANNED` (not `VERIFIED`) — only the layer target is committed at this point.
> **No specific file paths are invented here** — exact file names belong to Phase 2's `02-architecture/SAD.md`, which is the architectural deliverable that owns layer-to-file allocation.

| SRS Section | Target Layer (per NFR-06) | Function/Class | Lines | Status |
|-------------|---------------------------|----------------|-------|--------|
| SRS §3 FR-01 (line 79) | `cli` (submit) + `service` (TaskSubmission validation) | _TBD by Phase 3_ | _TBD_ | PLANNED |
| SRS §3 FR-02 (line 130) | `service` (executor) + `storage` (tasks.json atomic write) | _TBD by Phase 3_ | _TBD_ | PLANNED |
| SRS §3 FR-03 (line 172) | `service` (retry + breaker state machine) + `storage` (breaker.json) | _TBD by Phase 3_ | _TBD_ | PLANNED |
| SRS §3 FR-04 (line 210) | `service` (cache lookup) + `storage` (cache.json) | _TBD by Phase 3_ | _TBD_ | PLANNED |
| SRS §3 FR-05 (line 237) | `cli` (click group + all sub-commands) | _TBD by Phase 3_ | _TBD_ | PLANNED |
| SRS §3 FR-06 (line 277) | `service` (Kahn sort + cycle detection) + `cli` (`graph` sub-command) | _TBD by Phase 3_ | _TBD_ | PLANNED |
| SRS §3 FR-07 (line 310) | `service` (plugin loader + hook dispatch) + `cli` (`plugins list`) | _TBD by Phase 3_ | _TBD_ | PLANNED |
| SRS §3 FR-08 (line 347) | `observability` (JSONL audit append) + `service` (export formatters) | _TBD by Phase 3_ | _TBD_ | PLANNED |
| SRS §4 NFR-01 (line 394) | `service` (perf-critical paths) | _TBD by Phase 3_ | _TBD_ | PLANNED |
| SRS §4 NFR-02 (line 417) | `cli` + `service` + `models` (subprocess shlex; pydantic TaskSubmission; plugin loader) | _TBD by Phase 3_ | _TBD_ | PLANNED |
| SRS §4 NFR-03 (line 451) | `storage` (atomic write helper used by all 4 data files) | _TBD by Phase 3_ | _TBD_ | PLANNED |
| SRS §4 NFR-04 (line 486) | `observability` (redaction pre-write hook) + `service` (result capture) | _TBD by Phase 3_ | _TBD_ | PLANNED |
| SRS §4 NFR-05 (line 515) | `models` + `service` + `storage` + `cli` + `observability` (all public API surfaces) | _TBD by Phase 3_ | _TBD_ | PLANNED |
| SRS §4 NFR-06 (line 538) | project root (`.importlinter`, `.methodology/harness_config.json`) | _TBD by Phase 3_ | _TBD_ | PLANNED |
| SRS §4 NFR-07 (line 571) | project root (`requirements.txt`) + `08-config/SBOM.json` (artifact) | _TBD by Phase 3_ | _TBD_ | PLANNED |
| SRS §4 NFR-08 (line 609) | `service` + `storage` (mutation-test scope) | _TBD by Phase 3_ | _TBD_ | PLANNED |
| SRS §4 NFR-09 (line 640) | `03-development/tests/` (zero-skip invariant) | _TBD by Phase 3_ | _TBD_ | PLANNED |
| SRS §4 NFR-10 (line 687) | `03-development/tests/integration/` (CLI entry-point driven) | _TBD by Phase 3_ | _TBD_ | PLANNED |
| SRS §4 NFR-11 (line 720) | `03-development/src/` (whole tree, MI/CC/LOC/dir-count gates) | _TBD by Phase 3_ | _TBD_ | PLANNED |
| SRS §4 NFR-12 (line 748) | project root (`Makefile`) | _TBD by Phase 3_ | _TBD_ | PLANNED |

---

## Code <-> Test Mapping

> No code or tests authored yet (per `01-requirements/SPEC_TRACKING.md` all FR/NFR rows marked `DRAFT — code not yet authored; Owner: Phase 3 Agent C`). Code/Test file paths and coverage percentages are intentionally blank; a `PLANNED` row records the **target test file** so Phase 3 can land code+test atomically. Per NFR-09 anti-fabrication clause, no row may be marked `VERIFIED` until the test actually ran and passed.

| Code File | Test File | Coverage | Status |
|-----------|-----------|----------|--------|
| _TBD per Phase 3_ (`cli/submit.py`) | `03-development/tests/unit/test_submit.py` | _TBD_ | PLANNED |
| _TBD per Phase 3_ (`service/executor.py`) | `03-development/tests/unit/test_executor.py` | _TBD_ | PLANNED |
| _TBD per Phase 3_ (`service/breaker.py`) | `03-development/tests/unit/test_breaker.py` | _TBD_ | PLANNED |
| _TBD per Phase 3_ (`service/cache.py`) | `03-development/tests/unit/test_cache.py` | _TBD_ | PLANNED |
| _TBD per Phase 3_ (`cli/group.py`) | `03-development/tests/unit/test_cli.py` | _TBD_ | PLANNED |
| _TBD per Phase 3_ (`service/dag.py`) | `03-development/tests/unit/test_dag.py` | _TBD_ | PLANNED |
| _TBD per Phase 3_ (`service/plugins.py`) | `03-development/tests/unit/test_plugins.py` | _TBD_ | PLANNED |
| _TBD per Phase 3_ (`observability/audit.py` + `service/export.py`) | `03-development/tests/unit/test_audit_export.py` | _TBD_ | PLANNED |
| _all `service/` files_ (NFR-01 perf budget) | `03-development/tests/perf/test_perf_budget.py` | _TBD_ | PLANNED |
| _all `cli/` + `service/` + `models/` files_ (NFR-02 grep + blacklist + plugin regex) | `03-development/tests/security/test_injection_blacklist.py` + `03-development/tests/security/test_plugin_allowlist.py` | _TBD_ | PLANNED |
| _`storage/` atomic-write helper_ (NFR-03) | `03-development/tests/unit/test_atomic_write.py` + fault-injection test | _TBD_ | PLANNED |
| _`observability/` redaction_ (NFR-04) | `03-development/tests/security/test_secret_redaction.py` | _TBD_ | PLANNED |
| _public API of `03-development/src/taskq_plus/`_ (NFR-05) | `03-development/tests/unit/test_docstring_tags.py` | _TBD_ | PLANNED |
| _project root + all `src/`_ (NFR-06 architecture + NFR-11 readability) | `03-development/tests/lint/test_imports.py` + readability CI step | _TBD_ | PLANNED |
| _`requirements.txt` + installed tree_ (NFR-07) | `03-development/tests/compliance/test_licenses.py` + `08-config/SBOM.json` | _TBD_ | PLANNED |
| _`service/` + `storage/`_ (NFR-08 mutation) | `harness_cli.py mutation-test-score` (harness-owned) | _TBD_ | PLANNED |
| _`03-development/tests/`_ (NFR-09 zero-skip + assertion + anti-fabrication) | `03-development/tests/meta/test_no_skip_markers.py` + `ast-assertions` + harness `check-spec-alignment` | _TBD_ | PLANNED |
| _`03-development/tests/integration/`_ (NFR-10) | integration suite itself; entry-point assertion in `03-development/tests/integration/test_cli_entrypoint.py` | _TBD_ | PLANNED |
| _project root `Makefile`_ (NFR-12) | `03-development/tests/system/test_verify_system.py` | _TBD_ | PLANNED |

---

## Completeness Verification

> All four checks below are reported honestly. `FR → SRS` is `100%` (this round wired all 8 FR / 12 NFR rows to canonical SRS lines 79/130/172/210/237/277/310/347/394/417/451/486/515/538/571/609/640/687/720/748). The other three checks are `0%` because no code or test has been authored yet (per `01-requirements/SPEC_TRACKING.md` — all rows `DRAFT`, `Owner: Phase 3 Agent C`). Per NFR-09 anti-fabrication clause, none of the `0%` rows may be inflated to fake green; they must transition to a real percentage only when Phase 3+ evidence exists.

| Check | Target | Actual | Status |
|-------|--------|--------|--------|
| FR -> SRS mapping | 100% | 100% (8/8 FR + 12/12 NFR wired to canonical SRS lines) | MET |
| SRS -> Code mapping | 100% | 0% (0/20 FR/NFR have Phase 3 code; target layers recorded) | NOT_MET |
| Code -> Test mapping | 100% | 0% (0/20 FR/NFR have Phase 3 tests; target test files recorded) | NOT_MET |
| Test coverage | >=80% (P3: >=70%) | 0% (no code authored yet) | NOT_MET |

---

## ASPICE Compliance

> ASPICE SWE.3 / SYS.4 capability status is reported against the *current* state of this round-1 traceability deliverable. `SWE.3.B.SP1` is the only capability that is fully demonstrated *by this matrix itself* (every FR/NFR row carries its source citation + a target test file). `SP2` and `SP3` are partially demonstrated: bidirectional rows exist at the FR→SRS axis; the SRS→Code and Code→Test axes remain `PLANNED` because Phase 3 has not authored code. Per NFR-09, no row may be marked `VERIFIED` until paired with a real, ran-and-passed test.

| ASPICE Capability | Status |
|-------------------|--------|
| SWE.3.B.SP1 Task-to-work-product traceability | DEMONSTRATED — this matrix enumerates 8 FR / 12 NFR, each citing a canonical SRS line, a target code layer, and a target test file. |
| SWE.3.B.SP2 Bidirectional traceability | PARTIAL — FR→SRS axis is bidirectional (8/8 + 12/12); SRS→Code axis is unidirectional (SRS→planned-layer); Code→Test axis is forward-only (planned-test-file, no actual test). |
| SWE.3.B.SP3 Traceability consistency | PARTIAL — every row's source citation is the same canonical `01-requirements/SRS.md`; no contradictions among the 20 rows. Full consistency (Code+Test columns populated with real paths and run results) awaits Phase 3+ evidence. |
