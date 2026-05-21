"""Log-reading tool that bypasses Logs Insights.

The agent's main analytics path for logs (describe_log_groups,
execute_log_insights_query, analyze_log_group, etc.) goes through the
AWS Labs CloudWatch MCP server (see ``app/mcp_clients.py``), which
internally uses Logs Insights and therefore inherits its indexing lag.

This module exposes a single complementary tool, ``filter_log_events``,
that calls the FilterLogEvents API directly (the same path the
console's "Log events" tab uses). There is no indexing step: events
become visible as soon as PutLogEvents lands them. Useful when
Insights returns zero but the console clearly shows events.

For aggregations, stats, or wide historical windows, prefer the MCP
tool ``execute_log_insights_query`` — it is much cheaper on large
scans.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import boto3
from strands import tool

from app.config import REGION

_logs = boto3.client("logs", region_name=REGION)


@tool
def filter_log_events(
    log_group_name: str,
    filter_pattern: str | None = None,
    lookback_minutes: int = 20160,
    log_stream_names: list[str] | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Read raw events from a log group via FilterLogEvents.

    Does not use Logs Insights, so it is not affected by indexing lag.
    This is the right tool when the user expects to see recent activity
    and the Insights path is returning zero.

    Args:
        log_group_name: log group name, e.g. ``"/cloudwatch-agent/demo"``.
        filter_pattern: CloudWatch Logs filter pattern. For structured
            JSON logs use the JSON form, e.g. ``{ $.level = "ERROR" }``
            or ``{ $.service = "payments" && $.status_code = 500 }``.
            Omit to pull everything within the window.
        lookback_minutes: how far back from "now" to look. Default 20160
            (14 days) because the cost of a wide window is bounded by
            ``limit``, and a narrow default would miss backdated data.
        log_stream_names: optional subset of streams. Omit to scan every
            stream in the log group.
        limit: maximum number of events to return (FilterLogEvents
            returns up to 10000 per page; we only fetch one page).

    Returns:
        List of ``{timestamp, log_stream_name, message}`` dicts, with
        ``timestamp`` in ISO-8601 UTC and ``message`` as the raw body.
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
