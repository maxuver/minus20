# Local development environment

A three-node kind cluster with the full observability stack, so alerts can be
fired at `ingest-api` the same way they will be in production.

The quickstart in the root README does not need any of this: the chart runs on
a bare kind cluster with `k8s-events` as the only collector. This page is for
the full loop (Prometheus rule → Alertmanager → webhook) and the Loki and
Prometheus collectors. See also `deploy/README.md` for the agent and MCP.

## Prerequisites

| Tool | Check | Install (Windows) |
|------|-------|-------------------|
| Docker Desktop | `docker info` | must be **running** — WSL2 backend |
| kind | `kind --version` | `winget install Kubernetes.kind` |
| kubectl | `kubectl version --client` | `winget install Kubernetes.kubectl` |
| helm | `helm version` | `winget install Helm.Helm` |

## 1. Create the cluster

```bash
kind create cluster --config kind/cluster.yaml
```

Three nodes on purpose: a single-node cluster hides scheduling behaviour, and
several fault-injection scenarios (node pressure, eviction, anti-affinity) only
reproduce with real workers.

Verify:

```bash
kubectl get nodes -o wide
```

Expect one `control-plane` and two `worker` nodes in `Ready`.

## 2. Install the monitoring stack

```bash
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo add grafana https://grafana.github.io/helm-charts
helm repo update
```

```bash
helm install monitoring prometheus-community/kube-prometheus-stack \
  --namespace monitoring --create-namespace \
  --values kind/values-monitoring.yaml
```

Grafana lands on <http://localhost:3000>, Prometheus on <http://localhost:9090>,
Alertmanager on <http://localhost:9093> (port mappings come from the kind config).

## 3. Point Alertmanager at Minus20

Alertmanager reaches the ingest service through a webhook receiver — that single
config block is the entire integration surface:

```yaml
receivers:
  - name: minus20
    webhook_configs:
      - url: http://ingest-api.minus20.svc.cluster.local:8080/webhook/alertmanager
        send_resolved: true
```

## 4. Tear down

```bash
kind delete cluster --name minus20
```

The cluster is disposable — everything above is reproducible from this file.
