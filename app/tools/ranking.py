"""Service prioritization for the canonical-5 dashboard set.

The agent maintains five Grafana dashboards: a permanent
``cwagent-overview`` plus four per-service deep dives. When the
underlying data changes the set has to rebalance — drop the
lowest-priority service and promote whichever new offender is now
worse.

``rank_services_by_priority`` is the signal the agent reads to make
that decision. It runs a single Logs Insights query over a recent
window and assigns each service a TIER plus a within-tier composite
score:

- **Tier 1 — critical**: the service has at least one ERROR. Ranked
  by the original composite (absolute error volume + error rate +
  p99 latency).
- **Tier 2 — degraded**: no ERRORS but at least one WARN. Ranked by
  warn volume + warn rate + p99 latency.
- **Tier 3 — healthy**: only INFO. Ranked by traffic volume so the
  busiest healthy services still get a slot when nothing is failing.

Services come back sorted ``(tier asc, score desc)`` — Tier 1 first,
ties broken by score. The agent takes the top four entries for the
per-service dashboard slots; the overview always occupies slot 1.

The tier model means slot assignment is robust to a quiet account
(no errors anywhere): the agent still produces four useful
dashboards instead of bailing out, but the model can clearly see in
the result which slots are "real problems" and which are filler.
"""

from __future__ import annotations

import time
from typing import Any

import boto3
from strands import tool

from app.config import REGION

_logs = boto3.client("logs", region_name=REGION)

# Within-tier composite weights. Each tier reuses the same shape
# (volume + rate + latency tail) but applies it to the level that
# defines the tier.
_WEIGHT_PRIMARY_COUNT = 0.5
_WEIGHT_PRIMARY_RATE = 0.3
_WEIGHT_P99_LATENCY = 0.2

_INSIGHTS_QUERY_TIMEOUT_S = 30
_INSIGHTS_POLL_INTERVAL_S = 1.0

_TIER_CRITICAL = 1
_TIER_DEGRADED = 2
_TIER_HEALTHY = 3
_TIER_LABELS = {
    _TIER_CRITICAL: "critical",
    _TIER_DEGRADED: "degraded",
    _TIER_HEALTHY: "healthy",
}


def _start_and_wait(
    log_group_names: list[str],
    expression: str,
    lookback_seconds: int,
) -> list[dict[str, str]]:
    """Run a Logs Insights query and return the raw row list."""
    end_s = int(time.time())
    start_s = end_s - lookback_seconds

    started = _logs.start_query(
        logGroupNames=log_group_names,
        startTime=start_s,
        endTime=end_s,
        queryString=expression,
        limit=200,
    )
    query_id = started["queryId"]
    deadline = time.time() + _INSIGHTS_QUERY_TIMEOUT_S
    while time.time() < deadline:
        time.sleep(_INSIGHTS_POLL_INTERVAL_S)
        result = _logs.get_query_results(queryId=query_id)
        status = result.get("status")
        if status == "Complete":
            return result.get("results", [])
        if status in {"Failed", "Cancelled", "Timeout"}:
            raise RuntimeError(
                f"Logs Insights query ended with status={status}"
            )
    try:
        _logs.stop_query(queryId=query_id)
    except Exception:  # noqa: BLE001
        pass
    raise TimeoutError(
        f"Logs Insights query did not finish within {_INSIGHTS_QUERY_TIMEOUT_S}s"
    )


def _row_to_dict(row: list[dict[str, str]]) -> dict[str, str]:
    """Flatten Insights' ``[{field, value}, ...]`` row shape into a dict."""
    return {item["field"]: item["value"] for item in row}


def _normalize(values: list[float]) -> list[float]:
    """Min-max normalize a list to [0, 1].

    Degenerate inputs (empty, single element, or all-equal values)
    collapse to all-1.0 so the score still has a positive value the
    sort can use as a tie-breaker. Returns [] only on empty input.
    """
    if not values:
        return []
    if len(values) == 1:
        return [1.0]
    lo, hi = min(values), max(values)
    if hi == lo:
        return [1.0 for _ in values]
    return [(v - lo) / (hi - lo) for v in values]


def _score_tier(
    services: list[dict[str, Any]],
    count_key: str,
    rate_key: str,
) -> None:
    """Compute the within-tier composite score for each service in-place.

    Reads the tier-specific ``count_key`` and ``rate_key`` plus
    ``p99_latency_ms`` from each service dict, normalizes across the
    set, and writes ``composite_score`` on each entry. Mutates input.
    """
    count_norm = _normalize([s[count_key] for s in services])
    rate_norm = _normalize([s[rate_key] for s in services])
    p99_norm = _normalize([s["p99_latency_ms"] for s in services])
    for i, s in enumerate(services):
        s["composite_score"] = round(
            _WEIGHT_PRIMARY_COUNT * count_norm[i]
            + _WEIGHT_PRIMARY_RATE * rate_norm[i]
            + _WEIGHT_P99_LATENCY * p99_norm[i],
            4,
        )


