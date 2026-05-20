
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

# NOTE: the container image tag is no longer a variable. It is derived from
# a sha1 of the image source files (Dockerfile, pyproject.toml, uv.lock,
# app/) by terraform/build.tf, and the image is built and pushed during
# `terraform apply`. Any code change automatically produces a new
# content-addressed tag, so drift between the deployed runtime and the
# pushed image is impossible by construction.

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

# Grant the same Grafana role to EVERY IAM Identity Center user in the
# account's identity store. Without an association, an SSO login lands on
# "Login failed [sso.auth.access-denied]"; AMG does not grant access
# implicitly. Defaults to VIEWER so everyone can see the dashboards the
# agent creates (and VIEWER is the cheapest AMG user tier). Set to
# "EDITOR" if you want everyone to be able to modify dashboards too,
# "ADMIN" for full control, or "" to disable the auto-grant and assign
# users by hand in the AMG console / via grafana_admin_group_ids.
variable "grafana_grant_all_users_role" {
  description = "Grafana role granted to every Identity Center user (\"VIEWER\"|\"EDITOR\"|\"ADMIN\"). \"\" disables the auto-grant."
  type        = string
  default     = "VIEWER"
  validation {
    condition     = contains(["", "VIEWER", "EDITOR", "ADMIN"], var.grafana_grant_all_users_role)
    error_message = "grafana_grant_all_users_role must be one of: \"\", \"VIEWER\", \"EDITOR\", \"ADMIN\"."
  }
}
