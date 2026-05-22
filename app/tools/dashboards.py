"""Grafana dashboard helpers complementing the Grafana MCP.

The Grafana MCP server (``mcp-grafana``, spawned by
``app/mcp_clients.py``) provides the dashboard CRUD surface
(``grafana_search_dashboards``, ``grafana_get_dashboard_by_uid``,
``grafana_update_dashboard``, ``grafana_patch_dashboard``, …) and
datasource helpers. Three things the MCP does NOT cover that we add
here as custom tools:

- ``get_cloudwatch_datasource`` — returns the Terraform-provisioned
  CloudWatch data source UID. The agent reuses this on every
  dashboard build instead of paying for ``grafana_list_datasources``.
- ``delete_grafana_dashboard`` — direct HTTP DELETE against the
  Grafana API. ``mcp-grafana`` v0.7.0 does not expose a delete tool,
  so we hit ``DELETE /api/dashboards/uid/{uid}`` ourselves with the
  same EDITOR token the MCP uses.
- ``prune_dashboards_to_top_set`` — the auto-pruning enforcement
  point. Given the canonical set of UIDs that should exist, it
  deletes every ``cwagent-*`` dashboard that is NOT in the set. The
  agent calls this once after publishing the top-5 set to drop
  dashboards for services that lost their slot.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any

from strands import tool

from app.config import REGION, require_grafana_config
from app.mcp_clients import get_grafana_agent_token

logger = logging.getLogger(__name__)

# Prefix on every dashboard the agent owns. ``prune_dashboards_to_top_set``
# only ever deletes UIDs starting with this — guards against accidentally
# removing dashboards a human created outside the agent.
_AGENT_DASHBOARD_UID_PREFIX = "cwagent-"
# The overview UID is always part of the canonical top-5 set, so it is
# automatically preserved by the prune routine even if the caller forgets
# to include it in ``keep_uids``.
_OVERVIEW_UID = "cwagent-overview"
# Incident dashboards are out-of-band by design (created on user
# request for a specific outage) and live alongside the canonical 5.
# Auto-preserved by the prune routine so the model never has to
# remember to enumerate every active incident in ``keep_uids``.
_INCIDENT_UID_PREFIX = "cwagent-incident-"


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


def _grafana_api(method: str, path: str) -> tuple[int, str]:
    """Call the Grafana HTTP API with the cached EDITOR token.

    Returns ``(status_code, body_text)``. Raises ``RuntimeError`` if
    there is no token (the MCP failed to start, so we have no
    credentials) — that case is unrecoverable from a tool call.
    """
    _, endpoint, _, _ = require_grafana_config()
    token = get_grafana_agent_token()
    if not token:
        raise RuntimeError(
            "No Grafana token available — the Grafana MCP did not "
            "start successfully at container boot. Check the runtime "
            "logs for the mint failure."
        )
    req = urllib.request.Request(
        url=f"https://{endpoint}{path}",
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")


@tool
def delete_grafana_dashboard(uid: str) -> dict[str, Any]:
    """Delete a Grafana dashboard by UID.

    Wraps ``DELETE /api/dashboards/uid/{uid}`` since the Grafana MCP
    server does not expose a delete tool. Used by
    ``prune_dashboards_to_top_set`` to drop dashboards that fell out
    of the top set, and can also be called directly by the agent when
    a user explicitly asks to remove a dashboard.

    Args:
        uid: Dashboard UID, e.g. ``"cwagent-svc-orders"``. Must start
            with the ``cwagent-`` prefix — this tool refuses to delete
            dashboards outside the agent's namespace as a safety guard
            against the model trying to remove dashboards a human
            owns.

    Returns:
        Dict with:
        - ``ok`` (bool): True only on HTTP 200.
        - ``status`` (int): HTTP status code.
        - ``uid`` (str): echoed back so the agent can correlate.
        - ``error`` (str, optional): present when not ok.
    """
    if not uid.startswith(_AGENT_DASHBOARD_UID_PREFIX):
        return {
            "ok": False,
            "status": 0,
            "uid": uid,
            "error": (
                f"refusing to delete UID '{uid}': only dashboards with "
                f"prefix '{_AGENT_DASHBOARD_UID_PREFIX}' are managed by "
                "the agent."
            ),
        }
    try:
        status, body = _grafana_api("DELETE", f"/api/dashboards/uid/{uid}")
    except RuntimeError as exc:
        return {"ok": False, "status": 0, "uid": uid, "error": str(exc)}

    ok = 200 <= status < 300
    result: dict[str, Any] = {"ok": ok, "status": status, "uid": uid}
    if not ok:
        result["error"] = body[:300]
    return result


def _list_agent_dashboards() -> list[dict[str, Any]]:
    """Return every dashboard with the agent's UID prefix.

    Uses ``GET /api/search?type=dash-db&query=cwagent`` and filters
    results to those whose ``uid`` actually starts with the prefix —
    the search endpoint matches title substrings too, so the prefix
    filter is necessary to keep this strict.
    """
    status, body = _grafana_api(
        "GET", "/api/search?type=dash-db&query=cwagent&limit=200"
    )
    if not (200 <= status < 300):
        raise RuntimeError(
            f"Grafana search returned HTTP {status}: {body[:200]}"
        )
    try:
        items = json.loads(body)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Grafana search returned non-JSON: {exc}") from None
    return [
        item for item in items
        if isinstance(item, dict)
        and isinstance(item.get("uid"), str)
        and item["uid"].startswith(_AGENT_DASHBOARD_UID_PREFIX)
    ]


@tool
def prune_dashboards_to_top_set(keep_uids: list[str]) -> dict[str, Any]:
    """Delete every agent-owned dashboard NOT in ``keep_uids``.

    This is the enforcement point for the canonical-5 invariant:
    after publishing the new top set, call this with the five UIDs
    that should survive (overview + 4 service dashboards) and any
    other ``cwagent-*`` dashboard from a prior turn that is not in
    that list gets deleted.

    Two protections are auto-applied so the model can't accidentally
    nuke critical dashboards by forgetting to list them:

    - The overview UID (``cwagent-overview``) is always preserved.
    - Any UID starting with ``cwagent-incident-`` is always
      preserved. Incident dashboards live alongside the canonical 5
      and are managed explicitly (use ``delete_grafana_dashboard``
      when an incident is resolved).

    Args:
        keep_uids: UIDs to preserve. Anything outside the agent's
            ``cwagent-`` prefix is ignored on input (the agent only
            manages its own namespace).

    Returns:
        Dict with:
        - ``kept`` (list[str]): UIDs that were left alone (includes
          auto-preserved overview / incident dashboards).
        - ``deleted`` (list[str]): UIDs that were successfully
          deleted by this call.
        - ``failed`` (list[dict]): per-UID delete failures with the
          HTTP status and error body.
        - ``inspected_total`` (int): how many ``cwagent-*`` dashboards
          existed before the prune (for sanity-checking).
    """
    keep_set = {u for u in keep_uids if u.startswith(_AGENT_DASHBOARD_UID_PREFIX)}
    keep_set.add(_OVERVIEW_UID)

    try:
        existing = _list_agent_dashboards()
    except RuntimeError as exc:
        return {
            "kept": sorted(keep_set),
            "deleted": [],
            "failed": [],
            "inspected_total": 0,
            "error": str(exc),
        }

    kept: list[str] = []
    deleted: list[str] = []
    failed: list[dict[str, Any]] = []

    for item in existing:
        uid = item["uid"]
        # Explicit keep list, overview, or any active incident.
        if uid in keep_set or uid.startswith(_INCIDENT_UID_PREFIX):
            kept.append(uid)
            continue
        outcome = delete_grafana_dashboard(uid)
        if outcome.get("ok"):
            deleted.append(uid)
        else:
            failed.append(
                {
                    "uid": uid,
                    "status": outcome.get("status"),
                    "error": outcome.get("error"),
                }
            )

    return {
        "kept": sorted(kept),
        "deleted": sorted(deleted),
        "failed": failed,
        "inspected_total": len(existing),
    }
