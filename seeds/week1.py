"""Week 1 seed: the baseline.

Writes ~7 days of data ending ~6 days ago (so it stays well under the
14-day CloudWatch Logs backdating limit while still being a distinct
calendar week from week 2).

The shape here is deliberately steady so the first dashboard set the
agent builds has stable, meaningful panels:

* ``payments`` has a recurring WARN pattern ("retrying downstream
  dependency"). Week 2 makes this pattern disappear, which is what makes
  a week-1 payments dashboard go empty and forces the agent to
  regenerate it.
* ``orders``, ``auth``, ``gateway`` have low, healthy error rates.
* No incident.

Run:
    uv run python -m seeds.week1
"""

from __future__ import annotations

from seeds._common import SeedSpec, ServiceProfile, run_seed

# Baseline per-service behavior. Counts are per level per day (jittered
# +/-30% by the generator).
PROFILES: dict[str, ServiceProfile] = {
    "payments": ServiceProfile(
        per_day={"INFO": 120, "WARN": 40, "ERROR": 4},
        templates={
            "INFO": [
                "payment authorized",
                "payment captured",
                "refund processed",
            ],
            # This WARN family is the signal week 2 removes on purpose.
            "WARN": [
                "retrying downstream dependency: card-network",
                "retrying downstream dependency: ledger",
                "slow response from card-network, retrying",
            ],
            "ERROR": [
                "payment declined by processor",
                "ledger write failed, will retry",
            ],
        },
        status={"INFO": [200, 201], "WARN": [200, 429], "ERROR": [402, 502]},
        latency_ms={"INFO": (40, 180), "WARN": (180, 600), "ERROR": (300, 1200)},
    ),
    "orders": ServiceProfile(
        per_day={"INFO": 160, "WARN": 18, "ERROR": 5},
        templates={
            "INFO": ["order created", "order fulfilled", "order shipped"],
            "WARN": ["inventory low for SKU", "address validation soft-fail"],
            "ERROR": ["order persistence error", "payment hold timeout"],
        },
        status={"INFO": [200, 201], "WARN": [200, 409], "ERROR": [500, 504]},
        latency_ms={"INFO": (30, 150), "WARN": (150, 500), "ERROR": (400, 1500)},
    ),
    "auth": ServiceProfile(
        per_day={"INFO": 200, "WARN": 25, "ERROR": 3},
        templates={
            "INFO": ["login succeeded", "token refreshed", "logout"],
            "WARN": ["failed login attempt", "MFA challenge issued"],
            "ERROR": ["token signing error", "identity provider unreachable"],
        },
        status={"INFO": [200], "WARN": [401, 403], "ERROR": [500, 503]},
        latency_ms={"INFO": (10, 90), "WARN": (20, 120), "ERROR": (200, 800)},
    ),
    "gateway": ServiceProfile(
        per_day={"INFO": 300, "WARN": 30, "ERROR": 6},
        templates={
            "INFO": ["request routed", "cache hit", "health check ok"],
            "WARN": ["upstream 4xx passed through", "rate limit applied"],
            "ERROR": ["upstream timeout", "bad gateway from upstream"],
        },
        status={"INFO": [200, 204], "WARN": [400, 404, 429], "ERROR": [502, 504]},
        latency_ms={"INFO": (5, 60), "WARN": (20, 200), "ERROR": (250, 900)},
    ),
}

SPEC = SeedSpec(
    name="week1-baseline",
    start_days_ago=13,
    num_days=7,
    profiles=PROFILES,
    incident=None,
    notes=[
        "Baseline week written. Suggested prompt: \"Look at the logs in "
        "the /cloudwatch-agent/demo log group for the last 14 days and "
        "create a Grafana dashboard set: one overview plus one dashboard "
        "per service (payments, orders, auth, gateway).\"",
        "Remember the 'payments' WARN 'retrying downstream dependency' "
        "pattern — week 2 removes it so that dashboard goes stale.",
    ],
)

if __name__ == "__main__":
    run_seed(SPEC)
