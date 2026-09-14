# Real runs on AWS EKS

## Run 1 — 2026-09-13: infrastructure and the reflex path

**2026-09-13, eu-central-1.** The Terraform in `infra/terraform` was applied to
a real AWS account for the first time, the published Helm chart was installed
from GHCR onto the resulting cluster with no clone and no build, a real alert
went through the whole pipeline, and the cluster was destroyed 18 minutes after
it came up (31 minutes from the first `apply` line to the last `destroy` line). Everything below is copied from the session; identifiers are
masked.

### Timeline

| UTC | Event |
|---|---|
| 12:11:09 | `terraform apply` started (62 resources planned, 0 to change) |
| 12:24:17 | `Apply complete! Resources: 62 added` — 13 min 8 s |
| 12:25:01 | `helm upgrade --install so oci://ghcr.io/maxuver/charts/sentinelops` — `Pulled: …:0.4.0`, `STATUS: deployed` |
| ~12:29 | crash-looping pod injected, alert posted, `status=analyzed` in the worker log |
| 12:29:04 | `helm uninstall`, `terraform destroy` started |
| 12:42:02 | `Destroy complete! Resources: 62 destroyed.` — 12 min 58 s |
| 12:43 | `aws eks list-clusters`, `describe-nat-gateways`, `describe-vpcs`, `describe-instances`, `describe-volumes`, `describe-addresses`: all empty |

### What the cluster was

```
$ terraform output
cluster_name     = "sentinelops"
cluster_endpoint = "https://<id>.gr7.eu-central-1.eks.amazonaws.com"
region           = "eu-central-1"

$ kubectl get nodes -o wide
NAME                                        STATUS  VERSION               OS-IMAGE                        CONTAINER-RUNTIME
ip-10-0-x-x.eu-central-1.compute.internal   Ready   v1.36.3-eks-cb19647   Amazon Linux 2023.12.20260831   containerd://2.2.5
ip-10-0-x-x.eu-central-1.compute.internal   Ready   v1.36.3-eks-cb19647   Amazon Linux 2023.12.20260831   containerd://2.2.5

$ kubectl version | grep Server
Server Version: v1.36.4-eks-4cc7921
```

Two `t3.small` SPOT nodes in private subnets, one NAT gateway, EKS 1.36
(standard support), a $10 monthly budget with e-mail alerts at 50% actual and
100% forecast.

### What ran on it

```
$ helm upgrade --install so oci://ghcr.io/maxuver/charts/sentinelops -n sentinelops --create-namespace
Pulled: ghcr.io/maxuver/charts/sentinelops:0.4.0
STATUS: deployed

$ kubectl -n sentinelops get pods
so-analyzer-worker-5c7c5649c8-895nc   1/1   Running
so-ingest-api-6b8bb66556-lfnbr        1/1   Running
so-redis-59c86b58cc-mjtln             1/1   Running
```

### The incident

```
$ kubectl -n sentinelops run billing-api --image=busybox --restart=Always --command -- \
    sh -c "echo 'ERROR could not connect to postgres:5432'; sleep 2; exit 1"

$ kubectl -n sentinelops get events --field-selector involvedObject.name=billing-api
Warning   BackOff   pod/billing-api   Back-off restarting failed container billing-api in pod billing-api_…

$ curl -X POST localhost:18080/webhook/alertmanager -d @alert.json     # via port-forward to so-ingest-api
{"queued":1}

$ kubectl -n sentinelops logs deploy/so-analyzer-worker
incident alert=KubePodCrashLooping status=analyzed backend=stub latency=1ms cost=$0.000000 cause='stub backend: …'
```

No collector failures were logged: the worker read the pod's real events
through its read-only ServiceAccount. The backend was the stub on purpose:
there is no Ollama on this cluster and no paid API key was involved, so this
run proves the infrastructure and the pipeline, not the model. The model was
proven separately on kind (see `docs/BENCHMARKS.md`).

