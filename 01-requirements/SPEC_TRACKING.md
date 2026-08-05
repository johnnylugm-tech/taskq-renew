# Specification Tracking Matrix — taskq

> Human-readable view of the 8 FR / 12 NFR surface declared in `SPEC.md` v1.0.0 (2026-07-30).
> The canonical spec source path is the project-root `SPEC.md` (NOT `01-requirements/SPEC.md`).

## Project Info

- Project Name: taskq
- Version: v1.0.0
- Created: 2026-07-30
- Spec Version Date: 2026-07-30

## Specification Status

> **The Status column is machine-refreshed** — `advance-phase` overwrites each
> FR's Status from `build_traceability`'s live code/test scan (IN_PROGRESS once
> code/module exists, VERIFIED once code+test exist). The authoritative status is
> that scan / `quality_manifest.json`, NOT this hand-filled cell. Fill the
> semantic columns (Spec Description / Intent Class / Decision Framework / Notes);
> leave Status to refresh itself (a hand-edit is overwritten on the next advance).

### Functional Requirements (FR)

| FR ID | Spec Description | Intent Class | Decision Framework | Status | Notes |
|-------|-----------------|--------------|-------------------|--------|-------|
| FR-01 | 任務提交與驗證 — `submit "<cmd>"` 由 `pydantic` `TaskSubmission` 驗證(非空/長度≤1000/注入字元黑名單/名稱唯一/相依存在),通過後產生 uuid4 前 8-hex id,寫入 `$TASKQ_HOME/tasks.json`,發 `submit` 稽核事件(SPEC.md §3 FR-01) | input-validation + persistence | DRAFT — code not yet authored; AC list: AC-01-1..AC-01-5 (SRS §3 FR-01) | VERIFIED | Owner: Phase 3 Agent C; Source: SPEC.md |
| FR-02 | 任務執行器 — `subprocess.run(shlex.split(cmd), …, timeout=…)`(禁 `shell=True`);狀態機 `pending → running → done/failed/timeout/blocked`;結果欄位 `exit_code`/`stdout_tail`/`stderr_tail`/`duration_ms`/`finished_at`;`--all` 以 `ThreadPoolExecutor` 依 DAG 拓撲順序並發;共享 `threading.Lock` 保護存儲;單任務 `timeout` → exit 4 (SPEC.md §3 FR-02) | subprocess-execution + concurrency | DRAFT — code not yet authored; AC list: AC-02-1..AC-02-4 (SRS §3 FR-02) | VERIFIED | Owner: Phase 3 Agent C; Source: SPEC.md; cross-cuts NFR-02 (no shell=True), NFR-03 (atomic write) |
| FR-03 | 重試與斷路器 — 失敗/timeout 自動重試至 `TASKQ_RETRY_LIMIT` 次,指數退避 `TASKQ_BACKOFF_BASE × 2^n`(sleep 函式可注入);連續最終失敗 ≥ `TASKQ_BREAKER_THRESHOLD` → `OPEN`,期間 `run` 立即 exit 3 不執行 subprocess;經 `TASKQ_BREAKER_COOLDOWN` 進入 `HALF_OPEN`,放行一任務;狀態持久化於 `breaker.json` 原子寫 (SPEC.md §3 FR-03) | retry + circuit-breaker | DRAFT — code not yet authored; AC list: AC-03-1..AC-03-3 (SRS §3 FR-03) | VERIFIED | Owner: Phase 3 Agent C; Source: SPEC.md; cross-cuts NFR-03 (atomic write, recovery timing) |
| FR-04 | 結果 TTL 快取 — 簽名 `sha256(command)`;`run <id> --cached` 在 `TASKQ_CACHE_TTL` 內同簽名 `done` 結果直接回放(`cached: true`),不執行 subprocess;過期或不存在 → 正常執行後寫 `cache.json`;讀寫原子+執行緒安全 (SPEC.md §3 FR-04) | caching | DRAFT — code not yet authored; AC list: AC-04-1, AC-04-2 (SRS §3 FR-04) | VERIFIED | Owner: Phase 3 Agent C; Source: SPEC.md |
| FR-05 | CLI 整合 — `click` 群組化子命令,入口 `python -m taskq_plus`;命令:submit / run(status/list/graph/plugins/export/clear) ;全域 `--json` flag;Exit codes: 0/1/2/3/4/5/6 (SPEC.md §3 FR-05) | cli-surface | DRAFT — code not yet authored; AC list: AC-05-1..AC-05-3 (SRS §3 FR-05) | VERIFIED | Owner: Phase 3 Agent C; Source: SPEC.md |
| FR-06 | 任務相依 DAG — `--after` 建邊;`run --all` 以 Kahn 拓撲排序決定執行順序(同層 in-degree 0 才並發);相依非 `done` → 下游 `blocked` 不執行且不計入斷路器失敗;循環偵測 → exit 5 列出循環路徑;深度 > `TASKQ_MAX_DAG_DEPTH` → exit 5;`graph --format dot | text` (SPEC.md §3 FR-06) | dag-scheduling | VERIFIED | DRAFT | Owner: Phase 3 Agent C; Source: SPEC.md |
| FR-07 | Plugin Hook 系統 — Plugin 為 Python 模組提供 `pre_run`/`post_run` hook;載入來源僅限 `TASKQ_PLUGINS` 環境變數 allowlist(逗號分隔),`importlib.import_module` 具名載入;安全鐵律:禁 `eval`/`exec`/`__import__`、禁路徑或 URL、模組名匹配 `^[A-Za-z_][A-Za-z0-9_.]*$` 否則 exit 6;plugin 拋例外不中斷任務(記 `plugin_error` 稽核事件),連續 3 次失敗停用;`plugins list` 列出載入狀態 (SPEC.md §3 FR-07) | plugin-isolation | DRAFT — code not yet authored; AC list: AC-07-1..AC-07-3 (SRS §3 FR-07) | VERIFIED | Owner: Phase 3 Agent C; Source: SPEC.md; cross-cuts NFR-02 |
| FR-08 | 結構化稽核日誌與匯出 — `$TASKQ_AUDIT_LOG` 預設 `$TASKQ_HOME/audit.jsonl`,JSON Lines,append-only;欄位 `ts`/`event`/`task_id`/`correlation_id`/`detail`,每筆 CLI 呼叫共用同一 `correlation_id`;事件種類 submit/run_start/run_end/retry/breaker_open/breaker_close/cache_hit/blocked/plugin_error;NFR-04 redaction 寫入前套用;`export --format json\|csv\|md` 三格式任務筆數與欄位集合一致,CSV 正確跳脫逗號/引號 (SPEC.md §3 FR-08) | observability + export | DRAFT — code not yet authored; AC list: AC-08-1, AC-08-2 (SRS §3 FR-08) | VERIFIED | Owner: Phase 3 Agent C; Source: SPEC.md; cross-cuts NFR-04 |

