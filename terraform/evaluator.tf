# AgentCore Evaluator + OnlineEvaluationConfig: post-hoc evaluation of
# the agent's traces with an LLM-as-a-Judge managed by AgentCore.
# Complementary to the runtime judge_dashboard_quality tool, which is
# the synchronous gate that blocks grafana_update_dashboard turn by
# turn. This evaluator scores the FULL trace after the fact and
# publishes scores in the AgentCore console.
#
# The resources live under the awscc provider (not hashicorp/aws)
# because AgentCore is too new in CloudFormation.

# Execution role for the evaluator: reads traces (spans in aws/spans)
# and invokes the judge model on Bedrock.
data "aws_iam_policy_document" "evaluator_trust" {
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

    # Wildcard on SourceArn because both the evaluator and the
    # online-evaluation-config can be the source when assuming this
    # role. aws:SourceAccount already scopes to this account.
    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:aws:bedrock-agentcore:${local.region}:${local.account_id}:*"]
    }
  }
}

data "aws_iam_policy_document" "evaluator_permissions" {
  # Judge model invocation. Same Opus 4.6 the agent uses. If
  # llm_as_a_judge.model_config is switched to a different model in
  # the Anthropic family, adjust the ARNs here.
  statement {
    sid    = "EvaluatorInvokeJudgeModel"
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

  # Read spans from aws/spans (CloudWatch Transaction Search).
  statement {
    sid    = "EvaluatorReadRuntimeTraces"
    effect = "Allow"
    actions = [
      "logs:DescribeLogGroups",
      "logs:DescribeLogStreams",
      "logs:FilterLogEvents",
      "logs:GetLogEvents",
      "logs:GetLogRecord",
      "logs:StartQuery",
      "logs:StopQuery",
      "logs:GetQueryResults",
    ]
    resources = ["*"]
  }

  # Write the evaluator's output (a managed log group that AgentCore
  # resolves at runtime).
  statement {
    sid    = "EvaluatorWriteOutput"
    effect = "Allow"
    actions = [
      "logs:CreateLogGroup",
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role" "evaluator" {
  name               = "${var.project_name}-evaluator"
  assume_role_policy = data.aws_iam_policy_document.evaluator_trust.json
  description        = "Execution role for the AgentCore Evaluator: read runtime traces + invoke the LLM-as-a-judge model."
}

resource "aws_iam_role_policy" "evaluator" {
  name   = "${var.project_name}-evaluator-permissions"
  role   = aws_iam_role.evaluator.id
  policy = data.aws_iam_policy_document.evaluator_permissions.json
}

# Evaluator. level=TRACE scores the full invocation (not per
# individual tool_call nor per multi-turn session). Reuses Opus 4.6
# as the judge; if you switch to Sonnet/Haiku adjust the IAM ARNs.
resource "awscc_bedrockagentcore_evaluator" "quality" {
  evaluator_name = "${replace(var.project_name, "-", "_")}_quality"
  description    = "Post-hoc LLM-as-a-Judge over CloudWatch Agent traces; complements the runtime judge_dashboard_quality tool."

  level = "TRACE"

  evaluator_config = {
    llm_as_a_judge = {
      # The instructions MUST include at least one of {context} or
      # {assistant_turn} (placeholders required by AgentCore for
      # TRACE-level online eval). The service injects the actual data
      # at evaluation time. AgentCore automatically appends a
      # "reason + score" suffix, so do not include output formatting
      # here.
      instructions = <<-EOT
        You are evaluating a single CloudWatch → Grafana dashboard
        generation trace from an autonomous agent.

        Trace context (system prompt, user prompt, prior turns, tool
        calls and tool results across the invocation):
        {context}

        The agent's assistant turn(s) being evaluated:
        {assistant_turn}

        Rubric — score against the project conventions:

        - Discovery first: the agent must have inspected the
          relevant log groups / metrics before designing panels
          (filter_log_events or cw_mcp_describe_log_groups).
        - Judge gate: BEFORE every grafana_update_dashboard the agent
          MUST have called judge_dashboard_quality on the same JSON
          and iterated until verdict=approve. Skipping or ignoring a
          revise/reject verdict is the heaviest penalty.
        - Dashboard JSON quality: every panel and every target sets
          ``datasource`` of type "cloudwatch" with the workspace UID;
          log panels use ``queryMode: "Logs"`` with a non-empty
          ``logGroupNames`` and ``expression``; metric panels carry
          ``namespace`` + ``metricName`` + ``region``.
        - Stable naming: uids follow the cwagent-* convention
          (cwagent-overview, cwagent-svc-<service>,
          cwagent-incident-<slug>).
        - No hallucinations: every referenced log group, metric,
          namespace, or service must have been confirmed by a prior
          discovery tool call in this trace.

        Rating guide (use the numerical scale below):
        - 10 = full discovery → judge → approve → publish path, valid
          JSON, stable uids, no hallucinations.
        - 8 = good with minor issues (one missing dimension, slight
          layout overlap, one extra discovery call).
        - 5 = published despite skipping or ignoring the judge gate,
          OR JSON has multiple fixable issues the judge should have
          caught.
        - 1 = published dashboards with wrong datasource, broken
          queryMode, or references to entities that don't exist in the
          discovery results.
      EOT

      model_config = {
        bedrock_evaluator_model_config = {
          model_id = "us.anthropic.claude-opus-4-6-v1"
          # Opus 4.6 rejects requests with both temperature AND top_p.
          # We pick temperature=0 for deterministic scoring; do NOT
          # add top_p here.
          inference_config = {
            temperature = 0.0
            max_tokens  = 800
          }
        }
      }

      rating_scale = {
        numerical = [
          {
            value      = 1
            label      = "unusable"
            definition = "Published dashboards have critical defects (wrong datasource, broken queries, hallucinated entities)."
          },
          {
            value      = 5
            label      = "borderline"
            definition = "Published dashboards work but judge gate was skipped or JSON has multiple fixable issues."
          },
          {
            value      = 8
            label      = "good"
            definition = "Followed the rubric, judge gate used, minor issues only."
          },
          {
            value      = 10
            label      = "excellent"
            definition = "Discovery -> judge -> approve -> publish path executed cleanly with high-quality JSON."
          },
        ]
      }
    }
  }
}

# OnlineEvaluationConfig: wires the evaluator to live spans. The
# runtime's OTLP spans land in aws/spans (account-global log group
# created by CloudWatch Transaction Search; see README prerequisites).
# The runtime log group /aws/bedrock-agentcore/runtimes/<id>-DEFAULT
# only holds stdout/stderr, NOT spans, so it is not referenced here.
#
# service_names follows the <runtime_name>.<endpoint_name> convention
# of AgentCore.
#
# sampling_percentage=100 is fine for the demo; in production with
# high traffic, lower it to 10-20% to bound cost.
resource "awscc_bedrockagentcore_online_evaluation_config" "agent" {
  online_evaluation_config_name = "${replace(var.project_name, "-", "_")}_online_eval"
  description                   = "Routes cloudwatch_agent runtime spans to the quality evaluator."

  evaluation_execution_role_arn = aws_iam_role.evaluator.arn

  # Without this the resource is created with execution_status=DISABLED
  # (the API default when enableOnCreate=false) and processes nothing.
  # To pause without a destroy, change to "DISABLED" and apply.
  execution_status = "ENABLED"

  evaluators = [
    {
      evaluator_id = awscc_bedrockagentcore_evaluator.quality.evaluator_id
    },
  ]

  data_source_config = {
    cloudwatch_logs = {
      log_group_names = ["aws/spans"]
      service_names = [
        "${aws_bedrockagentcore_agent_runtime.this.agent_runtime_name}.DEFAULT",
      ]
    }
  }

  rule = {
    sampling_config = {
      sampling_percentage = 100
    }
  }
}
