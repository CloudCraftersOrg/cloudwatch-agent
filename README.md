# cloudwatch-agent

## What it does

`cloudwatch-agent` is an AI agent that inspects an AWS account's CloudWatch
state — metrics, log groups, and the EC2 / RDS / Lambda resources that emit
them — and publishes tailored **Grafana dashboards** to an **Amazon Managed
Grafana** workspace on the user's behalf. Dashboards query CloudWatch
directly through Grafana's built-in CloudWatch data source (no Prometheus
involved). The agent runs as a managed HTTPS endpoint on **Amazon Bedrock
AgentCore Runtime**, uses **Claude Opus 4.6** on Bedrock for reasoning, and
persists conversation state and learned user preferences in **Bedrock
AgentCore Memory**. The reasoning loop and tool dispatch are implemented
with the **Strands Agents** SDK.

To write dashboards the agent does not hold a long-lived Grafana key:
its IAM role mints a short-lived Grafana service-account token per
request via the AWS Grafana API, uses it against the Grafana HTTP API,
and deletes it immediately.

## Architecture

```
                +-----------------------------+
                |          End user           |
                | (CLI / app / SigV4 client)  |
                +--------------+--------------+
                               |
                               | HTTPS (SigV4, IAM auth)
                               v
                +-----------------------------+
                | Bedrock AgentCore Runtime   |
                |  (managed microVM, ARM64)   |
                +--------------+--------------+
                               |
                               | invokes container at POST /invocations
                               v
                +-----------------------------+
                |    Strands Agent (app/)     |
                |  - System prompt            |
                |  - Tool dispatch loop       |
                +--+-----------+-----------+--+
                   |           |           |
                   v           v           v
           +--------------+  +-----+  +-----------------+
           | Bedrock Opus |  | CW  |  | AgentCore       |
           |   4.6 model  |  | EC2 |  | Memory          |
           |  (reasoning) |  | RDS |  | (short + long)  |
           +--------------+  | Lam |  +-----------------+
                             | Logs|
                             +--+--+
                                | mint short-lived token,
                                | POST dashboard (Grafana HTTP API)
                                v
                +-----------------------------+
                | Amazon Managed Grafana      |
                |  workspace (SSO login)      |
                |  CloudWatch data source ----+--> CloudWatch
                +-----------------------------+
```

## Prerequisites

- An AWS account with **Anthropic Claude Opus 4.6 enabled in Bedrock**
  via the AWS Console (Bedrock → Model access) **in all three regions
  the cross-region inference profile fans out to: `us-east-1`,
  `us-east-2`, AND `us-west-2`**. Opus 4.6 is inference-profile-only on
  Bedrock (the bare foundation model rejects on-demand invocation with
  `ValidationException`), so the agent uses the `us.anthropic.claude-opus-4-6-v1`
  profile; enabling the model only in your home region (e.g. `us-east-1`)
  is NOT enough — whichever region the profile routes to must be
  subscribed too. Invocations to an un-subscribed region fail with
  `AccessDeniedException ... aws-marketplace:Subscribe`. The runtime
  role does NOT have marketplace permissions on purpose (over-broad);
  the model must be pre-subscribed by a human admin.
- **AWS IAM Identity Center enabled** in the account. Amazon Managed
  Grafana requires it (or SAML) for human login; enabling Identity
  Center is an organization-level action and is intentionally **out of
  scope for this Terraform stack**. Identity Center is account-global
  (region-agnostic), so it doesn't need to match the deploy region. The
  agent itself does not use SSO — it authenticates via a Grafana
  service account.
- **Terraform `>= 1.9`** (AWS provider `~> 6.18`, plus the
  `grafana/grafana` provider `~> 3.0` used to provision the data source).
- **Python 3.13** (managed via `.python-version` and `uv`).
- **uv** for dependency management (`brew install uv` or
  `curl -LsSf https://astral.sh/uv/install.sh | sh`).
- **Docker with buildx** for building ARM64 images locally. AgentCore
  Runtime only accepts `linux/arm64` container images, so the buildx
  multi-arch builder is required even on x86_64 hosts.

## Local development