@tool
def rank_services_by_priority(
    log_group_name: str,
    lookback_minutes: int = 60,
) -> dict[str, Any]:
    """Score and rank services for the per-service dashboard slots.

    Reads the structured JSON events in ``log_group_name`` over the
    last ``lookback_minutes`` and assigns each service a TIER plus a
    within-tier composite score:

    - Tier 1 (``critical``): the service emitted at least one ERROR.
      Ranked by a weighted blend of normalized ``error_count``,
      ``error_rate`` and ``p99_latency_ms``.
    - Tier 2 (``degraded``): no ERRORS, at least one WARN. Ranked by
      the same shape but using ``warn_count`` / ``warn_rate``.
    - Tier 3 (``healthy``): only INFO. Ranked by traffic volume so
      busy services still get a slot when nothing is failing.

    The returned list is sorted ``(tier asc, composite_score desc)``.
    The agent should take the top four entries as the per-service
    dashboard set; the overview occupies the fifth slot independent
    of this ranking.

    Args:
        log_group_name: log group with a ``service`` and ``level``
            field, e.g. ``"/cloudwatch-agent/demo"``.
        lookback_minutes: how far back to look. Default 60 matches
            the seed window; widen for longer-running workloads.

    Returns:
        Dict with:
        - ``services`` (list[dict]): per-service stats sorted
          worst-first. Each entry has ``service``, ``total_count``,
          ``error_count``, ``warn_count``, ``info_count``,
          ``error_rate``, ``warn_rate``, ``p99_latency_ms``,
          ``tier`` (1-3), ``tier_label``, ``composite_score``.
        - ``weights`` (dict): the weights used (same shape each tier).
        - ``lookback_minutes`` (int): the window actually queried.
        - ``error`` (str, optional): present only if the query failed.
    """
    expression = (
        "fields service, level, latency_ms\n"
        "| stats count(*) as total_count, "
        "sum(level = 'ERROR') as error_count, "
        "sum(level = 'WARN') as warn_count, "
        "sum(level = 'INFO') as info_count, "
        "pct(latency_ms, 99) as p99_latency_ms "
        "by service"
    )
    weights = {
        "primary_count": _WEIGHT_PRIMARY_COUNT,
        "primary_rate": _WEIGHT_PRIMARY_RATE,
        "p99_latency_ms": _WEIGHT_P99_LATENCY,
    }
    try:
        rows = _start_and_wait(
            [log_group_name], expression, lookback_minutes * 60
        )
    except (TimeoutError, RuntimeError) as exc:
        return {
            "services": [],
            "weights": weights,
            "lookback_minutes": lookback_minutes,
            "error": str(exc),
        }

    parsed: list[dict[str, Any]] = []
    for row in rows:
        d = _row_to_dict(row)
        service = d.get("service")
        if not service:
            continue
        total = float(d.get("total_count", 0) or 0)
        errors = float(d.get("error_count", 0) or 0)
        warns = float(d.get("warn_count", 0) or 0)
        infos = float(d.get("info_count", 0) or 0)
        p99 = float(d.get("p99_latency_ms", 0) or 0)
        parsed.append(
            {
                "service": service,
                "total_count": int(total),
                "error_count": int(errors),
                "warn_count": int(warns),
                "info_count": int(infos),
                "error_rate": (errors / total) if total > 0 else 0.0,
                "warn_rate": (warns / total) if total > 0 else 0.0,
                "p99_latency_ms": p99,
            }
        )

    # Bucket each service into a tier. ERROR presence wins, then
    # WARN, then anything else (including services that only emit
    # INFO and services that are entirely silent within the window).
    critical = [s for s in parsed if s["error_count"] > 0]
    degraded = [
        s for s in parsed if s["error_count"] == 0 and s["warn_count"] > 0
    ]
    healthy = [
        s for s in parsed
        if s["error_count"] == 0 and s["warn_count"] == 0
    ]

    # Within-tier ranking. Each tier uses the same composite shape on
    # the level that defines it (errors for critical, warns for
    # degraded, plain volume for healthy).
    _score_tier(critical, count_key="error_count", rate_key="error_rate")
    _score_tier(degraded, count_key="warn_count", rate_key="warn_rate")
    # Healthy: there is no "rate" of failure to rank by, so the score
    # is purely the normalized info volume. Reuse _score_tier by
    # mapping rate_key onto info_count too — the duplication just
    # weights traffic volume more heavily for healthy services,
    # which is the right behavior (busier healthy service > quieter
    # healthy service).
    _score_tier(healthy, count_key="info_count", rate_key="info_count")

    for s in critical:
        s["tier"] = _TIER_CRITICAL
        s["tier_label"] = _TIER_LABELS[_TIER_CRITICAL]
    for s in degraded:
        s["tier"] = _TIER_DEGRADED
        s["tier_label"] = _TIER_LABELS[_TIER_DEGRADED]
    for s in healthy:
        s["tier"] = _TIER_HEALTHY
        s["tier_label"] = _TIER_LABELS[_TIER_HEALTHY]

    critical.sort(key=lambda s: s["composite_score"], reverse=True)
    degraded.sort(key=lambda s: s["composite_score"], reverse=True)
    healthy.sort(key=lambda s: s["composite_score"], reverse=True)

    return {
        "services": critical + degraded + healthy,
        "weights": weights,
        "lookback_minutes": lookback_minutes,
    }
