"""Grafana dashboard glue (the heavy lifting is now done by the Grafana MCP).

The Grafana MCP server (Grafana Labs ``mcp-grafana``, spawned by
``app/mcp_clients.py``) provides the full dashboard CRUD surface:
``grafana_search_dashboards``, ``grafana_get_dashboard_by_uid``,
``grafana_update_dashboard`` (create + update), ``grafana_patch_dashboard``,
``grafana_list_datasources``, ``grafana_get_dashboard_summary``,
``grafana_get_dashboard_property``, ``grafana_get_dashboard_panel_queries``,
plus annotation and panel-image helpers. We no longer need the custom
``list_grafana_dashboards`` / ``get_grafana_dashboard`` /
``put_grafana_dashboard`` tools that used to live here.

One tiny custom tool remains: ``get_cloudwatch_datasource``. It returns
the UID of the CloudWatch data source Terraform provisioned in the
workspace. Without it, the agent would have to call
``grafana_list_datasources`` every turn and filter for the CloudWatch
entry — one extra round-trip on every dashboard generation. Keeping
this one-liner saves the agent a turn and makes the prompt's "set every
panel's datasource to that UID" rule cheap to follow.
"""

from __future__ import annotations

from strands import tool

from app.config import REGION, require_grafana_config


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
