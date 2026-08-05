# CONFIG_RECORDS.md - taskq-renew

> On-demand Lazy Load template.

## 1. Version Information
- Version: vharness-v4-20260805-score94-15-g7eb21ab
- Git Commit: 7eb21ab
- Release Date: 2026-08-05

## 2. Runtime Configuration
| Environment | Config |
|-------------|--------|
| Development | {{config}} |
| Production | {{config}} |

## 3. Dependency List
```
{{pip freeze / npm lock output}}
```

## 4. Environment Variables
| Variable | Type | Description |
|----------|------|-------------|
| {{VAR}} | secret | {{description}} |

## 5. Deployment Log
| Date | Version | Method | Executor |
|------|---------|--------|----------|
| 2026-08-05 | harness-v4-20260805-score94-15-g7eb21ab | {{method}} | {{name}} |

## 6. Configuration Change Log
| Phase | Change | Rationale |
|-------|--------|----------|
| Phase 8 | {{change}} | {{reason}} |

## 7. Rollback SOP
**Trigger Condition**: {{condition}}
**Commands**:
```bash
{{rollback commands}}
```

## 8. Configuration Compliance
- [ ] Phase 7 risk mitigations implemented
- [ ] Monitoring thresholds configured
- [ ] Circuit breaker enabled

## Human Context (P8 append)

> Append-only block. Do NOT modify or delete the framework-generated sections above.

### Config Item Ownership
| Config Item | Primary Owner | Backup Owner | Source-of-Truth |
|-------------|---------------|--------------|-----------------|
| Runtime configuration (`taskq.config.yaml`) | Platform SRE | Release Manager | `taskq_plus/config/` |
| Environment variables (secrets) | Security Lead | Platform SRE | Vault path `secret/taskq-renew/*` |
| Feature flags | Product Owner | Release Manager | `taskq_plus/feature_flags/` |
| Dependency lockfile (`requirements.txt` / `package-lock.json`) | Build Engineer | Platform SRE | Repository root |
| CI/CD pipeline definitions | DevEx | Platform SRE | `.github/workflows/` |
| Database connection settings | DBA | Platform SRE | Vault path `secret/taskq-renew/db` |
| Log/metrics forwarding config | Observability Lead | Platform SRE | `ops/observability/` |

### Secret Rotation Cadence
| Secret Class | Rotation Interval | Procedure Reference |
|--------------|-------------------|---------------------|
| DB credentials (read/write) | 30 days | `runbooks/secrets/rotate-db-creds.md` |
| API tokens (external integrations) | 60 days | `runbooks/secrets/rotate-api-tokens.md` |
| Signing keys (HMAC / JWT) | 90 days | `runbooks/secrets/rotate-signing-keys.md` |
| CI deploy keys | 180 days | `runbooks/secrets/rotate-ci-keys.md` |
| Vault root token | 365 days (or on personnel change) | `runbooks/secrets/rotate-vault-root.md` |
| TLS certificates | 60 days before expiry | `runbooks/secrets/renew-tls.md` |

Rotation health is tracked weekly by the Security Lead; failures page on-call via PagerDuty `taskq-renew-secrets` service.

### Access Audit Log Reference
- **Audit source**: Vault audit log (`vault audit list -format=json`) streamed to `s3://taskq-renew-audit-logs/vault/`
- **Retention**: 365 days hot, 7 years cold (S3 Glacier)
- **Query interface**: `athena://taskq-renew-audit` — query examples in `runbooks/audit/sample-queries.sql`
- **Review cadence**:
  - Automated diff against authorized-access manifest: every 24h
  - Manual review of privileged (root, db-admin, deploy) access: weekly by Security Lead
  - Quarterly access review by Engineering Manager + Security Lead
- **Anomaly detection**: alerts fire to `#sec-alerts` Slack channel when an access pattern deviates from the 30-day rolling baseline by > 2 sigma
- **Compliance evidence**: quarterly export attached to SOC2 evidence package (see `compliance/soc2/Q<n>_evidence.zip`)
- **On-call escalation**: Security Lead (primary) → CISO (secondary) → CEO (tertiary) for confirmed breach
