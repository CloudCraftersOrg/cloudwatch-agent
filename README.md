# cloudwatch-agent

## What it does

This repository deploys an AI agent that talks to a user, explores an AWS account (CloudWatch logs, EC2/RDS/Lambda resources, metrics, alarms), and publishes dashboards in an Amazon Managed Grafana workspace without anyone having to write JSON or learn the Grafana API.

The agent runs as an HTTPS endpoint behind Amazon Bedrock AgentCore Runtime. It reasons with Claude Opus 4.6 on Bedrock, persists conversational state in Bedrock AgentCore Memory, and evaluates itself in two ways. A synchronous judge validates every dashboard JSON before publishing it. A managed AgentCore Evaluator scores each full trace after the fact.

All the infrastructure comes up with Terraform from a clean account. There is a local invocation script (`invoke.py`) with a colorful REPL for talking to the agent from a terminal, and two seed scripts that populate a CloudWatch log group with realistic JSON events so the agent has something concrete to visualize during a demo.

## Architecture

The core component is a Python 3.13 container running on AgentCore Runtime in `us-east-1`. Inside the container, `app/main.py` boots a `BedrockAgentCoreApp` that exposes `POST /invocations`. On every invocation it builds a Strands `Agent` wired with three families of tools.

The first family is three custom tools that live in `app/tools/`. `filter_log_events` reads raw events from CloudWatch Logs without going through Insights, which matters because Insights has indexing lag that can mislead the agent. `discover_resources` enumerates EC2, RDS, and Lambda. `get_cloudwatch_datasource` returns the UID of the CloudWatch datasource that Terraform provisioned inside the Grafana workspace, so the agent does not need to discover it every turn. There is a fourth custom tool, `judge_dashboard_quality`, that invokes a separate Bedrock model with a strict rubric to evaluate the quality of a dashboard JSON before it gets published.

The second family is the tool set exposed by the AWS Labs CloudWatch MCP server (`awslabs.cloudwatch-mcp-server`). It runs as a stdio subprocess inside the same container and contributes around twenty tools for Logs Insights, metrics, alarms, and PromQL queries against CloudWatch Metrics Insights.

The third family is the official Grafana Labs MCP server (`mcp-grafana`, a Go binary built in a dedicated Dockerfile stage and copied into the final container). It provides the full dashboard management surface: searching, reading, creating or updating with full JSON, patching specific changes, listing datasources, generating deep links, and rendering panels as PNG.

To authenticate against Grafana, the agent does not keep a static token. When the container starts, it mints a short-lived EDITOR service-account token by calling the AWS Managed Grafana API, passes it as an environment variable to the `mcp-grafana` subprocess, and tries to delete it on shutdown. If the container restarts often and tokens accumulate, there is an automatic startup cleanup that removes any orphan tokens left behind by previous containers.

Conversational memory is persisted in Bedrock AgentCore Memory, configured with three strategies: summarization for summarizing long sessions, user preference for learning per-user preferences, and semantic facts for extracting relevant facts. Memory is per-session, which means continuity across turns depends on the client sending the same `runtimeSessionId`. The `invoke.py` REPL handles this automatically.

To evaluate agent quality in production, beyond the synchronous judge already mentioned, there is a TRACE-level AgentCore Evaluator configured in LLM-as-a-Judge mode. It reads OTLP spans from the account (in the account-global `aws/spans` log group that CloudWatch Transaction Search creates) and scores them against a rubric similar to the synchronous judge, but after the fact. The scores appear in the AgentCore console under Evaluation.

## Before you start

### AWS account requirements

The agent uses Claude Opus 4.6 through the `us.anthropic.claude-opus-4-6-v1` inference profile, which fans traffic out between `us-east-1`, `us-east-2`, and `us-west-2`. You need to enable Model Access for Anthropic Claude Opus 4.6 in all three regions from the Bedrock console, in each one separately. If you only enable it in `us-east-1` (the main deploy region), the first invocation that lands in another region will fail with `AccessDeniedException` mentioning `aws-marketplace:Subscribe`. The runtime role intentionally lacks marketplace permissions because they are too broad, so the subscription has to be done by a human once.

You need IAM Identity Center enabled in the account. Amazon Managed Grafana requires it so humans can log in. The agent itself does not use Identity Center (it authenticates with a Grafana service account), but the people who view the dashboards do. Identity Center is account-global but its instance lives in one specific region, normally the first one where the account was set up. If that region differs from the deploy region, you need to tell Terraform via an explicit variable, because the data sources that enumerate Identity Center users only see the instance from its home region.

