# The identity SentinelOps runs as when it reads CloudWatch.
#
# The chart's ServiceAccount (`<release>-analyzer`) gets an IAM role through
# EKS Pod Identity: no keys in Secrets, no key rotation, and the policy is
# the whole list of what the software may do in the account. Read only:
# Logs Insights queries and metric reads. Nothing that writes, nothing
# outside CloudWatch.

variable "sentinelops_namespace" {
  description = "Namespace the chart is installed into."
  type        = string
  default     = "sentinelops"
}

variable "sentinelops_release" {
  description = "Helm release name; the ServiceAccount is <release>-analyzer."
  type        = string
  default     = "so"
}

data "aws_iam_policy_document" "sentinelops_trust" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole", "sts:TagSession"]
    principals {
      type        = "Service"
      identifiers = ["pods.eks.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "sentinelops_cloudwatch_read" {
  statement {
    sid    = "LogsInsightsRead"
    effect = "Allow"
    actions = [
      "logs:StartQuery",
      "logs:GetQueryResults",
      "logs:StopQuery",
      "logs:DescribeLogGroups",
    ]
    resources = ["*"] # Logs Insights queries are not resource-scoped by log group ARN in IAM
  }
  statement {
    sid       = "MetricsRead"
    effect    = "Allow"
    actions   = ["cloudwatch:GetMetricData", "cloudwatch:ListMetrics"]
    resources = ["*"]
  }
}

resource "aws_iam_role" "sentinelops" {
  name               = "${var.cluster_name}-sentinelops-reader"
  assume_role_policy = data.aws_iam_policy_document.sentinelops_trust.json
  tags               = var.tags
}

resource "aws_iam_role_policy" "sentinelops_cloudwatch_read" {
  name   = "cloudwatch-read"
  role   = aws_iam_role.sentinelops.id
  policy = data.aws_iam_policy_document.sentinelops_cloudwatch_read.json
}

resource "aws_eks_pod_identity_association" "sentinelops" {
  cluster_name    = module.eks.cluster_name
  namespace       = var.sentinelops_namespace
  service_account = "${var.sentinelops_release}-analyzer"
  role_arn        = aws_iam_role.sentinelops.arn
  tags            = var.tags
}

output "sentinelops_role_arn" {
  description = "Role the chart's ServiceAccount assumes through Pod Identity."
  value       = aws_iam_role.sentinelops.arn
}
