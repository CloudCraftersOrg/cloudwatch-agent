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
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import boto3
from strands import tool

from app.config import REGION

logger = logging.getLogger(__name__)

# Judge runs against Claude Haiku 4.5 by default. The judge does
# structural review of a JSON object + (server-side) data-plane
# validation by running Insights queries; it does not need Sonnet
# or Opus reasoning depth. Haiku is roughly 3× faster than Sonnet
# and 5-7× faster than Opus per inference, so with 5-15 judge
# calls in a typical full-rebuild turn we save 30-60 s.
#
# The exact inference-profile ID is taken from the model's Bedrock
# detail page — Haiku 4.5 still uses the old long form with the
# date suffix and ``:0``, unlike the newer Sonnet 4.6 (short form).
# Override via ``JUDGE_MODEL_ID`` if you want to A/B against Sonnet
# (``us.anthropic.claude-sonnet-4-6``) or Opus.
JUDGE_MODEL_ID: str = os.environ.get(
    "JUDGE_MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0"
)

# Module-level Bedrock Runtime client; thread-safe for the read APIs we
# use (converse is a single round-trip non-streaming call).
_bedrock = boto3.client("bedrock-runtime", region_name=REGION)

# Reused for the data-validation step. CloudWatch Logs Insights queries
# are issued per log panel during judging so we can reject dashboards
# whose queries return zero rows.
_logs = boto3.client("logs", region_name=REGION)

# Hard upper bound on how long the judge will wait for one Insights
# query to complete before treating the panel as failed. Insights
# usually finishes a small query in 2-5 s; 15 s leaves margin for cold
# Insights and avoids hanging the agent loop on a runaway query.
_INSIGHTS_QUERY_TIMEOUT_S = 15
_INSIGHTS_POLL_INTERVAL_S = 1.0
# Lower bound on the validation window. Even if the dashboard requests
# something tighter than this, we widen — the goal is to catch "panel
# will render empty against real data", not to perfectly mirror the
# dashboard's render time range.
_MIN_VALIDATION_WINDOW_S = 60 * 60  # 1 hour
# Max concurrent Insights queries during data-plane validation.
# AWS allows ~30 concurrent Insights queries per account; we stay
# well under that since other tooling on the account may also be
# issuing queries. 10 is enough to drain a 10-panel overview in a
# single batch with headroom.
_DATA_PLANE_PARALLELISM = 10

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


def _parse_relative_time(value: str) -> int:
    """Convert a Grafana-style relative time (``now-6h``) to seconds.

    Defaults to 1 hour if the value is unrecognized. Only handles the
    ``now-<int><unit>`` form, which is what the agent emits in
    practice; absolute timestamps (``2026-05-21T...``) and
    ``now/d``-style snap expressions are not parsed and fall back to
    the default so validation still runs against a reasonable window.
    """
    if not isinstance(value, str) or not value.startswith("now-"):
        return _MIN_VALIDATION_WINDOW_S
    tail = value[len("now-"):]
    try:
        if tail.endswith("s"):
            return max(int(tail[:-1]), _MIN_VALIDATION_WINDOW_S)
        if tail.endswith("m"):
            return max(int(tail[:-1]) * 60, _MIN_VALIDATION_WINDOW_S)
        if tail.endswith("h"):
            return max(int(tail[:-1]) * 3600, _MIN_VALIDATION_WINDOW_S)
        if tail.endswith("d"):
            return max(int(tail[:-1]) * 86400, _MIN_VALIDATION_WINDOW_S)
    except ValueError:
        pass
    return _MIN_VALIDATION_WINDOW_S


def _run_insights_query(
    log_groups: list[str], expression: str, lookback_s: int
) -> int:
    """Run a single Logs Insights query and return its row count.

    Raises ``TimeoutError`` if the query does not complete within
    ``_INSIGHTS_QUERY_TIMEOUT_S``, and ``RuntimeError`` if Insights
    reports Failed / Cancelled. Callers should catch both and turn
    them into critique entries.
    """
    end_s = int(time.time())
    start_s = end_s - lookback_s
    started = _logs.start_query(
        logGroupNames=log_groups,
        startTime=start_s,
        endTime=end_s,
        queryString=expression,
        limit=10,
    )
    query_id = started["queryId"]
    deadline = time.time() + _INSIGHTS_QUERY_TIMEOUT_S
    while time.time() < deadline:
        time.sleep(_INSIGHTS_POLL_INTERVAL_S)
        result = _logs.get_query_results(queryId=query_id)
        status = result.get("status")
        if status == "Complete":
            return len(result.get("results", []))
        if status in {"Failed", "Cancelled", "Timeout"}:
            raise RuntimeError(f"Insights query ended with status={status}")
    # Best-effort cancel so we don't leave queries running on the
    # account. StopQuery is idempotent on already-finished queries.
    try:
        _logs.stop_query(queryId=query_id)
    except Exception:  # noqa: BLE001
        pass
    raise TimeoutError(
        f"Insights query did not complete in {_INSIGHTS_QUERY_TIMEOUT_S}s"
    )


