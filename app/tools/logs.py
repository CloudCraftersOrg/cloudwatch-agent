"""CloudWatch Logs tools.

Two tools:

- ``list_log_groups``: enumerate log groups in the account.
- ``run_logs_insights_query``: run a Logs Insights query against one or
  more log groups and return the results.

Logs Insights is asynchronous: ``StartQuery`` returns a query ID, and the
caller must poll ``GetQueryResults`` until the query reports a terminal
status. We hide the polling loop from the agent so it can treat the tool
as a synchronous query.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Any

import boto3
from strands import tool

from app.config import REGION

# Module-level client, reused across invocations (see app/tools/metrics.py
# for the rationale).
_logs = boto3.client("logs", region_name=REGION)

# Polling configuration for Logs Insights. Insights queries usually
# complete in well under a second for small log groups, but heavy queries
# over wide time ranges can take 30+ seconds. We cap the wait at 60s to
# avoid blocking the AgentCore Runtime invocation indefinitely.
_POLL_INTERVAL_SECONDS = 1.0
_POLL_TIMEOUT_SECONDS = 60.0


@tool
def list_log_groups(name_prefix: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    """List CloudWatch log groups, optionally filtered by name prefix.

    Args:
        name_prefix: Optional log-group name prefix filter (e.g.
            ``/aws/lambda/``). When omitted, all log groups are returned
            up to ``limit``.
        limit: Maximum number of log groups to return. Defaults to 50,
            which keeps token usage bounded; the LLM can request a
            wider page by raising this value.

    Returns:
        List of dicts with ``log_group_name``, ``creation_time`` (ISO-8601),
        ``stored_bytes``, and ``retention_in_days`` (or ``None`` for
        log groups with no retention policy set).
    """
    kwargs: dict[str, Any] = {"limit": min(limit, 50)}
    if name_prefix:
        kwargs["logGroupNamePrefix"] = name_prefix

    response = _logs.describe_log_groups(**kwargs)

    results: list[dict[str, Any]] = []
    for log_group in response.get("logGroups", []):
        # creationTime is returned as epoch milliseconds by the API.
        creation_ms = log_group.get("creationTime", 0)
        results.append(
            {
                "log_group_name": log_group["logGroupName"],
                "creation_time": datetime.fromtimestamp(
                    creation_ms / 1000, tz=UTC
                ).isoformat(),
                "stored_bytes": log_group.get("storedBytes", 0),
                # retentionInDays is omitted for "Never expire" log groups.
                "retention_in_days": log_group.get("retentionInDays"),
            }
        )
    return results


@tool
def run_logs_insights_query(
    log_group_names: list[str],
    query: str,
    lookback_minutes: int = 60,
    limit: int = 100,
) -> list[dict[str, str]]:
    """Run a CloudWatch Logs Insights query and return the results.

    Hides the asynchronous query lifecycle from the caller: starts the
    query, polls until it reaches a terminal status, and returns the
    flattened result rows.

    Args:
        log_group_names: List of log group names to query against.
            Insights supports up to 50 log groups in a single query.
        query: The Logs Insights query string. See
            https://docs.aws.amazon.com/AmazonCloudWatch/latest/logs/CWL_QuerySyntax.html.
        lookback_minutes: Time window for the query, ending at "now".
            Defaults to 60 minutes.
        limit: Maximum number of result rows to return at the API level.
            Insights itself caps results at 10,000 per query.

    Returns:
        List of result rows. Each row is a flat ``{field_name: value}``
        dict, projecting away the ``[{"field": ..., "value": ...}, ...]``
        envelope that the raw API returns.

    Raises:
        TimeoutError: If the query does not complete within
            ``_POLL_TIMEOUT_SECONDS``.
        RuntimeError: If the query reaches a ``Failed`` or ``Cancelled``
            terminal status.
    """
    end_time = datetime.now(UTC)
    start_time = end_time - timedelta(minutes=lookback_minutes)

    # StartQuery accepts epoch seconds (not millis like most other Logs APIs).
    start_response = _logs.start_query(
        logGroupNames=log_group_names,
        startTime=int(start_time.timestamp()),
        endTime=int(end_time.timestamp()),
        queryString=query,
        limit=limit,
    )
    query_id = start_response["queryId"]

    # Poll for completion. Insights statuses are: Scheduled, Running,
    # Complete, Failed, Cancelled, Timeout. The last four are terminal.
    deadline = time.monotonic() + _POLL_TIMEOUT_SECONDS
    while True:
        if time.monotonic() > deadline:
            # Stop the query so we don't leave it running on the server.
            try:
                _logs.stop_query(queryId=query_id)
            except Exception:
                # Best-effort cleanup; surface the original timeout.
                pass
            raise TimeoutError(
                f"Logs Insights query {query_id} did not complete within "
                f"{_POLL_TIMEOUT_SECONDS}s."
            )

        result = _logs.get_query_results(queryId=query_id)
        status = result["status"]

        if status == "Complete":
            break
        if status in ("Failed", "Cancelled", "Timeout"):
            raise RuntimeError(
                f"Logs Insights query {query_id} ended with status {status!r}."
            )

        time.sleep(_POLL_INTERVAL_SECONDS)

    # Flatten the [{"field": ..., "value": ...}] envelope into a plain dict.
    return [{field["field"]: field["value"] for field in row} for row in result.get("results", [])]
