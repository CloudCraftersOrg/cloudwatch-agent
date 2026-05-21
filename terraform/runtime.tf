# AgentCore Runtime. Managed service that runs the agent container on
# an ARM64 microVM. AgentCore auto-creates a DEFAULT endpoint when the
# runtime is created; callers invoke the runtime ARN with
# `--qualifier DEFAULT`. To add non-default endpoints (e.g. CANARY),
# declare aws_bedrockagentcore_agent_runtime_endpoint with a name
# other than DEFAULT.

resource "aws_bedrockagentcore_agent_runtime" "this" {
  agent_runtime_name = replace(var.project_name, "-", "_")
  description        = "AgentCore Runtime for the CloudWatch Agent."
  role_arn           = aws_iam_role.runtime.arn

  # local.image_uri is content-addressed (tag = short sha1 of
  # Dockerfile + pyproject + uv.lock + app/). depends_on guarantees
  # Terraform builds and pushes the image via terraform_data.image
  # before updating container_uri here.
  agent_runtime_artifact {
    container_configuration {
      container_uri = local.image_uri
    }
  }

  depends_on = [terraform_data.image]

  # PUBLIC: public endpoint managed by AgentCore, with IAM auth. VPC
  # would be for private resources; this agent only calls AWS APIs.
  network_configuration {
    network_mode = "PUBLIC"
  }

  # Environment variables read by app/config.py at import time. Any
  # change here triggers a runtime update (brief cold start).
  environment_variables = {
    AWS_REGION = var.region
    MODEL_ID   = "us.anthropic.claude-opus-4-6-v1"
    MEMORY_ID  = aws_bedrockagentcore_memory.this.id

    GRAFANA_WORKSPACE_ID              = aws_grafana_workspace.this.id
    GRAFANA_WORKSPACE_ENDPOINT        = aws_grafana_workspace.this.endpoint
    GRAFANA_SERVICE_ACCOUNT_ID        = aws_grafana_workspace_service_account.agent.service_account_id
    GRAFANA_CLOUDWATCH_DATASOURCE_UID = grafana_data_source.cloudwatch.uid

    # Native observability: aws-opentelemetry-distro bundles the
    # exporters to X-Ray and CloudWatch. These vars are also set in
    # the Dockerfile for parity when running the container locally.
    AGENT_OBSERVABILITY_ENABLED = "true"
    OTEL_PYTHON_DISTRO          = "aws_distro"
    OTEL_PYTHON_CONFIGURATOR    = "aws_configurator"
  }
}
