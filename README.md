# K8s Autopilot — AI Remediation Engine

[![CI](https://github.com/jigarp77/k8s-autopilot/actions/workflows/ci.yml/badge.svg)](https://github.com/jigarp77/k8s-autopilot/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://python.org)

A production-grade **Kubernetes operator** that watches cluster events, diagnoses failures using **Claude AI**, and remediates them automatically — with human-in-the-loop Slack approval, circuit breaking, rate limiting, and a full audit trail.

---

## What it does

```
K8s Event (pod crash / node NotReady / OOMKill)
          │
          ▼
  Context Collector      ← pod logs, events, resource limits, node conditions
          │
          ▼
  Claude AI Diagnosis    ← root cause, severity, confidence, recommended action
          │
          ▼
  Policy Engine          ← mode check → rate limit → circuit breaker → approval gate
          │
          ▼
  Action Registry        ← restart_pod / rollback_deployment / cordon_node / …
          │
          ▼
  Audit Logger + Slack + PagerDuty + Prometheus
```

---

## Features

| Feature | Details |
|---------|---------|
| **AI root-cause diagnosis** | Claude AI analyses pod logs, events, and node conditions to identify the precise root cause |
| **Four operating modes** | `dry-run` / `suggest` / `auto` / `approval` — safe by default |
| **Human-in-the-loop** | Slack Block Kit approval with approve/reject buttons and configurable timeout |
| **Action registry** | Plugin pattern — add new remediations without touching the engine |
| **Rate limiting** | Per-resource + global sliding window, configurable cooldowns |
| **Circuit breaker** | Halts remediation after consecutive failures, prevents runaway automation |
| **AutopilotPolicy CRD** | Namespace-scoped policy configuration — different rules per environment |
| **RemediationAudit CRD** | Every action recorded as a K8s object for `kubectl` visibility |
| **SQLite audit trail** | Persistent audit log with full diagnosis JSON, mounted as PVC |
| **Prometheus metrics** | Remediations, diagnosis latency, rate limits, circuit state, token usage |
| **PagerDuty integration** | Auto-triggers incidents for critical/high severity diagnoses |
| **Runbook generation** | AI generates markdown runbooks, posted as Slack thread replies |
| **Namespace filtering** | Target specific namespaces or label selectors per policy |

---

## Supported triggers

| Trigger | Resource | Default action |
|---------|----------|---------------|
| `CrashLoopBackOff` | Pod | `restart_pod` (after N restarts) |
| `OOMKilled` | Pod | notify + suggest memory limit increase |
| `ImagePullError` | Pod | notify (fix requires deployment patch) |
| `PendingScheduling` | Pod | notify (node capacity / taint issue) |
| `LivenessProbeFailure` | Pod | `restart_pod` |
| `NodeNotReady` | Node | `cordon_node` + notify |
| `NodeDiskPressure` | Node | notify + `drain_node` (with approval) |
| `NodeMemoryPressure` | Node | notify + cordon (with approval) |
| `DeploymentStalled` | Deployment | `rollback_deployment` (approval required) |

---

## Quick start

### Prerequisites

- Kubernetes cluster (1.24+)
- `kubectl` configured
- Python 3.11+
- Anthropic API key
- Slack bot token (optional, for notifications and approvals)

### 1. Install CRDs

```bash
kubectl apply -f crds/
```

### 2. Create secrets

```bash
kubectl create namespace k8s-autopilot

kubectl create secret generic autopilot-secrets \
  --namespace k8s-autopilot \
  --from-literal=anthropic-api-key="sk-ant-..." \
  --from-literal=slack-bot-token="xoxb-..." \
  --from-literal=slack-channel="#incidents" \
  --from-literal=slack-approval-channel="#sre-approvals" \
  --from-literal=pd-api-key="..."         # optional
```

### 3. Deploy the operator

```bash
# Edit deploy/deployment.yaml — replace image with your built image
kubectl apply -f deploy/deployment.yaml

# Verify it's running
kubectl get pods -n k8s-autopilot
kubectl logs -f deployment/k8s-autopilot -n k8s-autopilot
```

### 4. Apply a policy

```bash
# Start with suggest mode — notifications only, no automated actions
kubectl apply -f deploy/example-policy.yaml
```

### 5. Verify

```bash
# Check metrics
kubectl port-forward svc/k8s-autopilot 8000:8000 -n k8s-autopilot
curl http://localhost:8000/metrics

# View audit records
kubectl get remediationaudits -A

# Check audit log
kubectl exec -n k8s-autopilot deployment/k8s-autopilot -- \
  sqlite3 /data/autopilot-audit.db "SELECT resource_id, trigger, action, outcome FROM audit_log ORDER BY started_at DESC LIMIT 20;"
```

---

## Operating modes

Set `AUTOPILOT_MODE` env var or `spec.mode` in `AutopilotPolicy`:

```
dry-run   → Log only. No Slack messages, no actions. Safe for initial deployment.
suggest   → Send Slack notification. Never take automated action.
auto      → Act immediately on safe triggers (restart_pod). Unsafe actions still require approval.
approval  → Every action requires Slack approval, regardless of safety classification.
```

**Recommended rollout:** `dry-run` → `suggest` (1 week) → `approval` (2 weeks) → `auto`

---

## AutopilotPolicy CRD

```yaml
apiVersion: autopilot.k8s.io/v1alpha1
kind: AutopilotPolicy
metadata:
  name: my-policy
  namespace: production
spec:
  mode: approval
  targetNamespaces: [production]
  rateLimit:
    globalMaxPerHour: 10
    perResourceMaxPerHour: 2
    cooldownSeconds: 600
  rules:
    - trigger: CrashLoopBackOff
      requireApproval: true
      maxPerHour: 2
      minRestartCount: 5
      notifyPagerDuty: true
    - trigger: OOMKilled
      requireApproval: true
      notifyPagerDuty: true
```

---

## Project structure

```
k8s-autopilot/
├── autopilot/
│   ├── main.py                        # Kopf operator entrypoint + webhook server
│   ├── config.py                      # Config management (env + YAML overlay)
│   ├── engines/
│   │   ├── context_collector.py       # Gathers pod/node context from K8s API
│   │   ├── diagnosis_engine.py        # Claude AI diagnosis
│   │   ├── remediation_engine.py      # Orchestrates the full remediation pipeline
│   │   └── approval_engine.py         # Slack human-in-the-loop approval
│   ├── handlers/
│   │   ├── pod_handler.py             # Kopf pod event handler + trigger detection
│   │   └── node_handler.py            # Kopf node event handler
│   ├── remediations/
│   │   ├── registry.py                # Plugin registry (@registry.register decorator)
│   │   ├── pod_actions.py             # restart_pod, delete_pod, force_delete_pod
│   │   └── deployment_actions.py      # rollback, restart, scale + node actions
│   ├── integrations/
│   │   ├── slack.py                   # Block Kit notifications + interactive approval
│   │   ├── pagerduty.py               # Events API v2 incident creation
│   │   └── prometheus.py              # Metrics exposition
│   ├── audit/
│   │   └── logger.py                  # SQLite audit trail
│   └── utils/
│       ├── k8s_client.py              # Async K8s client wrapper
│       ├── rate_limiter.py            # Token-bucket rate limiter
│       └── circuit_breaker.py         # Circuit breaker
├── crds/
│   ├── autopilot-policy.yaml          # AutopilotPolicy CRD
│   └── remediation-audit.yaml         # RemediationAudit CRD
├── deploy/
│   ├── deployment.yaml                # Full K8s deployment manifest
│   └── example-policy.yaml            # Example policies per environment
├── tests/
│   ├── unit/
│   │   ├── test_rate_limiter.py
│   │   ├── test_circuit_breaker.py
│   │   ├── test_diagnosis_engine.py
│   │   ├── test_remediation_engine.py
│   │   └── test_pod_handler.py
│   └── integration/
├── Dockerfile                         # Multi-stage production image
├── Makefile                           # Developer convenience targets
├── requirements.txt
├── requirements-dev.txt
└── .github/workflows/ci.yml           # CI: lint → test → security scan → build → push
```

---

## Adding a custom remediation action

The action registry uses a plugin pattern. Adding a new action takes 15 lines:

```python
# autopilot/remediations/my_actions.py

from autopilot.remediations.registry import ActionResult, registry
from autopilot.utils.k8s_client import K8sClient

@registry.register(
    "clear_evicted_pods",
    description="Delete all Evicted pods in a namespace to free quota",
    safe_auto=True,
)
async def clear_evicted_pods(
    k8s: K8sClient,
    namespace: str,
    name: str,
    dry_run: bool = False,
    **kwargs,
) -> ActionResult:
    pods = await k8s.list_pods(namespace, field_selector="status.phase=Failed")
    evicted = [p for p in pods if p.status.reason == "Evicted"]
    if dry_run:
        return ActionResult(success=True, action="clear_evicted_pods", dry_run=True,
                            message=f"[DRY-RUN] Would delete {len(evicted)} evicted pods")
    for pod in evicted:
        await k8s.delete_pod(pod.metadata.name, namespace)
    return ActionResult(success=True, action="clear_evicted_pods",
                        message=f"Deleted {len(evicted)} evicted pods in {namespace}")
```

Then import it in `autopilot/main.py`:

```python
import autopilot.remediations.my_actions  # noqa: F401
```

The AI can now return `"action": "clear_evicted_pods"` and the engine will call your function.

---

## Prometheus metrics

| Metric | Type | Labels |
|--------|------|--------|
| `autopilot_remediations_total` | Counter | `action`, `trigger`, `outcome` |
| `autopilot_events_processed_total` | Counter | `kind`, `trigger` |
| `autopilot_diagnosis_duration_seconds` | Histogram | — |
| `autopilot_rate_limited_total` | Counter | — |
| `autopilot_circuit_open_total` | Counter | — |
| `autopilot_approval_pending` | Gauge | — |
| `autopilot_ai_tokens_total` | Counter | — |

Import the included Grafana dashboard from `deploy/grafana-dashboard.json` (generated separately).

---

## Development

```bash
# Install deps
make install

# Run unit tests
make test

# Lint + type check
make lint

# Run locally in dry-run mode (uses your kubeconfig)
ANTHROPIC_API_KEY=sk-ant-... make dry-run

# Build and push image
make build push REGISTRY=ghcr.io/your-org IMAGE_TAG=v0.1.0

# Deploy to cluster
make crds deploy
```

---

## Security considerations

- The operator runs as a **non-root user** (UID 1000) with a read-only root filesystem
- RBAC is scoped to the minimum required — no cluster-admin
- Secrets are never logged — only the presence/absence of credentials is reported
- All Slack webhook payloads are validated before processing
- The SQLite audit DB is mounted on a PVC — survives pod restarts
- Circuit breaker prevents runaway remediation from a broken action implementation
- Rate limiting prevents thundering-herd scenarios during cluster-wide failures

---

## IAM / RBAC summary

The operator's `ClusterRole` grants:

```
pods                → get, list, watch, delete
nodes               → get, list, watch, patch, update
deployments         → get, list, watch, patch, update
pods/log            → get
pods/eviction       → create
autopilotpolicies   → full CRUD
remediationaudits   → full CRUD
events              → create, patch
```

No `secrets` access. No `exec` into pods. No wildcard verbs.

---

## License

MIT — see [LICENSE](LICENSE)

---

## Contributing

1. Fork the repository
2. Create a feature branch: `git checkout -b feat/my-feature`
3. Run tests: `make test`
4. Open a pull request

Please read [CONTRIBUTING.md](CONTRIBUTING.md) for code style and commit message conventions.
