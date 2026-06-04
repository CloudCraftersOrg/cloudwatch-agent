
# #############################################################################
# ECR repository for the agent container image.
#
# Purpose: Holds the agent container image. AgentCore Runtime pulls images from
# ECR on every cold start, so this repository must exist in the same account and
# region as the runtime for low-latency and cost efficiency.
# #############################################################################
resource "aws_ecr_repository" "this" {
  # Repository name must be lowercase and may only contain dashes or slashes.
  name = var.project_name

  # MUTABLE allows CI to re-tag `latest` on every successful build, in addition
  # to pushing the immutable commit-SHA tag. If you only want to deploy by SHA,
  # switch this to IMMUTABLE and remove the `latest` push step from deploy.yml.
  image_tag_mutability = "MUTABLE"

  # Enable scan on push for basic ECR scanning (CVE detection) at no extra cost.
  image_scanning_configuration {
    scan_on_push = true
  }

  # Allow deletion of non-empty repository during terraform destroy to avoid state conflicts.
  force_delete = true
}

# #############################################################################
# ECR lifecycle policy: keep the 10 most recent images, expire older ones.
#
# Purpose: Prevents unbounded growth of the repository by expiring old images.
# #############################################################################
resource "aws_ecr_lifecycle_policy" "this" {
  repository = aws_ecr_repository.this.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Keep the 10 most recently pushed images; expire older ones."
        selection = {
          tagStatus   = "any"
          countType   = "imageCountMoreThan"
          countNumber = 10
        }
        action = {
          type = "expire"
        }
      }
    ]
  })
}
