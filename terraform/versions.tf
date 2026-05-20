
# #############################################################################
# Terraform and provider version constraints
#
# Purpose: Pins AWS provider to ~> 6.18 for bedrock-agentcore resources.
# Adds the grafana/grafana provider, used to provision the CloudWatch data
# source inside the Amazon Managed Grafana workspace at apply time.
# Requires Terraform >= 1.9 for compatibility with AWS provider 6.x features.
# #############################################################################
terraform {
  required_version = ">= 1.9"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.18"
    }
    grafana = {
      source  = "grafana/grafana"
      version = "~> 3.0"
    }
  }
}
