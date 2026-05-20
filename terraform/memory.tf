
# #############################################################################
# Bedrock AgentCore Memory
#
# Purpose: Provides conversational and long-term memory for the agent.
# In AWS provider 6.x, strategies are NOT inline blocks on the memory
# resource; each is a separate aws_bedrockagentcore_memory_strategy that
# attaches by memory_id (see below). The agent reads them automatically
# via the Strands AgentCoreMemorySessionManager.
# #############################################################################
resource "aws_bedrockagentcore_memory" "this" {
  name        = replace(var.project_name, "-", "_") # underscores only.
  description = "Conversational and long-term memory for the CloudWatch Agent."

  # Raw event retention (days). Strategy outputs are retained independently.
  event_expiry_duration = 30

  # Strategy extraction/consolidation invokes a model on the agent's behalf,
  # so the memory needs an execution role.
  memory_execution_role_arn = aws_iam_role.runtime.arn
}

# #############################################################################
# Memory strategies. Three built-in types, each attached to the memory above:
#   - SUMMARIZATION:    short-term per-session summarization.
#   - USER_PREFERENCE:  long-term per-actor preferences.
#   - SEMANTIC:         long-term extracted facts.
# Default (omitted) configuration uses AgentCore's built-in extraction.
# #############################################################################
resource "aws_bedrockagentcore_memory_strategy" "summary" {
  memory_id  = aws_bedrockagentcore_memory.this.id
  name       = "session_summary"
  type       = "SUMMARIZATION"
  namespaces = ["/summaries/{actorId}/{sessionId}"]
}

resource "aws_bedrockagentcore_memory_strategy" "user_preferences" {
  memory_id  = aws_bedrockagentcore_memory.this.id
  name       = "user_preferences"
  type       = "USER_PREFERENCE"
  namespaces = ["/users/{actorId}/preferences"]
}

resource "aws_bedrockagentcore_memory_strategy" "semantic_facts" {
  memory_id  = aws_bedrockagentcore_memory.this.id
  name       = "semantic_facts"
  type       = "SEMANTIC"
  namespaces = ["/users/{actorId}/facts"]
}