def _check_one_target(
    title: str,
    log_groups: list[str],
    expression: str,
    lookback_s: int,
) -> str | None:
    """Run one log target's query and return a critique string or None.

    Encapsulates the per-target work so the parallel validator can
    fan it out via a thread pool. Returns ``None`` on success (>=1
    row), or a one-line critique describing the failure mode.
    boto3 clients are thread-safe for read APIs, so the module-level
    ``_logs`` client is shared across workers.
    """
    if not log_groups or not expression:
        return f"panel '{title}': missing logGroupNames or expression."
    try:
        rows = _run_insights_query(log_groups, expression, lookback_s)
    except (TimeoutError, RuntimeError) as exc:
        return (
            f"panel '{title}': query failed ({exc}). Expression: "
            f"{expression[:120]}"
        )
    except Exception as exc:  # noqa: BLE001
        return (
            f"panel '{title}': query error "
            f"({type(exc).__name__}: {str(exc)[:120]})."
        )
    if rows == 0:
        return (
            f"panel '{title}': returns 0 rows over the last "
            f"{lookback_s // 60} min. Widen the time range, relax "
            f"the filter, or confirm the field/service name exists "
            f"in the data. Expression: {expression[:120]}"
        )
    return None


def _validate_log_panels_return_data(dashboard: dict[str, Any]) -> list[str]:
    """Execute every log-mode panel's query in parallel and report failures.

    Returns one critique string per panel that would render empty or
    whose query is broken. An empty list means every log target on
    every panel returned at least one row over the dashboard's
    declared time range (or a 1 h fallback). Metric panels and
    panels with no Logs target are skipped — this validation only
    covers the Insights data plane.

    The per-target queries run concurrently in a thread pool
    (``_DATA_PLANE_PARALLELISM`` workers) so a dense overview with
    10+ panels validates in roughly the time of one query instead of
    N × query-time. boto3's CloudWatch Logs client is documented as
    thread-safe for the StartQuery / GetQueryResults APIs we use.
    """
    lookback_s = _parse_relative_time(
        (dashboard.get("time") or {}).get("from", "now-1h")
    )

    # Flatten (panel, target) pairs so each parallel worker handles
    # one query, even when a panel has multiple targets.
    work: list[tuple[str, list[str], str]] = []
    for panel in dashboard.get("panels", []) or []:
        title = panel.get("title", f"panel_id={panel.get('id', '?')}")
        for target in panel.get("targets", []) or []:
            if target.get("queryMode") != "Logs":
                continue
            work.append(
                (
                    title,
                    target.get("logGroupNames") or [],
                    (target.get("expression") or "").strip(),
                )
            )

    if not work:
        return []

    issues: list[str] = []
    with ThreadPoolExecutor(max_workers=_DATA_PLANE_PARALLELISM) as pool:
        futures = [
            pool.submit(_check_one_target, title, lg, expr, lookback_s)
            for title, lg, expr in work
        ]
        for future in futures:
            result = future.result()
            if result is not None:
                issues.append(result)
    return issues


@tool
def judge_dashboard_quality(dashboard: dict[str, Any] | str) -> dict[str, Any]:
    """Judge a proposed Grafana dashboard model.

    Two-stage gate, in order:

    1. LLM rubric pass — structure, datasource UIDs, queryMode, stable
       naming, layout, usefulness. Cheap.
    2. Data-plane validation — for every log panel with
       ``queryMode == "Logs"``, the judge ACTUALLY RUNS the panel's
       Logs Insights query against CloudWatch over the dashboard's
       declared time range (clamped to a 1 h minimum). If any panel
       returns 0 rows or the query errors, the verdict is overridden
       to ``revise`` with the specific empty panels in the critique.
       This catches dashboards that look right but render empty —
       wrong service filter, time range narrower than the seed window,
       hallucinated fields, etc. The data check only runs when stage 1
       returned ``approve``; broken structure is reported first.

    Call this BEFORE ``grafana_update_dashboard``. Iterate (refine +
    re-judge) until verdict is ``approve``, then publish.

    Args:
        dashboard: The Grafana dashboard JSON model. Either a dict
            (preferred) or a JSON string.

    Returns:
        Dict with:
        - ``score`` (int, 1-10).
        - ``verdict`` (str: ``approve`` | ``revise`` | ``reject``).
        - ``critique`` (list[str]): concrete issues to fix. Empty when
          verdict is ``approve``. Data-plane failures are prefixed
          ``DATA-PLANE CHECK FAILED`` so they are easy to spot.
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

    result = {
        "score": int(verdict.get("score", 0)),
        "verdict": str(verdict.get("verdict", "revise")),
        "critique": list(verdict.get("critique", [])),
        "model_id": JUDGE_MODEL_ID,
    }

    # Data-plane validation: only worth running when the LLM thinks the
    # dashboard is otherwise ready. If the LLM already rejected it for
    # structural reasons, the agent has plenty to fix and running
    # Insights queries against a broken dashboard would just add noise.
    if result["verdict"] == "approve":
        try:
            empty_panel_issues = _validate_log_panels_return_data(model)
        except Exception as exc:  # noqa: BLE001
            # Validation itself blew up (IAM, network, ...). Fail open
            # so the agent isn't blocked by a transient infra issue,
            # but surface it as a critique entry so it's at least
            # visible in the trace.
            logger.warning("Dashboard data validation crashed: %s", exc)
            empty_panel_issues = [
                f"data-validation step crashed: {type(exc).__name__}: "
                f"{str(exc)[:160]} (publishing was NOT blocked)."
            ]
            result["critique"] = empty_panel_issues + result["critique"]
        else:
            if empty_panel_issues:
                # Override the LLM's verdict: a structurally clean
                # dashboard with empty panels is still unusable.
                result["score"] = min(result["score"], 5)
                result["verdict"] = "revise"
                result["critique"] = (
                    [
                        "DATA-PLANE CHECK FAILED — the following panels "
                        "would render empty. Fix each one (widen the "
                        "time range, relax the filter, or use a service "
                        "name confirmed by a prior tool call) and "
                        "re-judge:"
                    ]
                    + empty_panel_issues[:6]
                    + result["critique"]
                )

    return result
