"""Week 3 seed: the rebalance phase (appended on top of weeks 1 and 2).

Writes another wave of events into the same log group, spread uniformly
over the last ``WINDOW_MINUTES`` minutes (see ``seeds/_common.py``).
Does NOT delete prior streams — it layers a *new world* on top of the
existing data so the agent, on a fresh prompt, must:

1. Deprecate two old services. ``payments`` and ``auth`` drop to nearly
   zero traffic and the only messages left are deprecation WARNs
   ("service deprecated: traffic migrated to <new>"). Any week-1 or
   week-2 dashboard pointed at them now shows a flatline + a couple
   of WARN spikes.
2. Promote two brand-new services. ``risk`` (fraud / risk engine) and
   ``identity`` (new auth replacement) come online with **error rates
   well above any week-1/week-2 service** — both > 40% ERROR, latency
   tails into the 3-4 second range. When the agent regenerates the
   dashboard set, these two MUST end up on top because the ranking
   signals (error rate, error count, latency) all point at them.
3. Surface a sharp incident on ``risk``: a tight, bigger-than-week2
   ERROR burst (``RiskModelInferencePoolExhausted``) inside the same
   recent window — the new incident-dashboard candidate.

Run (after week1, week2, and after building / regenerating dashboards):
    uv run python -m seeds.week3
"""

from __future__ import annotations

from seeds._common import Incident, SeedSpec, ServiceProfile, run_seed

PROFILES: dict[str, ServiceProfile] = {
    # --- DEPRECATED -----------------------------------------------------
    # payments: in week 2 we already emitted a single "deprecation: v1
    # charge API in use" WARN. In week 3 the migration is essentially
    # complete: volume collapses to almost nothing and every surviving
    # event carries a deprecation message. A dashboard built on the old
    # payments behavior is now an empty flatline with a few WARNs — the
    # signal the agent needs to *retire* that dashboard.
    "payments": ServiceProfile(
        per_day={"INFO": 4, "WARN": 8, "ERROR": 1},
        templates={
            "INFO": ["payment authorized (legacy path)"],
            "WARN": [
                "service deprecated: traffic migrated to risk-engine",
                "legacy payments client still calling /v1/charge",
            ],
            "ERROR": ["legacy payments backend unreachable"],
        },
        status={"INFO": [200], "WARN": [200, 410], "ERROR": [503]},
        latency_ms={"INFO": (40, 170), "WARN": (50, 200), "ERROR": (400, 1200)},
    ),
    # auth: same story. Replaced by the new ``identity`` service.
    # Volume drops by ~20x relative to week 2 and the surviving messages
    # are all deprecation noise.
    "auth": ServiceProfile(
        per_day={"INFO": 6, "WARN": 10, "ERROR": 1},
        templates={
            "INFO": ["legacy session validated"],
            "WARN": [
                "service deprecated: migrate clients to identity-service",
                "legacy auth client detected",
            ],
            "ERROR": ["legacy auth backend unreachable"],
        },
        status={"INFO": [200], "WARN": [200, 410], "ERROR": [503]},
        latency_ms={"INFO": (10, 90), "WARN": (20, 120), "ERROR": (200, 800)},
    ),
    # --- SURVIVING (carry forward, similar profile) ---------------------
    # orders: healthy baseline, same shape as week 2. The week-2 incident
    # is over; orders is back to normal.
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
    # gateway: routing layer, unchanged.
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
    # checkout: still running, slight uptick in INFO from the new
    # identity/risk integrations adding handshake hops.
    "checkout": ServiceProfile(
        per_day={"INFO": 160, "WARN": 22, "ERROR": 9},
        templates={
            "INFO": ["cart created", "checkout started", "checkout completed"],
            "WARN": ["coupon expired", "tax service slow, using cache"],
            "ERROR": ["checkout failed: payment timeout", "cart state corrupted"],
        },
        status={"INFO": [200, 201], "WARN": [200, 409], "ERROR": [500, 504]},
        latency_ms={"INFO": (40, 220), "WARN": (200, 700), "ERROR": (500, 2000)},
    ),
    # --- NEW (high-error, latency-heavy — the rebalance signal) ---------
    # risk: fraud / risk-engine that replaces the legacy payments scoring
    # path. Per-window ERROR volume (~180/d × 7) is an order of magnitude
    # above any surviving service, and the ERROR latency tail reaches
    # 4 s — the worst tail in the seed. These are the signals the agent
    # should use to promote risk to the top of the dashboard set.
    "risk": ServiceProfile(
        per_day={"INFO": 100, "WARN": 70, "ERROR": 180},
        templates={
            "INFO": ["transaction approved", "risk score computed"],
            "WARN": [
                "risk score borderline, manual review queued",
                "rule engine soft-fail, falling back to baseline",
                "feature store stale, using last-known good",
            ],
            "ERROR": [
                "fraud model inference timeout",
                "external risk provider 5xx",
                "rule engine evaluation failed",
                "feature store unreachable",
            ],
        },
        status={"INFO": [200], "WARN": [200, 202], "ERROR": [500, 502, 504]},
        latency_ms={"INFO": (50, 220), "WARN": (200, 600), "ERROR": (1500, 4000)},
    ),
    # identity: new auth platform that replaces ``auth``. Also lots of
    # ERRORS (~200/d × 7) — the rotation/migration is rough. Latency tail
    # to 3 s. Together with ``risk``, these two services should dominate
    # any "what's broken right now" view the agent assembles.
    "identity": ServiceProfile(
        per_day={"INFO": 130, "WARN": 60, "ERROR": 200},
        templates={
            "INFO": ["token issued", "session validated", "MFA verified"],
            "WARN": [
                "token nearly expired, refresh recommended",
                "legacy auth client migrated to identity",
                "session store eventual-consistency lag observed",
            ],
            "ERROR": [
                "token signing key rotation failed",
                "session store write conflict",
                "OIDC provider unreachable",
                "MFA challenge expired before response",
            ],
        },
        status={"INFO": [200], "WARN": [200, 401], "ERROR": [500, 503, 504]},
        latency_ms={"INFO": (20, 120), "WARN": (50, 250), "ERROR": (800, 3000)},
    ),
}

