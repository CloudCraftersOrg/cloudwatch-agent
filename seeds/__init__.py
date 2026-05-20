"""Demo data seeds for the CloudWatch Agent.

These scripts are operator-run *demo setup*, not part of the agent. They
populate a single CloudWatch Logs log group with two weeks of synthetic,
structured JSON log events so the agent can later read them via
CloudWatch Logs Insights and build Grafana dashboards on prompt.

Run order for the demo:
    uv run python -m seeds.week1   # baseline week  (~13..6 days ago)
    # ...prompt the agent to build the first dashboard set...
    uv run python -m seeds.week2   # evolved week   (~7..0 days ago)
    # ...prompt the agent to regenerate the set + add an incident board...

Run each script exactly ONCE. Re-runs are not idempotent (timestamps are
recomputed from the wall clock, so events are duplicated). week2 is meant
to append to week1; to restart the whole demo, delete the log group first
(`aws logs delete-log-group --log-group-name /cloudwatch-agent/demo`).
"""
