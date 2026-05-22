"""Entrypoint for the CloudWatch Agent.

Connects three pieces:

- The AgentCore Runtime HTTP server (``BedrockAgentCoreApp``), which
  exposes ``POST /invocations``.
- The Strands ``Agent``, which runs the reasoning loop and dispatches
  tools.
- AgentCore Memory, wired in via a session manager when ``MEMORY_ID``
  is set (always set in production; in local dev it can be omitted,
  in which case the agent degrades to in-process state).

The runtime may serve several invocations of the same session on the
same container; module-level state (boto3 clients in ``app/tools``)
is reused across invocations. A fresh ``Agent`` is constructed per
invocation because Strands keeps history per instance.
"""

from __future__ import annotations

import json
import logging
import uuid

from bedrock_agentcore.memory.integrations.strands.config import AgentCoreMemoryConfig
from bedrock_agentcore.memory.integrations.strands.session_manager import (
    AgentCoreMemorySessionManager,
)
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands import Agent
from strands.hooks import BeforeInvocationEvent, BeforeToolCallEvent, HookRegistry
from strands.models import BedrockModel

from app.config import MEMORY_ID, MODEL_ID, REGION
from app.prompts import SYSTEM_PROMPT
from app.tools import TOOLS

# Logger for invocation-level audit lines. AgentCore Runtime forwards
# the container's stdout to ``/aws/bedrock-agentcore/runtimes/<id>-DEFAULT``,
# so anything emitted here is queryable in CloudWatch Logs alongside
# the agent's response stream. We use a recognizable prefix
# (``invocation_prompt``) so operators can grep / Insights-filter for
# prompt traceability without parsing the full container log.
logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)


# Hard cap on tool calls per invocation. Strands' Agent has no native
# reasoning-loop bound — it loops until the model emits end_turn
# without a tool_use. Opus 4.6 has been observed to occasionally
# re-call the same cw_mcp_* tool with the same input after a valid
# result (suspected: the non-standard ``structuredContent``/``isError``
# fields the MCP layer adds confuse the model's "did this tool already
# answer me?" check). Without a cap a single turn can churn through
# many tool calls and stretch beyond any reasonable client timeout.
#
# 35 covers the worst-case canonical-5 rebuild end-to-end:
#   3 discovery (describe_log_groups + filter_log_events +
#               get_cloudwatch_datasource)
# + 1 get_data_window
# + 1 rank_services_by_priority
# + 5 dashboards × (1 fetch_version + up to 3 judge cycles + 1 publish)
# + 1 prune_dashboards_to_top_set
# = ~30, with ~5 calls of slack for ad-hoc inspection by the model.
_MAX_TOOL_CALLS_PER_INVOCATION = 35


class _ToolCallLimiter:
    """Strands hook that caps total tool calls in a single invocation.

    Strands invokes ``register_hooks`` once when the agent is built.
    The counter resets on every ``BeforeInvocationEvent`` so the cap
    is per-turn, not per-agent-lifetime. ``BeforeToolCallEvent`` is
    interruptible: setting ``cancel_tool`` short-circuits the tool
    with an error result the model can read, which lets it wrap up
    instead of crashing the stream.
    """

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._count = 0

    def register_hooks(self, registry: HookRegistry, **_: object) -> None:
        registry.add_callback(BeforeInvocationEvent, self._on_invocation_start)
        registry.add_callback(BeforeToolCallEvent, self._on_tool_call)

    def _on_invocation_start(self, _event: BeforeInvocationEvent) -> None:
        self._count = 0

    def _on_tool_call(self, event: BeforeToolCallEvent) -> None:
        self._count += 1
        if self._count > self._limit:
            event.cancel_tool = (
                f"Tool-call budget exhausted ({self._limit} calls per "
                "invocation). Summarize what you already have from prior "
                "tool results and respond to the user without calling "
                "any more tools."
            )

