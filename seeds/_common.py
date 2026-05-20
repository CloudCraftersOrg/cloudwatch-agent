"""Shared machinery for the demo seeds.

Both week scripts write structured JSON log events into ONE CloudWatch
Logs log group, using one log stream per simulated service. The agent
reads them later with CloudWatch Logs Insights (which auto-discovers the
JSON fields ``level``, ``service``, ``status_code``, ``latency_ms`` ...).

CloudWatch Logs constraints handled here so the week scripts don't have
to think about them:

* A ``PutLogEvents`` batch must be sorted by timestamp, span <= 24h, and
  stay under 10k events / ~1 MB. We push one batch per (service, day),
  which satisfies all three by construction.
* Events older than 14 days (or the log group's retention) are rejected
  by the API. We drop anything beyond a safe age and warn, rather than
  letting the whole batch fail — this is what keeps the "two real weeks"
  timeline robust if the demo runs slowly.
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
DEFAULT_REGION = "us-west-2"
RETENTION_DAYS = 30

# PutLogEvents limits, with safety margins below the hard caps.
_MAX_BATCH_EVENTS = 1000
_MAX_BATCH_BYTES = 900_000
_EVENT_OVERHEAD = 26  # Bytes CloudWatch adds per event for size accounting.

# Refuse events older than this. The hard API limit is 14 days; we keep a
# 3-hour margin so clock skew / a slow demo run can't trip a hard reject.
_MAX_AGE = timedelta(days=14) - timedelta(hours=3)

# Per-hour weighting (UTC) to give the data a believable diurnal shape so
# dashboards look like real traffic instead of white noise. Low overnight,
# peak around midday/afternoon in the Americas.
_HOUR_WEIGHTS = [
    0.3, 0.2, 0.2, 0.2, 0.3, 0.4, 0.6, 0.9,  # 00-07
    1.3, 1.7, 2.0, 2.2, 2.3, 2.3, 2.2, 2.0,  # 08-15
    1.8, 1.6, 1.4, 1.2, 1.0, 0.8, 0.6, 0.4,  # 16-23
]


@dataclass
class ServiceProfile:
    """How one service behaves for a given week.

    Attributes:
        per_day: Approximate event count per level per day. Actual counts
            jitter +/-30% so days are not identical.
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

    The window is intentionally narrow and the ``error_code`` distinctive
    so a prompt like "build an incident dashboard for the orders outage"
    has something unambiguous to visualize.
    """

    service: str
    day_offset: int  # Days ago the incident occurred (must fall in the run).
    start_hour: int  # UTC hour the burst starts.
    duration_hours: int
    count: int
    error_code: str
    message: str


@dataclass
class SeedSpec:
    """Everything a week script needs to declare; the rest is generic."""

    name: str
    start_days_ago: int
    num_days: int
    profiles: dict[str, ServiceProfile]
    incident: Incident | None = None
    # Free-text lines printed after the run to tell the operator exactly
    # what changed and which prompts to try next.
    notes: list[str] = field(default_factory=list)


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
    # Retention must comfortably exceed the 13-day data span; 30 days also
    # means the API won't reject our oldest (~13d) backdated events.
    client.put_retention_policy(
        logGroupName=log_group, retentionInDays=RETENTION_DAYS
    )
    try:
        client.create_log_stream(logGroupName=log_group, logStreamName=stream)
    except client.exceptions.ResourceAlreadyExistsException:
        pass


def _put_batch(client, log_group: str, stream: str, batch: list[dict]) -> None:
    """Send one already-sorted batch, tolerating the modern token model.

    Since 2023 ``PutLogEvents`` no longer requires a sequence token. We
    omit it; if an older endpoint still complains we retry once with the
    token it hands back.
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
    """Sort, age-guard, chunk and push events for one (service, day).

    Args:
        events: ``(epoch_ms, message)`` pairs, any order.

    Returns:
        Number of events actually sent (after dropping too-old ones).
    """
    if not events:
        return 0

    min_ts_ms = int((datetime.now(UTC) - _MAX_AGE).timestamp() * 1000)
    fresh = sorted((e for e in events if e[0] >= min_ts_ms), key=lambda e: e[0])
    dropped = len(events) - len(fresh)
    if dropped:
        print(
            f"  ! {dropped} event(s) older than the 14-day CloudWatch Logs "
            f"limit were skipped on stream '{stream}'."
        )

    sent = 0
    batch: list[dict] = []
    batch_bytes = 0
    for ts_ms, message in fresh:
        size = len(message.encode("utf-8")) + _EVENT_OVERHEAD
        # Flush when adding this event would exceed either cap. (The 24h
        # span cap is satisfied automatically: callers flush per day.)
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

    The shape is intentionally flat and CloudWatch-Logs-Insights-friendly:
    Insights auto-extracts top-level JSON keys, so the agent can write
    ``stats count() by bin(1h), service`` style queries with no parsing.
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


