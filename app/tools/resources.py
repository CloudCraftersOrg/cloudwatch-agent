"""AWS resource discovery.

Single tool ``discover_resources`` that enumerates EC2 instances, RDS DB
instances, and Lambda functions in the current account and region. The
agent calls this when it needs to suggest a dashboard tailored to what
is actually running.

Each service uses a different boto3 client, but the projection logic is
shared: we trim the API responses to a small, dashboard-relevant subset
to keep token usage down.
"""

from __future__ import annotations

from typing import Any, Literal

import boto3
from strands import tool

from app.config import REGION

# Module-level clients per AWS service. Created lazily here at import
# time; each is independent and thread-safe for the read APIs we use.
_ec2 = boto3.client("ec2", region_name=REGION)
_rds = boto3.client("rds", region_name=REGION)
_lambda = boto3.client("lambda", region_name=REGION)

# Type alias for the supported service filters. Defined as a Literal so
# the LLM (and any human caller) sees the exact set of accepted values
# in the tool signature.
ResourceType = Literal["ec2", "rds", "lambda"]


def _list_ec2_instances() -> list[dict[str, Any]]:
    """Enumerate EC2 instances and project to a compact dashboard schema.

    Returns one dict per instance with the dimensions that matter when
    building dashboards: instance ID (the ``InstanceId`` dimension on
    AWS/EC2 metrics), instance type, state, and the ``Name`` tag if any.
    """
    instances: list[dict[str, Any]] = []
    paginator = _ec2.get_paginator("describe_instances")
    for page in paginator.paginate():
        # describe_instances groups instances by reservation, so the
        # response has an extra layer of nesting compared to most APIs.
        for reservation in page.get("Reservations", []):
            for instance in reservation.get("Instances", []):
                # Tags is omitted entirely for instances with no tags.
                tags = {tag["Key"]: tag["Value"] for tag in instance.get("Tags", [])}
                instances.append(
                    {
                        "instance_id": instance["InstanceId"],
                        "instance_type": instance.get("InstanceType"),
                        "state": instance.get("State", {}).get("Name"),
                        "name": tags.get("Name"),
                    }
                )
    return instances


def _list_rds_instances() -> list[dict[str, Any]]:
    """Enumerate RDS DB instances.

    The CloudWatch dimension for AWS/RDS metrics is
    ``DBInstanceIdentifier``, so that's the field we surface most
    prominently. Engine and class are useful context for the LLM when it
    decides which metrics (CPU, IOPS, connections, etc.) to put on a
    dashboard.
    """
    instances: list[dict[str, Any]] = []
    paginator = _rds.get_paginator("describe_db_instances")
    for page in paginator.paginate():
        for db in page.get("DBInstances", []):
            instances.append(
                {
                    "db_instance_identifier": db["DBInstanceIdentifier"],
                    "engine": db.get("Engine"),
                    "instance_class": db.get("DBInstanceClass"),
                    "status": db.get("DBInstanceStatus"),
                }
            )
    return instances


def _list_lambda_functions() -> list[dict[str, Any]]:
    """Enumerate Lambda functions.

    The CloudWatch dimension for AWS/Lambda metrics is ``FunctionName``,
    which is what we expose. Runtime and memory are surfaced because they
    are common axes for cost/perf dashboards.
    """
    functions: list[dict[str, Any]] = []
    paginator = _lambda.get_paginator("list_functions")
    for page in paginator.paginate():
        for fn in page.get("Functions", []):
            functions.append(
                {
                    "function_name": fn["FunctionName"],
                    "runtime": fn.get("Runtime"),
                    "memory_size_mb": fn.get("MemorySize"),
                    "last_modified": fn.get("LastModified"),
                }
            )
    return functions


@tool
def discover_resources(
    resource_types: list[ResourceType] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Discover AWS resources in the current account and region.

    Enumerates the requested resource families and returns a single
    dict keyed by service. The agent uses this to decide which metric
    namespaces and dimensions to put on a generated dashboard.

    Args:
        resource_types: Subset of ``["ec2", "rds", "lambda"]`` to query.
            ``None`` (default) means discover all three. Passing a
            narrower list is recommended when the user has already told
            the agent which service they care about — it avoids
            unnecessary API calls in large accounts.

    Returns:
        Dict with keys among ``ec2_instances``, ``rds_instances``,
        ``lambda_functions``. Only requested services appear in the
        response, so callers can branch on key presence.
    """
    # Default to all three when the caller doesn't specify.
    types = set(resource_types) if resource_types else {"ec2", "rds", "lambda"}

    result: dict[str, list[dict[str, Any]]] = {}
    if "ec2" in types:
        result["ec2_instances"] = _list_ec2_instances()
    if "rds" in types:
        result["rds_instances"] = _list_rds_instances()
    if "lambda" in types:
        result["lambda_functions"] = _list_lambda_functions()
    return result
