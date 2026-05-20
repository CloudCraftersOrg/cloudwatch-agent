"""CloudWatch Logs tools that BYPASS Logs Insights.

The agent's primary path for log analytics (describe_log_groups,
execute_log_insights_query, analyze_log_group, get_logs_anomaly_detectors,
metric helpers...) goes through the AWS Labs CloudWatch MCP server (see
``app/mcp_clients.py``). Those tools rely on Logs Insights internally,
which has indexing lag (minutes for new log groups, especially for a
backdated burst from the demo seeds).

This module exposes ONE complementary tool, ``filter_log_events``, that
uses the FilterLogEvents API directly — the same path the AWS Console's
"Log events" tab uses. No indexing required; events are visible
immediately after PutLogEvents. Use this when:

  - Recent events (last few minutes / hours) need to be read NOW.
  - Insights queries are returning 0 results despite the console
    showing events (the classic indexing-lag symptom).
  - The user wants raw event JSON without aggregation.

For stats / aggregations / wide historical time windows, prefer the
MCP's ``execute_log_insights_query`` (cheaper for large scans).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import boto3
from strands import tool

from app.config import REGION

# Module-level client, reused across invocations; thread-safe for the
# read APIs used here. See concurrency note in app/main.py.
_logs = boto3.client("logs", region_name=REGION)


@tool
def filter_log_events(
    log_group_name: str,
    filter_pattern: str | None = None,
    lookback_minutes: int = 20160,
    log_stream_names: list[str] | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Read raw log events from a CloudWatch log group via FilterLogEvents.

    Bypasses Logs Insights — events appear here as soon as they are
    ingested (no indexing lag), so this is the right tool when the user
    expects to see recent activity and Insights is returning 0 results.

    Args:
        log_group_name: Log group to read, e.g. ``"/cloudwatch-agent/demo"``.
        filter_pattern: Optional CloudWatch Logs filter pattern. For
            structured JSON logs use the JSON form, e.g.:

              ``{ $.level = "ERROR" }``
              ``{ $.service = "payments" && $.status_code = 500 }``
              ``{ $.error_code = "OrderDBConnectionPoolExhausted" }``

            Or a plain quoted word match (case-sensitive substring), e.g.
            ``"timeout"``. Omit to fetch every event in the window. See
            https://docs.aws.amazon.com/AmazonCloudWatch/latest/logs/FilterAndPatternSyntax.html.
        lookback_minutes: How far back to look, ending at "now". Default
            is ``20160`` (14 days) because the cost of a wide window is
            bounded by ``limit`` (which caps the response payload), and a
            narrow default would silently miss backdated demo data and
            older events. Set a smaller value (e.g. 60) explicitly if
            you only care about the last hour.
        log_stream_names: Optional subset of stream names to read from
            (e.g. ``["gateway", "orders"]``). Omit to scan every stream
            in the group.
        limit: Maximum events to return (FilterLogEvents caps a single
            page at 10000; we only fetch one page to bound token usage).

    Returns:
        List of ``{timestamp, log_stream_name, message}`` dicts. The
        ``timestamp`` is ISO-8601 UTC; ``message`` is the raw event body
        (already JSON for structured logs — the LLM can parse if needed).
    """
    end_ms = int(datetime.now(UTC).timestamp() * 1000)
    start_ms = end_ms - lookback_minutes * 60 * 1000

    kwargs: dict[str, Any] = {
        "logGroupName": log_group_name,
        "startTime": start_ms,
        "endTime": end_ms,
        "limit": limit,
    }
    if filter_pattern:
        kwargs["filterPattern"] = filter_pattern
    if log_stream_names:
        kwargs["logStreamNames"] = log_stream_names

    response = _logs.filter_log_events(**kwargs)
    return [
        {
            "timestamp": datetime.fromtimestamp(
                event["timestamp"] / 1000, tz=UTC
            ).isoformat(),
            "log_stream_name": event.get("logStreamName"),
            "message": event["message"],
        }
        for event in response.get("events", [])
    ]