### Non-Functional Requirements (NFR)

| NFR ID | Spec Description | Intent Class | Decision Framework | Status | Notes |
|--------|-----------------|--------------|-------------------|--------|-------|
| NFR-01 | 效能預算(`performance`)— `submit+status` 100 iter p95 < 50ms;`run --all` 對 200 任務拓撲排序階段 p95 < 200ms;量測 `pytest-benchmark` (SPEC.md §4 NFR-01) | performance-budget | DRAFT — code not yet authored; AC list: AC-01-1, AC-01-2 (SRS §4 NFR-01) | DRAFT | Owner: Phase 3 Agent C; Source: SPEC.md; harness `performance` dimension scores mean only, dedicated pytest-benchmark assertions required |
| NFR-02 | 執行與載入安全(`security`)— codebase `shell=True`/`eval(`/`exec(`/`__import__(` 0 命中(grep gate);FR-01 注入字元黑名單 7 字元逐一測試覆蓋;plugin 載入面 allowlist + 白名單正則 + 禁路徑/URL;`bandit -r 03-development/src/` 0 HIGH/0 MEDIUM (SPEC.md §4 NFR-02) | security-gates | DRAFT — code not yet authored; AC list: AC-02-1..AC-02-4 (SRS §4 NFR-02) | DRAFT | Owner: Phase 3 Agent C; Source: SPEC.md; cross-cuts FR-01, FR-07; harness `security` dimension is bandit-only — grep + per-character + plugin regex require dedicated CI |
| NFR-03 | 錯誤處理與原子性(`error_handling`)— 四資料檔(`tasks.json`/`breaker.json`/`cache.json`/`audit.jsonl`)原子寫(tmp+`os.replace`;audit append+fsync);禁裸 `except:`、`except Exception: pass`、吞 `KeyboardInterrupt`/`SystemExit`;每 `except` 區塊必須 re-raise/轉譯/記錄後明確 exit;breaker `OPEN→CLOSED` 恢復時間 ≤ `TASKQ_BREAKER_COOLDOWN` + 1s (SPEC.md §4 NFR-03) | atomicity + error-handling | DRAFT — code not yet authored; AC list: AC-03-1..AC-03-3 (SRS §4 NFR-03) | DRAFT | Owner: Phase 3 Agent C; Source: SPEC.md; cross-cuts FR-02, FR-03 |
| NFR-04 | 敏感資料遮蔽(`security`)— `stdout_tail`/`stderr_tail`/稽核日誌 `detail` 寫入前,匹配 `(sk-[A-Za-z0-9_-]{8,}|token=\S+|Bearer\s+\S+)` 的行整行以 `[REDACTED]` 取代;以「檔案內容不含明文 secret」斷言 (SPEC.md §4 NFR-04) | redaction | DRAFT — code not yet authored; AC list: AC-04-1..AC-04-3 (SRS §4 NFR-04) | DRAFT | Owner: Phase 3 Agent C; Source: SPEC.md; harness `security` dimension runs bandit only — secret-on-disk requires dedicated grep test; `secrets_scanning` covers source-tree, not runtime stdout |
| NFR-05 | 文件覆蓋(`documentation`)— `03-development/src/taskq_plus` 全部公開函式/類別 docstring 含 `[FR-XX]`/`[NFR-XX]` 引用;`ast-docstrings` 覆蓋 100% (SPEC.md §4 NFR-05) | documentation-coverage | DRAFT — code not yet authored; AC list: AC-05-1, AC-05-2 (SRS §4 NFR-05) | DRAFT | Owner: Phase 3 Agent C; Source: SPEC.md; harness `documentation` dimension scores presence only — `[FR-XX]`/`[NFR-XX]` token requirement requires dedicated test |
| NFR-06 | 架構分層契約(`architecture_constraints`)— 專案根 `.importlinter` 宣告 `cli > observability > service > storage > models`,上層可 import 下層,下層不得 import 上層;`config` 為 independence 模組;`lint-imports` exit 0;禁止刪除 `.importlinter`/放寬成 `ignore_imports`/降級為單條 `forbidden` 取得通過;CRG 校準 `crg_cohesion_healthy` 保持預設 (SPEC.md §4 NFR-06) | architecture-constraints | DRAFT — code not yet authored; AC list: AC-06-1..AC-06-3 (SRS §4 NFR-06) | DRAFT | Owner: Phase 3 Agent C; Source: SPEC.md; cross-cuts §10 CRG calibration; harness `architecture_constraints` scores lint-imports exit code only |
| NFR-07 | 依賴與授權合規(`license_compliance`)— runtime 依賴 `requirements.txt` 以 `==` 釘版(禁 `>=`/`~=`/無版本);license 限 MIT/BSD-2-Clause/BSD-3-Clause/Apache-2.0;**掃描範圍必須含已安裝依賴樹**(`pip-licenses --format=json --with-urls` 或 `scancode --license <venv>/lib/python3.11/site-packages --json-pp -`);產出 SBOM 至 `08-config/SBOM.json`(name/version/license) (SPEC.md §4 NFR-07) | dependency-compliance | DRAFT — code not yet authored; AC list: AC-07-1..AC-07-3 (SRS §4 NFR-07) | DRAFT | Owner: Phase 3 Agent C; Source: SPEC.md; Bug-D regression target — harness stock scan only covers first-party source |
| NFR-08 | 變異測試(`mutation_testing`)— `.methodology/harness_config.json` `features.mutation_testing: true`;mutation score ≥ 70;範圍限 `service/` + `storage/`,`harness_config.json` 註記「執行時間預算」理由 (SPEC.md §4 NFR-08) | mutation-testing | DRAFT — code not yet authored; AC list: AC-08-1..AC-08-3 (SRS §4 NFR-08) | DRAFT | Owner: Phase 3 Agent C; Source: SPEC.md; harness `mutation_testing` dimension owns the score — must use harness `mutation-test-score` command |
| NFR-09 | 驗證真實性(`test_assertion_quality`)— 任何 FR/NFR 驗證測試禁 `pytest.skip`/`pytest.mark.skip`/`skipif`/`xfail`/無斷言 stub;`pytest 03-development/tests -q` skipped 計數 = 0;每測試 ≥ 1 `assert`(`ast-assertions` `zero_assert == 0`);反造假條款禁 `--ignore`/`-k`/`--deselect`/`collect_ignore`/從 `testpaths` 移除目錄;`TRACEABILITY_MATRIX.md` `VERIFIED` 必須對應實際執行並通過的測試 (SPEC.md §4 NFR-09) | verification-honesty | DRAFT — code not yet authored; AC list: AC-09-1..AC-09-5 (SRS §4 NFR-09) | DRAFT | Owner: Phase 3 Agent C; Source: SPEC.md; cross-cuts all FR/NFR verification |
| NFR-10 | 整合覆蓋(`integration_coverage`)— `03-development/tests/integration/` 行覆蓋 ≥ 80%;整合測試經 CLI 入口(`python -m taskq_plus` 或 `click.testing.CliRunner`)驅動,禁直接呼叫內部函式;涵蓋 submit→run→status 全鏈、DAG 多層、breaker 開闔、cache 命中、plugin hook、三匯出格式 (SPEC.md §4 NFR-10) | integration-coverage | DRAFT — code not yet authored; AC list: AC-10-1, AC-10-2 (SRS §4 NFR-10) | DRAFT | Owner: Phase 3 Agent C; Source: SPEC.md; harness `integration_coverage` measures line % only — CLI-entry-point constraint requires dedicated test |
| NFR-11 | 可讀性(`readability`)— 專案 MI(LLOC 加權)≥ 80;單一函式 cyclomatic complexity ≤ 10;單一檔案 ≤ 400 行;單一目錄 ≤ 15 檔 (SPEC.md §4 NFR-11) | readability | DRAFT — code not yet authored; AC list: AC-11-1..AC-11-4 (SRS §4 NFR-11) | DRAFT | Owner: Phase 3 Agent C; Source: SPEC.md; harness `readability` dimension scores MI average only — CC, file-LOC, files-per-dir require dedicated CI |
| NFR-12 | 系統驗證目標(`execute_verification_target`)— `Makefile` 提供 `verify-system` target,串接全套測試 + CLI 冒煙(submit/run/status/graph/export/clear);`make verify-system` exit 0 且 stdout 含 `verify-system: PASS` (SPEC.md §4 NFR-12) | system-verification | DRAFT — code not yet authored; AC list: AC-12-1, AC-12-2 (SRS §4 NFR-12) | DRAFT | Owner: Phase 3 Agent C; Source: SPEC.md; harness `execute_verification_target` scores exit code only — stdout substring requires dedicated CI |

## Source-Citation Index

> All 20 rows above cite the project-root canonical spec source `SPEC.md` (NOT
> `01-requirements/SPEC.md`). Per `harness-cli` `check_forward_refs` gate, the
> canonical_spec SSOT is the repo-root `SPEC.md`; any reference written as
> `01-requirements/SPEC.md` is illegal. Rule: **R-CANONICAL-SPEC-PATH-001**.

| Spec Source | Canonical Path | Used In Rows |
|--------------|----------------|--------------|
| Canonical Spec (SSOT) | `SPEC.md` | FR-01, FR-02, FR-03, FR-04, FR-05, FR-06, FR-07, FR-08, NFR-01, NFR-02, NFR-03, NFR-04, NFR-05, NFR-06, NFR-07, NFR-08, NFR-09, NFR-10, NFR-11, NFR-12 |

## Update log

| Date | Change | By |
|------|--------|----|
| 2026-07-30 | Initial creation (template) | Agent A |
| 2026-08-04 | Round 1 fill — 8 FR / 12 NFR transcribed from SRS.md → SPEC.md (root canonical source); Status column intentionally left at DRAFT for `advance-phase` machine-refresh; SPEC.md referenced as bare root path per R-CANONICAL-SPEC-PATH-001 | Agent A |