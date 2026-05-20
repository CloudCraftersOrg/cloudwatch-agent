"""Entrypoint for the CloudWatch Agent.

This module wires together:

- The AgentCore Runtime HTTP server (``BedrockAgentCoreApp``).
- The Strands ``Agent`` (LLM reasoning loop + tool dispatch).
- AgentCore Memory (short-term session memory + long-term user memory),
  attached only when ``MEMORY_ID`` is configured.

The decorated ``invoke`` function is the single HTTP handler exposed at
``POST /invocations`` by AgentCore Runtime. Module-level state (e.g. the
boto3 clients in ``app/tools``) lives for the life of the container and
may serve multiple invocations of the same session, possibly
concurrently; it is never shared across different sessions' containers.
A fresh Strands ``Agent`` is built per invocation (see ``invoke``); the
boto3 clients are reused because they are thread-safe for our read APIs.
"""

from __future__ import annotations

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

# AgentCore Runtime expects a top-level ``app`` object that exposes an
# ASGI-compatible callable on port 8080. ``BedrockAgentCoreApp`` builds
# that for us and also registers the standard health-check and invocation
# endpoints required by the Runtime contract.
app = BedrockAgentCoreApp()


def _build_session_manager(
    session_id: str, user_id: str
) -> AgentCoreMemorySessionManager | None:
    """Construct the AgentCore Memory session manager, or ``None`` in dev.

    Production deployments always have ``MEMORY_ID`` set by Terraform.
    Local development typically does not, so we let the agent run without
    memory persistence rather than forcing contributors to provision an
    AgentCore Memory resource just to iterate on prompts.

    Args:
        session_id: AgentCore-supplied session identifier; used so that
            conversational history is preserved across multiple
            invocations within the same session.
        user_id: Caller-supplied principal identifier; used as the
            "actor" key for long-term memory (per-user preferences and
            facts).

    Returns:
        A configured session manager, or ``None`` if ``MEMORY_ID`` is
        unset (in which case Strands will keep state in-process for the
        duration of the invocation only).
    """
    if not MEMORY_ID:
        return None

    # The session manager bridges Strands' conversation history with
    # AgentCore Memory. Short-term history is automatically persisted to
    # the session; long-term strategies (summarization, user preference,
    # semantic facts) are evaluated asynchronously by AgentCore.
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
    """Handle a single agent invocation.

    Args:
        payload: JSON body sent by the caller. We expect at minimum a
            ``prompt`` field; ``userId`` is optional and defaults to
            ``"anonymous"`` for unauthenticated callers.
        context: Runtime-provided context object. We use
            ``context.session_id`` as the AgentCore Memory session
            identifier so that conversational state is preserved across
            invocations of the same session.

    Yields:
        Streaming events from the Strands ``Agent`` (tokens, tool calls,
        tool results). AgentCore Runtime forwards these to the client as
        Server-Sent Events.
    """
    user_message = payload.get("prompt", "")
    user_id = payload.get("userId", "anonymous")

    # ``context.session_id`` is Optional[str] and is None whenever the
    # caller invokes the runtime without a runtimeSessionId. AgentCore
    # Memory's config requires a non-empty session id (it raises a
    # pydantic ValidationError on None/""), so fall back to a generated
    # id rather than 500ing. The fallback is per-invocation, so memory
    # simply won't span calls when no session id is supplied — graceful
    # degradation, not a hard failure (mirrors the userId default above).
    session_id = context.session_id or f"auto-{uuid.uuid4().hex}"

    session_manager = _build_session_manager(session_id=session_id, user_id=user_id)

    # Construct a fresh Agent per invocation. Strands ``Agent`` instances
    # carry per-conversation state (history, tool-call cursors), so a new
    # instance is built for every invocation. ``region_name`` is pinned so
    # model calls always target the same region as the tool clients
    # (app/config.py), even if AWS_REGION is overridden.
    agent = Agent(
        model=BedrockModel(model_id=MODEL_ID, region_name=REGION),
        system_prompt=SYSTEM_PROMPT,
        tools=TOOLS,
        session_manager=session_manager,
    )

    # Stream events back to the client so the user sees incremental
    # progress (especially useful for long tool-using turns).
    async for event in agent.stream_async(user_message):
        yield event


if __name__ == "__main__":
    # Local development entrypoint. ``app.run()`` starts the same HTTP
    # server that AgentCore Runtime will start in production, so the
    # local dev loop is identical to the deployed behavior.
    app.run()
