"""Week 2 seed: the evolved week (appended on top of week 1).

Writes ~7 days of data ending now. It does NOT delete week 1 — it layers
a changed profile so the agent, on a fresh prompt, must:

1. Regenerate the set: the ``payments`` "retrying downstream dependency"
   WARN pattern is GONE (issue fixed), so a week-1 payments dashboard
   built on it now shows nothing -> agent should replace it with one
   that reflects current payments behavior.
2. Add a new dashboard with new scope: a brand-new ``checkout`` service
   appears that did not exist in week 1.
3. Build an incident dashboard: a sharp ``orders`` outage
   (``OrderDBConnectionPoolExhausted``) is injected in a ~2h window.

Run (after week1 and after building the first dashboard set):
    uv run python -m seeds.week2
"""

from __future__ import annotations

from seeds._common import Incident, SeedSpec, ServiceProfile, run_seed

PROFILES: dict[str, ServiceProfile] = {
    # payments: the week-1 retry WARNs are essentially gone. WARN volume
    # collapses and the surviving messages are unrelated, so any panel
    # filtering on "retrying downstream dependency" is now empty.
    "payments": ServiceProfile(
        per_day={"INFO": 130, "WARN": 2, "ERROR": 3},
        templates={
            "INFO": ["payment authorized", "payment captured", "refund processed"],
            "WARN": ["deprecation: v1 charge API in use"],
            "ERROR": ["payment declined by processor"],
        },
        status={"INFO": [200, 201], "WARN": [200], "ERROR": [402]},
        latency_ms={"INFO": (40, 170), "WARN": (60, 200), "ERROR": (300, 1100)},
    ),
    # orders: healthy baseline; the incident is injected separately so it
    # stands out as a burst rather than raised background noise.
    "orders": ServiceProfile(
        per_day={"INFO": 170, "WARN": 16, "ERROR": 6},
        templates={
            "INFO": ["order created", "order fulfilled", "order shipped"],
            "WARN": ["inventory low for SKU", "address validation soft-fail"],
            "ERROR": ["order persistence error", "payment hold timeout"],
        },
        status={"INFO": [200, 201], "WARN": [200, 409], "ERROR": [500, 504]},
        latency_ms={"INFO": (30, 150), "WARN": (150, 500), "ERROR": (400, 1500)},
    ),
    # auth: a credential-stuffing WARN spike — new dominant pattern that
    # did not exist in week 1, useful for "what changed?" prompts.
    "auth": ServiceProfile(
        per_day={"INFO": 210, "WARN": 140, "ERROR": 4},
        templates={
            "INFO": ["login succeeded", "token refreshed", "logout"],
            "WARN": [
                "possible credential stuffing detected",
                "failed login attempt",
                "rate limit applied to login",
            ],
            "ERROR": ["token signing error", "identity provider unreachable"],
        },
        status={"INFO": [200], "WARN": [401, 403, 429], "ERROR": [500, 503]},
        latency_ms={"INFO": (10, 90), "WARN": (20, 150), "ERROR": (200, 800)},
    ),
    "gateway": ServiceProfile(
        per_day={"INFO": 320, "WARN": 34, "ERROR": 8},
        templates={
            "INFO": ["request routed", "cache hit", "health check ok"],
            "WARN": ["upstream 4xx passed through", "rate limit applied"],
            "ERROR": ["upstream timeout", "bad gateway from upstream"],
        },
        status={"INFO": [200, 204], "WARN": [400, 404, 429], "ERROR": [502, 504]},
        latency_ms={"INFO": (5, 60), "WARN": (20, 200), "ERROR": (250, 900)},
    ),
    # checkout: brand-new service in week 2 (new scope for the agent).
    "checkout": ServiceProfile(
        per_day={"INFO": 140, "WARN": 22, "ERROR": 9},
        templates={
            "INFO": ["cart created", "checkout started", "checkout completed"],
            "WARN": ["coupon expired", "tax service slow, using cache"],
            "ERROR": ["checkout failed: payment timeout", "cart state corrupted"],
        },
        status={"INFO": [200, 201], "WARN": [200, 409], "ERROR": [500, 504]},
        latency_ms={"INFO": (40, 220), "WARN": (200, 700), "ERROR": (500, 2000)},
    ),
}

# Sharp, unambiguous outage 3 days ago, 14:00-16:00 UTC, in orders.
INCIDENT = Incident(
    service="orders",
    day_offset=3,
    start_hour=14,
    duration_hours=2,
    count=260,
    error_code="OrderDBConnectionPoolExhausted",
    message="order DB connection pool exhausted; requests timing out",
)

SPEC = SeedSpec(
    name="week2-evolved",
    start_days_ago=7,
    num_days=7,
    profiles=PROFILES,
    incident=INCIDENT,
    notes=[
        "payments 'retrying downstream dependency' WARNs are now ABSENT "
        "-> the week-1 payments dashboard goes empty. Prompt: \"The "
        "payments dashboard is empty now; analyze current payments logs "
        "and replace it with a dashboard that reflects today's behavior.\"",
        "NEW service 'checkout' exists only in week 2. Prompt: "
        "\"Regenerate the dashboard set; add any service that now has "
        "logs but no dashboard yet.\"",
        "Incident prompt: \"There was an orders outage ~3 days ago around "
        "14:00-16:00 UTC (OrderDBConnectionPoolExhausted). Build a "
        "dedicated incident dashboard for it.\"",
    ],
)

if __name__ == "__main__":
    run_seed(SPEC)
