
# #############################################################################
# Stack outputs
#
# Purpose: Exposes key resource identifiers for common operations:
# - Invoking the agent (agent_runtime_arn)
# - Pushing images (ecr_repository_url)
# - Inspecting state (memory_id)
# - Opening the Grafana workspace (grafana_workspace_url)
# #############################################################################

output "agent_runtime_arn" {
  description = "ARN of the AgentCore Runtime hosting the agent container."
  value       = aws_bedrockagentcore_agent_runtime.this.agent_runtime_arn
}

output "agent_runtime_endpoint_arn" {
  description = "ARN of the DEFAULT invocable endpoint. Callers pass this to bedrock-agentcore invoke-agent-runtime."
  value       = aws_bedrockagentcore_agent_runtime_endpoint.default.agent_runtime_endpoint_arn
}

output "ecr_repository_url" {
  description = "ECR repository URL. Used by CI as the docker image push target."
  value       = aws_ecr_repository.this.repository_url
}

output "memory_id" {
  description = "ID of the AgentCore Memory resource. Wired into the runtime as MEMORY_ID."
  value       = aws_bedrockagentcore_memory.this.id
}

output "grafana_workspace_url" {
  description = "HTTPS endpoint of the Amazon Managed Grafana workspace. Open this to view generated dashboards."
  value       = "https://${aws_grafana_workspace.this.endpoint}"
}

output "grafana_workspace_id" {
  description = "Amazon Managed Grafana workspace ID. Wired into the runtime as GRAFANA_WORKSPACE_ID."
  value       = aws_grafana_workspace.this.id
}

output "grafana_workspace_endpoint" {
  description = "Workspace host (no scheme). Set as GRAFANA_WORKSPACE_ENDPOINT for local runs."
  value       = aws_grafana_workspace.this.endpoint
}

output "grafana_service_account_id" {
  description = "Agent Grafana service account ID. Set as GRAFANA_SERVICE_ACCOUNT_ID for local runs."
  value       = aws_grafana_workspace_service_account.agent.service_account_id
}

output "grafana_cloudwatch_datasource_uid" {
  description = "UID of the CloudWatch data source. The agent references this in dashboard panels."
  value       = grafana_data_source.cloudwatch.uid
}
