# Execution role assumed by Bedrock AgentCore Runtime when invoking
# the agent, and reused as the memory execution role by AgentCore
# Memory for strategy extraction and consolidation.

# Trust policy. Only the bedrock-agentcore service can assume this
# role, and only when acting on behalf of a resource in this
# account/region. SourceArn allows both runtime/* (agent
# invocations) and memory/* (AgentCore Memory strategy processing).
data "aws_iam_policy_document" "runtime_trust" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["bedrock-agentcore.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }

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

# Runtime permissions. Each statement is scoped to the actual surface
# the agent needs. If you change models, regions, or tools, revisit
# the corresponding ARNs.
data "aws_iam_policy_document" "runtime_permissions" {

  # Main model invocation. Claude Opus 4.6 is inference-profile-only
  # (the API rejects on-demand against the raw foundation model), so
  # we point at the us.* profile and at the foundation model with a
  # region wildcard (the profile fans out to us-east-1, us-east-2,
  # and us-west-2).
  statement {
    sid    = "InvokeBedrockModels"
    effect = "Allow"
    actions = [
      "bedrock:InvokeModel",
      "bedrock:InvokeModelWithResponseStream",
    ]
    resources = [
      "arn:aws:bedrock:${local.region}::foundation-model/anthropic.claude-opus-4-6-v1*",
      "arn:aws:bedrock:${local.region}:${local.account_id}:inference-profile/us.anthropic.claude-opus-4-6-v1",
      "arn:aws:bedrock:*::foundation-model/anthropic.claude-opus-4-6-v1*",
    ]
  }

  # Model AgentCore Memory uses internally for its strategies
  # (summarization, user_preference, semantic_facts). It is a
  # service-managed model, not the Opus above, so we allow the full
  # Anthropic family with InvokeModel.
  statement {
    sid     = "InvokeBedrockMemoryStrategyModels"
    effect  = "Allow"
    actions = ["bedrock:InvokeModel"]
    resources = [
      "arn:aws:bedrock:${local.region}::foundation-model/anthropic.*",
      "arn:aws:bedrock:*::foundation-model/anthropic.*",
    ]
  }

  # CloudWatch metrics and alarms in read-only mode. Used by the AWS
  # Labs MCP server to answer questions about metrics, anomalies, and
  # alarms. Scoped to the whole account because these APIs do not
  # support resource-level permissions.
  statement {
    sid    = "CloudWatchMetricsRead"
    effect = "Allow"
    actions = [
      "cloudwatch:ListMetrics",
      "cloudwatch:GetMetricData",
      "cloudwatch:GetMetricStatistics",
      "cloudwatch:DescribeAlarms",
      "cloudwatch:DescribeAlarmHistory",
      "cloudwatch:DescribeAlarmsForMetric",
      "cloudwatch:DescribeAnomalyDetectors",
    ]
    resources = ["*"]
  }

  # Grafana service-account tokens. The agent mints one when the
  # container starts, passes it to the Grafana MCP server, and
  # deletes any orphan tokens from previous containers (Listing).
  statement {
    sid    = "GrafanaServiceAccountTokens"
    effect = "Allow"
    actions = [
      "grafana:CreateWorkspaceServiceAccountToken",
      "grafana:DeleteWorkspaceServiceAccountToken",
      "grafana:ListWorkspaceServiceAccountTokens",
    ]
    resources = [aws_grafana_workspace.this.arn]
  }

  # CloudWatch Logs in read-only mode. Covers both the custom
  # filter_log_events tool (direct FilterLogEvents, no indexing lag)
  # and the Insights queries the CloudWatch MCP triggers.
  statement {
    sid    = "CloudWatchLogsRead"
    effect = "Allow"
    actions = [
      "logs:DescribeLogGroups",
      "logs:DescribeLogStreams",
      "logs:DescribeMetricFilters",
      "logs:DescribeQueries",
      "logs:DescribeQueryDefinitions",
      "logs:FilterLogEvents",
      "logs:GetLogEvents",
      "logs:GetLogGroupFields",
      "logs:GetLogRecord",
      "logs:GetQueryResults",
      "logs:StartQuery",
      "logs:StopQuery",
      "logs:ListLogAnomalyDetectors",
      "logs:ListAnomalies",
    ]
    resources = ["*"]
  }

  # Resource discovery in read-only mode.
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

  # AgentCore Memory access. The actions list matches exactly what
  # the Strands AgentCoreMemorySessionManager calls on the data
  # plane. DeleteEvent is the right one for update and migration
  # flows (events are immutable, so update = create + delete).
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

  # ECR image pull at cold start.
  statement {
    sid       = "EcrAuth"
    effect    = "Allow"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    sid    = "EcrPull"
    effect = "Allow"
    actions = [
      "ecr:BatchGetImage",
      "ecr:GetDownloadUrlForLayer",
      "ecr:BatchCheckLayerAvailability",
    ]
    resources = [aws_ecr_repository.this.arn]
  }

  # Writing the runtime's own log group (container stdout/stderr).
  # The exact runtime ID is assigned by the service, so the prefix
  # carries a wildcard.
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

  # Native observability export: traces to X-Ray and metrics via
  # PutMetricData. The runtime has AGENT_OBSERVABILITY_ENABLED=true
  # and aws-opentelemetry-distro installed in the container; without
  # these permissions the OTEL exporter fails silently.
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

resource "aws_iam_role" "runtime" {
  name               = "${var.project_name}-runtime"
  assume_role_policy = data.aws_iam_policy_document.runtime_trust.json
  description        = "Execution role for the CloudWatch Agent's AgentCore Runtime."
}

resource "aws_iam_role_policy" "runtime" {
  name   = "${var.project_name}-runtime-permissions"
  role   = aws_iam_role.runtime.id
  policy = data.aws_iam_policy_document.runtime_permissions.json
}
