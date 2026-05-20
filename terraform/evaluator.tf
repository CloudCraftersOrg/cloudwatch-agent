# #############################################################################
# AgentCore Evaluator + Online Evaluation Config (post-hoc trace quality)
#
# Complementary to the runtime ``judge_dashboard_quality`` tool in app/:
#
#   - judge_dashboard_quality (Python tool) — SYNCHRONOUS, in-loop gate
#     that blocks ``grafana_update_dashboard`` until the LLM-as-a-judge
#     approves the JSON. Per-invocation. Refine + re-judge.
#
#   - This file — POST-HOC, async grading of EVERY runtime trace by an
#     AgentCore-managed Evaluator. Scores accumulate in AgentCore
#     observability for trend monitoring, drift alerts, and "did we
#     skip the judge gate?" detection. No effect on the live request.
#
# Two AWS resources are needed:
#   1. AWS::BedrockAgentCore::Evaluator               (the rubric)
#   2. AWS::BedrockAgentCore::OnlineEvaluationConfig  (the wiring)
# Both live in awscc (not hashicorp/aws) — AgentCore is too new in
# CloudFormation. The awscc provider is configured in providers.tf.
# #############################################################################

# -----------------------------------------------------------------------------
# Execution role the evaluator assumes to (a) read runtime traces from
# CloudWatch Logs and (b) invoke the judge model on Bedrock.
# -----------------------------------------------------------------------------
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

    # Confused-deputy guard. Both the evaluator resource and the
    # online-evaluation-config resource may show up as SourceArn when
    # AgentCore assumes this role; the wildcard scoped to this
    # account/region covers both without creating a dependency cycle
    # (we can't reference the evaluator ARN from its own trust policy).
    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:aws:bedrock-agentcore:${local.region}:${local.account_id}:*"]
    }
  }
}

data "aws_iam_policy_document" "evaluator_permissions" {
  # Invoke the LLM-as-judge model. Scoped to the Anthropic Opus 4.6
  # inference profile (same as the agent's main model statement). If
  # you switch evaluator_config.llm_as_a_judge.model_config to a
  # different anthropic.* model, broaden as needed.
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

  # Read traces from the runtime's CloudWatch log group. The
  # OnlineEvaluationConfig below points the evaluator at this log group;
  # the evaluator scans it for spans and feeds them to the judge.
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

  # Emit the evaluator's own output (per-trace scores) — by default
  # AgentCore writes results to a managed CloudWatch log group
  # (resolved at runtime, visible via the online_evaluation_config
  # ``output_config.cloudwatch_config.log_group_name`` attribute).
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

# -----------------------------------------------------------------------------
# The evaluator itself — declarative rubric + model + numerical scale.
#
# level = TRACE: evaluates the FULL invocation trace (every tool call,
# every model turn, the final response). TOOL_CALL would score each
# tool call independently; SESSION would aggregate across multiple
# invocations of the same runtime_session_id. TRACE matches our
# "did the agent build sensible dashboards on THIS request" question.
#
# We deliberately reuse the same model_id as the main agent. Override
# to a cheaper Anthropic profile if cost matters (the IAM statement
# above is scoped to anthropic.claude-opus-4-6-v1* — broaden if needed).
# -----------------------------------------------------------------------------
resource "awscc_bedrockagentcore_evaluator" "quality" {
  evaluator_name = "${replace(var.project_name, "-", "_")}_quality"
  description    = "Post-hoc LLM-as-a-Judge over CloudWatch Agent traces; complements the runtime judge_dashboard_quality tool."

  level = "TRACE"

  evaluator_config = {
    llm_as_a_judge = {
      # Placeholders required by AgentCore at TRACE level for online
      # evaluation: at least one of {context} or {assistant_turn} must
      # appear (the {expected_response} ground-truth placeholder is
      # forbidden in online configs — see AWS docs). The service
      # injects the actual trace data at evaluation time. Output
      # formatting (reason + score) is auto-appended by AgentCore;
      # don't add it here.
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
          # Opus 4.6 rejects requests that set BOTH temperature and
          # top_p ("`temperature` and `top_p` cannot both be specified
          # for this model. Please use only one."). We pick
          # ``temperature = 0`` for deterministic, reproducible
          # scoring; do NOT add ``top_p`` here.
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

# -----------------------------------------------------------------------------
# Wire the evaluator to live runtime traces.
#
# AgentCore emits per-invocation traces to a CloudWatch log group named
#   /aws/bedrock-agentcore/runtimes/<agent_runtime_id>-DEFAULT
# (the runtime's IAM also grants logs:PutLogEvents on that prefix —
# see iam.tf:RuntimeLogWriting). This config tells AgentCore: scan that
# log group for spans tagged with the agent's service.name and feed
# 100% of them to the evaluator above.
#
# sampling_percentage = 100 is for demo. In a production stack with
# high invocation volume, drop this to 5-20% to bound cost.
# -----------------------------------------------------------------------------
# OnlineEvaluationConfig reads OTLP SPANS, not container stdout. Per
# the AgentCore Observability docs the spans land in the global
# ``aws/spans`` log group (created automatically by CloudWatch
# Transaction Search — see the AWS::Logs::TransactionSearchConfig
# enablement step in the README). The runtime stdout log group
# ``/aws/bedrock-agentcore/runtimes/<id>-DEFAULT`` is for the
# container's print() / loguru output and does NOT contain the
# spans the evaluator needs.
#
# service_names follows the format ``<runtime_name>.<endpoint_name>``
# per the AWS docs example
# (e.g. "strands_healthcare_single_agent.DEFAULT"). Our endpoint is
# the auto-created DEFAULT one (see the note in runtime.tf), so we
# build the string from the runtime name.
resource "awscc_bedrockagentcore_online_evaluation_config" "agent" {
  online_evaluation_config_name = "${replace(var.project_name, "-", "_")}_online_eval"
  description                   = "Routes cloudwatch_agent runtime spans to the quality evaluator."

  evaluation_execution_role_arn = aws_iam_role.evaluator.arn

  # Start in ENABLED state so AgentCore processes traces as they
  # arrive. Without this the resource is created with execution_status
  # = "DISABLED" (the AWS API default for ``enableOnCreate=false``) and
  # no traces are evaluated — you'd see the config in the console but
  # zero scores. Toggle with the AgentCore CLI's
  # ``agentcore pause online-eval`` / ``resume online-eval`` if you
  # need to pause without destroying the resource.
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
