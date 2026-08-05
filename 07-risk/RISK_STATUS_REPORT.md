# 07-risk/RISK_STATUS_REPORT.md — taskq Risk Status Report

> **Phase 7 Risk Status Report** • Generated: 2026-08-05
> Snapshot date: 2026-08-05 (at Phase 7 entry; Gate 4 = PASS, score 93.57;
> Gate 3 = PASS, score 95.88; Gate 2 = PASS, score 95.30; Gate 1 = 7/8 FRs
> PASSED — FR-02 last re-PASSED at 100.0 on 2026-08-05T08:00:34Z).
>
> This is the **status rollup** of `RISK_REGISTER.md` with the action items
> tracked in `RISK_MITIGATION_PLANS.md`. Every row carries a verifiable exit
> signal, a named owner, and a target date so the report is operable at a
> glance.
>
> **Severity bands**: HIGH = L×I ≥ 9, MEDIUM = 4–8, LOW ≤ 3
> (see `RISK_REGISTER.md §6`).

---

## 1. Headline

- 15 risks tracked. 5 HIGH, 3 MEDIUM, 7 LOW.
- HIGH risks are split: **R1, R2, R5, R6, R8**. R5 is **Closed** as of
  fix_commit `affd223d`. R1/R2/R6/R8 have residual TODOs captured in
  `RISK_MITIGATION_PLANS.md`.
- All 5 HIGH risks have a named owner and a target date in §3.
- No new HIGH risks introduced by Phase 7 itself; residual risks are
  confined to the existing register.
- Gate 4 `da_waiver` for `architecture` (CRG 77.8 < 80) is preserved as
  R11 in the register. It does not change overall PASS.

## 2. Status legend

| Status | Definition |
|--------|------------|
| **Closed** | Verification artefact passes; no residual action. |
| **Mitigated** | Primary control shipped; verification passes; residual monitoring. |
| **Open** | Tracking-only; mitigation not yet started. |
| **Monitored** | Live watchlist (no active plan required). |
| **Waived** | Explicit framework waiver with documented justification. |
| **Accepted Limitation** | Acknowledged in SPEC; no action by the project. |
| **Reopens on** | Auto-reopen trigger documented. |

## 3. Per-risk status (15 rows)

| ID | Name | Band | Score | Owner | Status | Target date | Verifiable exit signal |
|----|------|------|-------|-------|--------|-------------|------------------------|
| R1 | Concurrent writes corrupt `tasks.json` | HIGH | 15 | executor-lead | Mitigated + residual A3 (cross-process stress) | Gate 5 close-out | `test_storage_task_store_concurrent_writers.py` passes (32 threads × 16 subprocesses) |
| R2 | Subprocess hangs / zombie tasks | HIGH | 9 | executor-lead | Mitigated + residual B3 (CLI budget) | P8-exit | `test_subprocess_hang_kill.py` passes; CLI exits within `TASKQ_RUN_BUDGET` |
| R5 | Secrets leak to disk | HIGH | 15 | security-lead | **Closed** as of `affd223d` | (closed) | `test_hunt_nfr04_write_through.py` passes; `grep -c "sk-" $TASKQ_HOME/audit.jsonl` = 0 |
| R6 | Plugin loader = arbitrary-code-execution entrypoint | HIGH | 15 | plugin-lead | Mitigated + residual D3 (CI hook) | P8-exit | `grep -RE "eval\(|exec\(|__import__\(" .../plugins.py` returns nothing |
| R8 | Plugin exception breaks main flow | HIGH | 9 | plugin-lead | Mitigated + residual E3 (threshold review) | P8-exit | `test_fr07_plugin_auto_disable.py` passes; auto-disable observed in synthetic 1000-task load |
| R3 | Circuit breaker false-lock | LOW | 3 | breaker-lead | Mitigated | Gate 5 close-out | `test_fr03_*` all pass; no manual unlock observed in CI |
| R4 | Cache replay returns stale results | MEDIUM | 3 | cache-lead | Mitigated | Gate 5 close-out | `test_fr04_ttl_expiry*` passes; cache_hit path returns re-executed result after TTL |
| R7 | Pathological DAG exhausts resources | LOW | 3 | dag-lead | Mitigated | Gate 5 close-out | `test_fr06_*_cycle_with_exit_5` passes; depth cap rejects oversize graph |
| R9 | Dependency introduces disallowed license | LOW | 3 | compliance-lead | Mitigated | Gate 5 close-out | Gate 5 `license_compliance` = 100; `pip-licenses --format=json` no out-of-allowlist |
| R10 | Audit log unbounded growth | MEDIUM | 5 | observability-lead | Accepted Limitation (per SPEC §9) | — | Operator policy: rotate externally |
| R11 | Architecture dim 77.8 (< 80 threshold) | MEDIUM | 6 | architecture-lead | **Waived** (per `da_waiver`); Monitored | Re-evaluate at next Gate | Gate 5 `architecture` ≥ 80 OR waiver extended with documented justification |
| R12 | Docstring gap on `TaskStore.{load, add, contains_name}` | MEDIUM | 4 | docs-lead | Open | P8-exit | Documentation dim = 100 (currently 96.25; 3 symbols missing) |
| R13 | NFR-01 macro-bench coverage gap (D4 91.0%) | MEDIUM | 6 | perf-lead | Open | P8-exit | D4 spec-coverage = 100; missing `test_nfr01_*` added |
| R14 | Mutation survivors 77 (score 79.8 ≥ 70) | MEDIUM | 4 | mutation-lead | Monitored | Re-evaluate on mutation drift | Mutation ≥ 70 holds across 2 consecutive gates; survivors list shrinks |
| R15 | Bandit LOW B404/B603 in `service/executor.py` | MEDIUM | 4 | security-lead | Mitigated (justified inline) | Gate 5 close-out | Bandit 0 HIGH, 0 MEDIUM holds; B404/B603 explicitly `# nosec`-justified |

