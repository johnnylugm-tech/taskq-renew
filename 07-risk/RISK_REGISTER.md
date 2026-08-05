# 07-risk/RISK_REGISTER.md — taskq Risk Register

> **Phase 7 Risk Register** • Generated: 2026-08-05
> Source-of-truth: `SPEC.md §9` risk matrix (R1–R10) seeded with open-issue review
> of `gate3_result.json` / `gate4_result.json` / `bug_hunt_report.json` /
> `gate_timestamps.jsonl`.
>
> **Scale**: Likelihood (1=Rare … 5=Almost Certain) × Impact (1=Negligible … 5=Catastrophic).
> **Severity band**: HIGH = L×I ≥ 9, MEDIUM = 4–8, LOW ≤ 3.
>
> **Note on referenced sources** (scope clarification):
> - `SPEC.md §9` (R1–R10) — risk matrix owned by the spec.
> - `.methodology/gate3_result.json` + `.methodology/gate4_result.json` — open
>   gate findings at P4-exit and P6-exit respectively.
> - `.methodology/bug_hunt_report.json` — adversarial bug-hunt findings (both
>   pre-Gate-3 highs are now `resolution.status=resolved` via fix_commit
>   `affd223d`, but the patterns are recorded here as live residual risks to
>   monitor).
> - `.methodology/gate_timestamps.jsonl` — gate timing & re-run cadence.
> - **NOT FOUND** in repo at this commit: `.methodology/deferred_fixes.md` and
>   `.sessi-work/issue_registry.json`. The SPEC's intent for those trackers is
>   preserved: any new deferred fix or tracked-issue must be added to R11 (Open
>   Findings) and R12 (Deferred Fixes) below as it is created.
>
> The **HIGH** band seeds formal mitigation plans in
> `07-risk/RISK_MITIGATION_PLANS.md`. The full status rollup is in
> `07-risk/RISK_STATUS_REPORT.md`.

---

## 1. Risk summary

| Band | Count | IDs |
|------|-------|-----|
| HIGH (L×I ≥ 9) | 5 | R1, R2, R5, R6, R8 |
| MEDIUM (4–8) | 3 | R4, R10, R13 |
| LOW (≤ 3) | 7 | R3, R7, R9, R11, R12, R14, R15 |
| **Total tracked** | **15** | |

## 2. Risk register (canonical, seeded from SPEC.md §9)

| ID | Name | Likelihood (1–5) | Impact (1–5) | Score | Band | Category | Primary mitigation | Owner | Status |
|----|------|----|----|------|----|----|----|----|----|
| **R1** | Concurrent writes corrupt `tasks.json` | 3 | 5 | 15 | HIGH | concurrency/storage | `taskq_plus.storage.atomic` (tmp + `os.replace`) + module-level `threading.Lock` (`task_store.add`); verified by FR-02 tests | executor-lead | Mitigated; residual kept under watch |
| **R2** | Subprocess hangs / zombie tasks | 3 | 3 | 9 | HIGH | reliability/subprocess | `subprocess.run(..., timeout=TASKQ_TASK_TIMEOUT)` mandatory; `TimeoutExpired` → state `timeout` (FR-02) | executor-lead | Mitigated |
| **R3** | Circuit breaker false-lock | 1 | 3 | 3 | LOW | reliability/breaker | Cooldown timer + `HALF_OPEN` half-probe state (FR-03); `BreakerError` propagation tests | breaker-lead | Mitigated |
| **R4** | Cache replay returns stale results | 3 | 1 | 3 | MEDIUM (≈low end; band is MEDIUM by 1pt arithmetic but treated operationally as low) | correctness/cache | TTL expiry triggers re-execution (FR-04); recorded as MEDIUM to honour the arithmetic band but flagged as operationally LOW | cache-lead | Mitigated |
| **R5** | Secrets leak to disk (tasks.json / cache.json / audit.jsonl) | 3 | 5 | 15 | HIGH | security/secrets | Regex redaction `(sk-[A-Za-z0-9_-]{8,}|token=\S+|Bearer\s+\S+)` BEFORE write; pre-write hook on stdout_tail/stderr_tail/audit.detail (NFR-04) | security-lead | Mitigated (fix_commit `affd223d`) |
| **R6** | Plugin loader becomes arbitrary-code-execution entrypoint | 3 | 5 | 15 | HIGH | security/plugin | `importlib` allowlist + name regex whitelist + ban `eval`/`exec`/path injection (FR-07 / NFR-02) | plugin-lead | Mitigated; bandit keeps flagging B404/B603 LOW |
| **R7** | Pathological DAG exhausts resources | 1 | 3 | 3 | LOW | correctness/dag | Kahn topological sort rejects cycles + depth cap (FR-06); covered by `test_fr06_*_cycle_with_exit_5` | dag-lead | Mitigated |
| **R8** | Plugin exception breaks main flow | 3 | 3 | 9 | HIGH | reliability/plugin | Exception isolation per hook + consecutive-failure auto-disable (FR-07) | plugin-lead | Mitigated |
| **R9** | Dependency introduces disallowed license | 1 | 3 | 3 | LOW | compliance/license | Pinned-version manifest + allowlist + SBOM scan (NFR-07) — Gate 4 distinct_licenses=[] | compliance-lead | Mitigated |
| **R10** | Audit log unbounded growth | 5 | 1 | 5 | MEDIUM (high-likelihood, low-impact) | observability/disk | append-only; rotation **deferred** to operator/owner; **known limitation** declared in SPEC | observability-lead | Accepted limitation |

