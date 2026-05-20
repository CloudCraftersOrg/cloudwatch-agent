# #############################################################################
# Amazon Managed Grafana workspace and Grafana wiring
#
# Purpose: Stands up the AMG workspace the agent writes dashboards into,
# plus the two service accounts involved:
#   1. A Terraform "provisioner" service account (ADMIN) whose short-lived
#      token lets the grafana provider create the CloudWatch data source at
#      apply time.
#   2. An "agent" service account (EDITOR) the runtime mints per-session
#      tokens against to create/update dashboards via the Grafana HTTP API.
# Human login uses AWS IAM Identity Center (SSO); the agent never uses SSO.
# #############################################################################

resource "aws_grafana_workspace" "this" {
  name        = replace(var.project_name, "-", "_")
  description = "Workspace where the CloudWatch Agent publishes generated dashboards."

  # SERVICE_MANAGED lets AMG create and maintain the workspace IAM role with
  # CloudWatch read access for the data sources listed below; we don't have to
  # hand-craft that role. CURRENT_ACCOUNT keeps data access to this account.
  account_access_type      = "CURRENT_ACCOUNT"
  permission_type          = "SERVICE_MANAGED"
  authentication_providers = ["AWS_SSO"]

  # Grant the workspace role the managed CloudWatch access policy so the
  # native CloudWatch data source can query metrics and Logs.
  data_sources = ["CLOUDWATCH"]

  grafana_version = var.grafana_version
}

# #############################################################################
# Provisioner service account: used ONLY by the grafana provider during apply.
# #############################################################################
resource "aws_grafana_workspace_service_account" "terraform" {
  name         = "terraform-provisioner"
  grafana_role = "ADMIN" # Needs ADMIN to manage data sources.
  workspace_id = aws_grafana_workspace.this.id
}

resource "aws_grafana_workspace_service_account_token" "terraform" {
  name               = "terraform-provisioner-token"
  service_account_id = aws_grafana_workspace_service_account.terraform.service_account_id
  workspace_id       = aws_grafana_workspace.this.id

  # 30 days is the AMG maximum. The token is only needed during `terraform
  # apply`; if an apply happens >30 days after the last one, taint this
  # resource so a fresh token is issued before the grafana provider runs.
  seconds_to_live = 2592000
}

# #############################################################################
# Agent service account: the runtime mints its own short-lived tokens against
# this account (grafana:CreateWorkspaceServiceAccountToken). EDITOR is enough
# to create/update dashboards but cannot change workspace settings or users.
# #############################################################################
resource "aws_grafana_workspace_service_account" "agent" {
  name         = "cloudwatch-agent"
  grafana_role = "EDITOR"
  workspace_id = aws_grafana_workspace.this.id
}

# #############################################################################
# CloudWatch data source. authType "default" makes Grafana use the workspace's
# service-managed IAM role via the AWS SDK default chain — no static keys.
# #############################################################################
resource "grafana_data_source" "cloudwatch" {
  type       = "cloudwatch"
  name       = "CloudWatch"
  is_default = true

  json_data_encoded = jsonencode({
    authType      = "default"
    defaultRegion = var.region
  })
}

# #############################################################################
# Optional: grant IAM Identity Center groups ADMIN on the workspace. Created
# only when group IDs are supplied; otherwise human access is assigned by hand
# in the AMG console. Does not affect the agent (it uses a service account).
# #############################################################################
resource "aws_grafana_role_association" "admin" {
  count = length(var.grafana_admin_group_ids) > 0 ? 1 : 0

  role         = "ADMIN"
  group_ids    = var.grafana_admin_group_ids
  workspace_id = aws_grafana_workspace.this.id
}
