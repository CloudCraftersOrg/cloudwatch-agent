"""LLM-as-judge tool — evaluate a Grafana dashboard JSON before publish.

Bedrock has no managed "judge at invocation time" feature (Bedrock
Evaluation Jobs are batch/offline), so we wire the classic LLM-as-judge
pattern as a tool the agent must call before ``grafana_update_dashboard``.

Flow:

1. Agent assembles a candidate dashboard JSON.
2. Agent calls ``judge_dashboard_quality(dashboard=...)``.
3. The judge — a separate ``bedrock-runtime.converse`` call with a
   strict rubric system prompt and ``temperature=0`` — returns a
   structured ``{score, verdict, critique}`` JSON.
4. The agent iterates (refine + re-judge) until ``verdict="approve"``,
   then calls ``grafana_update_dashboard``. The rule is enforced via
   ``app/prompts.py``.

The judge runs in the same Bedrock region and IAM context as the main
model, so no IAM changes are needed (``InvokeBedrockModels`` already
allows the Opus inference profile, and ``InvokeBedrockMemoryStrategyModels``
allows any Anthropic foundation model if ``JUDGE_MODEL_ID`` is set to a
cheaper Sonnet/Haiku via env).
"""

from __future__ import annotations

import json
import os
from typing import Any

import boto3
from strands import tool

from app.config import MODEL_ID, REGION

# Default the judge to the same model as the main agent. Override via
# JUDGE_MODEL_ID env (e.g. a cheaper Sonnet/Haiku profile) when token
# cost matters; the IAM policy already allows any anthropic.* model.
JUDGE_MODEL_ID: str = os.environ.get("JUDGE_MODEL_ID", MODEL_ID)

# Module-level Bedrock Runtime client; thread-safe for the read APIs we
# use (converse is a single round-trip non-streaming call).
_bedrock = boto3.client("bedrock-runtime", region_name=REGION)

_JUDGE_SYSTEM_PROMPT = """\
You are a strict reviewer of Grafana dashboard JSON intended for an
Amazon Managed Grafana workspace whose primary data source is
CloudWatch. Score the candidate dashboard against the rubric below and
return ONLY a single JSON object with these keys:

  score    : integer 1-10 (10 = ship it).
  verdict  : "approve" if score >= 8, "revise" if 5-7, "reject" if < 5.
  critique : array of 0-5 SHORT strings, each one a concrete issue.

Rubric:
1. Schema correctness — top-level ``uid``, ``title``, ``panels``,
   ``schemaVersion``, ``time`` present and correctly typed.
2. Datasource references — every panel AND every target inside each
   panel has ``datasource`` of type "cloudwatch" with the workspace
   data source UID.
3. Query mode — for log panels every target sets ``queryMode: "Logs"``,
   ``logGroupNames`` (non-empty list), ``region``, and a non-empty
   ``expression`` (valid Logs Insights syntax). For metric panels,
   ``namespace`` + ``metricName`` + ``region`` + ``statistics``.
4. Stable naming — uid follows the project convention:
   ``cwagent-overview``, ``cwagent-svc-<service>``,
   ``cwagent-incident-<slug>``.
5. Layout sanity — gridPos blocks don't overlap and fit on a 24-column
   grid.
6. Usefulness — panels answer a plausible operator question; no empty
   filler panels; queries reference fields that the agent has confirmed
   exist in the log group.

Output rules:
- Return ONLY the JSON object. No prose, no code fences.
- If the input is not valid JSON or is missing a title, score = 0,
  verdict = "reject", and put the parse error in critique.
""".strip()


@tool
def judge_dashboard_quality(dashboard: dict[str, Any] | str) -> dict[str, Any]:
    """Run an LLM-as-judge over a proposed Grafana dashboard model.

    Call this BEFORE ``grafana_update_dashboard`` to get an automated
    quality review. Iterate (refine + re-judge) until verdict is
    ``approve``, then publish.

    Args:
        dashboard: The Grafana dashboard JSON model. Either a dict
            (preferred) or a JSON string.

    Returns:
        Dict with:
        - ``score`` (int, 1-10).
        - ``verdict`` (str: ``approve`` | ``revise`` | ``reject``).
        - ``critique`` (list[str]): concrete issues to fix. Empty when
          verdict is ``approve``.
        - ``model_id`` (str): which Bedrock model judged.
    """
    # Normalize input to a dict so the judge sees structured JSON.
    if isinstance(dashboard, str):
        try:
            model = json.loads(dashboard)
        except json.JSONDecodeError as exc:
            return {
                "score": 0,
                "verdict": "reject",
                "critique": [f"dashboard is not valid JSON: {exc}"],
                "model_id": JUDGE_MODEL_ID,
            }
    else:
        model = dashboard

    if not isinstance(model, dict):
        return {
            "score": 0,
            "verdict": "reject",
            "critique": [
                f"dashboard must be a JSON object, got {type(model).__name__}."
            ],
            "model_id": JUDGE_MODEL_ID,
        }

    user_payload = (
        "Evaluate this Grafana dashboard JSON. Return ONLY the JSON "
        "verdict object per the rubric. No prose.\n\n"
        "```json\n" + json.dumps(model, indent=2) + "\n```"
    )

    # Bedrock Converse: non-streaming, deterministic (temperature=0).
    response = _bedrock.converse(
        modelId=JUDGE_MODEL_ID,
        system=[{"text": _JUDGE_SYSTEM_PROMPT}],
        messages=[{"role": "user", "content": [{"text": user_payload}]}],
        inferenceConfig={"maxTokens": 600, "temperature": 0.0},
    )
    raw = response["output"]["message"]["content"][0]["text"].strip()

    # Strip code fences if the model wrapped its JSON answer anyway.
    if raw.startswith("```"):
        # Drop leading fence (with optional language tag).
        _, _, after = raw.partition("\n")
        raw = after.rsplit("```", 1)[0].strip()

    try:
        verdict = json.loads(raw)
    except json.JSONDecodeError:
        return {
            "score": 0,
            "verdict": "reject",
            "critique": [
                "judge returned non-JSON output; either retry the call "
                "or refine the dashboard. Raw output (truncated): "
                + raw[:200]
            ],
            "model_id": JUDGE_MODEL_ID,
        }

    return {
        "score": int(verdict.get("score", 0)),
        "verdict": str(verdict.get("verdict", "revise")),
        "critique": list(verdict.get("critique", [])),
        "model_id": JUDGE_MODEL_ID,
    }