## 3. Gate 3 / Gate 4 + bug-hunt residual risks (newly tracked, P7-specific)

| ID | Name | Likelihood (1–5) | Impact (1–5) | Score | Band | Category | Source | Mitigation | Owner | Status |
|----|------|----|----|------|----|----|----|----|----|----|
| **R11** | Architecture dimension score 77.8 below 80-threshold (CRG community_cohesion) | 3 | 2 | 6 | MEDIUM | architecture/cohesion | `gate4_result.json::architecture` — 7 healthy / 9 total, two over-fragments of `task_store.py` | Documented `da_waiver`; **monitor** for trend regression; revisit only if N6 (next project) needs it | architecture-lead | Tracked / waived; not blocking |
| **R12** | Documentation gap on `TaskStore.{load,add,contains_name}` docstrings (96.25% vs NFR-05 100% target) | 4 | 1 | 4 | MEDIUM (operationally LOW; above-threshold but spec-target gap) | documentation | `gate4_result.json::documentation` (3 missing symbols on the hot I/O surface) | Add 3 docstrings in a follow-up PR (no behaviour change; coverage keeps at 100%) | docs-lead | Open |
| **R13** | NFR-01 macro-bench coverage gap (8 NFR-pattern tests missing; D4 spec-coverage 91.0% — above 90% threshold but spec-target 100%) | 3 | 2 | 6 | MEDIUM | performance/coverage | `gate4_result.json::traceability` 4b=91.0% | Add `test_nfr01_submit_status_p95_under_50ms` and `test_nfr01_topological_sort_200_tasks_p95_under_200ms` in a coverage-completion PR | perf-lead | Open |
| **R14** | Mutation survivors (77 survived; score 79.8 ≥ 70 — passing but residual) | 2 | 2 | 4 | MEDIUM (just over LOW boundary; tracked for trend) | quality/mutation | `gate4_result.json::mutation_testing` survivors list | Targeted regression tests over the surviving mutants in a future mutation round | mutation-lead | Monitored |
| **R15** | Bandit LOW findings B404/B603 in `service/executor.py` (subprocess invocation) | 4 | 1 | 4 | MEDIUM (higher likelihood but LOW impact; tracked to keep score at 98) | security/static | `gate4_result.json::security` (2 LOW; no HIGH/MEDIUM) | `# nosec` justified inline with documented justification; bandit explicitly accepts B404/B603 when shell=False and no untrusted input | security-lead | Mitigated w/ comment |

## 4. Notes on previously-resolved findings (kept for audit trail)

The bug-hunt report flagged two NFR-04 high-severity findings (2026-08-04):
- `tasks_cache_audit_redact#1` — `commands._persist_result` /
  `cache_record` skipping redaction before write.
- `plugins_audit_redact#1` — `service.plugins.append_audit_event` skipping redaction.

Both are now `resolution.status = resolved` via fix_commit `affd223d` and
verified by `test_hunt_nfr04_write_through.py`. **They are tracked under R5
above as the underlying risk class**; if a future regression reintroduces
the pattern, R5 must be reopened before Gate 5.

## 5. Risk-class taxonomy used

- **security**: secrets, plugin loader, subprocess injection, bandit/secrets-scanning signals.
- **reliability**: subprocess hangs, breaker false-lock, plugin exception isolation.
- **concurrency/storage**: task_store atomic write + lock.
- **correctness**: cache TTL, DAG cycle/depth guard.
- **compliance/license**: pinned-version allowlist + SBOM.
- **observability/disk**: audit-log unbounded growth (known limit).
- **architecture/cohesion**: CRG community-cohesion score.
- **documentation**: docstring coverage.
- **performance/coverage**: NFR macro-bench + D4 spec-coverage.
- **quality/mutation**: mutation-survivor regression.
- **security/static**: bandit LOW findings.

## 6. Severity bands — formal definition

| Band | Score range | Operational interpretation | Required artefacts |
|------|-------------|----------------------------|--------------------|
| **HIGH** | L×I ≥ 9 | Could block release / data loss / silent leak | Formal mitigation plan in `RISK_MITIGATION_PLANS.md` with owner + deadline |
| **MEDIUM** | 4–8 | Could degrade quality / forward-defer risk | Tracked in register + status report with at least one named owner |
| **LOW** | ≤ 3 | Hygienic / score-margin | Tracked in register only |

---

*End of RISK_REGISTER.md*