# Incident: a sharp burst on the NEW risk service. Bigger than the
# week-2 orders incident (260) — 480 events — because it's the new
# service melting down and we want the dashboard ranking to be
# unambiguous. The error_code stays in the "Pool exhausted" vocabulary
# we used for the week-2 incident for continuity.
INCIDENT = Incident(
    service="risk",
    count=480,
    error_code="RiskModelInferencePoolExhausted",
    message=(
        "risk model inference pool exhausted; falling back to "
        "conservative deny on all transactions"
    ),
)

SPEC = SeedSpec(
    name="week3-rebalance",
    num_days=7,
    profiles=PROFILES,
    incident=INCIDENT,
    notes=[
        "DEPRECATED in week 3: 'payments' and 'auth' collapsed to ~1% of "
        "their week-2 volume; surviving events are deprecation WARNs. "
        "Prompt: \"Two services were deprecated this week; the existing "
        "dashboards for them are now mostly empty. Retire or shrink them "
        "and surface only what still has signal.\"",
        "NEW in week 3: 'risk' and 'identity'. Both have ERROR counts "
        "and latency tails well above any surviving service. Prompt: "
        "\"Regenerate the dashboard set with the current data; the most "
        "prominent dashboards should be the services with the worst "
        "current signals (highest error rate, highest latency, biggest "
        "absolute error volume).\"",
        "Incident prompt: \"There was a recent risk-engine outage "
        "(RiskModelInferencePoolExhausted) in the last few minutes — "
        "see the printed window above. Build a dedicated incident "
        "dashboard for it.\"",
    ],
)

if __name__ == "__main__":
    run_seed(SPEC)
