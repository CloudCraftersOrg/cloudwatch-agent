
# #############################################################################
# Provider configuration
#
# Purpose: Configures the AWS provider for the region specified in `var.region`.
# Default tags are applied to all resources for cost allocation and ownership.
# The grafana provider authenticates against the Amazon Managed Grafana
# workspace using a short-lived ADMIN service-account token that Terraform
# itself provisions (see grafana.tf). That token is only used during apply
# to create the CloudWatch data source; it is unrelated to the per-session
# tokens the agent mints at runtime.
# #############################################################################
provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project   = var.project_name
      ManagedBy = "terraform"
    }
  }
}

provider "grafana" {
  # Amazon Managed Grafana exposes a standard Grafana HTTP API at the
  # workspace endpoint. The provider talks to that API as the Terraform
  # provisioner service account.
  url  = "https://${aws_grafana_workspace.this.endpoint}"
  auth = aws_grafana_workspace_service_account_token.terraform.key
}
