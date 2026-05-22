
# #############################################################################
# Build and push the agent container image as part of `terraform apply`.
#
# A single `terraform apply` does the whole deploy:
#   1. Creates / updates the ECR repository (ecr.tf).
#   2. Builds and pushes the linux/arm64 agent image (this file).
#   3. Updates the AgentCore Runtime to point at the new image (runtime.tf).
#
# Operator preconditions: docker with buildx, AWS CLI v2, and a working AWS
# session for the target account. Cross-arch from an amd64 host additionally
# needs QEMU binfmt; on macOS Apple Silicon arm64 is native.
# #############################################################################

locals {
  # Files that constitute the image content. Any change here changes the
  # source hash, which changes the image tag, which forces a rebuild +
  # push and an in-place update of the runtime's container_uri.
  # sort() keeps the hash deterministic across machines.
  _image_source_files = sort(concat(
    [
      "${path.module}/../Dockerfile",
      "${path.module}/../pyproject.toml",
      "${path.module}/../uv.lock",
    ],
    [for f in fileset("${path.module}/..", "app/**/*.py") : "${path.module}/../${f}"],
  ))

  # Content-addressed image tag. The runtime always points at an
  # immutable, traceable artifact (no more :latest drift). First 12 chars
  # of sha1 is plenty for a single-repo dedup space and mirrors
  # `git rev-parse --short`'s convention.
  image_source_hash = sha1(join("", [for f in local._image_source_files : filesha1(f)]))
  image_tag         = substr(local.image_source_hash, 0, 12)

  # Full image URI consumed by the AgentCore Runtime (see runtime.tf).
  image_uri = "${aws_ecr_repository.this.repository_url}:${local.image_tag}"

  # ECR registry hostname for `docker login` (the segment before the first /).
  ecr_registry = split("/", aws_ecr_repository.this.repository_url)[0]
}

# Build and push the image. triggers_replace forces a re-run whenever
# the source hash or target URI changes; on replace, Terraform destroys
# and re-creates the resource which re-fires the create-time provisioner.
# There is no destroy-time cleanup — images live on in ECR and are
# pruned by the lifecycle policy in ecr.tf.
resource "terraform_data" "image" {
  triggers_replace = {
    source_hash = local.image_source_hash
    image_uri   = local.image_uri
  }

  # Single bash invocation so a failure points at the exact command.
  # --provenance=false is required: buildx's default OCI image index +
  # attestations is rejected by AgentCore Runtime with an opaque
  # platform-mismatch error.
  provisioner "local-exec" {
    interpreter = ["bash", "-c"]
    working_dir = "${path.module}/.."
    command     = <<-EOT
      set -euo pipefail
      aws ecr get-login-password --region ${var.region} \
        | docker login --username AWS --password-stdin "${local.ecr_registry}"
      docker buildx build \
        --platform linux/arm64 \
        --provenance=false \
        -t "${local.image_uri}" \
        -t "${aws_ecr_repository.this.repository_url}:latest" \
        --push \
        .
    EOT
  }
}