# AgentCore Runtime expects a top-level ``app`` ASGI object listening
# on port 8080. BedrockAgentCoreApp registers the health-check and
# invocation endpoints the contract requires.
app = BedrockAgentCoreApp()


def _build_session_manager(
    session_id: str, user_id: str
) -> AgentCoreMemorySessionManager | None:
    """Build the AgentCore Memory session manager.

    Returns ``None`` when ``MEMORY_ID`` is empty (local dev without a
    provisioned memory resource), so the agent still starts and
    Strands keeps in-process state for the invocation.
    """
    if not MEMORY_ID:
        return None

    return AgentCoreMemorySessionManager(
        agentcore_memory_config=AgentCoreMemoryConfig(
            memory_id=MEMORY_ID,
            session_id=session_id,
            actor_id=user_id,
        ),
        region_name=REGION,
    )


@app.entrypoint
async def invoke(payload, context):
    """Handle a single invocation.

    Expects ``prompt`` in the payload (required) and an optional
    ``userId`` (default ``"anonymous"``). ``context.session_id`` is
    provided by AgentCore Runtime and used as the session key for
    memory.

    Yields streaming events (tokens, tool calls, tool results) that
    AgentCore forwards to the client as Server-Sent Events.
    """
    user_message = payload.get("prompt", "")
    user_id = payload.get("userId", "anonymous")

    # context.session_id is Optional[str]. AgentCoreMemoryConfig
    # requires a non-empty string (pydantic ValidationError otherwise),
    # so we generate a fallback when the caller invokes without
    # runtimeSessionId. The fallback is per-invocation: no memory
    # between calls, but the agent does not fail.
    session_id = context.session_id or f"auto-{uuid.uuid4().hex}"

    # Audit log: the inbound user prompt. AgentCore Runtime already
    # captures the assistant's stream in the container log group; we
    # add the prompt here so prompt -> response pairs are co-located
    # under the same session_id and trivially correlatable via a
    # Logs Insights query like
    #   filter @message like /invocation_prompt/
    #   | parse @message "session_id=* user_id=* prompt=*" as sid, uid, p
    # ``%r`` keeps multi-line prompts on a single log line (repr
    # escapes newlines) so the JSON-line container log stays clean.
    logger.info(
        "invocation_prompt session_id=%s user_id=%s prompt=%r",
        session_id,
        user_id,
        user_message,
    )

    session_manager = _build_session_manager(session_id=session_id, user_id=user_id)

    # New Agent per invocation (Strands keeps history per instance).
    # Explicit region_name on BedrockModel so model calls always land
    # in the same region as the tools' boto3 clients, even if
    # AWS_REGION is changed. The ``_ToolCallLimiter`` hook bounds the
    # reasoning loop — Strands itself has no built-in cap.
    agent = Agent(
        model=BedrockModel(model_id=MODEL_ID, region_name=REGION),
        system_prompt=SYSTEM_PROMPT,
        tools=TOOLS,
        session_manager=session_manager,
        hooks=[_ToolCallLimiter(limit=_MAX_TOOL_CALLS_PER_INVOCATION)],
    )

    # Event filter. stream_async emits two classes of items:
    # (a) JSON-serializable dicts (the Bedrock Converse stream events
    #     that the client consumes).
    # (b) diagnostic dicts with live Python objects (Agent, Trace,
    #     etc). When AgentCore tries to serialize these, json.dumps
    #     fails and it falls back to str(dict), inflating the SSE
    #     stream with megabytes of repr output and breaking browser
    #     readers.
    # The try/json.dumps drops the (b) items; cost is ~µs per event.
    async for event in agent.stream_async(user_message):
        if not isinstance(event, dict):
            continue
        try:
            json.dumps(event)
        except (TypeError, ValueError):
            continue
        yield event


if __name__ == "__main__":
    # Entry point for local dev. app.run() spins up the same HTTP
    # server AgentCore Runtime spins up in production, so the dev loop
    # mirrors the deployed behavior exactly.
    app.run()