### RBAC on the real cluster

```
$ sa=system:serviceaccount:sentinelops:so-analyzer
$ kubectl auth can-i list events      --as=$sa -A   # yes
$ kubectl auth can-i get pods/log     --as=$sa -A   # yes
$ kubectl auth can-i list secrets     --as=$sa -A   # no
$ kubectl auth can-i delete pods      --as=$sa -A   # no
$ kubectl auth can-i create pods/exec --as=$sa -A   # no
```

### What went wrong, and what changed because of it

1. **The year-old pin `cluster_version = "1.30"` would have failed.** Checked
   before applying: 1.30 no longer exists on EKS, 1.31–1.33 are in extended
   support at six times the hourly price, 1.34–1.36 are standard. Pinned 1.36
   and set `ami_type = "AL2023_x86_64_STANDARD"` explicitly (AL2 AMIs ended
   with 1.32). Lesson recorded in `infra/terraform/README.md`.
2. **Postgres stayed `Pending`: `pod has unbound immediate PersistentVolumeClaims`.**
   EKS ships a `gp2` StorageClass but no volume driver; the EBS CSI add-on has
   to be installed with an IAM role. The demo fell back to `config.store=memory`
   (the pipeline does not need Postgres). Fixed in `infra/terraform/addons.tf`
   (EBS CSI + Pod Identity), `terraform validate` passes; **not yet re-applied**,
   which the next run will do.
3. **The worker restarted twice at start** while Redis was still coming up,
   then ran. Known startup race, unchanged; an init wait is in the backlog.
4. **`AdministratorAccess` on the Terraform user** is too wide for a portfolio
   that talks about least privilege. The list of what the policy actually
   needs is in `infra/terraform/README.md`; deriving it from CloudTrail after
   this run is the next step.

### Cost

Cost Explorer, next day, for 2026-09-13 (`aws ce get-cost-and-usage`,
`RECORD_TYPE=Usage`):

| Usage type | Qty | List price |
|---|---|---|
| NAT gateway hours | 1.000 h | $0.0520 |
| NAT gateway bytes | 0.740 GB | $0.0385 |
| EKS cluster hours | 0.316 h | $0.0316 |
| Spot t3.small | 0.329 h | $0.0034 |
| Public IPv4 (in use + idle) | | $0.0016 |
| EBS gp3 | 0.010 GB-month | $0.0009 |
| **Total at list price** | | **$0.1281** |
| Credits applied | | −$0.1281 |
| **Billed** | | **$0.00** |

The NAT gateway, not the cluster, was the largest line: an hour is the
billing minimum, and pulling three images through it cost more in bytes than
the control plane cost in time. For a demo that is fine; for anything
longer-lived, pull images through a VPC endpoint or keep nodes public.

## Run 2 — 2026-09-14: the whole product on a real cluster

The first run proved the infrastructure and the reflex path with the
in-memory store. This one installs everything the chart offers on a real
cluster, with the EBS CSI add-on that run 1 showed was missing: Postgres on
a real EBS volume, the incident history, the MCP server, the web UI, and the
new `k8s-logs` collector.

### Timeline

