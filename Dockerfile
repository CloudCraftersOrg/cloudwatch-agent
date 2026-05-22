# ###########################################################################
# CloudWatch Agent container image.
#
# Two-stage build:
#   1. mcp-grafana-builder: ``go install`` builds the Grafana MCP server
#      (https://github.com/grafana/mcp-grafana) for linux/arm64. We
#      bundle it in the image so the agent process can spawn it locally
#      via stdio without depending on Docker-in-Docker or sidecars.
#   2. Final runtime image: Python 3.13 slim + uv-installed deps + the
#      app code + the mcp-grafana binary copied from stage 1.
#
# IMPORTANT: AgentCore Runtime ONLY accepts linux/arm64 images. Building
# on an x86_64 host requires `docker buildx build --platform linux/arm64`.
# Pushing an amd64 image will succeed at the ECR layer but fail at
# Runtime deployment with an opaque platform-mismatch error.
# ###########################################################################

# --- Stage 1: build the Grafana MCP server binary -------------------------
# golang:1.24-bookworm is required: mcp-grafana v0.7.0 declares
# ``go >= 1.24.6`` in its go.mod, so a 1.23.x toolchain fails build
# with "requires go >= 1.24.6 (running go 1.23.x)". Bump in lockstep
# with MCP_GRAFANA_VERSION if a future release raises the minimum.
FROM --platform=linux/arm64 golang:1.24-bookworm AS mcp-grafana-builder

# Pin a recent mcp-grafana release. Bump deliberately; ``@latest`` would
# pull whatever HEAD is at build time and undermine reproducibility.
ARG MCP_GRAFANA_VERSION=v0.7.0

# ``go install`` fetches the module + dependencies and produces a static
# binary at /go/bin/mcp-grafana. CGO disabled for a fully static binary
# that can run on python:3.13-slim without extra shared libs.
ENV CGO_ENABLED=0
RUN go install "github.com/grafana/mcp-grafana/cmd/mcp-grafana@${MCP_GRAFANA_VERSION}"

# --- Stage 2: runtime image ------------------------------------------------
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

# Bring in the mcp-grafana binary built in stage 1. The agent spawns it
# via stdio at boot (see app/mcp_clients.py); placing it in
# /usr/local/bin keeps it on the default PATH so the subprocess can be
# launched by short name.
COPY --from=mcp-grafana-builder /go/bin/mcp-grafana /usr/local/bin/mcp-grafana

# Copy the application code last so source edits don't bust the
# dependency layer above.
COPY app/ ./app/

# AgentCore Runtime expects the container to listen on port 8080.
EXPOSE 8080

# Run the agent module directly. ``python -m app.main`` triggers the
# ``if __name__ == "__main__"`` block, which calls ``app.run()`` and
# starts the BedrockAgentCoreApp HTTP server on 0.0.0.0:8080.
#
# Observability note: do NOT wrap with ``opentelemetry-instrument``.
# The AWS OTEL distro defaults its OTLP exporter to localhost:4317
# and the AgentCore microVM has no collector there, so every span
# export blocks until the client timeout. To re-enable traces, set
# OTEL_EXPORTER_OTLP_ENDPOINT to AgentCore's managed endpoint
# explicitly before re-introducing the wrapper.
CMD ["uv", "run", "python", "-m", "app.main"]
