# ###########################################################################
# S3 backend for Terraform state.
#
# Copy this file to `backend.tf` and fill in the placeholders before
# running `terraform init`. Backend configuration cannot be sourced from
# variables, so each environment maintains its own copy of this file
# (typically populated by an internal bootstrap process, never by hand
# in production).
#
# The backend block must reference an S3 bucket that already exists and
# a DynamoDB table for state locking. Both are deliberately left out of
# this stack — bootstrapping state-management resources from the same
# state file you're trying to manage is a chicken-and-egg problem.
# ###########################################################################
terraform {
  backend "s3" {
    bucket  = "sacm-development-tfstate"
    key     = "cloudwatch-agent/terraform.tfstate"
    region  = "us-west-2"
    encrypt = true
  }
}
