terraform {
  required_version = ">= 1.10" # S3 native state locking (use_lockfile)

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.40"
    }
  }

  # Remote state in S3 with native locking; no DynamoDB table. The bucket is
  # account-specific, so the values come from backend.hcl (gitignored):
  #   terraform init -backend-config=backend.hcl
  # backend.hcl.example shows the shape; bootstrap/ creates the bucket.
  backend "s3" {}
}

provider "aws" {
  region  = var.region
  profile = var.aws_profile # never keys in code; null = default credential chain
}
