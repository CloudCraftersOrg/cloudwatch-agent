# ###########################################################################
# CloudWatch Agent container image.
#
# Single-stage build. We deliberately keep this simple: AgentCore Runtime
# pulls images from ECR on every cold start, so a tighter image only
# matters insofar as it speeds up cold starts — and the heavy dependency
# here (boto3 + Strands + AgentCore SDK) dominates total size regardless
# of any multi-stage trickery.
#
# IMPORTANT: AgentCore Runtime ONLY accepts linux/arm64 images. Building
# on an x86_64 host requires `docker buildx build --platform linux/arm64`.
# Pushing an amd64 image will succeed at the ECR layer but fail at
# Runtime deployment with an opaque platform-mismatch error.
# ###########################################################################
FROM --platform=linux/arm64 public.ecr.aws/docker/library/python:3.13-slim

# Standard Python flags for containerized workloads:
#   - PYTHONDONTWRITEBYTECODE: don't litter /app with .pyc files; image
#     is read-only at runtime so they would be wasted layers.
#   - PYTHONUNBUFFERED: flush stdout/stderr immediately so logs reach
#     CloudWatch (via the OTEL log exporter) without buffering delays.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# OpenTelemetry / observability configuration. AgentCore's built-in
# observability ships traces, metrics and logs to CloudWatch via the
# AWS OTEL distro. These three env vars are the supported entrypoint:
#   - AGENT_OBSERVABILITY_ENABLED: gate flag read by the AgentCore SDK.
#   - OTEL_PYTHON_DISTRO / CONFIGURATOR: tell the OTEL SDK to load the
#     AWS-specific resource detectors and exporters at startup.
ENV AGENT_OBSERVABILITY_ENABLED=true \
    OTEL_PYTHON_DISTRO=aws_distro \
    OTEL_PYTHON_CONFIGURATOR=aws_configurator

WORKDIR /app

# Install uv. We use uv at build time too (rather than pip) to share the
# lock-file resolution with the dev workflow — what `uv sync` produces
# locally is exactly what runs in the container.
RUN pip install --no-cache-dir uv

# Copy dependency manifest first so the (slow) install layer is cached
# across code-only changes. The lock file is included when present so
# the build is fully reproducible.
COPY pyproject.toml uv.lock* ./

# Install runtime dependencies into the system site-packages. We pass
# --no-dev to skip ruff and any other dev-only packages.
RUN uv sync --frozen --no-dev || uv sync --no-dev

# Copy the application code last so source edits don't bust the
# dependency layer above.
COPY app/ ./app/

# AgentCore Runtime expects the container to listen on port 8080.
EXPOSE 8080

# Run the agent module directly. ``python -m app.main`` triggers the
# ``if __name__ == "__main__"`` block, which calls ``app.run()`` and
# starts the BedrockAgentCoreApp HTTP server on 0.0.0.0:8080.
CMD ["uv", "run", "python", "-m", "app.main"]
