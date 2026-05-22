"""Shared machinery for the demo seeds.

Every week script writes structured JSON log events into ONE
CloudWatch Logs log group, with one log stream per simulated service.
The agent reads them later via ``filter_log_events`` (lag-free) and
the ``cw_mcp_*`` Insights tools (for stats and aggregations).

All events are spread uniformly across the last ``WINDOW_MINUTES``
minutes ending at "now". This keeps both data planes happy — events
sit strictly after the log group's creationTime so Logs Insights
indexes them, and FilterLogEvents sees them immediately.

CloudWatch Logs constraints handled here so the week scripts don't
have to think about them:

* ``PutLogEvents`` batches must be sorted by timestamp, span <= 24h,
  and stay under 10k events / ~1 MB. The window is well under 24h
  and we chunk under the count/size caps below.
* Events older than 14 days are rejected by the API. The window is
  60 min, so this never trips.
"""

from __future__ import annotations

import argparse
import json
import random
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import boto3

# Defaults are overridable via CLI flags (see parse_args). A single log
# group keeps the demo simple; the per-service split lives in the stream
# name and the JSON ``service`` field.
DEFAULT_LOG_GROUP = "/cloudwatch-agent/demo"
DEFAULT_REGION = "us-east-1"
RETENTION_DAYS = 30

# All events are spread uniformly over the last ``WINDOW_MINUTES``
# minutes. Wide enough to show a sortable time series in dashboards;
# small enough to bound the data and keep PutLogEvents batches cheap.
WINDOW_MINUTES = 60

# PutLogEvents limits, with safety margins below the hard caps.
_MAX_BATCH_EVENTS = 1000
_MAX_BATCH_BYTES = 900_000
_EVENT_OVERHEAD = 26  # Bytes CloudWatch adds per event for size accounting.


@dataclass
class ServiceProfile:
    """How one service behaves for a given week.

    Attributes:
        per_day: Per-level event volume scalar. Total events per
            (service, level) emitted in one ``run_seed`` call is
            ``per_day[level] * SeedSpec.num_days`` with +/- 30 %
            jitter, all spread uniformly across the
            ``WINDOW_MINUTES`` window.
        templates: Candidate message strings per level.
        status: Candidate HTTP status codes per level.
        latency_ms: ``(low, high)`` latency range per level (ms).
    """

    per_day: dict[str, int]
    templates: dict[str, list[str]]
    status: dict[str, list[int]]
    latency_ms: dict[str, tuple[int, int]]


@dataclass
class Incident:
    """A concentrated error burst the agent should be able to find.

    The window is intentionally narrow and the ``error_code`` is
    distinctive so a prompt like "build an incident dashboard for the
    orders outage" has something unambiguous to visualize. The burst
    lasts ``_INCIDENT_BURST_MINUTES`` ending at "now".
    """

    service: str
    count: int
    error_code: str
    message: str


@dataclass
class SeedSpec:
    """Everything a week script needs to declare; the rest is generic."""

    name: str
    num_days: int  # volume scalar applied to per-window event counts
    profiles: dict[str, ServiceProfile]
    incident: Incident | None = None
    # Free-text lines printed after the run to tell the operator exactly
    # what changed and which prompts to try next.
    notes: list[str] = field(default_factory=list)


# How wide (in minutes) the incident burst spans, ending just before "now".
_INCIDENT_BURST_MINUTES = 10


def parse_args(description: str) -> argparse.Namespace:
    """Parse the small, shared CLI surface for both week scripts."""
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument(
        "--log-group",
        default=DEFAULT_LOG_GROUP,
        help=f"CloudWatch Logs log group (default: {DEFAULT_LOG_GROUP}).",
    )
    parser.add_argument(
        "--region",
        default=DEFAULT_REGION,
        help=f"AWS region (default: {DEFAULT_REGION}).",
    )
    parser.add_argument(
        "--rng-seed",
        type=int,
        default=1337,
        help="RNG seed; fixed by default so demo runs are reproducible.",
    )
    return parser.parse_args()


def _ensure_stream(client, log_group: str, stream: str) -> None:
    """Create the log group (with retention) and stream if absent.

    All the ``ResourceAlreadyExists`` paths are expected on re-runs and on
    week2 appending to week1's group, so they are swallowed deliberately.
    """
    try:
        client.create_log_group(logGroupName=log_group)
    except client.exceptions.ResourceAlreadyExistsException:
        pass
    client.put_retention_policy(
        logGroupName=log_group, retentionInDays=RETENTION_DAYS
    )
    try:
        client.create_log_stream(logGroupName=log_group, logStreamName=stream)
    except client.exceptions.ResourceAlreadyExistsException:
        pass


def _put_batch(client, log_group: str, stream: str, batch: list[dict]) -> None:
    """Send one already-sorted batch.

    ``PutLogEvents`` does not require a sequence token on current AWS
    endpoints; we omit it. If a regional endpoint still complains we
    retry once with the token it returns.
    """
    try:
        client.put_log_events(
            logGroupName=log_group, logStreamName=stream, logEvents=batch
        )
    except client.exceptions.InvalidSequenceTokenException as exc:
        token = exc.response.get("expectedSequenceToken")
        client.put_log_events(
            logGroupName=log_group,
            logStreamName=stream,
            logEvents=batch,
            sequenceToken=token,
        )
    except client.exceptions.DataAlreadyAcceptedException:
        # Idempotent re-run of an identical batch; nothing to do.
        pass


