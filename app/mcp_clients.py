"""MCP client wiring for the agent.

Spawns two MCP servers at module import time and exposes their tools to
the Strands ``Agent``:

  - **CloudWatch MCP** (AWS Labs): Python package
    ``awslabs.cloudwatch-mcp-server``, started via
    ``python -u -m awslabs.cloudwatch_mcp_server.server`` so we never
    depend on the venv's bin/ being on PATH and stdio is unbuffered
    (FastMCP's handshake response otherwise gets stuck in Python's
    line-buffered stdout and the MCP client times out).
  - **Grafana MCP** (Grafana Labs): Go binary ``mcp-grafana`` bundled
    in the image at /usr/local/bin (built by the Dockerfile's stage 1).
    It needs a Grafana service-account token — we mint a short-lived
    EDITOR token via the AWS Grafana control-plane API at startup,
    pass it as ``GRAFANA_SERVICE_ACCOUNT_TOKEN``, and delete it on
    container shutdown via ``atexit``.

Both subprocesses live for the container's lifetime (a single
``start()`` at import, a single ``stop()`` via ``atexit``), so
per-invocation tool calls do not pay any subprocess startup cost.

If either server fails to launch (binary missing, AWS creds unavailable
in a local dev shell, etc.) we log a warning and surface an empty tool
list for that server only — the rest of the agent still works, which
keeps local iteration unblocked.
"""

from __future__ import annotations

import atexit
import logging
import sys
import uuid

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


# ---------------------------------------------------------------------------
# CloudWatch MCP (AWS Labs)
# ---------------------------------------------------------------------------

# Spawn via ``python -u -m awslabs.cloudwatch_mcp_server.server`` rather
# than the ``awslabs.cloudwatch-mcp-server`` console script:
#   - sys.executable uses the exact same Python interpreter that's
#     running the agent (no PATH dependency, works identically in local
#     dev shells, the container CMD, and tests).
#   - ``-u`` + PYTHONUNBUFFERED=1 force unbuffered stdio. Without them
#     Python line-buffers stdout, the MCP ``initialize`` response sits
#     in the subprocess buffer, and Strands times out after 30 s waiting
#     for a handshake reply that never arrives.
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
except Exception as exc:  # noqa: BLE001 — degrade gracefully on startup failure
    logger.warning(
        "CloudWatch MCP server unavailable; continuing without its tools: %s",
        exc,
    )


# ---------------------------------------------------------------------------
# Grafana MCP (Grafana Labs)
# ---------------------------------------------------------------------------

# Token TTL handed to the Grafana MCP server. Kept short on purpose:
# AgentCore Runtime kills containers without SIGTERM most of the time,
# so the atexit cleanup that deletes our token doesn't always fire and
# tokens leak. AMG caps ~10 active tokens per service account, so a
# long TTL + frequent restarts hits ServiceQuotaExceededException
# ("Service Account Token quota has been reached"). 1 hour is long
# enough for any practical invocation, short enough that leaked tokens
# expire before they pile up. We ALSO actively delete stale "agent-mcp-"
# tokens at startup (see _start_grafana_mcp), which is the real
# defense against quota exhaustion.
_GRAFANA_TOKEN_TTL_SECONDS = 3600  # 1 hour

# Prefix used for tokens this module mints. Used at startup to identify
# and delete leaked tokens from previous containers before minting a
# fresh one (without this, ~10 leaks across restarts → quota error).
_AGENT_TOKEN_NAME_PREFIX = "agent-mcp-"


def _start_grafana_mcp() -> tuple[list, object | None]:
    """Mint an EDITOR token and start the Grafana MCP subprocess.

    Returns ``(tools, cleanup)``. On any failure (missing env vars,
    token mint denied, subprocess crash) returns ``([], None)`` after
    logging a warning, so the rest of the agent keeps working.
    """
    # Configured by Terraform; missing in local dev — skip cleanly.
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

    # Best-effort: delete any tokens left over from previous containers
    # that didn't shut down gracefully. AMG enforces ~10 active tokens
    # per service account; without this we hit
    # ServiceQuotaExceededException after enough restarts. We only
    # delete tokens whose name carries our prefix, so the Terraform
    # provisioner token (in a different service account anyway) is
    # untouched.
    try:
        leftover = grafana_ctl.list_workspace_service_account_tokens(
            workspaceId=GRAFANA_WORKSPACE_ID,
            serviceAccountId=GRAFANA_SERVICE_ACCOUNT_ID,
        ).get("serviceAccountTokens", [])
        for tok in leftover:
            if tok.get("name", "").startswith(_AGENT_TOKEN_NAME_PREFIX):
                try:
                    grafana_ctl.delete_workspace_service_account_token(
                        workspaceId=GRAFANA_WORKSPACE_ID,
                        serviceAccountId=GRAFANA_SERVICE_ACCOUNT_ID,
                        tokenId=tok["id"],
                    )
                    logger.info(
                        "Cleaned leftover Grafana token: %s", tok["id"]
                    )
                except Exception as del_exc:  # noqa: BLE001
                    logger.debug(
                        "Could not delete leftover token %s: %s",
                        tok.get("id"),
                        del_exc,
                    )
    except Exception as list_exc:  # noqa: BLE001
        # Listing is best-effort; if the IAM lacks the permission we
        # just proceed and hope we're under quota. Worst case: a future
        # restart hits the cap and the manual drain script in README
        # is needed.
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
                # Default transport is stdio; ``-t stdio`` is explicit
                # so this keeps working if a future mcp-grafana version
                # changes the default.
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
        # Best-effort token deletion; the token also expires on its own
        # after _GRAFANA_TOKEN_TTL_SECONDS, so we never leak permanently.
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
