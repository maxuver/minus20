variable "region" {
  description = "AWS region for the cluster."
  type        = string
  default     = "eu-central-1"
}

variable "aws_profile" {
  description = "Named AWS CLI profile (aws configure --profile ...). Null uses the default credential chain, which is what CI and SSO want."
  type        = string
  default     = null
}

variable "cluster_name" {
  description = "EKS cluster name."
  type        = string
  default     = "minus20"
}

variable "cluster_version" {
  description = "Kubernetes version."
  type        = string
  default     = "1.36"
}

variable "node_instance_types" {
  description = "Instance types for the managed node group (small + SPOT for a cheap ephemeral cluster)."
  type        = list(string)
  default     = ["t3.small"]
}

variable "node_desired_size" {
  description = "Desired number of worker nodes."
  type        = number
  default     = 2
}

variable "tags" {
  description = "Tags applied to all resources."
  type        = map(string)
  default = {
    Project   = "minus20"
    ManagedBy = "terraform"
    Lifecycle = "ephemeral" # apply -> demo -> destroy
  }
}

variable "budget_alert_email" {
  description = "Email for the monthly spend alarm. Leave empty to skip creating a budget."
  type        = string
  default     = ""
}

variable "budget_limit_usd" {
  description = "Monthly budget in USD. A demo cluster costs well under a dollar per hour; this is a guard against leaving it running."
  type        = string
  default     = "10"
}
