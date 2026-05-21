# Amazon Managed Grafana: workspace where the agent publishes
# dashboards, service accounts (one admin for Terraform, one EDITOR
# for the agent at runtime), the CloudWatch data source, and human
# role assignments via IAM Identity Center.

# Role the workspace assumes to read the CloudWatch data source. With
# CURRENT_ACCOUNT the API requires an explicit workspaceRoleArn; the
# auto-created role from SERVICE_MANAGED is no longer an option. We
# attach the AmazonGrafanaCloudWatchAccess managed policy, which
# covers what authType="default" needs on the data source (metrics +
# Logs Insights).
data "aws_iam_policy_document" "grafana_workspace_trust" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["grafana.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }

    # Workspace wildcard to avoid the trust-policy/workspace cycle.
    # aws:SourceAccount already restricts to this account.
    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:aws:grafana:${local.region}:${local.account_id}:/workspaces/*"]
    }
  }
}

resource "aws_iam_role" "grafana_workspace" {
  name               = "${var.project_name}-grafana-workspace"
  assume_role_policy = data.aws_iam_policy_document.grafana_workspace_trust.json
  description        = "Role the AMG workspace assumes to read its CloudWatch data source."
}

resource "aws_iam_role_policy_attachment" "grafana_workspace_cloudwatch" {
  role       = aws_iam_role.grafana_workspace.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonGrafanaCloudWatchAccess"
}

resource "aws_grafana_workspace" "this" {
  name        = replace(var.project_name, "-", "_")
  description = "Workspace where the CloudWatch Agent publishes dashboards."

  account_access_type      = "CURRENT_ACCOUNT"
  permission_type          = "CUSTOMER_MANAGED"
  role_arn                 = aws_iam_role.grafana_workspace.arn
  authentication_providers = ["AWS_SSO"]

  grafana_version = var.grafana_version
}

# Service account Terraform uses during apply to create the data
# source via the grafana provider. ADMIN because data source creation
# requires it. The token lives 30 days (AMG maximum). If more than
# 30 days pass between applies, run
# `terraform apply -replace=aws_grafana_workspace_service_account_token.terraform`
# first to refresh the token.
resource "aws_grafana_workspace_service_account" "terraform" {
  name         = "terraform-provisioner"
  grafana_role = "ADMIN"
  workspace_id = aws_grafana_workspace.this.id
}

resource "aws_grafana_workspace_service_account_token" "terraform" {
  name               = "terraform-provisioner-token"
  service_account_id = aws_grafana_workspace_service_account.terraform.service_account_id
  workspace_id       = aws_grafana_workspace.this.id
  seconds_to_live    = 2592000
}

# Agent service account. EDITOR is enough to create and update
# dashboards. The agent mints its own tokens against this SA when the
# container starts (see app/mcp_clients.py).
resource "aws_grafana_workspace_service_account" "agent" {
  name         = "cloudwatch-agent"
  grafana_role = "EDITOR"
  workspace_id = aws_grafana_workspace.this.id
}

# CloudWatch data source. authType "default" uses the workspace role
# via the AWS SDK credentials chain, with no static keys.
resource "grafana_data_source" "cloudwatch" {
  type       = "cloudwatch"
  name       = "CloudWatch"
  is_default = true

  json_data_encoded = jsonencode({
    authType      = "default"
    defaultRegion = var.region
  })
}

# Human access via IAM Identity Center. AMG does not grant access on
# SSO login without an explicit role association ("access-denied"
# otherwise). There are two opt-in mechanisms:
#
#   1. grafana_admin_group_ids: list of group IDs that receive ADMIN.
#   2. grafana_grant_all_users_role: discovers EVERY user in the
#      identity store and grants them the given role (default
#      VIEWER). Set to "" to turn it off. The per-user admins in
#      grafana_admin_user_names are excluded so they do not collide.
resource "aws_grafana_role_association" "admin" {
  count = length(var.grafana_admin_group_ids) > 0 ? 1 : 0

  role         = "ADMIN"
  group_ids    = var.grafana_admin_group_ids
  workspace_id = aws_grafana_workspace.this.id
}

# Identity Center instance. aws_ssoadmin_instances and
# aws_identitystore_users only see the instance from its home region,
# so we go through the aws.identity_center provider alias (configured
# in providers.tf with var.identity_center_region).
data "aws_ssoadmin_instances" "this" {
  count    = var.grafana_grant_all_users_role != "" ? 1 : 0
  provider = aws.identity_center
}

locals {
  _sso_identity_store_id = (
    var.grafana_grant_all_users_role != "" && length(data.aws_ssoadmin_instances.this) > 0
    ? try(tolist(data.aws_ssoadmin_instances.this[0].identity_store_ids)[0], null)
    : null
  )
}

# Sanity check with a clear error message when auto-grant is enabled
# but no Identity Center exists in the configured region.
check "identity_center_present_when_auto_grant_enabled" {
  assert {
    condition = (
      var.grafana_grant_all_users_role == "" || local._sso_identity_store_id != null
    )
    error_message = "grafana_grant_all_users_role is enabled but no IAM Identity Center exists in '${coalesce(var.identity_center_region, var.region)}'. Set identity_center_region in terraform.tfvars to the correct region, or set grafana_grant_all_users_role = \"\" to disable auto-grant."
  }
}

# Identity store users, re-read on every apply so newly onboarded
# users get access on the next apply.
data "aws_identitystore_users" "all" {
  count             = local._sso_identity_store_id != null ? 1 : 0
  provider          = aws.identity_center
  identity_store_id = local._sso_identity_store_id
}

locals {
  _admin_user_ids = (
    local._sso_identity_store_id != null
    ? [for u in data.aws_identitystore_user.admin_users : u.user_id]
    : []
  )

  # IDs of all non-admin users (AMG promotes to the highest role when
  # a user appears in multiple associations; this prevents admins
  # from appearing in the VIEWER list, which would leave that
  # association with 0 users and trigger an "empty result" provider
  # bug).
  _auto_grant_user_ids = (
    local._sso_identity_store_id != null
    ? tolist(setsubtract(
      toset([for u in data.aws_identitystore_users.all[0].users : u.user_id]),
      toset(local._admin_user_ids),
    ))
    : []
  )
}

resource "aws_grafana_role_association" "all_users" {
  count = (
    var.grafana_grant_all_users_role != "" &&
    length(local._auto_grant_user_ids) > 0
    ? 1 : 0
  )

  role         = var.grafana_grant_all_users_role
  user_ids     = local._auto_grant_user_ids
  workspace_id = aws_grafana_workspace.this.id
}

# Resolves each user_name in the list to its Identity Center user_id.
data "aws_identitystore_user" "admin_users" {
  for_each = (
    local._sso_identity_store_id != null
    ? toset(var.grafana_admin_user_names)
    : toset([])
  )
  provider          = aws.identity_center
  identity_store_id = local._sso_identity_store_id

  alternate_identifier {
    unique_attribute {
      attribute_path  = "UserName"
      attribute_value = each.value
    }
  }
}

resource "aws_grafana_role_association" "admin_users" {
  count = (
    length(var.grafana_admin_user_names) > 0 && local._sso_identity_store_id != null
    ? 1 : 0
  )

  role         = "ADMIN"
  user_ids     = [for u in data.aws_identitystore_user.admin_users : u.user_id]
  workspace_id = aws_grafana_workspace.this.id
}
