
# #############################################################################
# Input variables
# #############################################################################

# Logical project name. Used as a prefix for every named resource so multiple
# deployments can coexist in the same account if needed (e.g., dev/prod stacks).
# Also propagated as a default tag.
variable "project_name" {
  description = "Logical name used as a prefix for every named resource."
  type        = string
  default     = "cloudwatch-agent"
}

# AWS region. Single-region project (us-west-2 by default). The variable exists
# so contributors can spin up isolated stacks in other regions if needed.
variable "region" {
  description = "AWS region to deploy into. The agent calls CloudWatch in this same region."
  type        = string
  default     = "us-west-2"
}

# Container image tag deployed by the AgentCore Runtime. CI overrides this with
# the commit SHA on every push to main. Default is "latest" for the initial
# manual bootstrap apply.
#
# DRIFT WARNING: Terraform does not persist -var values. After CI has deployed
# with -var="image_tag=<sha>", a bare `terraform apply` (no -var) on the same
# state reverts the runtime to ":latest". Post-bootstrap, always pass
# -var="image_tag=<sha>" (the CI workflow does this automatically).
variable "image_tag" {
  description = "ECR image tag for the AgentCore Runtime container. Overridden by CI to the commit SHA. Always pass explicitly after bootstrap (see DRIFT WARNING in variables.tf)."
  type        = string
  default     = "latest"
}

# Grafana engine version for the Amazon Managed Grafana workspace. Pinned so
# that AMG upgrades are an explicit, reviewed change rather than implicit drift.
variable "grafana_version" {
  description = "Grafana version for the Amazon Managed Grafana workspace."
  type        = string
  default     = "10.4"
}

# Optional IAM Identity Center group IDs granted ADMIN on the workspace. Left
# empty by default because group IDs are account-specific; when empty, no role
# association is created and human access is assigned manually in the AMG
# console. This only affects human login, not the agent (the agent uses a
# service account, not SSO).
variable "grafana_admin_group_ids" {
  description = "IAM Identity Center group IDs to grant Grafana ADMIN. Empty = assign humans manually."
  type        = list(string)
  default     = []
}
