"""Direct CloudWatch Logs reads, bypassing Logs Insights.

Two complementary tools live here:

- ``filter_log_events`` — raw, lag-free reads via FilterLogEvents
  (same path the console's "Log events" tab uses). Visible the instant
  PutLogEvents lands an event. Use this when Insights returns zero
  but the console clearly shows events.
- ``get_data_window`` — returns the actual oldest/newest event
  timestamps for a log group via DescribeLogStreams, plus a
  recommended Grafana ``time.from`` value sized to fit the data with
  a safety buffer. Built specifically so dashboards never publish
  with a time range that excludes the seeded data.

For aggregations, stats, or wide historical windows, prefer the AWS
Labs MCP tool ``cw_mcp_execute_log_insights_query``.
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


# Safety multiplier applied to the data age when picking the dashboard
# ``time.from``. If the data spans the last 60 minutes at build time,
# we set the window to 120 min so a user opening the dashboard up to
# ~60 minutes after publish still sees the full series. 100 % buffer
# is enough for normal demo / interactive use without diluting the
# time axis to the point that the data clusters in a corner.
_TIME_WINDOW_BUFFER_FACTOR = 2.0
# Floor and ceiling on the recommended window so we never produce a
# nonsensically tiny or huge ``now-Xm`` (e.g. on a log group with one
# stale event from a month ago).
_TIME_WINDOW_MIN_MINUTES = 60
_TIME_WINDOW_MAX_MINUTES = 7 * 24 * 60


@tool
def get_data_window(log_group_name: str) -> dict[str, Any]:
    """Return the actual time span of events in a log group.

    Reads ``firstEventTimestamp`` / ``lastEventTimestamp`` from every
    stream in the log group and produces a recommended Grafana time
    range that fits the data with a buffer. The agent MUST call this
    before building dashboards over a log group — using a guessed
    ``now-6h`` and hoping for the best is what causes "dashboard
    publishes but every panel is empty" in this demo.

    Args:
        log_group_name: log group to inspect, e.g.
            ``"/cloudwatch-agent/demo"``.

    Returns:
        Dict with:
        - ``empty`` (bool): True if the log group has no streams or no
          events with timestamps. When True, the other fields are
          either absent or zero — do not build dashboards yet, run the
          seeds first.
        - ``oldest_event_iso`` / ``newest_event_iso`` (str): ISO-8601
          UTC timestamps of the oldest and newest events across all
          streams.
        - ``oldest_event_age_minutes`` (int): how long ago the oldest
          event was, relative to "now" at the moment of this call.
        - ``span_minutes`` (int): newest - oldest, in minutes.
        - ``recommended_time_from`` (str): a Grafana ``time.from``
          value (e.g. ``"now-2h"``) sized to cover the data plus a
          buffer, clamped to [``now-1h``, ``now-7d``]. Use this
          verbatim for the dashboard's ``time.from`` field.
        - ``recommended_time_to`` (str): always ``"now"``.
    """
    streams_resp = _logs.describe_log_streams(
        logGroupName=log_group_name,
        orderBy="LastEventTime",
        descending=True,
        limit=50,
    )
    streams = streams_resp.get("logStreams", [])

    first_ts = [s["firstEventTimestamp"] for s in streams if "firstEventTimestamp" in s]
    last_ts = [s["lastEventTimestamp"] for s in streams if "lastEventTimestamp" in s]

    if not first_ts or not last_ts:
        return {
            "empty": True,
            "oldest_event_iso": None,
            "newest_event_iso": None,
            "oldest_event_age_minutes": 0,
            "span_minutes": 0,
            "recommended_time_from": f"now-{_TIME_WINDOW_MIN_MINUTES}m",
            "recommended_time_to": "now",
        }

    oldest_ms = min(first_ts)
    newest_ms = max(last_ts)
    now_ms = int(datetime.now(UTC).timestamp() * 1000)

    oldest_age_min = max(0, (now_ms - oldest_ms) // 60_000)
    span_min = max(0, (newest_ms - oldest_ms) // 60_000)

    # Lookback = oldest event age + buffer. A user opening the
    # dashboard immediately after build needs ``oldest_age_min``;
    # the buffer factor extends that so opens minutes later still
    # land inside the window.
    raw_lookback = int(oldest_age_min * _TIME_WINDOW_BUFFER_FACTOR)
    lookback_min = max(
        _TIME_WINDOW_MIN_MINUTES, min(_TIME_WINDOW_MAX_MINUTES, raw_lookback)
    )

    # Prefer hour-rounded values when the lookback is wide enough that
    # minute precision is just noise — keeps the JSON readable.
    if lookback_min >= 120 and lookback_min % 60 == 0:
        time_from = f"now-{lookback_min // 60}h"
    else:
        time_from = f"now-{lookback_min}m"

    return {
        "empty": False,
        "oldest_event_iso": datetime.fromtimestamp(oldest_ms / 1000, tz=UTC).isoformat(),
        "newest_event_iso": datetime.fromtimestamp(newest_ms / 1000, tz=UTC).isoformat(),
        "oldest_event_age_minutes": int(oldest_age_min),
        "span_minutes": int(span_min),
        "recommended_time_from": time_from,
        "recommended_time_to": "now",
    }
