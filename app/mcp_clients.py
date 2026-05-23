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
import threading
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

# AMG caps each service account at 10 tokens. Once cleanup of expired
# tokens has run, if we still have this many of our own tokens lying
# around we delete the oldest until we drop below it. Leaves room for
# this cold-start's new token plus one concurrent sibling.
_QUOTA_RECOVERY_THRESHOLD = 8


# Mutable token state shared between the MCP subprocess env, direct
# HTTP callers (delete_grafana_dashboard), and the refresh path. The
# MCP transport callable reads from this dict on every (re)start, so
# bumping the values here is enough to make the next start() pick up
# a new token without rebuilding the MCPClient instance — which is
# important because Strands captured the MCPClient reference inside
# the MCPAgentTool wrappers at agent-init time.
#
# ``version`` is bumped on every successful mint so callers that hit a
# 401 can pass the version they observed; the refresh function then
# skips a redundant re-mint if a sibling already refreshed.
_grafana_state: dict[str, object] = {
    "token_key": None,  # str | None — bearer token used by MCP + direct HTTP
    "token_id": None,   # str | None — AMG token id, needed to delete on rotate
    "version": 0,       # int — bumped on every successful mint
}

# Serializes mint/refresh so concurrent 401s don't all rotate in
# parallel. Held only across the AMG API calls + MCPClient stop/start;
# tool calls themselves run outside the lock.
_grafana_lock = threading.Lock()

# Module-level handles so refresh_grafana_token_and_mcp can drive them
# without re-discovering state. The boto3 client is cached because
# building one is non-trivial (resolves region, credentials, etc).
_grafana_workspace_ctl: object | None = None
_grafana_mcp_client: MCPClient | None = None


def get_grafana_agent_token() -> str | None:
    """Return the current EDITOR Grafana token, or None.

    ``None`` means the Grafana MCP did not start successfully and the
    agent has no Grafana credentials at all; callers should surface
    that to the user rather than retry.
    """
    return _grafana_state["token_key"]  # type: ignore[return-value]


def get_grafana_token_version() -> int:
    """Return the current token's monotonic version counter.

    Callers that hit a 401 should capture this BEFORE the failing call
    and pass it to ``refresh_grafana_token_and_mcp`` so the refresh is
    a no-op if a sibling already rotated in the meantime.
    """
    return _grafana_state["version"]  # type: ignore[return-value]


def _grafana_transport():
    """Build the stdio transport for the mcp-grafana subprocess.

    Reads the current token from ``_grafana_state`` on every call so
    that ``MCPClient.start()`` after a refresh picks up the new token
    without needing a new MCPClient instance.
    """
    return stdio_client(
        StdioServerParameters(
            command="mcp-grafana",
            # stdio is the default, but we set it explicitly in case a
            # future version changes the default.
            args=["-t", "stdio"],
            env={
                "GRAFANA_URL": f"https://{GRAFANA_WORKSPACE_ENDPOINT}",
                "GRAFANA_SERVICE_ACCOUNT_TOKEN": (
                    _grafana_state["token_key"] or ""  # type: ignore[operator]
                ),
            },
        )
    )


