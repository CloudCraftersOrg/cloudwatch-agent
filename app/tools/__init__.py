"""Tool registry for the CloudWatch Agent.

The single ``TOOLS`` list is passed to the Strands ``Agent`` constructor
in ``app/main.py``. It is composed of three groups:

1. **Custom tools** (this package) — the small set of tools that aren't
   provided by either upstream MCP server:

   - ``filter_log_events`` (FilterLogEvents API): raw, lag-free reads of
     log events. The right tool for "what just happened" questions and
     for backdated seed data the moment after it's written, where the
     Insights tools would return 0 because indexing hasn't caught up.
   - ``discover_resources``: EC2 / RDS / Lambda enumeration projected
     to a compact dashboard schema.
   - ``get_cloudwatch_datasource``: one-line helper returning the
     Terraform-provisioned CloudWatch data source UID, so the agent
     does not need to ``grafana_list_datasources`` every turn.

2. **AWS Labs CloudWatch MCP server tools** (prefix ``cw_mcp_``) —
   ``describe_log_groups``, ``execute_log_insights_query``,
   ``analyze_log_group``, ``get_logs_anomaly_detectors``,
   ``get_metric_data``, ``get_metric_metadata``, ``analyze_metric``,
   ``get_active_alarms``, ``get_alarm_history``,
   ``get_recommended_metric_alarms``, plus PromQL helpers and
   Logs-Insights batch/index recommenders.

3. **Grafana Labs Grafana MCP server tools** (prefix ``grafana_``) —
   the dashboard CRUD surface (``search_dashboards``,
   ``get_dashboard_by_uid``, ``update_dashboard``, ``patch_dashboard``,
   ``get_dashboard_summary``, ``get_dashboard_property``,
   ``get_dashboard_panel_queries``), datasource helpers
   (``list_datasources``, ``get_datasource``, ``get_query_examples``),
   annotation tools, deeplink builder, and panel-image renderer.

Either MCP server may be missing if the binary is unavailable in local
dev or its initialization fails; in that case the corresponding tool
list is empty and the rest of the agent keeps working.
"""

from __future__ import annotations

from app.mcp_clients import CLOUDWATCH_MCP_TOOLS, GRAFANA_MCP_TOOLS
from app.tools.dashboards import get_cloudwatch_datasource
from app.tools.judge import judge_dashboard_quality
from app.tools.logs import filter_log_events
from app.tools.resources import discover_resources

TOOLS = [
    # Custom: lag-free log reads.
    filter_log_events,
    # Custom: AWS resource discovery.
    discover_resources,
    # Custom: CloudWatch data source UID lookup (cheaper than calling
    # grafana_list_datasources every dashboard build).
    get_cloudwatch_datasource,
    # Custom: LLM-as-judge for the dashboard JSON. The system prompt
    # requires this to be called before grafana_update_dashboard.
    judge_dashboard_quality,
    # AWS Labs CloudWatch MCP server (Insights, metrics, alarms, ...).
    *CLOUDWATCH_MCP_TOOLS,
    # Grafana Labs Grafana MCP server (dashboards, datasources, ...).
    *GRAFANA_MCP_TOOLS,
]

__all__ = ["TOOLS"]