```bash
# Install runtime + dev dependencies into a project-local .venv.
uv sync

# Start the agent locally. BedrockAgentCoreApp serves the same HTTP
# contract the Runtime uses in production, so this dev loop is faithful
# to the deployed behavior.
uv run python -m app.main   # listens on http://localhost:8080

# Send a test prompt. The endpoint is the same one AgentCore exposes
# in production: POST /invocations with a JSON body.
curl -X POST http://localhost:8080/invocations \
  -H "Content-Type: application/json" \
  -d '{"prompt": "List my CloudWatch namespaces"}'
```

For the local loop, `MEMORY_ID` may be left unset — the agent falls back
to in-process state and skips AgentCore Memory (it never hard-fails on a
missing `MEMORY_ID`). The CloudWatch read tools work locally with any AWS
credentials in your environment. The Grafana dashboard tools need the
four `GRAFANA_*` variables; after a deploy, export them from Terraform
outputs (your local AWS principal also needs
`grafana:CreateWorkspaceServiceAccountToken` on the workspace, which the
runtime role has but a developer profile may not):

```bash
export GRAFANA_WORKSPACE_ID=$(terraform -chdir=terraform output -raw grafana_workspace_id)
export GRAFANA_WORKSPACE_ENDPOINT=$(terraform -chdir=terraform output -raw grafana_workspace_endpoint)
export GRAFANA_SERVICE_ACCOUNT_ID=$(terraform -chdir=terraform output -raw grafana_service_account_id)
export GRAFANA_CLOUDWATCH_DATASOURCE_UID=$(terraform -chdir=terraform output -raw grafana_cloudwatch_datasource_uid)
```