def _spread_timestamps(
    rng: random.Random, day_start: datetime, upper: datetime, count: int
) -> list[datetime]:
    """Pick ``count`` timestamps within a day, diurnally weighted."""
    out: list[datetime] = []
    hours = list(range(24))
    for _ in range(count):
        hour = rng.choices(hours, weights=_HOUR_WEIGHTS, k=1)[0]
        when = day_start + timedelta(
            hours=hour, minutes=rng.randint(0, 59), seconds=rng.randint(0, 59)
        )
        # Never emit into the future / past the run moment.
        if when < upper:
            out.append(when)
    return out


def run_seed(spec: SeedSpec, build_client: Callable | None = None) -> None:
    """Generate and push a full week of data, then print a summary.

    Args:
        spec: The declarative week definition (see SeedSpec).
        build_client: Test seam; defaults to a real CloudWatch Logs client.
    """
    args = parse_args(f"Seed CloudWatch demo logs: {spec.name}")
    rng = random.Random(args.rng_seed)
    client = (build_client or (lambda: boto3.client("logs", region_name=args.region)))()

    now = datetime.now(UTC).replace(microsecond=0)
    # Day boundaries are floored to the hour so windows are stable.
    base = now.replace(minute=0, second=0)

    print(f"== seed '{spec.name}' ==")
    print(f"log group : {args.log_group}  (region {args.region})")
    print(
        f"window    : ~{spec.start_days_ago} .. "
        f"{spec.start_days_ago - spec.num_days} days ago "
        f"({spec.num_days} days)"
    )
    # Re-runs are NOT idempotent: timestamps are recomputed from the
    # current wall clock each run, so CloudWatch accepts them as new
    # events and the data is DUPLICATED (the DataAlreadyAccepted guard
    # only covers a byte-identical batch, which never recurs). week2 is
    # designed to append to week1; but re-running the SAME week doubles
    # its counts. To start clean, delete the log group first:
    #   aws logs delete-log-group --log-group-name <group> --region <r>
    print(
        f"! re-running this seed APPENDS (duplicates) data. To reset, first: "
        f"aws logs delete-log-group --log-group-name {args.log_group} "
        f"--region {args.region}"
    )

    # tally[service][level] = count, for the closing summary.
    tally: dict[str, dict[str, int]] = {}

    for service, profile in spec.profiles.items():
        stream = service
        _ensure_stream(client, args.log_group, stream)
        tally.setdefault(service, {})

        for i in range(spec.num_days):
            day_start = base - timedelta(days=spec.start_days_ago) + timedelta(days=i)
            day_upper = min(day_start + timedelta(days=1), now - timedelta(minutes=1))
            if day_upper <= day_start:
                continue

            day_events: list[tuple[int, str]] = []
            for level, mean in profile.per_day.items():
                # +/-30% jitter so no two days are identical.
                count = max(0, int(mean * rng.uniform(0.7, 1.3)))
                for when in _spread_timestamps(rng, day_start, day_upper, count):
                    msg = _event_message(rng, when, service, level, profile)
                    day_events.append((int(when.timestamp() * 1000), msg))
                    tally[service][level] = tally[service].get(level, 0) + 1

            # Counts are tracked via `tally`; _flush's return is unused here.
            _flush(client, args.log_group, stream, day_events)

    # Inject the incident as a tight ERROR burst on its own day.
    if spec.incident:
        inc = spec.incident
        profile = spec.profiles[inc.service]
        _ensure_stream(client, args.log_group, inc.service)
        # Anchor the incident to UTC MIDNIGHT (not the hour-floored `base`)
        # so `start_hour` is a real wall-clock UTC hour: the burst lands at
        # exactly inc.start_hour:00 UTC, inc.day_offset days ago. This makes
        # the suggested demo prompt ("~3 days ago, 14:00-16:00 UTC") true
        # regardless of what hour the operator runs the seed. Still well
        # inside the 14-day backdating limit (day_offset is small).
        midnight = base.replace(hour=0)
        day_start = midnight - timedelta(days=inc.day_offset)
        win_start = day_start + timedelta(hours=inc.start_hour)
        win_end = win_start + timedelta(hours=inc.duration_hours)
        burst: list[tuple[int, str]] = []
        for _ in range(inc.count):
            offset = rng.random() * inc.duration_hours
            when = win_start + timedelta(hours=offset)
            if when >= now:
                continue
            msg = _event_message(
                rng,
                when,
                inc.service,
                "ERROR",
                profile,
                error_code=inc.error_code,
                message_override=inc.message,
            )
            burst.append((int(when.timestamp() * 1000), msg))
        _flush(client, args.log_group, inc.service, burst)
        tally[inc.service]["ERROR"] = (
            tally[inc.service].get("ERROR", 0) + len(burst)
        )
        spec.notes.append(
            f"INCIDENT seeded: service='{inc.service}' "
            f"error_code='{inc.error_code}' window="
            f"{win_start.isoformat()} .. {win_end.isoformat()}"
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
