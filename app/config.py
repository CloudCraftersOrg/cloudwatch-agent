"""Runtime configuration for the CloudWatch Agent.

All values are read from environment variables so the same container image
can be promoted across environments unchanged. Defaults are chosen so the
agent runs out-of-the-box for local development; production overrides are
injected by Terraform via ``aws_bedrockagentcore_agent_runtime``'s
``environment_variables``.

Environment variables
---------------------
AWS_REGION : str, optional
    AWS region used for every boto3 client and for the Bedrock model
    inference profile prefix. Defaults to ``us-west-2`` because that is
    the only region this project targets.
MODEL_ID : str, optional
    Bedrock model identifier passed to the Strands ``Agent``. Defaults
    to the cross-region inference profile for Claude Opus 4.6 in the
    US commercial regions. Switch to a Sonnet-class profile to cut cost.
MEMORY_ID : str, optional
    Identifier of the AgentCore Memory resource provisioned by Terraform.
    When unset (typical for local development), the agent skips the
    AgentCore Memory session manager and falls back to in-process state.
    The agent degrades gracefully — it never hard-fails on a missing
    MEMORY_ID — so production simply gets memory because Terraform sets it.
GRAFANA_WORKSPACE_ID : str, required for dashboard tools
    Amazon Managed Grafana workspace ID. Used with the AWS Grafana API to
    mint a per-session service-account token.
GRAFANA_WORKSPACE_ENDPOINT : str, required for dashboard tools
    Workspace host (no scheme), e.g. ``g-abc123.grafana-workspace...``.
    The Grafana HTTP API base URL is ``https://<endpoint>``.
GRAFANA_SERVICE_ACCOUNT_ID : str, required for dashboard tools
    ID of the Terraform-created agent service account. Tokens are minted
    against this account; the agent cannot create the account itself.
GRAFANA_CLOUDWATCH_DATASOURCE_UID : str, required for dashboard tools
    UID of the CloudWatch data source provisioned by Terraform. Dashboard
    panels must reference this UID so Grafana queries CloudWatch.
"""

from __future__ import annotations

import os

# Default AWS region. Hardcoded fallback matches the single region this
# project targets; the env-var override exists so the same image can be
# tested against other regions during local development if needed.
REGION: str = os.environ.get("AWS_REGION", "us-west-2")

# Default Bedrock model ID for the main agent. Cross-region inference
# profile for Claude Sonnet 4.6 in the US partition (the ``us.``
# prefix). Sonnet 4.6 is ~2× faster than Opus 4.6 per inference and
# handles this agent's structured workflow well; pick Opus only when
# you specifically want its open-ended reasoning depth.
#
# On-demand invocation of the bare foundation model is NOT supported
# — Bedrock returns ValidationException requesting an inference
# profile instead. The profile fans out to us-east-1, us-east-2,
# and us-west-2; model access must be enabled in all three (see
# README Prerequisites).
#
# Note on naming: Sonnet 4.6 uses the NEW short ID form
# (no ``-v1`` / no ``:0``). Older Anthropic models in this codebase
# (Opus 4.6, Haiku 4.5) still use the longer form. Always copy the
# exact ID from the model's Bedrock detail page before changing it.
#
# To revert to Opus 4.6 (slower, deeper reasoning):
#   MODEL_ID=us.anthropic.claude-opus-4-6-v1
MODEL_ID: str = os.environ.get("MODEL_ID", "us.anthropic.claude-sonnet-4-6")

# AgentCore Memory ID. Optional locally so contributors don't need to
# provision real AWS resources just to iterate on prompts or tool code.
# In production, Terraform wires this in via the runtime's environment.
MEMORY_ID: str | None = os.environ.get("MEMORY_ID")

# Grafana wiring. All optional at import time so the module loads in local
# dev without AWS resources; the dashboard tools validate them lazily on
# first use via require_grafana_config(). In production Terraform sets all
# four (see terraform/runtime.tf).
GRAFANA_WORKSPACE_ID: str | None = os.environ.get("GRAFANA_WORKSPACE_ID")
GRAFANA_WORKSPACE_ENDPOINT: str | None = os.environ.get("GRAFANA_WORKSPACE_ENDPOINT")
GRAFANA_SERVICE_ACCOUNT_ID: str | None = os.environ.get("GRAFANA_SERVICE_ACCOUNT_ID")
GRAFANA_CLOUDWATCH_DATASOURCE_UID: str | None = os.environ.get("GRAFANA_CLOUDWATCH_DATASOURCE_UID")


def require_grafana_config() -> tuple[str, str, str, str]:
    """Return the four Grafana settings or raise if any is missing.

    The dashboard tools call this lazily (not at import time) so that the
    rest of the agent still runs locally without a Grafana workspace.

    Returns:
        ``(workspace_id, endpoint, service_account_id, datasource_uid)``.

    Raises:
        RuntimeError: If any required Grafana variable is unset, with a
            message pointing at where it comes from (Terraform).
    """
    missing = [
        name
        for name, value in (
            ("GRAFANA_WORKSPACE_ID", GRAFANA_WORKSPACE_ID),
            ("GRAFANA_WORKSPACE_ENDPOINT", GRAFANA_WORKSPACE_ENDPOINT),
            ("GRAFANA_SERVICE_ACCOUNT_ID", GRAFANA_SERVICE_ACCOUNT_ID),
            ("GRAFANA_CLOUDWATCH_DATASOURCE_UID", GRAFANA_CLOUDWATCH_DATASOURCE_UID),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(
            f"Missing Grafana environment variable(s): {', '.join(missing)}. "
            "These are set automatically by Terraform on the AgentCore "
            "Runtime; see terraform/runtime.tf and terraform/grafana.tf."
        )
    # Narrow Optional[str] -> str for type checkers; the guard above proves
    # none of these are None.
    return (
        GRAFANA_WORKSPACE_ID,  # type: ignore[return-value]
        GRAFANA_WORKSPACE_ENDPOINT,  # type: ignore[return-value]
        GRAFANA_SERVICE_ACCOUNT_ID,  # type: ignore[return-value]
        GRAFANA_CLOUDWATCH_DATASOURCE_UID,  # type: ignore[return-value]
    )
