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
to append to week1.

DO NOT delete the log group between runs. Logs Insights only indexes
events whose timestamp is >= the log group's creationTime; the seeds
ingest events with timestamps 6-13 days in the past, so any event
written right after recreating the log group is invisible to Insights
(``aws logs start-query`` returns MalformedQueryException, while
FilterLogEvents and the console "Log events" tab still see the events).
To start over cleanly, delete the individual streams instead:

    for s in payments orders auth gateway checkout; do
      aws logs delete-log-stream --log-group-name /cloudwatch-agent/demo \\
        --log-stream-name "$s" --region us-east-1 2>/dev/null
    done

That keeps the log group's creationTime old and Logs Insights happy.
"""
