
# #############################################################################
# Bedrock AgentCore Runtime and invocation endpoint
#
# Purpose: The runtime is a managed microVM-backed service that pulls the agent
# container image from ECR and exposes the agent over HTTPS. The endpoint is the
# named, invocable handle that callers target (DEFAULT here).
# #############################################################################

resource "aws_bedrockagentcore_agent_runtime" "this" {
  agent_runtime_name = replace(var.project_name, "-", "_") # underscores only.
  description        = "AgentCore Runtime hosting the CloudWatch Agent container."
  role_arn           = aws_iam_role.runtime.arn

  # Container image to run. Tag is overridden per-deploy by CI so the runtime always points at a known-good immutable artifact (commit SHA), not the floating "latest" tag.
  agent_runtime_artifact {
    container_configuration {
      container_uri = "${aws_ecr_repository.this.repository_url}:${var.image_tag}"
    }
  }

  # PUBLIC network mode: runtime hosts the agent on a service-managed public endpoint (IAM auth in front). VPC mode is for private resources, but this agent only calls AWS APIs over the internet.
  network_configuration {
    network_mode = "PUBLIC"
  }

  # Environment variables for every invocation. Read by app/config.py at import time. Changing any triggers a runtime update (brief cold start), so these are deploy-time config.
  environment_variables = {
    # Application config consumed by app/config.py.
    AWS_REGION = var.region
    MODEL_ID   = "us.anthropic.claude-opus-4-6-v1"
    MEMORY_ID  = aws_bedrockagentcore_memory.this.id

    # Grafana wiring. The agent mints a per-session token against the agent
    # service account and POSTs dashboards to the workspace HTTP API; panels
    # reference the CloudWatch data source by its UID.
    GRAFANA_WORKSPACE_ID              = aws_grafana_workspace.this.id
    GRAFANA_WORKSPACE_ENDPOINT        = aws_grafana_workspace.this.endpoint
    GRAFANA_SERVICE_ACCOUNT_ID        = aws_grafana_workspace_service_account.agent.service_account_id
    GRAFANA_CLOUDWATCH_DATASOURCE_UID = grafana_data_source.cloudwatch.uid

    # Observability config for aws-opentelemetry-distro. Also baked into Dockerfile for local run parity.
    AGENT_OBSERVABILITY_ENABLED = "true"
    OTEL_PYTHON_DISTRO          = "aws_distro"
    OTEL_PYTHON_CONFIGURATOR    = "aws_configurator"
  }
}

# #############################################################################
# DEFAULT endpoint. Callers invoke `arn ... :runtime/<id>` with this qualifier.
# Future endpoints (e.g., CANARY for blue/green) can be added without touching the runtime resource.
# #############################################################################
resource "aws_bedrockagentcore_agent_runtime_endpoint" "default" {
  agent_runtime_id = aws_bedrockagentcore_agent_runtime.this.agent_runtime_id
  name             = "DEFAULT"
  description      = "Default invocable endpoint for the CloudWatch Agent runtime."
}