You need to enable CloudWatch Transaction Search in `us-east-1`. That creates the `aws/spans` log group that receives the OTLP spans emitted by AgentCore Observability. Without this, the AgentCore Evaluator has nothing to read from and stays in FAILED state at create time. You enable it once from the CloudWatch console under Application Signals.

### Local tools

You need Terraform 1.9 or higher, Python 3.13 managed by `uv` (the modern way to handle Python virtual environments), Docker with buildx installed, and AWS CLI v2 with valid credentials for the target account. On Intel machines you also need QEMU binfmt registered so buildx can compile `linux/arm64` images (which is the only architecture AgentCore Runtime accepts). This is a one-time command: `docker run --privileged --rm tonistiigi/binfmt --install arm64`. On Apple Silicon this is unnecessary because arm64 is native.

## Step-by-step deploy

The deploy is a single `terraform apply` from a clean account. There is no separate bootstrap step, no need to create ECR ahead of time, no need to push an image by hand. Terraform itself builds the container image and uploads it to ECR as part of the apply, using a `terraform_data.image` resource that invokes `docker buildx build` and `docker push` locally whenever it detects that any source file changed. This means the person running the apply needs Docker running on their machine.

### Step 1. Configure variables

Copy `terraform/terraform.tfvars.example` to `terraform/terraform.tfvars` and edit it. The variables you usually need to set are `identity_center_region` (the region where you enabled Identity Center, for example `us-west-2` if it was the account's first region) and `grafana_admin_user_names` (list of Identity Center user names that should have the ADMIN role in Grafana, normally at least yours). The rest of the variables have reasonable defaults.

If you want an S3 remote backend for the state, copy `terraform/backend.tf.example` to `terraform/backend.tf` and fill in your bucket name. If you leave the backend local, the state stays on disk, which is fine for a demo but not for CI/CD.

### Step 2. Apply Terraform

```bash
cd terraform
terraform init
terraform apply
```

The first run takes between five and ten minutes. Most of that time is spent compiling the `mcp-grafana` binary with Go (downloading the module and building for `linux/arm64`), followed by the build and push of the full agent image. The resources get created in this logical order: the ECR repository, the agent image (build and push), the runtime IAM role, AgentCore Memory with its three strategies, the Amazon Managed Grafana workspace, the workspace service accounts (one admin for Terraform, one EDITOR for the agent at runtime), the CloudWatch datasource inside the workspace, the AgentCore Runtime with its DEFAULT endpoint, the AgentCore Evaluator with the quality rubric, and finally the OnlineEvaluationConfig that wires the evaluator to the `aws/spans` log group.

When the apply finishes, Terraform prints several useful outputs: the runtime ARN, the Grafana URL, the workspace ID, and the ARNs of the evaluator and online evaluation config.

### Step 3. Enable CloudWatch Transaction Search

This step is manual because AWS does not expose a stable Terraform-friendly API for it. Open the AWS console in `us-east-1`, navigate to CloudWatch, find Application Signals in the left menu, open Transaction Search, and press Enable. AWS creates the `aws/spans` log group automatically.

If you skip this step, the AgentCore Evaluator stays in FAILED state saying the log group does not exist. If you already applied and forgot this step, re-apply Terraform after enabling Transaction Search and the evaluator will create cleanly.

### Step 4. Verify things came up

A couple of quick checks help confirm the base is healthy before invoking the agent. The first one is the Grafana workspace: `terraform output grafana_workspace_url` gives you the URL, open it in a browser, you should reach an IAM Identity Center login screen, authenticate, and land on Grafana's home dashboard as ADMIN (because your user is in `grafana_admin_user_names`).

The second check is the agent itself. Make sure you have a seed inyected first (next step), because without data the agent does not have much to say.

### Step 5. Seed demo data

The agent is built to visualize structured CloudWatch logs. To have a concrete demo without waiting for real logs to appear, there are two scripts in `seeds/` that inject realistic JSON events (services `payments`, `orders`, `auth`, `gateway`, plus `checkout` in week two) into the log group `/cloudwatch-agent/demo`.

```bash
uv run python -m seeds.week1 --region us-east-1
```

That command writes several thousand events spread uniformly over the last 60 minutes. The reason the seed uses such a short window is important and explained later in the demo data section. The short version: AWS Logs Insights only indexes events whose timestamp is later than the log group's `creationTime`, so if the seed wrote timestamps from several days ago the agent would see the log group as empty.

When the script finishes, it prints a summary table of how many events were written per service and per level (`INFO`, `WARN`, `ERROR`).

There is a second seed (`seeds/week2`) you can run afterward to introduce changes on top of the first one: it adds a new service (`checkout`), removes a specific `WARN` pattern from `payments`, and inserts a burst of concentrated errors in the `orders` service. This is useful for demonstrating that the agent notices when data changes.

## Interacting with the agent

The recommended way is the `invoke.py` script in the repo root. It has two modes.

### Interactive mode (REPL)

If you run it with no arguments, it opens a REPL with a single session, which means the agent remembers what you said earlier (AgentCore Memory persists history across turns of the same `runtimeSessionId`).

```bash
uv run python invoke.py
```

A banner appears with the runtime ARN, the generated session id, the user, and the available commands. Then a `▸` prompt waits for your input. Every time you send a message and the agent responds, the text comes through as live Markdown (bold, lists, headers, code blocks render with real formatting), followed by a line with the turn's stats (tokens and latency) and another with the path where the raw SSE stream was saved for later inspection.

REPL commands start with a colon. `:help` shows the full list, `:session` prints the current session id, `:new` rotates it to a fresh one (which breaks continuity with AgentCore Memory and starts a brand new conversation), `:raw` toggles between the pretty rendering and the raw mode (which dumps every SSE event verbatim, useful for debugging), and `:exit` quits. Ctrl-D also quits. If you press Ctrl-C while the agent is responding, that stream cancels but the REPL stays alive for the next prompt.

### One-shot mode

If you pass the prompt as an argument or pipe it from stdin, the script runs once and exits. There is no memory continuity unless you pass the same `--session-id` explicitly.

```bash
uv run python invoke.py "List the existing Grafana dashboards"

cat prompts/incident.md | uv run python invoke.py
```

If you want to chain several one-shots and keep memory, generate a long session id (33 characters minimum per AgentCore rules) and pass it to every invocation:

```bash
SID="cwagent-demo-$(date +%s)-aaaaaaaaaaaaaaaaaa"
uv run python invoke.py --session-id "$SID" "Explore the /cloudwatch-agent/demo log group"
uv run python invoke.py --session-id "$SID" "Now create the dashboards you proposed"
```

### What to expect as a response

For an open-ended question like "what can you do", the agent replies with a short list of capabilities and finishes quickly. For something more concrete like "read the demo logs and propose a dashboard set", the agent normally runs a sequence of tool calls (you see them appear as `⚡ tool_name` with a green check when they return), evaluates the proposed JSON internally with `judge_dashboard_quality`, iterates if the judge asks for a revise, publishes the dashboards with `grafana_update_dashboard`, and at the end gives you a list of URLs with names and UIDs.

## Quality and observability

The agent has two complementary evaluation layers.

### Synchronous judge (blocking)

The `judge_dashboard_quality` tool is a Bedrock Converse call (same Opus 4.6 model) with a strict rubric that looks at the dashboard JSON before publishing it. It returns a numeric score, a verdict (`approve`, `revise`, or `reject`), and a list of concrete issues. The agent's system prompt requires calling this tool before every `grafana_update_dashboard` and instructs it to iterate when the verdict is `revise`. This blocks the publication of dashboards with empty queries, wrong datasource UID references, misconfigured queryMode, or uids that do not follow the project convention (`cwagent-overview`, `cwagent-svc-<service>`, `cwagent-incident-<slug>`).

The judge's cost is roughly one extra round-trip to Bedrock per dashboard published. For a set of five dashboards that is ten to fifteen extra seconds and a few thousand tokens. If latency bothers you for the demo, you can change the judge's model via the `JUDGE_MODEL_ID` environment variable pointing at a cheaper Sonnet or Haiku.

### AgentCore Evaluator (post hoc)

Independent of the synchronous judge, there is a TRACE-level AgentCore Evaluator running in the background. It reads OTLP spans that the runtime emits to `aws/spans`, runs them through the rubric hardcoded in `terraform/evaluator.tf` (which penalizes skipping the judge, malformed JSON, hallucinations, and inconsistent naming), and publishes scores in the AgentCore console.

To see them: AWS Console, look for Amazon Bedrock AgentCore (not Amazon Bedrock by itself), enter the service, and pick Evaluation in the left menu. You will see two sections, Custom evaluators (`cloudwatch_agent_quality`) and Online evaluation configurations (`cloudwatch_agent_online_eval`). In the second one, after invoking the agent at least once, scores appear per trace.

If scores do not show up even though the agent was invoked, the most likely cause is the online evaluation config sitting at `executionStatus = DISABLED`. That is controlled by the `execution_status` variable in Terraform and should be `ENABLED`. Another possible cause is that the `service_names` field does not match the real `service.name` AgentCore is emitting in its spans. Terraform uses `<agent-runtime-name>.DEFAULT` by convention.

### Runtime logs and metrics

AgentCore Runtime writes the container's stdout and stderr to a log group named `/aws/bedrock-agentcore/runtimes/<runtime-id>-DEFAULT`. That is where you find Python prints, exception traces, and the initialization logs of the MCP servers. Useful when something fails during the agent's handshake or when a tool raises an error the LLM did not surface to the user.

Runtime metrics (invocations, errors, latency, throttles) live in CloudWatch Metrics under the namespace `AWS/BedrockAgentCore`. The AgentCore console has a native dashboard in the Observability section.

## Demo data (seeds)

The seeds live in `seeds/week1.py` and `seeds/week2.py`. Each one declares service profiles (how many events per level per day, what message templates, which status codes, which latency ranges) and optionally an incident burst. The shared logic lives in `seeds/_common.py`.

The important implementation detail is that the seeds do not backdate events. All timestamps are distributed uniformly over the last 60 minutes counting from the moment you run the script. This decision is deliberate and stems from a poorly documented limitation of CloudWatch Logs Insights: Insights only indexes events whose timestamp is later than the log group's `creationTime`. If the seed put timestamps from 13 days ago into a log group created 13 hours ago, those events sit in the log group (you see them in the Log events tab of the console and `filter_log_events` reads them just fine), but Insights refuses to touch them and returns `MalformedQueryException`. That breaks the agent's flow because it prefers Insights for aggregations.

The practical consequence: do not delete the log group between runs. If you want to reset the data without losing the indexability property, delete the streams individually, not the whole log group:

```bash
for s in $(aws logs describe-log-streams --region us-east-1 \
            --log-group-name /cloudwatch-agent/demo \
            --query 'logStreams[].logStreamName' --output text); do
  aws logs delete-log-stream --region us-east-1 \
    --log-group-name /cloudwatch-agent/demo --log-stream-name "$s"
done
```

Then you can run the seed again.

## Common tasks

### Changing the Claude model

By default the agent uses the inference profile `us.anthropic.claude-opus-4-6-v1`. If you want to use Sonnet or Haiku, edit the `MODEL_ID` environment variable on the runtime (in `terraform/runtime.tf`) and also the default value in `app/config.py`. Remember to enable Model Access for the new model in the fan-out regions (`us-east-1`, `us-east-2`, `us-west-2`), and to adjust the model ARN in the `InvokeBedrockModels` statement of the IAM (`terraform/iam.tf`).

### Granting Grafana access to more people

There are two mechanisms. The first is automatic: the `grafana_grant_all_users_role` variable (default `VIEWER`) gives the role you indicate to every user in the Identity Store. If you want everyone to be able to edit dashboards, set it to `EDITOR`. If you want to turn off the auto-grant, set it to `""`. The second mechanism is per-user: add user names to the `grafana_admin_user_names` array and those become ADMIN specifically. Re-apply Terraform after changing either of them.

### Switching regions

Almost everything is parameterized by `var.region` and moves by changing that value in `terraform.tfvars`. The exceptions are the three fan-out regions of the Claude inference profile, hardcoded in the IAM wildcards (no need to change them unless you migrate to a different inference profile), and the account-global `aws/spans` log group, which lives in the deploy region. If Identity Center lives in a different region from the new deploy region, also adjust `identity_center_region`.

### Pausing the AgentCore Evaluator without destroying it

Set `execution_status = "DISABLED"` in `terraform/evaluator.tf` and apply. To reactivate it, set it back to `"ENABLED"` and apply. You can also do this from the AgentCore console, but keep in mind that the next `terraform apply` will overwrite the manual change unless you also modify the code.

## Troubleshooting

### The agent says it cannot find events in `/cloudwatch-agent/demo`

There are three usual causes. First, the seed never ran in the deploy region (check `aws logs describe-log-groups --region <region>` and verify that `storedBytes` is greater than zero). Second, the seed did run but the timestamps fell before the log group's `creationTime` (this happens if you deleted the log group and re-ran the seed; see the demo data section). Third, the account does not have Transaction Search enabled and the agent tried Insights before indexing catches up (wait a few minutes and retry, or explicitly ask the agent to use `filter_log_events`).

### Grafana login returns `sso.auth.access-denied`

Your Identity Center user does not have a role association on the workspace. Verify that you are in `grafana_admin_user_names` or that `grafana_grant_all_users_role` is not empty. Also check that `identity_center_region` points to the correct region. After any change in those variables, apply Terraform and refresh the Grafana page.

### Bedrock says `aws-marketplace:Subscribe`

That means the Claude model is not enabled in one of the fan-out regions of the inference profile. Open the Bedrock console in `us-east-1`, `us-east-2`, and `us-west-2` separately, and enable Anthropic Claude Opus 4.6 in each.

### `Service Account Token quota has been reached`

AMG limits active tokens per service account (around ten). If AgentCore Runtime restarted the container many times without a clean shutdown, the tokens minted at startup pile up. The `app/mcp_clients.py` cleans up orphans at startup, but if the IAM role lacks `grafana:ListWorkspaceServiceAccountTokens` (added in recent versions) the cleanup did not work and the limit was reached. Manual fix: list and delete every token for the `cloudwatch-agent` service account:

```bash
WS_ID=$(aws grafana list-workspaces --region us-east-1 \
  --query "workspaces[?name=='cloudwatch_agent'].id | [0]" --output text)
SA_ID=$(aws grafana list-workspace-service-accounts \
  --workspace-id "$WS_ID" --region us-east-1 \
  --query "serviceAccounts[?name=='cloudwatch-agent'].id | [0]" --output text)
for TID in $(aws grafana list-workspace-service-account-tokens \
    --workspace-id "$WS_ID" --service-account-id "$SA_ID" --region us-east-1 \
    --query 'serviceAccountTokens[].id' --output text); do
  aws grafana delete-workspace-service-account-token \
    --workspace-id "$WS_ID" --service-account-id "$SA_ID" \
    --token-id "$TID" --region us-east-1
done
```

Apply Terraform again so the next time the container starts the automatic cleanup runs.

### "Network error" when using the AgentCore web console

The AgentCore Runtime console has a browser-side SSE viewer that is fragile with long responses. The agent filters non-JSON-serializable events server side to keep the payload size under control, but if a response is still large (several MB), the browser may drop the connection and show that generic error. The robust alternative is to invoke from the CLI with `invoke.py`, which uses boto3 directly and handles streams of any size without problems.

### The AgentCore Evaluator stays in `DISABLED`

That means `execution_status` was not set to `ENABLED` at creation time. Check the value in `terraform/evaluator.tf` and apply again. It can also happen that the resource was created but the `aws/spans` log group does not exist (Transaction Search was never enabled), in which case the real state is FAILED and it surfaces as DISABLED.

## Cleanup

To destroy everything:

```bash
cd terraform
terraform destroy
```

This removes the AgentCore Runtime, memory, evaluator rubric, online evaluation config, Grafana workspace (including any dashboards the agent created, its service accounts and tokens), the ECR repository (including all images), the IAM roles, and the runtime's own log group at `/aws/bedrock-agentcore/runtimes/<id>-DEFAULT`.

There are things Terraform does not manage and stay around: the `/cloudwatch-agent/demo` log group (the seeds created it directly) and the `aws/spans` log group (CloudWatch Transaction Search created it). If you want to clean them up:

```bash
aws logs delete-log-group --log-group-name /cloudwatch-agent/demo --region us-east-1
aws logs delete-log-group --log-group-name aws/spans --region us-east-1
```

If `terraform destroy` runs more than 30 days after the last `apply`, the provisioner token used by the Grafana provider to administer the datasource has already expired (the AMG maximum TTL is 30 days) and the destroy will hang trying to delete the datasource. The workaround is to recreate the token first:

```bash
terraform -chdir=terraform apply \
  -replace=aws_grafana_workspace_service_account_token.terraform
terraform -chdir=terraform destroy
```

## Cost notes

The most expensive piece is Bedrock with Claude Opus 4.6, which is billed per input and output token. A typical set of five dashboards consumes between 80,000 and 120,000 tokens in total (across multiple tool-call turns). If token count matters, switching the model to Sonnet or Haiku is worth it (with the caveat that they have less reasoning capacity and dashboard quality drops).

Amazon Managed Grafana bills per active user per month, with different rates for admins/editors versus viewers. The agent's service account does not count as a billable user. People entering through Identity Center do, so it pays to review who you default to the `VIEWER` role.

AgentCore Runtime bills for microVM usage (vCPU-seconds and GB-seconds of memory). For a demo with low traffic it is negligible.

Bedrock AgentCore Memory bills per stored event and per strategy processing. With the three strategies enabled and low session cardinality, it stays very cheap.

CloudWatch Logs bills per GB ingested and stored, which is the usual story. The `aws/spans` log group can grow if you have many invocations; adjust retention if you notice it.

The Bedrock AgentCore Evaluator bills per evaluated trace and per judge model tokens. The online evaluation config's sampling rate is set to 100% by default, which is good for a demo but worth lowering to something like 10% or 20% if you leave the agent running in production.
