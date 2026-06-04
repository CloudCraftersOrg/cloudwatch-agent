"""Demo data seeds for the CloudWatch Agent.

These scripts are operator-run *demo setup*, not part of the agent. They
populate a single CloudWatch Logs log group with successive waves of
synthetic, structured JSON log events so the agent can later read them
via FilterLogEvents + CloudWatch Logs Insights and build (then
re-balance) Grafana dashboards on prompt.

Each "week" script lays down events spread uniformly over the last
``WINDOW_MINUTES`` minutes (see ``seeds/_common.py``). Running the next
one APPENDS more events into the same window — the data planes
(FilterLogEvents and Insights) always see "everything ingested in the
last hour". No backdating.

Run order for the full demo:
    uv run python -m seeds.week1   # baseline                    (5 services)
    # ...prompt the agent to build the first dashboard set...
    uv run python -m seeds.week2   # evolved + first incident    (+1 service)
    # ...prompt the agent to regenerate + add an incident board...
    uv run python -m seeds.week3   # rebalance + new world       (+2 services, -2 deprecated)
    # ...prompt the agent to rebalance: retire deprecated dashboards
    #    and promote the new high-error services to the top...

Run each script exactly ONCE per "phase". Re-runs of the same script
APPEND duplicate data (timestamps are recomputed from the wall clock).
To reset cleanly, delete the per-service STREAMS (not the log group
itself — that would also reset its creationTime, which Logs Insights
uses as the lower bound for indexing):

    for s in payments orders auth gateway checkout risk identity; do
      aws logs delete-log-stream --log-group-name /cloudwatch-agent/demo \\
        --log-stream-name "$s" --region us-west-2 2>/dev/null
    done
"""