Without them, only the Grafana tools fail (with a clear "missing Grafana
environment variable" error); the rest of the agent still runs.

## Deploy & test (step by step)

Run everything from the repo root. A single `terraform apply` does the
whole deploy: it creates the ECR repository, **builds and pushes the
linux/arm64 agent image itself** (via `terraform_data.image` in
[terraform/build.tf](terraform/build.tf)), then creates the AgentCore
Runtime + Memory + AMG workspace + IAM. The image is tagged with a
content hash of `Dockerfile` + `pyproject.toml` + `uv.lock` + `app/`, so
the runtime always points at an immutable, traceable artifact and there
is no `:latest` drift.

**0. Preconditions**

- AWS credentials for the target account exported in your shell
  (`aws sts get-caller-identity` works), with permission to create the
  resources below and `bedrock-agentcore:InvokeAgentRuntime`.
- Claude Opus 4.6 enabled in Bedrock in `us-east-1` + `us-east-2` +
  `us-west-2` (see Prerequisites for why all three), and IAM Identity
  Center enabled in the account (see Prerequisites).
- `uv`, Terraform ≥ 1.9, Docker with buildx, and AWS CLI v2 installed.
  On an x86_64 host, also QEMU binfmt (`docker run --privileged --rm
  tonistiigi/binfmt --install arm64`) so buildx can cross-compile to
  linux/arm64; on Apple Silicon arm64 is native and you can skip this.

**1. Configure variables (optional)**

```bash
cp terraform/terraform.tfvars.example terraform/terraform.tfvars   # edit if desired
```

This quick test uses **local Terraform state** — do *not* create
`backend.tf`. (`backend.tf` + a real S3 bucket is only needed for the
CI/CD pipeline, where state must be shared; see "CI/CD" below.)

**2. Deploy (single command)**

```bash
terraform -chdir=terraform init
terraform -chdir=terraform apply
```

That's it. The apply creates the ECR repository, then
`terraform_data.image` runs `docker login` + `docker buildx build
--platform linux/arm64 --provenance=false --push` against your local
docker, then the AgentCore Runtime is created pointing at the freshly
pushed content-addressed tag. The Amazon Managed Grafana workspace,
CloudWatch data source, IAM role, and AgentCore Memory + strategies are
created in the same apply. If the `grafana` provider errors because the
workspace is not `ACTIVE` yet, just re-run the apply.

Subsequent applies only rebuild the image when the source files
actually change (the hash drives `triggers_replace`), so a no-op apply
is cheap.

**3. Open the Grafana workspace**

By default, every IAM Identity Center user in the account is granted
the `VIEWER` role on the workspace at apply time (controlled by the
`grafana_grant_all_users_role` variable — set to `"EDITOR"` if you also
want everyone to edit dashboards, `"ADMIN"` for full control, or `""` to
opt out and assign access by hand). For named admin groups, set
`grafana_admin_group_ids` in `terraform.tfvars` and re-apply.

> Identity Center is account-global but its instance lives in **one
> specific region** (the one it was originally enabled in). If that
> region differs from `var.region`, set `identity_center_region` in
> `terraform.tfvars` to point at the Identity Center home region —
> otherwise the auto-grant fails with
> `check "identity_center_present_when_auto_grant_enabled"`.

Then open the workspace:

```bash
terraform -chdir=terraform output -raw grafana_workspace_url
```

If a fresh SSO login lands on `Login failed [sso.auth.access-denied]`,
the user has not been associated yet — re-run `terraform apply` to pick
up new Identity Center users.

**4. Seed week 1 and build the first dashboard set**

```bash
uv run python -m seeds.week1          # prints suggested prompts
RUNTIME_ARN=$(terraform -chdir=terraform output -raw agent_runtime_arn)
# Reuse one session id across the whole demo (>= 33 chars, required by
# AgentCore) so week-2 regeneration shares conversational memory with
# week 1. If you omit --runtime-session-id, the agent generates a fresh
# per-call id and memory will not span invocations.
SESSION_ID="cloudwatch-agent-demo-session-000001"
aws bedrock-agentcore invoke-agent-runtime \
  --region us-east-1 --agent-runtime-arn "$RUNTIME_ARN" --qualifier DEFAULT \
  --runtime-session-id "$SESSION_ID" \
  --payload '{"prompt": "Read the /cloudwatch-agent/demo logs for the last 14 days and create a Grafana dashboard set: an overview plus one dashboard per service."}' \
  /tmp/agent.json && cat /tmp/agent.json
```

**5. Seed week 2 and regenerate**

```bash
uv run python -m seeds.week2          # prints what changed + prompts
# Then invoke again with the SAME --runtime-session-id "$SESSION_ID", e.g.
# "Regenerate the dashboard set for current data, replace any dashboard
# that is now empty, add dashboards for new services, and build an
# incident dashboard for the orders outage."
```

See "Demo data & flow" for exactly what week 2 changes and why. Verify
the results in the Grafana workspace from step 3.

Note: the `grafana/grafana` provider authenticates with a 30-day
provisioner token Terraform creates. If an apply runs more than 30 days
after the previous one, taint it first:
`terraform -chdir=terraform taint aws_grafana_workspace_service_account_token.terraform`.

## CI/CD

Every push to `main` touching `app/**`, `terraform/**`, `Dockerfile`,
`pyproject.toml`, or `uv.lock` runs `.github/workflows/deploy.yml`, which
is exactly one `terraform apply` — the build + push happens inside
Terraform (`terraform_data.image`). The workflow sets up QEMU and
buildx for the cross-arch build, then calls `terraform init` +
`terraform apply -auto-approve`. It requires an `AWS_ROLE_ARN` repo
variable/secret (OIDC, allowing both Terraform-apply and ECR-push
permissions) and a **committed `backend.tf`** with a real S3 bucket —
CI runners are ephemeral, so shared remote state is mandatory there
(unlike the local quick test above).

Because the image tag is the sha1 of the image source files, the same
commit always produces the same tag — no drift between what was last
deployed and what `terraform apply` would deploy now.

## Invoking the agent (reference)

The endpoint is invoked with the AWS CLI (or any SigV4 client). The
payload is JSON with a `prompt` field and an optional `userId`:

```bash
aws bedrock-agentcore invoke-agent-runtime \
  --region us-east-1 \
  --agent-runtime-arn "$(terraform -chdir=terraform output -raw agent_runtime_arn)" \
  --qualifier DEFAULT \
  --runtime-session-id "cloudwatch-agent-demo-session-000001" \
  --payload '{"prompt": "<your prompt>", "userId": "demo"}' \
  /tmp/agent-response.json && cat /tmp/agent-response.json
```

Pass a stable `--runtime-session-id` (33–100 chars) to keep
conversational memory across calls; reuse the same value for a
multi-turn session. It is optional — if omitted, the agent generates a
fresh per-call id and memory simply won't span invocations (it never
fails for a missing session id).

The caller's IAM principal must hold `bedrock-agentcore:InvokeAgentRuntime`
on the runtime ARN. No Cognito or JWT authorizer is configured in v1.