| UTC | Event |
|---|---|
| 20:41:17 | `terraform apply` (69 resources: run 1's 62, plus the EBS CSI driver, its Pod Identity role and the pod-identity agent) |
| 20:53:13 | `Apply complete` — 11 min 56 s |
| 20:54 | `helm upgrade --install so oci://ghcr.io/maxuver/charts/sentinelops` — `Pulled: …:0.7.0`, with `config.store=postgres`, `webUi.enabled=true`, `mcp.enabled=true` |
| 20:55–21:00 | Postgres PVC `Pending` again, for a different reason (below); fixed live; volume bound, Postgres `Running` |
| 21:02 | crash-looping pod injected, alert posted: `status=analyzed`, **38.9 s from the alert firing to the hypothesis**, context of 1152 chars stored in Postgres on EBS |
| 21:03 | MCP over HTTP from the laptop: `node_status`, `pod_logs`, `recent_incidents` answered from the cloud cluster |
| 21:04 | `helm uninstall`, PVC deleted (its EBS volume with it), `terraform destroy` |
| 21:15 | destroy stuck on the node security group and a subnet: a leaked VPC CNI ENI (`aws-K8S-i-…`, status `available`, its node long gone) still referenced both. Deleted by hand; destroy proceeded |
| 21:26:54 | `Destroy complete! Resources: 69 destroyed.` Account verified empty: no cluster, NAT, VPC, instances, volumes, EIPs or ENIs |

### What the reflex captured, on the cluster, with no Loki

```
## Kubernetes events
Warning BackOff pod/billing-api x2: Back-off restarting failed container billing-api in pod billing-api_sentinelops(…)
Normal Scheduled pod/billing-api: Successfully assigned sentinelops/billing-api to ip-10-0-x-x…
## Logs
--- previous container (billing-api)
ERROR could not connect to postgres:5432
```

That log line is the reason the process died, read from the previous
container through the Kubernetes API. On run 1 the context had only the
events. The backend was still the stub (no model runs on this cluster; the
model is measured in `docs/BENCHMARKS.md`), so the point of the row is the
context and the clocks, not the hypothesis text.

```
$ kubectl -n sentinelops get pvc so-postgres
so-postgres   Bound   pvc-a3b1baa2-…   2Gi   gp2

$ MCP client → http://localhost:18765/mcp (port-forward to so-mcp on EKS)
tools: 8
[node_status] ip-10-0-x-x…: Ready=True allocatable cpu=1930m memory=1468156Ki pods=11 kubelet=v1.36.3-eks-cb19647 / …
[pod_logs] ERROR could not connect to postgres:5432 / --- previous container …
[recent_incidents] #070d7b06 2026-09-14 21:02 KubePodCrashLooping ns=sentinelops sev=warning status=analyzed: …
```

### What went wrong this time, and what changed

1. **The EBS CSI add-on worked, and the PVC still stayed `Pending`:**
   `no persistent volumes available for this claim and no storage class is
   set`. EKS creates a `gp2` StorageClass but does **not** mark it as the
   default, so a chart that leaves `storageClassName` empty gets nothing.
   Fixed live with one annotation (`is-default-class: "true"`; Kubernetes
   1.28+ assigns a default retroactively) and in the chart with
   `postgres.storageClass` (`--set postgres.storageClass=gp2` on EKS).
2. **The worker and the MCP server crash-looped (6 restarts each)** while
   Postgres was pending: `ConnectionRefusedError` at startup. They recovered
   once it was up, but with Kubernetes' backoff that takes minutes. Now every
   process waits for Postgres and Redis (up to ~90 s) before giving up.
3. **A PVC's EBS volume is not Terraform's.** `terraform destroy` would have
   left it behind, billing. The volume must be deleted with the PVC before
   the cluster goes; the write-up's order (uninstall → delete PVC → destroy)
   is now the documented order.
4. **Destroy hung for ten minutes** on the node security group and a private
   subnet: a network interface the VPC CNI had created for pods (`aws-K8S-…`)
   outlived its node in state `available` and kept both referenced. Terraform
   does not own it and cannot delete it. `aws ec2 delete-network-interface`
   on the leaked ENI let the destroy finish. This is a known EKS teardown
   behaviour; the teardown order is now: uninstall the release, delete PVCs,
   `terraform destroy`, and if it stalls on a security group, look for
   `available` ENIs in the VPC.
5. Two SPOT `t3.small` nodes carry the whole stack with room to spare
   (allocatable 1930m CPU / 1.4 GiB each; the six pods request 150m / 384Mi).

### Cost

Same shape as run 1, about 25 minutes of cluster time plus one 2 GiB gp2
volume for ten minutes: at list price around $0.12, covered by credits. The
Cost Explorer figure will follow once billing settles.
