# Cluster add-ons the Helm chart needs.
#
# The first real run (2026-09-13) exposed this: the chart's Postgres PVC stayed
# Pending because EKS ships a `gp2` StorageClass but no volume driver. Since
# Kubernetes 1.23 the in-tree EBS provisioner is gone and the EBS CSI driver
# must be installed as an add-on with an IAM role. Without it, any PVC on EKS
# waits forever. The demo fell back to the in-memory store; this fixes it.
#
# Pod Identity is used rather than IRSA: one agent add-on, one role that
# trusts pods.eks.amazonaws.com, no OIDC condition strings to get wrong.

data "aws_iam_policy_document" "ebs_csi_trust" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole", "sts:TagSession"]
    principals {
      type        = "Service"
      identifiers = ["pods.eks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "ebs_csi" {
  name               = "${var.cluster_name}-ebs-csi"
  assume_role_policy = data.aws_iam_policy_document.ebs_csi_trust.json
  tags               = var.tags
}

resource "aws_iam_role_policy_attachment" "ebs_csi" {
  role       = aws_iam_role.ebs_csi.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy"
}
