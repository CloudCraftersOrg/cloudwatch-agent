"""Grafana dashboard tools.

The agent designs dashboards from CloudWatch discovery (metrics/logs/
resources) and publishes them to an Amazon Managed Grafana (AMG)
workspace via the standard Grafana HTTP API.

Authentication model
---------------------
AMG has no static API keys. Instead, the agent's IAM role is allowed to
call ``grafana:CreateWorkspaceServiceAccountToken`` against a Terraform-
provisioned EDITOR service account. For each tool call we mint a
short-lived token, use it as an HTTP ``Bearer`` credential, and delete
it immediately afterwards (best-effort) so no long-lived secret exists.

Four tools are exposed:

- ``get_cloudwatch_datasource``: return the CloudWatch data source UID to
  reference in dashboard panels.
- ``list_grafana_dashboards``: search existing dashboards.
- ``get_grafana_dashboard``: fetch one dashboard model by UID.
- ``put_grafana_dashboard``: create or update a dashboard.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import boto3
from strands import tool

from app.config import REGION, require_grafana_config

# Module-level Amazon Managed Grafana client, reused across invocations.
# This is the AWS control-plane client (used only to mint/delete tokens),
# NOT the Grafana data-plane HTTP API.
_grafana = boto3.client("grafana", region_name=REGION)

# Lifetime of a minted token. Kept tiny: a token only has to survive the
# one or two HTTP calls made within a single tool invocation. AMG accepts
# 1..2592000 seconds.
_TOKEN_TTL_SECONDS = 300

# Timeout for Grafana HTTP API calls. Generous enough for a large
# dashboard POST but bounded so a hung call can't stall the runtime.
_HTTP_TIMEOUT_SECONDS = 30


@contextmanager
def _grafana_token() -> Iterator[tuple[str, str]]:
    """Mint a short-lived Grafana token and clean it up afterwards.

    Yields:
        ``(base_url, token_key)`` where ``base_url`` is the workspace
        HTTPS root (no trailing slash) and ``token_key`` is the Bearer
        credential.

    The token is deleted on exit. Deletion failure is swallowed because
    the token expires on its own within ``_TOKEN_TTL_SECONDS`` anyway;
    raising there would mask the real result of the API call.
    """
    workspace_id, endpoint, service_account_id, _ = require_grafana_config()
    base_url = f"https://{endpoint}"

    created = _grafana.create_workspace_service_account_token(
        workspaceId=workspace_id,
        serviceAccountId=service_account_id,
        # Name must be unique among a service account's live tokens.
        name=f"agent-{uuid.uuid4().hex}",
        secondsToLive=_TOKEN_TTL_SECONDS,
    )
    token = created["serviceAccountToken"]
    token_id = token["id"]
    token_key = token["key"]

    try:
        yield base_url, token_key
    finally:
        try:
            _grafana.delete_workspace_service_account_token(
                workspaceId=workspace_id,
                serviceAccountId=service_account_id,
                tokenId=token_id,
            )
        except Exception:
            # Best-effort: the token self-expires shortly regardless.
            pass


def _grafana_request(
    method: str,
    path: str,
    token: str,
    base_url: str,
    body: dict[str, Any] | None = None,
) -> Any:
    """Make one authenticated call to the Grafana HTTP API.

    Args:
        method: HTTP verb (``GET``/``POST``).
        path: API path beginning with ``/`` (e.g. ``/api/search``).
        token: Bearer credential from :func:`_grafana_token`.
        base_url: Workspace HTTPS root.
        body: Optional JSON body for write calls.

    Returns:
        The parsed JSON response (dict or list).

    Raises:
        RuntimeError: On any non-2xx response, with the Grafana error
            body included so the agent can correct and retry.
    """
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        url=f"{base_url}{path}",
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT_SECONDS) as response:
            raw = response.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        # Grafana returns a JSON error body; surface it verbatim so the
        # model gets an actionable message (e.g. "title is empty").
        detail = exc.read().decode(errors="replace")
        raise RuntimeError(
            f"Grafana API {method} {path} failed ({exc.code}): {detail}"
        ) from exc


@tool
def get_cloudwatch_datasource() -> dict[str, str]:
    """Return the CloudWatch data source to use in dashboard panels.

    Every panel in a generated dashboard must point at this data source
    (by UID) so Grafana queries CloudWatch. The data source is
    provisioned by Terraform; the agent never creates it.

    Returns:
        Dict with ``uid``, ``type`` (always ``cloudwatch``), and the
        ``default_region`` panels should query unless overridden.
    """
    _, _, _, datasource_uid = require_grafana_config()
    return {
        "uid": datasource_uid,
        "type": "cloudwatch",
        "default_region": REGION,
    }


@tool
def list_grafana_dashboards(query: str | None = None) -> list[dict[str, Any]]:
    """Search existing dashboards in the Grafana workspace.

    Args:
        query: Optional case-insensitive title search. When omitted, all
            dashboards are returned (Grafana caps the page size).

    Returns:
        List of dicts with ``uid``, ``title``, ``folder_title`` (empty
        string for the General folder), and ``url`` (path relative to
        the workspace root).
    """
    # type=dash-db excludes folders from the search results.
    path = "/api/search?type=dash-db"
    if query:
        path += f"&query={urllib.parse.quote(query)}"

    with _grafana_token() as (base_url, token):
        results = _grafana_request("GET", path, token, base_url)

    return [
        {
            "uid": item["uid"],
            "title": item["title"],
            "folder_title": item.get("folderTitle", ""),
            "url": item.get("url", ""),
        }
        for item in results
    ]


@tool
def get_grafana_dashboard(uid: str) -> dict[str, Any]:
    """Fetch a single dashboard model by its UID.

    Args:
        uid: Dashboard UID (from :func:`list_grafana_dashboards`).

    Returns:
        Dict with ``uid``, ``title``, ``version`` (needed to safely
        update without clobbering concurrent edits), and ``dashboard``
        (the full Grafana dashboard model as a dict).
    """
    with _grafana_token() as (base_url, token):
        payload = _grafana_request("GET", f"/api/dashboards/uid/{uid}", token, base_url)

    dashboard = payload["dashboard"]
    return {
        "uid": dashboard.get("uid", uid),
        "title": dashboard.get("title", ""),
        "version": dashboard.get("version"),
        "dashboard": dashboard,
    }


@tool
def put_grafana_dashboard(
    dashboard: dict[str, Any] | str,
    overwrite: bool = False,
    folder_uid: str | None = None,
) -> dict[str, Any]:
    """Create or update a Grafana dashboard.

    The system prompt requires the agent to confirm with the user before
    setting ``overwrite=True`` on an existing dashboard, because that
    replaces the stored model.

    Args:
        dashboard: The Grafana dashboard model, as a dict (preferred) or
            a JSON string. Must contain at least a ``title``. For a new
            dashboard leave ``id``/``uid`` unset; to update an existing
            one include its ``uid``. Panels should reference the
            CloudWatch data source UID from
            :func:`get_cloudwatch_datasource`.
        overwrite: Allow replacing an existing dashboard with the same
            UID/title. Defaults to ``False`` so accidental clobbers fail
            loudly.
        folder_uid: Optional target folder UID. Omitted = General folder.

    Returns:
        Dict with ``uid``, ``version``, ``status``, and an absolute
        ``url`` to open the dashboard in the workspace.

    Raises:
        ValueError: If ``dashboard`` is a string that is not valid JSON,
            or is not a JSON object, or has no ``title``.
        RuntimeError: If the Grafana API rejects the dashboard.
    """
    if isinstance(dashboard, str):
        try:
            model = json.loads(dashboard)
        except json.JSONDecodeError as exc:
            raise ValueError(f"dashboard is not valid JSON: {exc}") from exc
    else:
        model = dashboard

    if not isinstance(model, dict):
        raise ValueError(
            f"dashboard must be a JSON object, got {type(model).__name__}."
        )
    if not model.get("title"):
        raise ValueError("dashboard must include a non-empty 'title'.")

    # Grafana requires `id: null` to create a new dashboard; leaving a
    # stale numeric id in the model is a common cause of silent 412s.
    model.setdefault("id", None)

    payload: dict[str, Any] = {"dashboard": model, "overwrite": overwrite}
    if folder_uid:
        payload["folderUid"] = folder_uid

    with _grafana_token() as (base_url, token):
        result = _grafana_request(
            "POST", "/api/dashboards/db", token, base_url, body=payload
        )
        return {
            "uid": result.get("uid"),
            "version": result.get("version"),
            "status": result.get("status"),
            # Grafana returns a root-relative URL; make it clickable.
            "url": f"{base_url}{result.get('url', '')}",
        }
