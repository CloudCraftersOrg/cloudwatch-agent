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
import uuid

from bedrock_agentcore.memory.integrations.strands.config import AgentCoreMemoryConfig
from bedrock_agentcore.memory.integrations.strands.session_manager import (
    AgentCoreMemorySessionManager,
)
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands import Agent
from strands.models import BedrockModel

from app.config import MEMORY_ID, MODEL_ID, REGION
from app.prompts import SYSTEM_PROMPT
from app.tools import TOOLS

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

    session_manager = _build_session_manager(session_id=session_id, user_id=user_id)

    # New Agent per invocation (Strands keeps history per instance).
    # Explicit region_name on BedrockModel so model calls always land
    # in the same region as the tools' boto3 clients, even if
    # AWS_REGION is changed.
    agent = Agent(
        model=BedrockModel(model_id=MODEL_ID, region_name=REGION),
        system_prompt=SYSTEM_PROMPT,
        tools=TOOLS,
        session_manager=session_manager,
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
