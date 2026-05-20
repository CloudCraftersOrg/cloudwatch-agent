
# #############################################################################
# IAM role for AgentCore Runtime
#
# Purpose: Grants the AgentCore Runtime the minimum permissions required to invoke the agent.
# The role is split into two halves:
#   1. Trust policy: who can assume the role (AgentCore Runtime service principal, scoped to this account/region).
#   2. Inline permissions policy: what the agent can do at runtime (see annotated statements below).
# #############################################################################

# #############################################################################
# Trust policy: Only the AgentCore Runtime service may assume this role, and
# only on behalf of a bedrock-agentcore resource in this account/region. This
# role doubles as the AgentCore Memory execution role (see memory.tf), so the
# SourceArn allowlist must include BOTH the runtime/* context (normal agent
# invocation) and the memory/* context (strategy extraction/consolidation).
# Without the memory/* entry the memory service cannot assume this role and
# all long-term/summary strategies silently fail. aws:SourceAccount +
# aws:SourceArn still block confused-deputy attacks.
# #############################################################################
data "aws_iam_policy_document" "runtime_trust" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["bedrock-agentcore.amazonaws.com"]
    }

    # Lock the assume to this AWS account.
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }

    # Lock the assume to this account/region, for either the runtime
    # context (agent invocation) or the memory context (AgentCore Memory
    # strategy processing on this same execution role). Trailing wildcard
    # matches any runtime / memory resource ID.
    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values = [
        "arn:aws:bedrock-agentcore:${local.region}:${local.account_id}:runtime/*",
        "arn:aws:bedrock-agentcore:${local.region}:${local.account_id}:memory/*",
      ]
    }
  }
}

