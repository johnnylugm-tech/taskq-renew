# RELEASE_CHECKLIST

## Pre-Release Checks
- [ ] All P1-P7 phases completed and artifacts generated.
- [ ] CI pipeline fully passed.
- [ ] Final Sign Off approved.
- [ ] Production environment provisioned.
- [ ] Rollback plan documented.

## Human Context (P8 append)

> Append-only block. Framework-generated Pre-Release Checks above MUST NOT be modified.

### Deployment Runbook
- **Primary runbook**: `https://runbooks.internal/taskq-renew/deploy/production`
- **Pre-deploy checklist**: `https://runbooks.internal/taskq-renew/deploy/preflight`
- **Smoke-test suite**: `https://runbooks.internal/taskq-renew/deploy/smoke-tests`
- **Deploy command reference**: `https://runbooks.internal/taskq-renew/deploy/cli-reference`
- Mirror (offline access): `docs/runbooks/deploy/` in the ops wiki export

### Rollback Owner + On-Call
| Role | Primary | Secondary | Escalation |
|------|---------|-----------|------------|
| Rollback decision authority | Release Manager on shift | Engineering Manager | VP Engineering |
| Rollback executor | Platform SRE on shift | DevEx on call | Release Manager |
| Incident commander | On-call IR lead (rotates weekly) | Backup IR lead | Engineering Manager |
| Communications owner | DevRel on call | Product Manager | VP Product |
| Customer-facing escalation | Customer Success Lead | Account Manager for impacted tenants | VP Customer Success |

PagerDuty schedule: `taskq-renew-rollback`. Acknowledge SLA 5 min, decision SLA 15 min, execution SLA 30 min from trigger.

### Post-Release Monitoring Dashboard
- **Primary dashboard**: Grafana `https://grafana.internal/d/taskq-renew-release` (folder: `Release / Production`)
- **Required panels (auto-pinned at release)**:
  - Request rate by endpoint (p50 / p95 / p99 latency)
  - Error rate (4xx, 5xx) per service
  - Queue depth and worker saturation
  - DB connection pool utilization + slow query count
  - Memory / CPU per pod, pod restart count
  - Business KPI: jobs enqueued, jobs completed, retry rate, dead-letter rate
- **Alert thresholds** (first 4h post-release are extra-strict, see `https://grafana.internal/d/taskq-renew-release-alerts`):
  - 5xx rate > 0.5% sustained 5 min → page
  - p99 latency > 2× pre-release baseline sustained 10 min → page
  - Dead-letter rate > 3× pre-release baseline → ticket + Slack `#taskq-renew-release`
  - Pod restart count > 0 in any service → ticket
- **Owner**: Observability Lead (primary), Platform SRE (secondary)

### Customer Comms Template
Subject: `[taskq-renew] <version> released — no action required` (or `action required` variant)

Body:
```
Hi <customer-name>,

We have released <version> of taskq-renew to production on <release-date>.

What changed:
- <bullet 1: user-facing change>
- <bullet 2: user-facing change>
- <bullet 3: user-facing change>

Action required from you: <none | describe>

Expected impact: <none | describe with timing window>

If you observe any unexpected behavior, please contact support@taskq-renew.example
or open a ticket via the customer portal. Reference release ID <release-id>.

Status page: https://status.taskq-renew.example
Detailed changelog: https://docs.taskq-renew.example/changelog/<version>

Thank you,
The taskq-renew team
```

Variants maintained in `docs/comms/templates/release-announcement-{none,action-required,incident}.md`. Sent via Customer Success Lead within 4h of green post-release monitoring window.
