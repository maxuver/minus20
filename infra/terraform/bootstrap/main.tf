# One-time bootstrap: the S3 bucket that holds the Terraform state of the
# main configuration. Kept separate because a backend cannot create its own
# bucket. Terraform >= 1.10 locks state natively in S3 (`use_lockfile`), so no
# DynamoDB table is needed any more.
#
# S3 bucket names cannot be changed in place: once created, keep passing the
# same `bucket_prefix`, or Terraform plans to replace the bucket that holds
# the state.
#
#   cd infra/terraform/bootstrap
#   AWS_PROFILE=minus20 terraform init && terraform apply
#   terraform output -raw backend_hcl > ../backend.hcl      # gitignored
#   cd .. && terraform init -migrate-state -backend-config=backend.hcl

terraform {
  required_version = ">= 1.10"
  required_providers {
    aws    = { source = "hashicorp/aws", version = ">= 5.40" }
    random = { source = "hashicorp/random", version = ">= 3.6" }
  }
}

variable "region" {
  type    = string
  default = "eu-central-1"
}

variable "aws_profile" {
  description = "Named AWS CLI profile. Null means the default credential chain (CI, SSO, env)."
  type        = string
  default     = null
}

provider "aws" {
  region  = var.region
  profile = var.aws_profile
}

# Bucket names are global; a short random suffix avoids collisions without
# putting the account id in the name.
resource "random_id" "suffix" {
  byte_length = 3
}

variable "bucket_prefix" {
  description = "Prefix of the state bucket name; a random suffix is appended."
  type        = string
  default     = "minus20-tfstate"
}

resource "aws_s3_bucket" "state" {
  bucket = "${var.bucket_prefix}-${random_id.suffix.hex}"
  tags   = { Project = "minus20", Purpose = "terraform-state" }
}

resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id
  versioning_configuration {
    status = "Enabled" # every state change is recoverable
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "state" {
  bucket                  = aws_s3_bucket.state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

output "bucket" {
  value = aws_s3_bucket.state.bucket
}

output "backend_hcl" {
  description = "Paste into infra/terraform/backend.hcl (gitignored: account-specific)."
  value       = <<-EOT
    bucket       = "${aws_s3_bucket.state.bucket}"
    key          = "eks/terraform.tfstate"
    region       = "${var.region}"
    use_lockfile = true
    profile      = "${coalesce(var.aws_profile, "default")}"
  EOT
}
