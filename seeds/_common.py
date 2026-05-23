"""Shared machinery for the demo seeds.

Every week script writes structured JSON log events into ONE
CloudWatch Logs log group, with one log stream per simulated service.
The agent reads them later via ``filter_log_events`` (lag-free) and
the ``cw_mcp_*`` Insights tools (for stats and aggregations).

All events are spread uniformly across a window ending at "now", with
the lower bound clamped strictly after the log group's
``creationTime``. Logs Insights silently drops anything older than
``creationTime``, so we enforce the floor at sample time AND validate
it again at flush. On a fresh log group (week 1) "now" sits at the
moment of creation, so the window slides a small step forward to keep
events just after the floor instead of all sharing one timestamp.

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
from datetime import UTC, datetime

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


def _ensure_log_group(client, log_group: str) -> None:
    """Create the log group (with retention) if absent.

    Split out from stream creation so callers can read ``creationTime``
    once, up front, before sampling event timestamps.
    """
    try:
        client.create_log_group(logGroupName=log_group)
    except client.exceptions.ResourceAlreadyExistsException:
        pass
    client.put_retention_policy(
        logGroupName=log_group, retentionInDays=RETENTION_DAYS
    )


def _ensure_stream(client, log_group: str, stream: str) -> None:
    """Create the log stream if absent."""
    try:
        client.create_log_stream(logGroupName=log_group, logStreamName=stream)
    except client.exceptions.ResourceAlreadyExistsException:
        pass


def _log_group_creation_time_ms(client, log_group: str) -> int:
    """Return the log group's ``creationTime`` in epoch ms.

    Anchors the seed window so we never emit events Logs Insights would
    silently drop for predating the group.
    """
    paginator = client.get_paginator("describe_log_groups")
    for page in paginator.paginate(logGroupNamePrefix=log_group):
        for lg in page.get("logGroups", []):
            if lg.get("logGroupName") == log_group:
                return int(lg["creationTime"])
    raise RuntimeError(f"log group {log_group!r} not found after create")


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


def _flush(
    client,
    log_group: str,
    stream: str,
    events: list[tuple[int, str]],
    creation_time_ms: int,
) -> int:
    """Sort and chunk events; push under the PutLogEvents limits.

    Args:
        events: ``(epoch_ms, message)`` pairs, any order.
        creation_time_ms: Log group ``creationTime``. Any event at or
            before this is a bug — Logs Insights would silently drop
            it — so we fail loud instead.

    Returns:
        Number of events actually sent.
    """
    if not events:
        return 0

    # Sampling already enforces this floor; the check converts any
    # future regression from silent data loss into a visible failure.
    for ts_ms, _ in events:
        if ts_ms <= creation_time_ms:
            raise ValueError(
                f"event ts={ts_ms} on stream {stream!r} is at or before "
                f"log group creationTime={creation_time_ms}; Logs Insights "
                f"would drop it"
            )

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

    The seed window ends at "now" and starts at
    ``max(now - WINDOW_MINUTES, creationTime + 1ms)``. On a fresh log
    group (typical week 1 run) "now" sits at the moment of creation, so
    the window slides forward 1 s past ``creationTime`` instead of
    collapsing — events land "just after" the log group exists. Volume
    per (service, level) is ``profile.per_day[level] * spec.num_days``
    with +/-30% jitter.

    Args:
        spec: The declarative week definition (see SeedSpec).
        build_client: Test seam; defaults to a real CloudWatch Logs client.
    """
    args = parse_args(f"Seed CloudWatch demo logs: {spec.name}")
    rng = random.Random(args.rng_seed)
    client = (build_client or (lambda: boto3.client("logs", region_name=args.region)))()

    # Create the log group first so its ``creationTime`` is known
    # before we sample any timestamps. Without this anchor, week 1
    # against a fresh group emits events whose ts predates the group
    # itself and Logs Insights silently drops them.
    _ensure_log_group(client, args.log_group)
    creation_time_ms = _log_group_creation_time_ms(client, args.log_group)
    floor_ms = creation_time_ms + 1  # strictly after creationTime

    now_raw = datetime.now(UTC).replace(microsecond=0)
    now_ms_raw = int(now_raw.timestamp() * 1000)
    window_seconds = WINDOW_MINUTES * 60

    if now_ms_raw <= floor_ms:
        # Group was created in this same run (week 1 typical path).
        # Slide the window a small step forward of creationTime so each
        # event gets a unique-ish ms timestamp and the batch sits
        # cleanly "just after" creation rather than collapsing onto it.
        window_start_ms = floor_ms
        end_ms = floor_ms + 1000
    else:
        end_ms = now_ms_raw
        window_start_ms = max(now_ms_raw - window_seconds * 1000, floor_ms)

    creation_iso = datetime.fromtimestamp(creation_time_ms / 1000, UTC).isoformat()
    start_iso = datetime.fromtimestamp(window_start_ms / 1000, UTC).isoformat()
    end_iso = datetime.fromtimestamp(end_ms / 1000, UTC).isoformat()
    print(f"== seed '{spec.name}' ==")
    print(f"log group : {args.log_group}  (region {args.region})")
    print(f"created   : {creation_iso}")
    print(f"window    : {start_iso} .. {end_iso}")
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
                ts_ms = rng.randint(window_start_ms, end_ms)
                when = datetime.fromtimestamp(ts_ms / 1000, tz=UTC)
                msg = _event_message(rng, when, service, level, profile)
                events.append((ts_ms, msg))
                tally[service][level] = tally[service].get(level, 0) + 1

        _flush(client, args.log_group, service, events, creation_time_ms)

    # Incident burst: ``count`` ERROR events with the distinctive
    # error_code, spread uniformly across the last
    # ``_INCIDENT_BURST_MINUTES`` minutes of the seed window — clamped
    # to ``window_start_ms`` so a fresh-group run never drifts below
    # the floor.
    if spec.incident:
        inc = spec.incident
        profile = spec.profiles[inc.service]
        _ensure_stream(client, args.log_group, inc.service)
        burst_window_sec = _INCIDENT_BURST_MINUTES * 60
        burst_start_ms = max(end_ms - burst_window_sec * 1000, window_start_ms)
        burst: list[tuple[int, str]] = []
        for _ in range(inc.count):
            ts_ms = rng.randint(burst_start_ms, end_ms)
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
        _flush(client, args.log_group, inc.service, burst, creation_time_ms)
        tally[inc.service]["ERROR"] = (
            tally[inc.service].get("ERROR", 0) + len(burst)
        )
        burst_start_iso = datetime.fromtimestamp(
            burst_start_ms / 1000, UTC
        ).isoformat()
        spec.notes.append(
            f"INCIDENT seeded: service='{inc.service}' "
            f"error_code='{inc.error_code}' count={inc.count} "
            f"({burst_start_iso} .. {end_iso})"
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
