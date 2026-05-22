"""Tool registry for the CloudWatch Agent.

The single ``TOOLS`` list is passed to the Strands ``Agent`` constructor
in ``app/main.py``. It composes three groups:

1. **Custom tools** (this package):

   - ``filter_log_events`` — FilterLogEvents API: raw, lag-free reads
     of log events. The right tool for "what just happened" questions.
   - ``get_data_window`` — actual oldest/newest event timestamps for
     a log group plus a recommended Grafana ``time.from`` value. The
     agent calls this before building dashboards so the time range
     always covers real data.
   - ``discover_resources`` — EC2 / RDS / Lambda enumeration projected
     to a compact dashboard schema.
   - ``rank_services_by_priority`` — composite (count + rate +
     latency) score per service. The agent uses the top 4 to pick the
     per-service dashboards (the 5th slot is the overview).
   - ``get_cloudwatch_datasource`` — one-line helper returning the
     Terraform-provisioned CloudWatch data source UID, so the agent
     does not need to ``grafana_list_datasources`` every turn.
   - ``judge_dashboard_quality`` — LLM-as-judge plus data-plane
     validation (runs each panel's Insights query) before publish.
   - ``delete_grafana_dashboard`` — direct ``DELETE`` against the
     Grafana API (the MCP does not expose one).
   - ``prune_dashboards_to_top_set`` — enforcement point for the
     "exactly 5 dashboards" invariant; deletes ``cwagent-*``
     dashboards not in the canonical set.

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

Either MCP server may be missing if its binary is unavailable in local
dev or its initialization fails; the corresponding tool list is then
empty and the rest of the agent keeps working.
"""

from __future__ import annotations

from app.mcp_clients import CLOUDWATCH_MCP_TOOLS, GRAFANA_MCP_TOOLS
from app.tools.dashboards import (
    delete_grafana_dashboard,
    get_cloudwatch_datasource,
    prune_dashboards_to_top_set,
)
from app.tools.judge import judge_dashboard_quality
from app.tools.logs import filter_log_events, get_data_window
from app.tools.ranking import rank_services_by_priority
from app.tools.resources import discover_resources

TOOLS = [
    # Custom: lag-free log reads.
    filter_log_events,
    # Custom: actual data time-range discovery for correct
    # dashboard time windows.
    get_data_window,
    # Custom: AWS resource discovery (EC2 / RDS / Lambda).
    discover_resources,
    # Custom: per-service priority scoring for the top-5 dashboard set.
    rank_services_by_priority,
    # Custom: CloudWatch data source UID lookup.
    get_cloudwatch_datasource,
    # Custom: LLM-as-judge + data-plane validation before publish.
    judge_dashboard_quality,
    # Custom: Grafana dashboard delete (not in the MCP) and the
    # top-set enforcer that uses it.
    delete_grafana_dashboard,
    prune_dashboards_to_top_set,
    # AWS Labs CloudWatch MCP server (Insights, metrics, alarms, ...).
    *CLOUDWATCH_MCP_TOOLS,
    # Grafana Labs Grafana MCP server (dashboards, datasources, ...).
    *GRAFANA_MCP_TOOLS,
]

__all__ = ["TOOLS"]
