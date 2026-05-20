"""CloudWatch metric tools.

Three tools are exposed:

- ``list_namespaces``: enumerate metric namespaces present in the account.
- ``list_metrics``: enumerate metrics within a given namespace.
- ``get_metric_data``: read recent data points for a specific metric.

The CloudWatch ``ListMetrics`` API does not have a dedicated "list
namespaces" endpoint, so ``list_namespaces`` is implemented by paginating
``ListMetrics`` and projecting unique namespace strings.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import boto3
from strands import tool

from app.config import REGION

# Single module-level boto3 client. boto3 clients are thread-safe for the
# read APIs used here, and module-level state lives for the life of the
# container and may serve multiple (possibly concurrent) invocations of a
# session, so reusing a client avoids per-request setup cost. See the
# concurrency note in app/main.py's module docstring.
_cloudwatch = boto3.client("cloudwatch", region_name=REGION)


@tool
def list_namespaces() -> list[str]:
    """List the unique CloudWatch metric namespaces in the current account.

    CloudWatch has no dedicated namespace API, so this paginates through
    ``ListMetrics`` and de-duplicates the ``Namespace`` field. The result
    is returned sorted for deterministic output, which makes it easier
    for the LLM to compare against prior turns.

    Returns:
        Sorted list of namespace strings (e.g. ``["AWS/EC2", "AWS/Lambda"]``).
    """
    namespaces: set[str] = set()
    paginator = _cloudwatch.get_paginator("list_metrics")
    # No filters — we want every namespace in the account.
    for page in paginator.paginate():
        for metric in page.get("Metrics", []):
            namespaces.add(metric["Namespace"])
    return sorted(namespaces)


@tool
def list_metrics(namespace: str, metric_name: str | None = None) -> list[dict[str, Any]]:
    """List CloudWatch metrics in a namespace, optionally filtered by name.

    Args:
        namespace: CloudWatch namespace, e.g. ``AWS/EC2``. Required because
            an unfiltered list across all namespaces is rarely useful and
            can return tens of thousands of entries on a busy account.
        metric_name: Optional metric name filter (e.g. ``CPUUtilization``).
            When omitted, all metrics in the namespace are returned.

    Returns:
        List of dicts with keys ``namespace``, ``metric_name`` and
        ``dimensions`` (the dimensions list as returned by CloudWatch).
        We project to a smaller schema rather than returning the raw
        boto3 response to keep token usage low.
    """
    kwargs: dict[str, Any] = {"Namespace": namespace}
    if metric_name:
        kwargs["MetricName"] = metric_name

    results: list[dict[str, Any]] = []
    paginator = _cloudwatch.get_paginator("list_metrics")
    for page in paginator.paginate(**kwargs):
        for metric in page.get("Metrics", []):
            results.append(
                {
                    "namespace": metric["Namespace"],
                    "metric_name": metric["MetricName"],
                    # Dimensions can be empty (account-level metrics).
                    "dimensions": metric.get("Dimensions", []),
                }
            )
    return results


@tool
def get_metric_data(
    namespace: str,
    metric_name: str,
    dimensions: list[dict[str, str]] | None = None,
    statistic: str = "Average",
    period_seconds: int = 300,
    lookback_minutes: int = 60,
) -> dict[str, Any]:
    """Fetch recent data points for a single CloudWatch metric.

    Uses the ``GetMetricData`` API (preferred over the legacy
    ``GetMetricStatistics`` because it is cheaper, paginates, and returns
    a deterministic shape).

    Args:
        namespace: CloudWatch namespace (e.g. ``AWS/EC2``).
        metric_name: Metric name (e.g. ``CPUUtilization``).
        dimensions: Optional list of ``{"Name": ..., "Value": ...}`` dicts.
            When omitted, CloudWatch will only return the all-dimension
            aggregate, which is rarely what you want for per-resource
            metrics — pass dimensions for instance-level data.
        statistic: One of ``Average``, ``Sum``, ``Minimum``, ``Maximum``,
            ``SampleCount``. Defaults to ``Average``.
        period_seconds: Aggregation period in seconds. Must be a multiple
            of 60. Default of 300s matches the CloudWatch detailed-metrics
            granularity for most AWS services.
        lookback_minutes: How far back to look. Default 60 minutes keeps
            response size small; the LLM can call again with a wider
            window if needed.

    Returns:
        Dict with ``timestamps`` and ``values`` lists (parallel arrays,
        ordered chronologically). ``timestamps`` are ISO-8601 strings.
    """
    end_time = datetime.now(UTC)
    start_time = end_time - timedelta(minutes=lookback_minutes)

    # GetMetricData accepts a list of MetricDataQuery; we issue exactly
    # one per call. The "id" field is required and must match /^[a-z]/.
    response = _cloudwatch.get_metric_data(
        StartTime=start_time,
        EndTime=end_time,
        MetricDataQueries=[
            {
                "Id": "m1",
                "MetricStat": {
                    "Metric": {
                        "Namespace": namespace,
                        "MetricName": metric_name,
                        "Dimensions": dimensions or [],
                    },
                    "Period": period_seconds,
                    "Stat": statistic,
                },
                # ReturnData defaults to True, but being explicit here
                # makes the intent obvious to anyone reading the code.
                "ReturnData": True,
            }
        ],
        # CloudWatch returns most-recent-first by default; we want the
        # natural chronological order for downstream charting.
        ScanBy="TimestampAscending",
    )

    result = response["MetricDataResults"][0]
    return {
        # ISO-format keeps the payload JSON-serializable and avoids the
        # LLM having to guess a date format.
        "timestamps": [ts.isoformat() for ts in result.get("Timestamps", [])],
        "values": result.get("Values", []),
    }
