
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

# AWS region. Single-region project (us-east-1 by default). The variable exists
# so contributors can spin up isolated stacks in other regions if needed.
variable "region" {
  description = "AWS region to deploy into. The agent calls CloudWatch in this same region."
  type        = string
  default     = "us-east-1"
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

# Specific Identity Center USERS (by user_name) to grant Grafana ADMIN.
# Resolved against the same Identity Center as the all-users auto-grant
# (var.identity_center_region). Useful for the deploy operator(s): keeps
# their ADMIN role in IaC so it survives a destroy/recreate, instead of
# relying on a manual console assignment.
variable "grafana_admin_user_names" {
  description = "IAM Identity Center user names to grant Grafana ADMIN. Empty = no per-user admins."
  type        = list(string)
  default     = ["santiacmaestre"]
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

# IAM Identity Center is account-global but its instance lives in ONE
# specific region (the one it was enabled in). The data sources that
# enumerate Identity Center users and groups (aws_ssoadmin_instances,
# aws_identitystore_users) only return results from that home region —
# calling them from any other region returns an empty list and the
# auto-grant in grafana.tf errors with "Invalid index ... empty list".
# The deploy account's Identity Center was enabled in us-west-2; the
# rest of the stack runs in var.region (us-east-1).
variable "identity_center_region" {
  description = "Region where the account's IAM Identity Center instance was created."
  type        = string
  default     = "us-west-2"
}