# #############################################################################
# Permissions policy: what the agent can do at runtime. Each statement is annotated with its purpose.
# #############################################################################
data "aws_iam_policy_document" "runtime_permissions" {
  # Bedrock model invocation. Scoped to the specific Opus 4.6 model and cross-region inference profile prefix.
  statement {
    sid    = "InvokeBedrockModels"
    effect = "Allow"
    actions = [
      "bedrock:InvokeModel",
      "bedrock:InvokeModelWithResponseStream",
    ]
    resources = [
      "arn:aws:bedrock:${local.region}::foundation-model/anthropic.claude-opus-4-6-v1:0",
      "arn:aws:bedrock:${local.region}:${local.account_id}:inference-profile/us.anthropic.claude-opus-4-6-v1",
      "arn:aws:bedrock:*::foundation-model/anthropic.claude-opus-4-6-v1:0",
    ]
  }

  # AgentCore Memory strategy extraction/consolidation. This role doubles
  # as the memory execution role (see memory.tf); AgentCore invokes an
  # AWS-managed Anthropic model on its behalf to extract summaries /
  # preferences / semantic facts. That model is service-chosen and is NOT
  # the Opus profile the agent itself uses, so the statement above does
  # not cover it — without this, every strategy silently AccessDenies.
  # Scoped to the Anthropic foundation-model family (read-only InvokeModel).
  statement {
    sid     = "InvokeBedrockMemoryStrategyModels"
    effect  = "Allow"
    actions = ["bedrock:InvokeModel"]
    resources = [
      "arn:aws:bedrock:${local.region}::foundation-model/anthropic.*",
      "arn:aws:bedrock:*::foundation-model/anthropic.*",
    ]
  }

  # CloudWatch metrics, read-only. Used to discover/inspect metrics when
  # designing dashboards. Dashboards now live in Grafana, so no
  # cloudwatch:*Dashboard* permissions are granted. Account-wide because
  # ListMetrics/GetMetricData do not support resource-level scoping.
  statement {
    sid    = "CloudWatchMetricsRead"
    effect = "Allow"
    actions = [
      "cloudwatch:ListMetrics",
      "cloudwatch:GetMetricData",
    ]
    resources = ["*"]
  }

  # Grafana token minting. The agent creates a short-lived service-account
  # token per session to call the Grafana HTTP API, then deletes it. Scoped
  # to this workspace only; the agent service account itself is created by
  # Terraform (see grafana.tf) and cannot be created by the agent.
  statement {
    sid    = "GrafanaServiceAccountTokens"
    effect = "Allow"
    actions = [
      "grafana:CreateWorkspaceServiceAccountToken",
      "grafana:DeleteWorkspaceServiceAccountToken",
    ]
    resources = [aws_grafana_workspace.this.arn]
  }

  # CloudWatch Logs Insights. Account-scoped at IAM layer; agent decides log groups at runtime.
  statement {
    sid    = "CloudWatchLogsInsights"
    effect = "Allow"
    actions = [
      "logs:DescribeLogGroups",
      "logs:StartQuery",
      "logs:GetQueryResults",
      "logs:StopQuery",
    ]
    resources = ["*"]
  }

  # Resource discovery. Read-only descriptors for EC2, RDS, and Lambda. No resource-level scoping for list/describe variants.
  statement {
    sid    = "ResourceDiscoveryReadOnly"
    effect = "Allow"
    actions = [
      "ec2:DescribeInstances",
      "rds:DescribeDBInstances",
      "lambda:ListFunctions",
    ]
    resources = ["*"]
  }

  # AgentCore Memory access. Scoped to the specific memory resource and
  # sub-resources. The action list is exactly what the Strands
  # AgentCoreMemorySessionManager calls on the data plane:
  #   - CreateEvent           : persist a conversation turn.
  #   - GetEvent / ListEvents : restore session history.
  #   - DeleteEvent           : update_message (events are immutable, so
  #                             update = create-new + delete-old) and the
  #                             legacy-session migration path. The earlier
  #                             policy granted DeleteMemoryRecord instead,
  #                             which the SDK never calls — so message
  #                             updates/redaction AccessDenied'd.
  #   - RetrieveMemoryRecords : long-term (semantic/preference) recall.
  statement {
    sid    = "AgentCoreMemoryAccess"
    effect = "Allow"
    actions = [
      "bedrock-agentcore:CreateEvent",
      "bedrock-agentcore:GetEvent",
      "bedrock-agentcore:ListEvents",
      "bedrock-agentcore:DeleteEvent",
      "bedrock-agentcore:RetrieveMemoryRecords",
    ]
    resources = [
      aws_bedrockagentcore_memory.this.arn,
      "${aws_bedrockagentcore_memory.this.arn}/*",
    ]
  }

  # ECR pull. AgentCore Runtime pulls the container image from ECR on every cold start. Read access scoped to this repository.
  statement {
    sid       = "EcrAuth"
    effect    = "Allow"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"] # GetAuthorizationToken does not support resource scoping.
  }

  statement {
    sid    = "EcrPull"
    effect = "Allow"
    actions = [
      "ecr:BatchGetImage",
      "ecr:GetDownloadUrlForLayer",
      # Part of the standard ECR pull set (AmazonEC2ContainerRegistryReadOnly);
      # AgentCore's cold-start image pull can fail without it.
      "ecr:BatchCheckLayerAvailability",
    ]
    resources = [aws_ecr_repository.this.arn]
  }

  # CloudWatch Logs writes for the runtime's own log group. Path is well-known but runtime ID is allocated by the service, so scope by prefix wildcard.
  statement {
    sid    = "RuntimeLogWriting"
    effect = "Allow"
    actions = [
      "logs:CreateLogGroup",
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = [
      "arn:aws:logs:${local.region}:${local.account_id}:log-group:/aws/bedrock-agentcore/runtimes/*",
      "arn:aws:logs:${local.region}:${local.account_id}:log-group:/aws/bedrock-agentcore/runtimes/*:log-stream:*",
    ]
  }

  # Built-in observability export. AGENT_OBSERVABILITY_ENABLED=true plus
  # the aws-opentelemetry-distro (see Dockerfile / runtime.tf) ship
  # traces to X-Ray and metrics to CloudWatch via EMF/PutMetricData.
  # None of these support resource-level scoping, so resources must be
  # "*"; without this statement OTEL export silently fails (non-fatal,
  # but the runtime's built-in observability would be a no-op).
  statement {
    sid    = "ObservabilityExport"
    effect = "Allow"
    actions = [
      "cloudwatch:PutMetricData",
      "xray:PutTraceSegments",
      "xray:PutTelemetryRecords",
      "xray:GetSamplingRules",
      "xray:GetSamplingTargets",
    ]
    resources = ["*"]
  }
}

# #############################################################################
# IAM role resource. Referenced by AgentCore Runtime in runtime.tf.
# #############################################################################
resource "aws_iam_role" "runtime" {
  name               = "${var.project_name}-runtime"
  assume_role_policy = data.aws_iam_policy_document.runtime_trust.json

  description = "Execution role assumed by Bedrock AgentCore Runtime when invoking the CloudWatch Agent."
}

# Inline policy keeps lifecycle management trivial: policy travels with the role, so `terraform destroy` removes both atomically.
resource "aws_iam_role_policy" "runtime" {
  name   = "${var.project_name}-runtime-permissions"
  role   = aws_iam_role.runtime.id
  policy = data.aws_iam_policy_document.runtime_permissions.json
}
