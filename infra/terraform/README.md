# Minus20 on AWS EKS (Terraform)

Production-shaped infrastructure as code for an **ephemeral** EKS cluster:
a VPC (three AZs, single NAT gateway for cost) plus an EKS cluster with a managed
node group, built on the community `terraform-aws-modules` (the same modules
behind EKS blueprints).

## Status (honest)

Applied to a real AWS account once, on 2026-09-13: 62 resources in 13 minutes,
the published chart installed from GHCR, a real incident analysed on the
cluster, everything destroyed 18 minutes later, about ten cents. The full
record, including what went wrong, is in [`docs/EKS-RUN.md`](../../docs/EKS-RUN.md).
The EBS CSI add-on (`addons.tf`) was applied on the second run, 2026-09-14,
and Postgres bound a real EBS volume; note that EKS's `gp2` StorageClass is
not marked default, so the chart needs `--set postgres.storageClass=gp2`. Applying is intentionally ephemeral: apply, deploy the
Helm chart, demo, then `destroy`, to keep the bill to cents. The same
[`deploy/minus20`](../../deploy/minus20) Helm chart runs unchanged on
this cluster and on local kind, so nothing about the application layer is
AWS-specific.

## Validate (no AWS account needed)

```bash
cd infra/terraform
terraform init
terraform validate
```

## Provision, demo, destroy (needs AWS credentials)

```bash
# once per account: the S3 bucket that holds the state (versioned, encrypted, private)
terraform -chdir=bootstrap init && terraform -chdir=bootstrap apply -var aws_profile=minus20
terraform -chdir=bootstrap output -raw backend_hcl > backend.hcl   # gitignored

terraform init -backend-config=backend.hcl
terraform plan  -var aws_profile=minus20 -var budget_alert_email=you@example.com
terraform apply -var aws_profile=minus20 -var budget_alert_email=you@example.com

aws eks update-kubeconfig --region eu-central-1 --name minus20
helm upgrade --install m20 ../../deploy/minus20 -n minus20 --create-namespace

# ... demo ...

terraform destroy   # important: tear it down when finished
```

## What it costs

The expensive mistake is not running the demo — it is forgetting to destroy it
afterwards. The EKS control plane and the NAT gateway bill hourly whether or not
anything is running.

| Line item | Rate | 3-hour demo | Left running a month |
|---|---|---|---|
| EKS control plane | $0.10/hour | $0.30 | **$73** |
| 2× t3.small (SPOT) | ~$0.007/hour each | $0.04 | ~$10 |
| NAT gateway | $0.045/hour + traffic | $0.14 | **$33** |
| EBS volumes | — | ~$0.01 | ~$3 |
| **Total** | | **≈ $0.50** | **≈ $120** |

Rates are list prices for a typical EU/US region and move over time; check the
[AWS pricing calculator](https://calculator.aws) for your own region before a
long-running deployment. The shape of the answer does not change: a demo is
cents, a forgotten cluster is real money.

### The spend alarm

Set `budget_alert_email` and Terraform creates a monthly AWS Budget that warns at
50% of actual spend and again when the forecast crosses the limit:

```bash
terraform apply -var budget_alert_email=you@example.com -var budget_limit_usd=10
```

Leave it empty and no budget is created, for teams that manage budgets centrally.

## Cost control

- `single_nat_gateway = true` (one NAT, not one per AZ)
- `capacity_type = "SPOT"` on small `t3.small` nodes
- Everything tagged `Lifecycle = ephemeral`

Destroy the cluster when the demo is over. This is not meant to run 24/7.

## State

State is remote: an S3 bucket created by `bootstrap/` (versioned, AES-256,
public access blocked), locked natively by Terraform >= 1.10 (`use_lockfile =
true`), so there is no DynamoDB table to run. The bucket name is
account-specific and lives in `backend.hcl`, which is gitignored;
`backend.hcl.example` shows the shape. State contains every resource
attribute in clear text, which is why it is never local and never committed.

## Credentials and IAM (what this run used, and what it should use)

The first real run used an IAM user (`sentinelops-terraform`, created before the rename) with
`AdministratorAccess` and a long-lived access key, configured as a named
profile (`aws configure --profile minus20`, passed as `-var
aws_profile=minus20`; the provider never sees a key). Stated plainly
because it is the wrong long-term shape:

- **Prefer short-lived credentials.** AWS CLI v2's `aws login` (browser sign-in
  with console credentials) or IAM Identity Center issue temporary keys; a CSV
  access key on a laptop is a standing liability. Delete or rotate the key
  after each demo.
- **Least privilege is a backlog item, not optional.** What this configuration
  actually needs, to scope a policy to: `eks:*` on the cluster and node group;
  `ec2:*` for VPC, subnets, route tables, NAT and security groups; `iam:` for
  the cluster and node roles, instance profile and OIDC provider (`CreateRole`,
  `AttachRolePolicy`, `PassRole`, `CreateOpenIDConnectProvider`, plus the
  read/delete counterparts); `kms:*` on the cluster secrets key the module
  creates; `logs:` for the control-plane log group; `budgets:` for the spend
  alarm; `sts:GetCallerIdentity`; `ssm:GetParameter` for the AMI lookup. S3 and
  DynamoDB only if state moves to a remote backend (it is local here). Nothing
  else. Building that policy from CloudTrail after one run is the honest way
  to derive it.
- **MFA on the root user** and never using root keys for Terraform.

Kubernetes version note: this configuration pins a version in **standard**
support (`cluster_version`, checked with `aws eks describe-cluster-versions`).
Versions in extended support cost six times more per hour ($0.60 vs $0.10) and
versions past extended support cannot be created at all, which is how a
year-old pin of 1.30 would have failed on first apply.
