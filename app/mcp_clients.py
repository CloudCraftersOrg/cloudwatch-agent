"""Wires MCP servers into the agent.

Importing this module spawns two MCP subprocesses that live for the
full lifetime of the container:

- **CloudWatch MCP** (AWS Labs ``awslabs.cloudwatch-mcp-server``): a
  Python package run as
  ``python -u -m awslabs.cloudwatch_mcp_server.server``.

- **Grafana MCP** (Grafana Labs ``mcp-grafana``): a Go binary baked
  into the image at ``/usr/local/bin/mcp-grafana``. It needs a
  service-account token; we mint one via the Amazon Managed Grafana
  API at startup, pass it to the subprocess as an env var, and let
  ``atexit`` delete it on shutdown.

If either fails to start (binary missing, AWS credentials absent,
etc.) we log a warning and that server's tool list stays empty; the
rest of the agent keeps working.
"""

from __future__ import annotations

import atexit
import logging
import sys
import uuid
from datetime import UTC, datetime

import boto3
from mcp import StdioServerParameters
from mcp.client.stdio import stdio_client
from strands.tools.mcp import MCPClient

from app.config import (
    GRAFANA_SERVICE_ACCOUNT_ID,
    GRAFANA_WORKSPACE_ENDPOINT,
    GRAFANA_WORKSPACE_ID,
    REGION,
)

logger = logging.getLogger(__name__)


# CloudWatch MCP
#
# We launch with ``python -u -m ...`` instead of the console script:
# - sys.executable avoids depending on the venv's PATH (works the
#   same in local dev, container, and tests).
# - ``-u`` + PYTHONUNBUFFERED=1 force unbuffered stdio. Without this,
#   Python buffers per line, the MCP handshake response gets trapped
#   in the subprocess buffer, and Strands times out at 30 seconds
#   waiting for a response that never comes.
_cloudwatch_mcp = MCPClient(
    lambda: stdio_client(
        StdioServerParameters(
            command=sys.executable,
            args=["-u", "-m", "awslabs.cloudwatch_mcp_server.server"],
            env={
                "AWS_REGION": REGION,
                "PYTHONUNBUFFERED": "1",
                "FASTMCP_LOG_LEVEL": "WARNING",
            },
        )
    ),
    prefix="cw_mcp",
)

CLOUDWATCH_MCP_TOOLS: list = []
try:
    _cloudwatch_mcp.start()
    CLOUDWATCH_MCP_TOOLS = _cloudwatch_mcp.list_tools_sync()
    atexit.register(lambda: _cloudwatch_mcp.stop(None, None, None))
    logger.info(
        "CloudWatch MCP server ready (%d tools).", len(CLOUDWATCH_MCP_TOOLS)
    )
except Exception as exc:  # noqa: BLE001
    logger.warning(
        "CloudWatch MCP server unavailable; continuing without its tools: %s",
        exc,
    )


# Grafana MCP
#
# The token is minted once at cold start and then frozen into the env
# of the mcp-grafana subprocess. AgentCore Runtime keeps containers
# warm between invocations, so the TTL must cover the container's
# full lifetime; otherwise grafana_update_dashboard starts returning
# 401 after a few hours while the rest of the agent still looks
# healthy. 24h is a conservative ceiling (AMG allows up to 30 days)
# and the per-SA token quota stays under control via the active
# orphan cleanup that runs at startup below.
_GRAFANA_TOKEN_TTL_SECONDS = 86400

# Prefix used to identify our tokens so we can clean them up without
# touching tokens belonging to other integrations.
_AGENT_TOKEN_NAME_PREFIX = "agent-mcp-"


