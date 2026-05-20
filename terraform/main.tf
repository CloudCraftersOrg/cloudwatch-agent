
# #############################################################################
# Top-level orchestration and shared data sources
#
# Purpose: Contains only shared data lookups and locals used by multiple
# resource files (iam.tf, ecr.tf, memory.tf, runtime.tf). All resource
# definitions live in topic-specific files.
# #############################################################################

# Identity of the caller running `terraform apply`. Used for resource ARNs and policy scoping.
data "aws_caller_identity" "current" {}

# Region where the provider is configured. Derived from the provider for future alias support.
data "aws_region" "current" {}

locals {
  # Shorthand for the account ID. Used in ARN construction throughout the stack.
  account_id = data.aws_caller_identity.current.account_id

  # Region as a string. Use the new `.region` attribute from AWS provider v6.
  region = data.aws_region.current.region
}