def _cleanup_old_grafana_tokens(grafana_ctl) -> None:
    """Two-tier cleanup of orphaned tokens to keep us under the AMG quota.

    Tier 1 — always safe: delete any of our tokens that are already
        expired. Frees quota slots without affecting any running
        sibling container.

    Tier 2 — quota recovery: if our tokens still occupy >=
        ``_QUOTA_RECOVERY_THRESHOLD`` slots after Tier 1, delete the
        OLDEST ones until we are back below it. This can race with a
        long-lived sibling container that still uses an old token, but
        that risk is bounded: we only kill the oldest, never the
        newest, and only when the quota is actually about to deny the
        next mint. The alternative (let the mint fail, the MCP not
        start, the agent silently lose all grafana_* tools) is
        strictly worse because it has no recovery path short of manual
        ``aws grafana delete-workspace-service-account-token``.

    If the IAM role lacks ``ListWorkspaceServiceAccountTokens`` we log
    and move on; the subsequent mint may still succeed if there is
    slack.
    """
    try:
        leftover = grafana_ctl.list_workspace_service_account_tokens(
            workspaceId=GRAFANA_WORKSPACE_ID,
            serviceAccountId=GRAFANA_SERVICE_ACCOUNT_ID,
        ).get("serviceAccountTokens", [])
    except Exception as list_exc:  # noqa: BLE001
        logger.warning(
            "Could not list existing Grafana tokens (continuing): %s",
            list_exc,
        )
        return

    now = datetime.now(UTC)
    our_tokens = [
        tok for tok in leftover
        if tok.get("name", "").startswith(_AGENT_TOKEN_NAME_PREFIX)
    ]

    def _delete(tok: dict, reason: str) -> bool:
        try:
            grafana_ctl.delete_workspace_service_account_token(
                workspaceId=GRAFANA_WORKSPACE_ID,
                serviceAccountId=GRAFANA_SERVICE_ACCOUNT_ID,
                tokenId=tok["id"],
            )
            logger.info("Cleaned Grafana token %s (%s)", tok["id"], reason)
            return True
        except Exception as del_exc:  # noqa: BLE001
            logger.debug(
                "Could not delete Grafana token %s (%s): %s",
                tok.get("id"),
                reason,
                del_exc,
            )
            return False

    surviving: list[dict] = []
    for tok in our_tokens:
        expires_at = tok.get("expiresAt")
        if expires_at is not None and expires_at <= now:
            _delete(tok, "expired")
        else:
            surviving.append(tok)

    while len(surviving) >= _QUOTA_RECOVERY_THRESHOLD:
        oldest = min(surviving, key=lambda t: t.get("createdAt") or now)
        if _delete(oldest, "quota recovery"):
            surviving.remove(oldest)
        else:
            break  # Avoid an infinite loop on persistent delete failures.


def _mint_grafana_token(grafana_ctl) -> tuple[str, str] | None:
    """Mint a fresh EDITOR token via the AMG API.

    Returns ``(token_id, token_key)`` on success, ``None`` on failure.
    """
    try:
        created = grafana_ctl.create_workspace_service_account_token(
            workspaceId=GRAFANA_WORKSPACE_ID,
            serviceAccountId=GRAFANA_SERVICE_ACCOUNT_ID,
            name=f"{_AGENT_TOKEN_NAME_PREFIX}{uuid.uuid4().hex[:12]}",
            secondsToLive=_GRAFANA_TOKEN_TTL_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Failed to mint Grafana service-account token: %s", exc,
        )
        return None
    return (
        created["serviceAccountToken"]["id"],
        created["serviceAccountToken"]["key"],
    )


def _delete_grafana_token_safe(grafana_ctl, token_id: str) -> None:
    """Best-effort token delete; swallow errors (token may already be gone)."""
    try:
        grafana_ctl.delete_workspace_service_account_token(
            workspaceId=GRAFANA_WORKSPACE_ID,
            serviceAccountId=GRAFANA_SERVICE_ACCOUNT_ID,
            tokenId=token_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("Could not delete Grafana token %s: %s", token_id, exc)


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

    global _grafana_workspace_ctl, _grafana_mcp_client
    _grafana_workspace_ctl = boto3.client("grafana", region_name=REGION)

    _cleanup_old_grafana_tokens(_grafana_workspace_ctl)

    minted = _mint_grafana_token(_grafana_workspace_ctl)
    if minted is None:
        logger.warning(
            "Grafana MCP unavailable: token mint failed at cold start."
        )
        return [], None
    token_id, token_key = minted
    _grafana_state["token_id"] = token_id
    _grafana_state["token_key"] = token_key
    _grafana_state["version"] = (_grafana_state["version"] or 0) + 1  # type: ignore[operator]

    _grafana_mcp_client = MCPClient(_grafana_transport, prefix="grafana")

    def _cleanup() -> None:
        try:
            if _grafana_mcp_client is not None:
                _grafana_mcp_client.stop(None, None, None)
        except Exception:  # noqa: BLE001
            pass
        current_id = _grafana_state.get("token_id")
        if current_id:
            _delete_grafana_token_safe(_grafana_workspace_ctl, current_id)  # type: ignore[arg-type]

    try:
        _grafana_mcp_client.start()
        tools = _grafana_mcp_client.list_tools_sync()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Grafana MCP server failed to start; continuing without it: %s",
            exc,
        )
        _cleanup()
        return [], None

    logger.info("Grafana MCP server ready (%d tools).", len(tools))
    return tools, _cleanup


