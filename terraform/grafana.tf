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

# #############################################################################
# Workspace IAM role. With CURRENT_ACCOUNT, the AMG CreateWorkspace API
# requires an explicit workspaceRoleArn (the "SERVICE_MANAGED auto-creates a
# role for you" path is no longer accepted by the API and now fails with
# "ValidationException: When the accountAccessType is CURRENT_ACCOUNT a
# Workspace Role ARN should be provided."), so we provision the role
# ourselves under CUSTOMER_MANAGED and attach AWS's managed
# AmazonGrafanaCloudWatchAccess policy. That policy covers both CloudWatch
# metrics (ListMetrics / GetMetricData) AND Logs Insights (StartQuery /
# GetQueryResults / DescribeLogGroups / ...), so authType="default" on the
# CloudWatch data source resolves to this role for every read — matching
# what SERVICE_MANAGED + data_sources=["CLOUDWATCH"] used to do.
# #############################################################################
data "aws_iam_policy_document" "grafana_workspace_trust" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["grafana.amazonaws.com"]
    }

    # Lock the assume to this account.
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }

    # Lock the assume to any AMG workspace in this account/region.
    # Wildcard (rather than the workspace ARN) avoids the trust-policy /
    # workspace circular dependency; aws:SourceAccount already binds it
    # to our account, so this is still confused-deputy safe.
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
  description        = "Role the AMG workspace assumes to query its CloudWatch data source (metrics + Logs Insights)."
}

resource "aws_iam_role_policy_attachment" "grafana_workspace_cloudwatch" {
  role       = aws_iam_role.grafana_workspace.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonGrafanaCloudWatchAccess"
}

resource "aws_grafana_workspace" "this" {
  name        = replace(var.project_name, "-", "_")
  description = "Workspace where the CloudWatch Agent publishes generated dashboards."

  # CUSTOMER_MANAGED + an explicit role we own (above). CURRENT_ACCOUNT
  # keeps data access to this account. data_sources is intentionally
  # omitted: that argument is a SERVICE_MANAGED hint for the role AWS
  # would create, and is meaningless when we provide the role ourselves.
  account_access_type      = "CURRENT_ACCOUNT"
  permission_type          = "CUSTOMER_MANAGED"
  role_arn                 = aws_iam_role.grafana_workspace.arn
  authentication_providers = ["AWS_SSO"]

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
# Human access via IAM Identity Center.
#
# AMG does not grant SSO logins access by default — an unassociated user
# lands on "Login failed [sso.auth.access-denied]". We therefore wire up
# two complementary role associations, both opt-in:
#
#   1. grafana_admin_group_ids: explicit ADMIN access for named groups.
#      Defaults to []; if set, those Identity Center groups become
#      workspace ADMINs.
#
#   2. grafana_grant_all_users_role: auto-discovers every user in the
#      account's Identity Center store and grants them this role
#      (default VIEWER). Set to "" to disable. The data sources below
#      are only consulted when the variable is non-empty.
# #############################################################################
resource "aws_grafana_role_association" "admin" {
  count = length(var.grafana_admin_group_ids) > 0 ? 1 : 0

  role         = "ADMIN"
  group_ids    = var.grafana_admin_group_ids
  workspace_id = aws_grafana_workspace.this.id
}

# Identity Center instance lookup. Must run against the region where the
# Identity Center instance was created (see var.identity_center_region) —
# from any other region this data source returns an empty list and the
# downstream resources error with "Invalid index ... empty list".
data "aws_ssoadmin_instances" "this" {
  count    = var.grafana_grant_all_users_role != "" ? 1 : 0
  provider = aws.identity_center
}

locals {
  # First identity_store_id returned by the instance lookup, or null when
  # Identity Center is not present in identity_center_region. The null
  # path lets the auto-grant degrade gracefully (no role association) so
  # the rest of the stack still applies; we surface a clear hint via the
  # validation block below instead of an opaque "empty list" error.
  _sso_identity_store_id = (
    var.grafana_grant_all_users_role != "" && length(data.aws_ssoadmin_instances.this) > 0
    ? try(tolist(data.aws_ssoadmin_instances.this[0].identity_store_ids)[0], null)
    : null
  )
}

# Sanity check: if the user asked for the auto-grant but no Identity
# Center instance was found in identity_center_region, fail fast with a
# pointer to the variable instead of letting the apply continue silently.
check "identity_center_present_when_auto_grant_enabled" {
  assert {
    condition = (
      var.grafana_grant_all_users_role == "" || local._sso_identity_store_id != null
    )
    error_message = "grafana_grant_all_users_role is set but no IAM Identity Center instance was found in '${coalesce(var.identity_center_region, var.region)}'. Set identity_center_region in terraform.tfvars to the region where Identity Center was enabled, or set grafana_grant_all_users_role = \"\" to disable the auto-grant."
  }
}

# All users in that identity store. Re-read on every apply, so newly
# onboarded Identity Center users get access on the next apply.
data "aws_identitystore_users" "all" {
  count             = local._sso_identity_store_id != null ? 1 : 0
  provider          = aws.identity_center
  identity_store_id = local._sso_identity_store_id
}

# Grant every Identity Center user the configured role. user_ids is a
# flat list; AMG handles association creation/removal in-place when the
# set changes between applies.
resource "aws_grafana_role_association" "all_users" {
  count = local._sso_identity_store_id != null ? 1 : 0

  role         = var.grafana_grant_all_users_role
  user_ids     = [for u in data.aws_identitystore_users.all[0].users : u.user_id]
  workspace_id = aws_grafana_workspace.this.id
}