def _start_grafana_mcp() -> tuple[list, object | None]:
    """Mint an EDITOR token and spawn the mcp-grafana subprocess.

    Returns ``(tools, cleanup)``. If anything fails (missing env vars,
    denied token, crashed subprocess) it returns ``([], None)`` after
    logging a warning, so the rest of the agent stays up.
    """
    if not (
        GRAFANA_WORKSPACE_ID
        and GRAFANA_WORKSPACE_ENDPOINT
        and GRAFANA_SERVICE_ACCOUNT_ID
    ):
        logger.info(
            "Grafana env vars not set; skipping Grafana MCP startup "
            "(this is expected in local dev without GRAFANA_* exported)."
        )
        return [], None

    grafana_ctl = boto3.client("grafana", region_name=REGION)

    # Best-effort cleanup: only delete tokens that are ALREADY EXPIRED.
    # Earlier versions deleted every token sharing our prefix, which
    # raced with sibling containers: a cold-starting container would
    # nuke a still-valid token held by another container, and that
    # container would then 401 on the next dashboard update. Expired
    # tokens are guaranteed dead, so deleting them is safe and still
    # frees the per-SA quota slot (AMG caps at ~10 tokens per SA). If
    # the IAM role lacks ListWorkspaceServiceAccountTokens we log and
    # move on.
    try:
        leftover = grafana_ctl.list_workspace_service_account_tokens(
            workspaceId=GRAFANA_WORKSPACE_ID,
            serviceAccountId=GRAFANA_SERVICE_ACCOUNT_ID,
        ).get("serviceAccountTokens", [])
        now = datetime.now(UTC)
        for tok in leftover:
            if not tok.get("name", "").startswith(_AGENT_TOKEN_NAME_PREFIX):
                continue
            expires_at = tok.get("expiresAt")
            # Skip anything still valid: it may belong to a sibling
            # container that is currently serving traffic. Missing
            # expiresAt is treated as "still valid" out of caution.
            if expires_at is None or expires_at > now:
                continue
            try:
                grafana_ctl.delete_workspace_service_account_token(
                    workspaceId=GRAFANA_WORKSPACE_ID,
                    serviceAccountId=GRAFANA_SERVICE_ACCOUNT_ID,
                    tokenId=tok["id"],
                )
                logger.info("Cleaned expired Grafana token: %s", tok["id"])
            except Exception as del_exc:  # noqa: BLE001
                logger.debug(
                    "Could not delete expired token %s: %s",
                    tok.get("id"),
                    del_exc,
                )
    except Exception as list_exc:  # noqa: BLE001
        logger.warning(
            "Could not list existing Grafana tokens (continuing): %s",
            list_exc,
        )

    try:
        created = grafana_ctl.create_workspace_service_account_token(
            workspaceId=GRAFANA_WORKSPACE_ID,
            serviceAccountId=GRAFANA_SERVICE_ACCOUNT_ID,
            name=f"{_AGENT_TOKEN_NAME_PREFIX}{uuid.uuid4().hex[:12]}",
            secondsToLive=_GRAFANA_TOKEN_TTL_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Failed to mint Grafana service-account token; Grafana MCP "
            "will be unavailable: %s",
            exc,
        )
        return [], None

    token_id = created["serviceAccountToken"]["id"]
    token_key = created["serviceAccountToken"]["key"]

    client = MCPClient(
        lambda: stdio_client(
            StdioServerParameters(
                command="mcp-grafana",
                # stdio is the default, but we set it explicitly in
                # case a future version changes the default.
                args=["-t", "stdio"],
                env={
                    "GRAFANA_URL": f"https://{GRAFANA_WORKSPACE_ENDPOINT}",
                    "GRAFANA_SERVICE_ACCOUNT_TOKEN": token_key,
                },
            )
        ),
        prefix="grafana",
    )

    def _cleanup() -> None:
        try:
            client.stop(None, None, None)
        except Exception:  # noqa: BLE001
            pass
        try:
            grafana_ctl.delete_workspace_service_account_token(
                workspaceId=GRAFANA_WORKSPACE_ID,
                serviceAccountId=GRAFANA_SERVICE_ACCOUNT_ID,
                tokenId=token_id,
            )
        except Exception:  # noqa: BLE001
            pass

    try:
        client.start()
        tools = client.list_tools_sync()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Grafana MCP server failed to start; continuing without it: %s",
            exc,
        )
        _cleanup()
        return [], None

    logger.info("Grafana MCP server ready (%d tools).", len(tools))
    return tools, _cleanup


GRAFANA_MCP_TOOLS, _grafana_cleanup = _start_grafana_mcp()
if _grafana_cleanup is not None:
    atexit.register(_grafana_cleanup)