def refresh_grafana_token_and_mcp(known_version: int) -> int:
    """Rotate the Grafana token and restart the MCP subprocess.

    Call this when a tool returns 401/Unauthorized. Pass the
    ``version`` you observed via ``get_grafana_token_version()``
    BEFORE the failing call so a sibling that already rotated wins the
    race and we don't burn quota on a redundant re-mint.

    Returns the version after the call. Callers should re-fetch the
    token with ``get_grafana_agent_token()`` after this returns.

    On any failure (no client to rotate, mint denied, subprocess fails
    to restart) we log and return the current version unchanged — the
    agent keeps running with whatever token it had, and the caller's
    retry will surface the same 401 to the user.
    """
    if _grafana_mcp_client is None or _grafana_workspace_ctl is None:
        logger.warning(
            "refresh_grafana_token_and_mcp called but Grafana MCP was "
            "never started; nothing to refresh."
        )
        return _grafana_state["version"]  # type: ignore[return-value]

    with _grafana_lock:
        current_version = _grafana_state["version"]  # type: ignore[assignment]
        if current_version > known_version:  # type: ignore[operator]
            # A concurrent caller already rotated. Skip the work; the
            # caller's retry will use the new token already in state.
            logger.info(
                "Grafana token already rotated by sibling (v%d -> v%d); "
                "skipping redundant refresh.",
                known_version,
                current_version,
            )
            return current_version  # type: ignore[return-value]

        old_token_id = _grafana_state.get("token_id")
        logger.info("Rotating Grafana token (v%d -> ?) after 401.", current_version)

        try:
            _grafana_mcp_client.stop(None, None, None)
        except Exception as stop_exc:  # noqa: BLE001
            logger.warning("Stopping Grafana MCP before refresh failed: %s", stop_exc)

        minted = _mint_grafana_token(_grafana_workspace_ctl)
        if minted is None:
            logger.warning(
                "Grafana token refresh failed at mint; MCP stays stopped."
            )
            return current_version  # type: ignore[return-value]
        new_token_id, new_token_key = minted

        _grafana_state["token_id"] = new_token_id
        _grafana_state["token_key"] = new_token_key
        _grafana_state["version"] = current_version + 1  # type: ignore[operator]

        try:
            _grafana_mcp_client.start()
        except Exception as start_exc:  # noqa: BLE001
            logger.warning(
                "Grafana MCP failed to restart after token refresh: %s",
                start_exc,
            )
            # We have a new token in state but no live subprocess; the
            # next tool call will fail and the user will see it. Don't
            # bump version backwards — that would only encourage another
            # mint that would also have nowhere to land.
            return _grafana_state["version"]  # type: ignore[return-value]

        # Only delete the old token AFTER the new one is live, so a
        # restart failure doesn't leave us tokenless.
        if old_token_id:
            _delete_grafana_token_safe(_grafana_workspace_ctl, old_token_id)  # type: ignore[arg-type]

        logger.info(
            "Grafana token rotated successfully (now v%d).",
            _grafana_state["version"],
        )
        return _grafana_state["version"]  # type: ignore[return-value]


GRAFANA_MCP_TOOLS, _grafana_cleanup = _start_grafana_mcp()
if _grafana_cleanup is not None:
    atexit.register(_grafana_cleanup)
