# First real run on AWS EKS

**2026-09-13, eu-central-1.** The Terraform in `infra/terraform` was applied to
a real AWS account for the first time, the published Helm chart was installed
from GHCR onto the resulting cluster with no clone and no build, a real alert
went through the whole pipeline, and the cluster was destroyed 18 minutes after
it came up (31 minutes from the first `apply` line to the last `destroy` line). Everything below is copied from the session; identifiers are
masked.

## Timeline

| UTC | Event |
|---|---|
| 12:11:09 | `terraform apply` started (62 resources planned, 0 to change) |
| 12:24:17 | `Apply complete! Resources: 62 added` — 13 min 8 s |
| 12:25:01 | `helm upgrade --install so oci://ghcr.io/maxuver/charts/sentinelops` — `Pulled: …:0.4.0`, `STATUS: deployed` |
| ~12:29 | crash-looping pod injected, alert posted, `status=analyzed` in the worker log |
| 12:29:04 | `helm uninstall`, `terraform destroy` started |
| 12:42:02 | `Destroy complete! Resources: 62 destroyed.` — 12 min 58 s |
| 12:43 | `aws eks list-clusters`, `describe-nat-gateways`, `describe-vpcs`, `describe-instances`, `describe-volumes`, `describe-addresses`: all empty |

## What the cluster was

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

## What ran on it

```
$ helm upgrade --install so oci://ghcr.io/maxuver/charts/sentinelops -n sentinelops --create-namespace
Pulled: ghcr.io/maxuver/charts/sentinelops:0.4.0
STATUS: deployed

$ kubectl -n sentinelops get pods
so-analyzer-worker-5c7c5649c8-895nc   1/1   Running
so-ingest-api-6b8bb66556-lfnbr        1/1   Running
so-redis-59c86b58cc-mjtln             1/1   Running
```

## The incident

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

## RBAC on the real cluster

```
$ sa=system:serviceaccount:sentinelops:so-analyzer
$ kubectl auth can-i list events      --as=$sa -A   # yes
$ kubectl auth can-i get pods/log     --as=$sa -A   # yes
$ kubectl auth can-i list secrets     --as=$sa -A   # no
$ kubectl auth can-i delete pods      --as=$sa -A   # no
$ kubectl auth can-i create pods/exec --as=$sa -A   # no
```

## What went wrong, and what changed because of it

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

## Cost

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