This demo path exercises only the logs and Grafana tools; the metric
and resource-discovery tools are part of the product surface but are
not used by the seeded log-based scenario.

## Demo data & flow

Two operator-run seed scripts populate a single CloudWatch Logs log
group (`/cloudwatch-agent/demo`) with structured JSON events so the agent
has something real to read and visualize. They are demo *setup*, not
agent tools — the agent only reads/builds dashboards when you prompt it.
Run them as part of the step-by-step above (steps 6–7):
`uv run python -m seeds.week1` then, later, `uv run python -m seeds.week2`.

Each script prints a per-service breakdown and concrete suggested
prompts. Phase 2 deliberately changes the data so the agent must adapt:

- The `payments` "retrying downstream dependency" WARN pattern from week
  1 **disappears** → a week-1 payments dashboard goes empty and should be
  regenerated to reflect current behavior.
- A brand-new `checkout` service appears → a new dashboard with new
  scope should be added to the set.
- A sharp `orders` outage (`OrderDBConnectionPoolExhausted`, ~2h window)
  is injected → ask the agent to build a dedicated **incident
  dashboard** for it.

Run each script **exactly once**. Re-runs are not idempotent —
timestamps are recomputed from the wall clock, so CloudWatch accepts the
events as new and the data is duplicated. week2 is designed to append to
week1.

> **Do NOT `aws logs delete-log-group` between seed runs.** Logs Insights
> only indexes events whose timestamp is ≥ the log group's
> `creationTime`. The seeds backdate events 6-13 days into the past
> (the "two real weeks" narrative), so any event written right after
> recreating the log group is invisible to Insights — `start_query`
> returns `MalformedQueryException` or `scanned=0` even though the
> events are stored and the Console "Log events" tab shows them. The
> agent works around this by preferring `filter_log_events` over Logs
> Insights for backdated data (see [app/prompts.py](app/prompts.py)),
> but the Insights MCP tools will not work. To start cleanly, delete
> the individual streams instead so the log group's `creationTime`
> stays old:
>
> ```bash
> for s in payments orders auth gateway checkout; do
>   aws logs delete-log-stream --log-group-name /cloudwatch-agent/demo \
>     --log-stream-name "$s" --region us-east-1 2>/dev/null
> done
> ```

The scripts run with your AWS credentials (not the agent role) and need
`logs:CreateLogGroup`, `logs:CreateLogStream`, `logs:PutLogEvents`, and
`logs:PutRetentionPolicy`. CloudWatch Logs rejects events older than 14
days; the scripts clamp anything past a safe age and warn rather than
failing, so the "two real weeks" window stays valid even if the demo
runs over a few days. The injected incident is anchored to real UTC
midnight, so "≈3 days ago, 14:00–16:00 UTC" holds regardless of the hour
you run the seed (the script also prints the exact window). Override the
log group or region with `--log-group` / `--region`.

## Cleanup

```bash
cd terraform
terraform destroy
```

If more than 30 days have passed since the last apply, the `grafana`
provider's provisioner token has expired and `destroy` will fail when it
tries to delete the CloudWatch data source. Recreate the token first
(`terraform -chdir=terraform apply -replace=aws_grafana_workspace_service_account_token.terraform`),
then `destroy`.

Delete the demo log group separately (Terraform does not manage it):
`aws logs delete-log-group --log-group-name /cloudwatch-agent/demo --region us-east-1`.
This is fine at the very end of the demo (you're tearing the whole
stack down). But if you plan to re-seed and demo again, delete the
streams individually instead (see "Demo data & flow" above), so Logs
Insights stays usable on the next run.

This removes the AgentCore Runtime endpoint, the runtime, the memory
resource, the IAM role, the ECR repository (including all images), and
the Amazon Managed Grafana workspace (including its dashboards and
service accounts).

## Cost notes

- Claude Opus 4.6 is the most expensive Anthropic model on Bedrock. To
  cut inference costs by roughly an order of magnitude, set `MODEL_ID` to
  a Sonnet-class model (e.g. `us.anthropic.claude-sonnet-4-6-v1`) in
  `app/config.py` or as the `MODEL_ID` env var on the runtime.
- Amazon Managed Grafana bills per active user license per month
  (separate rates for admins/editors vs. viewers). The agent's service
  account is not a billable user, but every human you grant SSO access to
  is.