## 4. HIGH-risk closure plan (rolled up)

| ID | Owner | Target | Locked-in next steps |
|----|-------|--------|----------------------|
| R1 | executor-lead | Gate 5 | A3 — write `test_storage_task_store_concurrent_writers.py` (32 threads × 16 processes) |
| R2 | executor-lead | P8-exit | B3 — add `TASKQ_RUN_BUDGET` wall-clock budget test |
| R5 | security-lead | (closed) | C5 — wire CI gate on `_redact` call sites; auto-reopen if `test_nfr04_*` fails |
| R6 | plugin-lead | P8-exit | D3 — add CI regex tripwire for `eval(` / `exec(` / `__import__(` in `service/plugins.py` |
| R8 | plugin-lead | P8-exit | E3 — synthetic 1000-task load verifying auto-disable threshold |

## 5. Acceptance criteria for P7 (handoff gate)

The validate-handoff check at Phase 7 exit expects:

- `07-risk/RISK_REGISTER.md` — present, well-formed (≥ 10 risks, each with
  ID/name/L/I/category/mitigation). **Met**: 15 risks; per-row schema
  satisfied.
- `07-risk/RISK_MITIGATION_PLANS.md` — formal plans for HIGH risks with
  owner + deadline. **Met**: 5 HIGH plans (R1/R2/R5/R6/R8), each with
  owner/deadline/success criterion.
- `07-risk/RISK_STATUS_REPORT.md` — summary of all risks, owner + target
  date. **Met**: this document.

## 6. Open count by Gate

| Gate | open_critical | open_high | open_medium | Source |
|------|---------------|-----------|-------------|--------|
| Gate 3 | 0 | 0 | 0 | `gate3_result.json::open_*_count` |
| Gate 4 | 0 | 0 | 0 | `gate4_result.json::open_*_count` |
| Gate 5 (planned) | 0 | 0 (after R5 closure) | ≥ 3 (R12/R13/R15) | extrapolated from current state |

> `open_*_count` from the gate result JSONs is the framework's authoritative
> open-issue counter; Phase 7 does not introduce any new open high or critical
> issues.

## 7. Outstanding MEDIUM work (P8 backlog)

| ID | Name | Action | Owner |
|----|------|--------|-------|
| R12 | Documentation gap (3 symbols missing) | Add 3 docstrings to `TaskStore.{load, add, contains_name}` (docs-only PR) | docs-lead |
| R13 | NFR-01 macro-bench | Author 8 missing `test_nfr01_*` per D4 spec-coverage list | perf-lead |
| R15 | Bandit LOW B404/B603 | Document `# nosec` justification inline OR refactor `service/executor.py` to use `subprocess.list2cmdline` indirectly | security-lead |

## 8. Watchlist (LOW — typically read-only)

| ID | Name | Why on watchlist |
|----|------|------------------|
| R3 | Breaker false-lock | Could surface if breaker cooldown tuning changes |
| R7 | Pathological DAG | Could surface if DAG limits relaxed |
| R9 | Disallowed license | Could surface if a new transitive dep lands |
| R10 | Audit log growth | Accepted; operator-rotation policy required |
| R11 | Architecture CRG 77.8 | Watch for trend; re-evaluate on N6 |
| R14 | Mutation survivors | Watch for drift between gates |

## 9. Cadence

- **Owner reporting**: each named owner reports into this document after
  every Gate 5 milestone (or sooner if a residual gate fires).
- **Auto-reopen triggers**:
  - R5 reopens if `test_nfr04_*` or `test_hunt_nfr04_*` fails on any PR.
  - R1 reopens if `test_storage_task_store_concurrent_writers.py` fails or
    if data-loss is observed in any concurrent-write path.
  - R6 reopens if `service/plugins.py` introduces `eval`/`exec`/`__import__(`
    *or* a path outside the `taskq_plus.plugins.` prefix.

## 10. P7 exit checklist

- [x] `07-risk/RISK_REGISTER.md` — 15 risks, schema satisfied.
- [x] `07-risk/RISK_MITIGATION_PLANS.md` — 5 HIGH risks covered.
- [x] `07-risk/RISK_STATUS_REPORT.md` — this document.
- [x] All HIGH risks have an owner and a target date.
- [x] R5 is recorded as **Closed** with fix_commit `affd223d`.
- [x] No new HIGH risks introduced by P7.
- [x] Cross-references to `gate3_result.json` / `gate4_result.json` /
      `bug_hunt_report.json` preserved for audit.

---

*End of RISK_STATUS_REPORT.md*