def _flush(client, log_group: str, stream: str, events: list[tuple[int, str]]) -> int:
    """Sort and chunk events; push under the PutLogEvents limits.

    Args:
        events: ``(epoch_ms, message)`` pairs, any order.

    Returns:
        Number of events actually sent.
    """
    if not events:
        return 0

    fresh = sorted(events, key=lambda e: e[0])
    sent = 0
    batch: list[dict] = []
    batch_bytes = 0
    for ts_ms, message in fresh:
        size = len(message.encode("utf-8")) + _EVENT_OVERHEAD
        if batch and (
            len(batch) >= _MAX_BATCH_EVENTS or batch_bytes + size > _MAX_BATCH_BYTES
        ):
            _put_batch(client, log_group, stream, batch)
            sent += len(batch)
            batch, batch_bytes = [], 0
        batch.append({"timestamp": ts_ms, "message": message})
        batch_bytes += size

    if batch:
        _put_batch(client, log_group, stream, batch)
        sent += len(batch)
    return sent


def _event_message(
    rng: random.Random,
    when: datetime,
    service: str,
    level: str,
    profile: ServiceProfile,
    *,
    error_code: str | None = None,
    message_override: str | None = None,
) -> str:
    """Build one structured JSON log line.

    The shape is intentionally flat and Logs-Insights-friendly:
    Insights auto-extracts top-level JSON keys, so the agent can write
    ``stats count() by bin(1m), service`` style queries with no parsing.
    """
    lo, hi = profile.latency_ms[level]
    payload = {
        "timestamp": when.isoformat(),
        "level": level,
        "service": service,
        "message": message_override or rng.choice(profile.templates[level]),
        "status_code": rng.choice(profile.status[level]),
        "latency_ms": rng.randint(lo, hi),
        "request_id": uuid.uuid4().hex[:12],
    }
    if error_code:
        payload["error_code"] = error_code
    return json.dumps(payload, separators=(",", ":"))


def run_seed(spec: SeedSpec, build_client: Callable | None = None) -> None:
    """Generate and push the seed events, then print a summary.

    All events are spread uniformly over the last ``WINDOW_MINUTES``
    minutes ending at "now". Volume per (service, level) is
    ``profile.per_day[level] * spec.num_days`` with +/-30% jitter (same
    as before, just collapsed into a single window).

    Args:
        spec: The declarative week definition (see SeedSpec).
        build_client: Test seam; defaults to a real CloudWatch Logs client.
    """
    args = parse_args(f"Seed CloudWatch demo logs: {spec.name}")
    rng = random.Random(args.rng_seed)
    client = (build_client or (lambda: boto3.client("logs", region_name=args.region)))()

    now = datetime.now(UTC).replace(microsecond=0)
    now_ms = int(now.timestamp() * 1000)
    window_seconds = WINDOW_MINUTES * 60

    print(f"== seed '{spec.name}' ==")
    print(f"log group : {args.log_group}  (region {args.region})")
    print(f"window    : last {WINDOW_MINUTES} minutes (ending {now.isoformat()})")
    print(
        f"! re-runs APPEND (duplicate) data. To reset cleanly, delete "
        f"individual STREAMS (not the log group itself): "
        f"aws logs delete-log-stream --log-group-name {args.log_group} "
        f"--log-stream-name <stream> --region {args.region}"
    )

    # tally[service][level] = count, for the closing summary.
    tally: dict[str, dict[str, int]] = {}

    for service, profile in spec.profiles.items():
        _ensure_stream(client, args.log_group, service)
        tally.setdefault(service, {})

        events: list[tuple[int, str]] = []
        for level, mean in profile.per_day.items():
            # Total event count = per_day * num_days with +/- 30% jitter.
            # num_days is a volume scalar, not real days.
            count = max(0, int(mean * spec.num_days * rng.uniform(0.7, 1.3)))
            for _ in range(count):
                offset_sec = rng.uniform(0, window_seconds)
                ts_ms = now_ms - int(offset_sec * 1000)
                when = datetime.fromtimestamp(ts_ms / 1000, tz=UTC)
                msg = _event_message(rng, when, service, level, profile)
                events.append((ts_ms, msg))
                tally[service][level] = tally[service].get(level, 0) + 1

        _flush(client, args.log_group, service, events)

    # Incident burst: ``count`` ERROR events with the distinctive
    # error_code, spread uniformly across the last
    # ``_INCIDENT_BURST_MINUTES`` minutes (well inside the main window).
    if spec.incident:
        inc = spec.incident
        profile = spec.profiles[inc.service]
        _ensure_stream(client, args.log_group, inc.service)
        burst_window_sec = _INCIDENT_BURST_MINUTES * 60
        burst: list[tuple[int, str]] = []
        for _ in range(inc.count):
            offset_sec = rng.uniform(0, burst_window_sec)
            ts_ms = now_ms - int(offset_sec * 1000)
            when = datetime.fromtimestamp(ts_ms / 1000, tz=UTC)
            msg = _event_message(
                rng,
                when,
                inc.service,
                "ERROR",
                profile,
                error_code=inc.error_code,
                message_override=inc.message,
            )
            burst.append((ts_ms, msg))
        _flush(client, args.log_group, inc.service, burst)
        tally[inc.service]["ERROR"] = (
            tally[inc.service].get("ERROR", 0) + len(burst)
        )
        burst_start = now - timedelta(minutes=_INCIDENT_BURST_MINUTES)
        spec.notes.append(
            f"INCIDENT seeded: service='{inc.service}' "
            f"error_code='{inc.error_code}' "
            f"count={inc.count} within last {_INCIDENT_BURST_MINUTES} min "
            f"({burst_start.isoformat()} .. {now.isoformat()})"
        )

    print("\nper-service counts:")
    for service in sorted(tally):
        levels = ", ".join(f"{lvl}={n}" for lvl, n in sorted(tally[service].items()))
        print(f"  {service:<10} {levels}")

    if spec.notes:
        print("\nnotes for the demo:")
        for line in spec.notes:
            print(f"  - {line}")
    print("\ndone.")
