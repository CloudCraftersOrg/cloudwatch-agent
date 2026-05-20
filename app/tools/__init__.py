"""Tool registry for the CloudWatch Agent.

This module re-exports a single ``TOOLS`` list that is passed to the
Strands ``Agent`` constructor in ``app/main.py``. Keeping the registry
here means new tools only need to be appended to one list (instead of
being added in two places: the module that defines them and the main
file that wires them up).
"""

from __future__ import annotations

from app.tools.dashboards import (
    get_cloudwatch_datasource,
    get_grafana_dashboard,
    list_grafana_dashboards,
    put_grafana_dashboard,
)
from app.tools.logs import list_log_groups, run_logs_insights_query
from app.tools.metrics import get_metric_data, list_metrics, list_namespaces
from app.tools.resources import discover_resources

# Order is preserved when the Agent advertises tools to the model. We
# group by capability (metrics, logs, resources, Grafana dashboards) so
# the system prompt's narrative matches the order the LLM sees. Total
# tool count: 10 (at the 10-tool cap in the brief).
TOOLS = [
    # CloudWatch metric introspection (used to design dashboards).
    list_namespaces,
    list_metrics,
    get_metric_data,
    # CloudWatch log introspection.
    list_log_groups,
    run_logs_insights_query,
    # Resource discovery (single tool that fans out to EC2, RDS, Lambda).
    discover_resources,
    # Grafana dashboard CRUD (published to Amazon Managed Grafana).
    get_cloudwatch_datasource,
    list_grafana_dashboards,
    get_grafana_dashboard,
    put_grafana_dashboard,
]

__all__ = ["TOOLS"]
