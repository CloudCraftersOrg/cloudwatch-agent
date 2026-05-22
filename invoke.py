"""Invoke the cloudwatch_agent runtime from the CLI.

Convenience client so you can talk to the deployed agent without going
through the AgentCore Console (whose SSE viewer is fragile for large
streams) or the ``aws bedrock-agentcore`` CLI subcommand (not present
in older AWS CLIs). Uses boto3 — the same SDK the agent itself uses —
and prints a summarized live view of the streaming response.

Modes::

    # Interactive REPL (default when no prompt arg and stdin is a TTY).
    # Same session id across all turns -> AgentCore Memory carries
    # context. Use :new to rotate the session, :exit (or Ctrl-D) to quit.
    uv run python invoke.py
    >>> Read /cloudwatch-agent/demo logs and propose a dashboard set
    >>> Now create the dashboards you described.
    >>> :exit

    # One-shot from positional arg.
    uv run python invoke.py "List the existing Grafana dashboards"

    # Multi-turn one-shots: reuse the same --session-id across calls.
    SID="cwagent-demo-$(date +%s)-aaaaaaaaaaaaaaaaaa"
    uv run python invoke.py --session-id "$SID" "Discover the log groups first."
    uv run python invoke.py --session-id "$SID" "Now build the dashboards."

    # Long prompt piped from stdin / a file (one-shot, no REPL).
    cat prompts/incident.md | uv run python invoke.py --session-id "$SID"

    # Dump the raw SSE stream verbatim (no summarization) for debugging.
    uv run python invoke.py --raw "list the workspaces"

Requirements:
  - AWS credentials in the environment with permission
    ``bedrock-agentcore:InvokeAgentRuntime`` and
    ``bedrock-agentcore-control:ListAgentRuntimes`` (your SSO /
    Administrator session typically has both).
  - ``boto3`` available — the project's ``.venv`` already includes it
    (so ``uv run python invoke.py …`` works out of the box).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import boto3
from botocore.config import Config
from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.spinner import Spinner
from rich.text import Text

# AgentCore Runtime streams SSE for the full life of an invocation. A
# normal turn (1-3 tool calls + a model summary) fits comfortably in
# 60 s, but Opus 4.6 + a multi-KB tool result + the agent's
# max_iterations=10 ceiling can stretch a single turn to a few
# minutes when the model reasons heavily. 300 s is wide enough to see
# any legitimate turn finish, narrow enough that a runaway loop fails
# loudly instead of hanging the CLI for 15 minutes. ``max_attempts=1``
# disables retries — auto-retry on a streaming RPC would re-invoke
# the agent from scratch, duplicating tool calls and tokens, which is
# never the right behavior here.
_RUNTIME_BOTO_CONFIG = Config(
    connect_timeout=10,
    read_timeout=300,
    retries={"max_attempts": 1, "mode": "standard"},
)

# stdout console for assistant output (tool calls, results, final text).
# stderr console for the banner / status / errors so the assistant
# response can be cleanly redirected (e.g. ``invoke.py "…" > out.txt``).
console = Console(highlight=False)
err_console = Console(stderr=True, highlight=False)


# ---------------------------------------------------------------------------
# Activity indicator (spinner with live elapsed time)
# ---------------------------------------------------------------------------
#
# The agent has long "silent" stretches — model thinking before
# emitting the first token, tool calls running for 5-30 s, judge
# data-plane validation parallel-firing 10 Insights queries. Without
# any feedback the CLI looks frozen and feels much slower than it
# actually is. The indicator below shows a ``rich`` spinner with a
# current-phase label and a live elapsed-seconds counter on every
# silent stretch, then steps aside the moment the agent emits text
# or finishes a tool. A daemon thread refreshes the timer twice a
# second so the user can see the clock move.
#
# This is a small ``rich.Live`` region (one line) — well within
# repaint-safe territory, unlike the earlier "wrap the whole buffer
# in Live" approach that broke on long content.


class ActivityIndicator:
    """Single-line spinner + label + elapsed-time counter.

    Safe to ``stop()`` repeatedly; ``start(label)`` replaces any
    running spinner with a new one. Use ``start("thinking")``
    whenever the agent goes silent (between events), and ``stop()``
    the instant the agent emits something visible.
    """

    def __init__(self, console: Console) -> None:
        self._console = console
        self._live: Live | None = None
        self._spinner: Spinner | None = None
        self._label: str = ""
        self._start_time: float = 0.0
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self, label: str, spinner_name: str = "dots") -> None:
        self.stop()
        self._label = label
        self._start_time = time.time()
        self._spinner = Spinner(spinner_name, text=self._format(), style="cyan")
        self._live = Live(
            self._spinner,
            console=self._console,
            refresh_per_second=12,
            transient=True,
        )
        self._live.start()
        # Daemon thread keeps the elapsed counter ticking even when no
        # SSE events arrive (the agent is mid-think or mid-tool).
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._tick, daemon=True)
        self._thread.start()

    def update_label(self, label: str) -> None:
        """Change the label without restarting the spinner / timer."""
        self._label = label
        if self._spinner is not None:
            self._spinner.update(text=self._format())

    def stop(self) -> None:
        if self._thread is not None:
            self._stop_event.set()
            # No join: thread is daemon and tick interval is short,
            # avoids any risk of blocking the event loop here.
            self._thread = None
        if self._live is not None:
            try:
                self._live.stop()
            except Exception:  # noqa: BLE001
                pass
            self._live = None
            self._spinner = None

    def _tick(self) -> None:
        # 0.5 s interval matches refresh_per_second=12 well enough
        # for the timer to look smooth without spamming Console.
        while not self._stop_event.is_set():
            if self._spinner is not None:
                self._spinner.update(text=self._format())
            self._stop_event.wait(0.5)

    def _format(self) -> Text:
        elapsed = time.time() - self._start_time
        line = Text()
        line.append(self._label, style="bold cyan")
        line.append(f"  {elapsed:>5.1f}s", style="dim")
        return line


# Single module-level indicator — the SSE handler is sequential, so
# there's never more than one phase active at a time.
_activity = ActivityIndicator(console)


# ---------------------------------------------------------------------------
# Tool-result summarizers
# ---------------------------------------------------------------------------
#
# Tool result payloads come back as the JSON the tool returned, wrapped
# in a Bedrock ``toolResult`` envelope. The previous renderer only
# showed the byte size, which is unhelpful. The summarizers below
# inspect the result for well-known fields per tool and produce a
# short, human-meaningful one-liner ("5 services ranked",
# "approved (9/10)", "published"). Falls back to byte size for tools
# we don't have a specific summary for.


def _summarize_tool_result(tool_name: str, body_text: str) -> str:
    """Best-effort one-liner describing what a tool returned.

    ``tool_name`` is the most recent tool name from
    ``_turn_tool_calls``; ``body_text`` is the concatenated text
    content of the toolResult block (usually a JSON string). All
    parsing is wrapped in try/except so a bad payload never breaks
    the renderer — we just fall back to the byte count.
    """
    size = len(body_text)
    short_size = f"{size:,} b"
    if not body_text:
        return short_size
    try:
        data = json.loads(body_text)
    except (json.JSONDecodeError, ValueError):
        return short_size
    if not isinstance(data, dict):
        return short_size

    # Custom tools (known shapes).
    if tool_name == "rank_services_by_priority":
        services = data.get("services") or []
        if isinstance(services, list):
            return f"{len(services)} services ranked"
    if tool_name == "judge_dashboard_quality":
        verdict = data.get("verdict")
        score = data.get("score")
        if verdict is not None and score is not None:
            return f"{verdict} (score {score})"
    if tool_name == "get_data_window":
        if data.get("empty"):
            return "empty log group"
        tf = data.get("recommended_time_from")
        span = data.get("span_minutes")
        if tf is not None and span is not None:
            return f"time.from={tf}, span={span} min"
    if tool_name == "prune_dashboards_to_top_set":
        kept = len(data.get("kept", []) or [])
        deleted = len(data.get("deleted", []) or [])
        return f"kept {kept}, deleted {deleted}"
    if tool_name == "delete_grafana_dashboard":
        if data.get("ok"):
            return f"deleted {data.get('uid', '?')}"
        return f"failed: {str(data.get('error') or '?')[:60]}"
    if tool_name == "filter_log_events":
        # filter_log_events returns a JSON array, not a dict — caught
        # earlier by the isinstance(data, dict) guard; handled below.
        pass

    # cw_mcp_describe_log_groups returns log_group_metadata array.
    log_groups = data.get("log_group_metadata")
    if isinstance(log_groups, list):
        return f"{len(log_groups)} log groups"

    # Grafana MCP update_dashboard returns {url, uid, version, ...}.
    if "uid" in data and "url" in data:
        return f"published {data['uid']}"

    return short_size


def _summarize_tool_result_root(tool_name: str, body_text: str) -> str:
    """Handle tools whose JSON root is a list (e.g. filter_log_events)."""
    if not body_text:
        return "0 events"
    try:
        data = json.loads(body_text)
    except (json.JSONDecodeError, ValueError):
        return f"{len(body_text):,} b"
    if isinstance(data, list):
        if tool_name == "filter_log_events":
            return f"{len(data)} events"
        return f"{len(data)} items"
    return _summarize_tool_result(tool_name, body_text)


# ---------------------------------------------------------------------------
# Streaming-text renderer
# ---------------------------------------------------------------------------
#
# Assistant text arrives token-by-token over the SSE stream. We buffer
# each chunk silently and render the FULL block as rich Markdown once
# at block close — so bold, lists, headers, and code blocks render
# properly without ever needing to walk the cursor back through
# previously-printed lines. The old approach streamed each chunk live
# and then tried to re-render as Markdown by writing ``\033[<n>F\033[J``;
# the row counter drifted on emoji and wrapped lines and ended up
# wiping previous prompts in REPL mode.
#
# Trade-off vs live streaming: prose no longer appears character by
# character. To keep the experience feeling responsive, the activity
# spinner updates to ``writing`` once the first text delta of a block
# arrives, so the user can see the model IS emitting tokens even when
# the rendered block won't appear until the model closes it. Each new
# block prints a dim ``↪`` marker as a per-iteration reasoning hint.

_md_buffer: str = ""
# True once the current text block has emitted at least one chunk —
# used so we only print the "reasoning hint" marker on the FIRST
# chunk of a new block, not on every delta.
_md_block_started: bool = False

# Export collector. Every assistant text block closed via ``_md_close``
# is appended here (in order) so ``--export`` can serialize the cleaned
# conversation alongside the raw SSE dump. Reset at the start of each
# turn by ``_invoke_once``.
_turn_text_blocks: list[str] = []
# Tool names invoked during the current turn, in call order. Populated
# from contentBlockStart events.
_turn_tool_calls: list[str] = []


def _md_open() -> None:
    global _md_buffer, _md_block_started
    _md_buffer = ""
    _md_block_started = False


def _md_append(text: str) -> None:
    """Buffer one chunk of assistant text. No live print.

    Streaming the chunk live forced a cursor-walk-back at block close
    to replace plain text with rendered Markdown, and the row counter
    drifted on emoji / wrapped lines, wiping previous prompts. We
    now buffer silently and render the entire block in one shot at
    ``_md_close``, so the cursor is never touched and rich Markdown
    (bold / lists / code blocks) renders correctly.

    On the FIRST chunk of a new block we flip the activity spinner
    label to ``writing`` so the user can see the model is actively
    emitting tokens — the buffer itself won't be visible until the
    block closes.
    """
    global _md_buffer, _md_block_started
    if not text:
        return
    if not _md_block_started:
        _activity.update_label("✏️  writing")
        _md_block_started = True
    _md_buffer += text


def _md_close() -> None:
    """Render the buffered block as Markdown statically.

    Stops the activity spinner (so it doesn't paint over the
    Markdown), prints a dim ``↪`` lead-in so each agent iteration is
    visually scannable, then prints the full block via
    ``rich.Markdown`` for proper bold / lists / headers / code
    rendering. Never touches the cursor — terminal history above is
    preserved.
    """
    global _md_buffer, _md_block_started
    if not _md_buffer:
        _md_block_started = False
        return
    _turn_text_blocks.append(_md_buffer)
    # The spinner is a one-line rich.Live region; killing it before
    # printing the Markdown block keeps its transient cleanup from
    # clipping the first line of the rendered output.
    _activity.stop()
    console.print()  # blank line for breathing room
    console.print("  [dim]↪[/]", highlight=False)
    console.print(
        Markdown(_md_buffer, code_theme="monokai", justify="left")
    )
    _md_buffer = ""
    _md_block_started = False

# ---------------------------------------------------------------------------
# Discovery + helpers
# ---------------------------------------------------------------------------

def _load_dotenv(path: str = ".env") -> dict[str, str]:
    """Parse a simple KEY=VALUE .env file. Quotes are stripped."""
    env: dict[str, str] = {}
    p = Path(path)
    if not p.exists():
        return env
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def _tf_output(tf_dir: str) -> dict[str, Any]:
    """Run ``terraform output -json`` in ``tf_dir`` and return the parsed
    mapping, or ``{}`` if the directory does not exist, the binary is
    missing, or the command fails. Errors are surfaced as dim warnings,
    never raised — the caller will fall through to the next strategy.
    """
    tf_path = Path(tf_dir).expanduser().resolve()
    if not tf_path.is_dir():
        return {}
    try:
        result = subprocess.run(
            ["terraform", "output", "-json"],
            cwd=str(tf_path),
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        err_console.print(f"[dim yellow]terraform output skipped: {exc}[/]")
        return {}
    if result.returncode != 0:
        err_console.print(
            f"[dim yellow]terraform output failed: "
            f"{result.stderr.strip()[:200]}[/]"
        )
        return {}
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        err_console.print(f"[dim yellow]terraform output not JSON: {exc}[/]")
        return {}


def _arn_from_terraform(tf_dir: str) -> str | None:
    """Look up the runtime ARN in ``terraform output``.

    Accepts any of a few common output key names so the script works
    whether the user named their output ``agent_runtime_arn``,
    ``runtime_arn``, or ``agentcore_runtime_arn``.
    """
    tf = _tf_output(tf_dir)
    for key in ("agent_runtime_arn", "runtime_arn", "agentcore_runtime_arn"):
        obj = tf.get(key)
        if not obj:
            continue
        value = obj.get("value") if isinstance(obj, dict) else obj
        if value:
            return str(value)
    return None


def _resolve_runtime_arn(
    region: str, runtime_name_prefix: str, tf_dir: str
) -> str:
    """Resolve the runtime ARN by trying every strategy in order.

    The chain is:
      1. ``AGENT_RUNTIME_ARN`` in the environment.
      2. ``AGENT_RUNTIME_ARN`` in a ``.env`` file in CWD.
      3. ``terraform output`` in ``tf_dir`` (accepts a few key names).
      4. ``bedrock-agentcore-control:ListAgentRuntimes`` filtered by
         ``runtime_name_prefix`` (the original behavior).

    The first hit wins and is annotated in stderr so the user can see
    where the ARN came from. The function ``sys.exit``s with a clear
    error if everything fails — the caller never has to ``None``-check.
    """
    arn = os.environ.get("AGENT_RUNTIME_ARN", "").strip()
    if arn:
        err_console.print("[dim]ARN from env var[/]")
        return arn

    arn = _load_dotenv(".env").get("AGENT_RUNTIME_ARN", "").strip()
    if arn:
        err_console.print("[dim]ARN from .env[/]")
        return arn

    arn = _arn_from_terraform(tf_dir)
    if arn:
        err_console.print(f"[dim]ARN from terraform output ({tf_dir})[/]")
        return arn

    # Final fallback: discover by name prefix via the control plane.
    err_console.print(
        f"[dim]ARN via list-agent-runtimes (prefix='{runtime_name_prefix}')[/]"
    )
    return _find_runtime_arn(region, runtime_name_prefix)


def _find_runtime_arn(region: str, runtime_name_prefix: str) -> str:
    """List AgentCore runtimes and return the ARN matching a name prefix.

    AgentCore runtime names are unique within an account, but other
    runtimes (workshops, experiments) may live in the same account.
    Filtering by prefix and erroring on ambiguity avoids the classic
    "invoked the wrong agent and got pseudocode back" bug.
    """
    ctl = boto3.client("bedrock-agentcore-control", region_name=region)
    runtimes = ctl.list_agent_runtimes().get("agentRuntimes", [])
    matches = [
        r for r in runtimes
        if r["agentRuntimeName"].startswith(runtime_name_prefix)
    ]
    if not matches:
        available = ", ".join(r["agentRuntimeName"] for r in runtimes) or "(none)"
        sys.exit(
            f"No AgentCore runtime found with name starting with "
            f"'{runtime_name_prefix}' in {region}.\nAvailable: {available}"
        )
    if len(matches) > 1:
        names = ", ".join(r["agentRuntimeName"] for r in matches)
        sys.exit(
            f"Multiple runtimes match prefix '{runtime_name_prefix}': "
            f"{names}\nUse --runtime-name with a more specific prefix."
        )
    return matches[0]["agentRuntimeArn"]


def _fresh_session_id() -> str:
    """Generate a session id that satisfies AgentCore's ``>= 33 chars`` rule."""
    return f"cwagent-cli-{uuid.uuid4().hex}"


# ---------------------------------------------------------------------------
# SSE event interpretation
# ---------------------------------------------------------------------------
#
# The agent emits two relevant event families on its stream
# (the rest is filtered out by app/main.py before they ever leave the
# container):
#
#   1. Bedrock Converse-stream events wrapped as ``{"event": {...}}``:
#      messageStart / contentBlockStart / contentBlockDelta /
#      contentBlockStop / messageStop / metadata.
#
#   2. Full message snapshots ``{"message": {role, content[]}}`` emitted
#      between event-loop cycles. These carry full tool-use inputs and
#      tool-result statuses.
#
# Anything else is control-plane noise (init_event_loop, start, etc.)
# and is skipped.


def _render_event(payload: Any) -> None:
    """Pretty-print one SSE payload to the rich console.

    Events come in three relevant shapes (everything else is dropped):

    * Bedrock Converse stream events wrapped as ``{"event": {...}}``:
      ``contentBlockStart`` (open a tool-use or text block),
      ``contentBlockDelta`` (incremental text or tool input),
      ``contentBlockStop`` (close a block),
      ``messageStop`` / ``metadata`` (end-of-turn).
    * Full message snapshots ``{"message": {...}}`` carrying complete
      tool results once a tool call returns.
    * Control signals like ``init_event_loop`` — skipped.

    Text deltas stream straight to the console as plain text (see
    _md_append). Any non-text event closes the current block first
    so a trailing newline keeps the next event on its own row, but
    the text itself stays on screen permanently — no cursor walk-
    back, no risk of wiping earlier prompts.
    """
    if not isinstance(payload, dict):
        return

    # --- Bedrock Converse stream events --------------------------------
    if "event" in payload:
        ev = payload["event"]

        # Tool-use block opens here. We print just the name now (no
        # args yet); the args/input arrive as deltas we skip, and the
        # result is rendered when the matching message snapshot lands.
        # Spinner switches from "thinking" to the tool name so the
        # user sees what is currently running.
        if "contentBlockStart" in ev:
            _md_close()  # any preceding text block ends before this tool starts
            tu = ev["contentBlockStart"].get("start", {}).get("toolUse", {})
            if tu:
                name = tu.get("name", "?")
                _turn_tool_calls.append(name)
                _activity.stop()
                console.print(f"  [yellow]⚡[/] [bold cyan]{name}[/]")
                _activity.start(label=f"⏳ {name}", spinner_name="dots")
            return

        # Incremental deltas. Text deltas feed the silent buffer
        # (rendered as Markdown all at once when the block closes);
        # tool-input deltas are ignored (rendering the model
        # assembling tool args adds noise without value). The spinner
        # keeps running through both — ``_md_append`` flips its label
        # to ``writing`` on the first text chunk so the user can see
        # the model is actively emitting tokens, and ``_md_close``
        # stops it before printing the rendered block.
        if "contentBlockDelta" in ev:
            delta = ev["contentBlockDelta"].get("delta", {})
            if "text" in delta:
                _md_append(delta["text"])
            return

        # End of any content block — explicitly close the markdown
        # stream so the next event (tool call or stats) draws cleanly,
        # and start a "thinking" spinner for the gap until the next
        # block opens.
        if "contentBlockStop" in ev:
            _md_close()
            _activity.start(label="thinking", spinner_name="dots")
            return

        # End-of-turn stats. Stop any in-flight spinner first — the
        # turn is over.
        if "metadata" in ev:
            _md_close()
            _activity.stop()
            usage = ev["metadata"].get("usage", {}) or {}
            metrics = ev["metadata"].get("metrics", {}) or {}
            if usage or metrics:
                in_t = usage.get("inputTokens")
                out_t = usage.get("outputTokens")
                lat = metrics.get("latencyMs")
                in_s = f"{in_t:,}" if isinstance(in_t, int) else str(in_t)
                out_s = f"{out_t:,}" if isinstance(out_t, int) else str(out_t)
                lat_s = f"{lat:,}" if isinstance(lat, int) else str(lat)
                err_console.print(
                    f"\n[dim italic]· {in_s} tokens in   "
                    f"·   {out_s} out   ·   {lat_s} ms[/]"
                )
            return

        if "messageStop" in ev:
            _md_close()
            _activity.stop()
            return
        return

    # --- Tool-result message snapshots ---------------------------------
    if "message" in payload:
        _md_close()
        # Strands also emits envelopes where ``message`` is a plain
        # string (status/error/control signals — e.g. "force_stop"
        # reasons). Those don't carry a Bedrock content[] array; treat
        # them as opaque and skip rendering so the loop doesn't crash
        # on ``str.get``.
        message = payload["message"]
        if not isinstance(message, dict):
            return
        for block in message.get("content", []):
            if not isinstance(block, dict) or "toolResult" not in block:
                continue  # toolUse blocks are already shown by contentBlockStart
            tr = block["toolResult"]
            status = tr.get("status", "?")
            body = tr.get("content", []) or []
            # Tool result closes the per-tool spinner; the next event
            # will either start streaming text (model produces
            # response) or open another tool, each of which restarts
            # its own indicator.
            _activity.stop()
            tool_name = _turn_tool_calls[-1] if _turn_tool_calls else "?"
            body_text = "".join(b.get("text", "") for b in body)
            if status == "success":
                summary = _summarize_tool_result(tool_name, body_text)
                # Some tools (filter_log_events) return a JSON list at
                # the root; the dict-based summarizer can't read those.
                if summary.endswith(" b") and body_text.startswith("["):
                    summary = _summarize_tool_result_root(tool_name, body_text)
                console.print(f"     [green]✓[/] [dim]{summary}[/]")
            else:
                first = body[0].get("text", "")[:200] if body else ""
                console.print(f"     [bold red]✗[/] [red]{first}[/]")
            # After a tool result, the model is usually thinking
            # before either calling another tool or emitting text.
            # Start the thinking spinner so the user sees activity.
            _activity.start(label="thinking", spinner_name="dots")
        return


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _print_block(block: bytes) -> None:
    """Parse one SSE event block and dispatch every event to the renderer."""
    for line in block.splitlines():
        if not line.startswith(b"data:"):
            continue
        payload_str = line[5:].strip().decode("utf-8", errors="replace")
        if not payload_str:
            continue
        try:
            payload = json.loads(payload_str)
        except json.JSONDecodeError:
            # Should not happen any more (app/main.py filters
            # non-JSON-serializable events server-side), but be tolerant.
            continue
        _render_event(payload)


def _invoke_once(
    client,
    arn: str,
    session_id: str,
    user_id: str,
    prompt: str,
    output_path: str,
    raw: bool,
) -> dict[str, Any]:
    """Send one prompt, stream the response, write the raw SSE to a file.

    Returns a dict with the byte count, elapsed time, the cleaned
    assistant text blocks (in model-round order), and the tool-call
    names captured during the turn. Used by ``--export`` to assemble a
    session bundle. On streaming errors (incl. user Ctrl-C mid-stream)
    the partial stream is still flushed and the partial result is
    returned rather than raising.
    """
    global _turn_text_blocks, _turn_tool_calls
    _turn_text_blocks = []
    _turn_tool_calls = []

    t0 = time.time()
    try:
        response = client.invoke_agent_runtime(
            agentRuntimeArn=arn,
            qualifier="DEFAULT",
            runtimeSessionId=session_id,
            payload=json.dumps({"prompt": prompt, "userId": user_id}).encode(),
        )
    except Exception as exc:  # noqa: BLE001
        err_console.print(
            f"\n[bold red]error[/] "
            f"[red]invoke_agent_runtime failed: "
            f"{type(exc).__name__}: {exc}[/]"
        )
        return {
            "prompt": prompt,
            "bytes": 0,
            "elapsed_s": 0.0,
            "assistant_blocks": [],
            "tool_calls": [],
            "raw_path": output_path,
            "error": f"{type(exc).__name__}: {exc}",
        }

    body = response["response"]
    total = 0
    buffer = b""
    interrupted = False
    # Start the "thinking" spinner immediately so the user sees
    # activity even while the agent is still cold-starting / loading
    # context / waiting for its first model response. The spinner
    # auto-stops on the first text delta or tool call (see
    # ``_render_event``).
    if not raw:
        _activity.start(label="thinking", spinner_name="dots")
    with open(output_path, "wb") as raw_file:
        try:
            for chunk in body.iter_chunks(chunk_size=4096):
                raw_file.write(chunk)
                total += len(chunk)
                if raw:
                    # Raw mode: dump the chunk verbatim to stdout.
                    sys.stdout.buffer.write(chunk)
                    sys.stdout.buffer.flush()
                    continue
                buffer += chunk
                while b"\n\n" in buffer:
                    block, _, buffer = buffer.partition(b"\n\n")
                    _print_block(block)
        except KeyboardInterrupt:
            interrupted = True
            _activity.stop()
            _md_close()  # flush the partial text block before exiting
            err_console.print(
                "\n[yellow]⚠ stream interrupted by Ctrl-C[/]"
            )
        # Flush any tail event that didn't end with a blank line.
        if not raw and buffer.strip():
            _print_block(buffer)
        # Final safety net: kill any spinner left behind by a stream
        # that ended without a clean ``messageStop`` / metadata event,
        # then flush the in-flight text block if any.
        _activity.stop()
        _md_close()

    elapsed = time.time() - t0
    suffix = "   [yellow][partial][/]" if interrupted else ""
    err_console.print(
        f"[dim]──  {total:,} bytes   ·   {elapsed:.1f}s   ·   "
        f"raw → {output_path}[/]{suffix}"
    )
    return {
        "prompt": prompt,
        "bytes": total,
        "elapsed_s": round(elapsed, 3),
        "assistant_blocks": list(_turn_text_blocks),
        "tool_calls": list(_turn_tool_calls),
        "raw_path": output_path,
        "interrupted": interrupted,
    }


def _write_export_bundle(
    export_dir: str,
    session_id: str,
    user_id: str,
    region: str,
    arn: str,
    turns: list[dict[str, Any]],
    label: str,
) -> Path:
    """Serialize a session bundle to ``<export_dir>/<label>-<sid>-<ts>.json``.

    The bundle is meant to be human-readable and grep-friendly: prompt,
    cleaned assistant text per round, tool-call sequence, byte counts,
    and pointers back to the raw SSE dumps. The directory is created on
    demand so ``--export`` works with a fresh repo. Returns the path
    written.
    """
    out_dir = Path(export_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    path = out_dir / f"{label}-{session_id[:8]}-{ts}.json"
    bundle = {
        "session_id": session_id,
        "user_id": user_id,
        "region": region,
        "runtime_arn": arn,
        "exported_at": ts,
        "turns": turns,
    }
    path.write_text(
        json.dumps(bundle, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def _per_turn_output(template: str, turn: int) -> str:
    """Build a per-turn raw-stream path from a base template.

    ``/tmp/agent.json`` + turn=3 -> ``/tmp/agent-turn-3.json``.
    Lets REPL mode avoid overwriting the previous turn's raw dump.
    """
    p = Path(template)
    return str(p.with_name(f"{p.stem}-turn-{turn}{p.suffix}"))


_REPL_HELP = """\
Commands:
  :help            show this help
  :new             start a new session (resets AgentCore Memory)
  :session         print the current session id
  :raw             toggle raw SSE output for the next turns
  :exit  /  :quit  exit the REPL (Ctrl-D also exits)

Notes:
  - Lines starting with `:` are commands; anything else is sent as a
    prompt to the agent.
  - Multi-turn memory is ON by default: every turn reuses the same
    session id (printed at startup). `:new` rotates it.
  - Each turn's raw SSE stream is written to a separate file based on
    --output (e.g. /tmp/agent-turn-1.json, -turn-2.json, ...).
"""


def _banner_panel(arn: str, session_id: str, user_id: str, raw: bool) -> Panel:
    """Build the styled startup banner for the REPL."""
    body = Text()
    body.append("runtime  ", style="dim")
    body.append(arn, style="cyan")
    body.append("\nsession  ", style="dim")
    body.append(session_id, style="cyan")
    body.append("  ", style="dim")
    body.append("memory ON", style="dim italic green")
    body.append("\nuser     ", style="dim")
    body.append(user_id, style="cyan")
    body.append("\nraw      ", style="dim")
    body.append("on" if raw else "off", style="cyan")
    body.append("\n\ncommands ", style="dim")
    body.append(":help :new :session :raw :exit", style="bold yellow")
    body.append("\n         ", style="dim")
    body.append("(Ctrl-D exits · Ctrl-C cancels current input/stream)", style="dim italic")
    return Panel(
        body,
        title="[bold cyan]⚡ CloudWatch Agent[/]",
        title_align="left",
        border_style="cyan",
        padding=(1, 2),
    )


def _interactive_repl(args, arn: str, session_id: str) -> int:
    """Multi-turn REPL: prompt -> invoke -> stream -> repeat.

    Memory continuity comes for free because every turn uses the same
    ``session_id``. ``:new`` rotates it (and flushes any pending
    ``--export`` bundle for the previous session). Ctrl-D exits;
    Ctrl-C cancels the current input (or the current in-flight stream)
    without exiting the REPL.
    """
    # readline gives input() history + arrow-key editing for free.
    try:
        import readline  # noqa: F401
    except ImportError:
        pass

    client = boto3.client(
        "bedrock-agentcore",
        region_name=args.region,
        config=_RUNTIME_BOTO_CONFIG,
    )
    raw = args.raw

    err_console.print()
    err_console.print(_banner_panel(arn, session_id, args.user_id, raw))

    turn = 0
    collected_turns: list[dict[str, Any]] = []

    def _flush_export(reason: str) -> None:
        # Local closure so :new and final exit share the same logic.
        if not args.export or not collected_turns:
            return
        path = _write_export_bundle(
            export_dir=args.export_dir,
            session_id=session_id,
            user_id=args.user_id,
            region=args.region,
            arn=arn,
            turns=collected_turns,
            label="repl",
        )
        err_console.print(
            f"[green]✓[/] [dim]exported {len(collected_turns)} turn(s) "
            f"→[/] [cyan]{path}[/] [dim]({reason})[/]"
        )

    while True:
        try:
            line = console.input("\n[bold magenta]▸[/] ").strip()
        except EOFError:
            _flush_export("Ctrl-D")
            err_console.print("\n[dim cyan]bye 👋[/]")
            return 0
        except KeyboardInterrupt:
            err_console.print()  # break the line, fresh prompt
            continue

        if not line:
            continue

        # --- Special commands -----------------------------------------
        if line in (":exit", ":quit", "exit", "quit"):
            _flush_export(":exit")
            err_console.print("[dim cyan]bye 👋[/]")
            return 0
        if line == ":help":
            err_console.print(
                Panel(
                    _REPL_HELP.strip(),
                    title="[bold cyan]help[/]",
                    title_align="left",
                    border_style="dim cyan",
                    padding=(1, 2),
                )
            )
            continue
        if line == ":new":
            _flush_export(":new")
            session_id = _fresh_session_id()
            turn = 0
            collected_turns = []
            err_console.print(
                f"[bold green]✓[/] [dim]new session →[/] [cyan]{session_id}[/]"
            )
            continue
        if line == ":session":
            err_console.print(
                f"[dim]session →[/] [cyan]{session_id}[/]"
            )
            continue
        if line == ":raw":
            raw = not raw
            state = "[green]on[/]" if raw else "[dim]off[/]"
            err_console.print(f"[dim]raw mode →[/] {state}")
            continue
        if line.startswith(":"):
            err_console.print(
                f"[red]unknown command: {line}[/]  [dim](type :help)[/]"
            )
            continue

        # --- Regular prompt -------------------------------------------
        turn += 1
        out_path = _per_turn_output(args.output, turn)
        result = _invoke_once(
            client=client,
            arn=arn,
            session_id=session_id,
            user_id=args.user_id,
            prompt=line,
            output_path=out_path,
            raw=raw,
        )
        if args.export:
            collected_turns.append(result)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="\n".join(__doc__.splitlines()[1:]),
    )
    parser.add_argument(
        "prompt",
        nargs="?",
        help=(
            "Prompt to send. If omitted AND stdin is a TTY, an "
            "interactive REPL starts. If omitted AND stdin is piped, "
            "the full stdin is sent as a single prompt."
        ),
    )
    parser.add_argument(
        "--session-id",
        default=None,
        help=(
            "AgentCore runtime session id (>= 33 chars). Pass the same "
            "value across multiple invocations to share AgentCore "
            "Memory. In REPL mode this is the initial session id; "
            "every turn reuses it until you run ``:new``. Default: a "
            "fresh id generated at startup."
        ),
    )
    parser.add_argument(
        "--user-id",
        default="cli-user",
        help="Identifier sent as the agent's ``userId`` (default: cli-user).",
    )
    parser.add_argument(
        "--region",
        default="us-east-1",
        help="AWS region of the runtime (default: us-east-1).",
    )
    parser.add_argument(
        "--runtime-name",
        default="cloudwatch_agent",
        help="Agent runtime name prefix to invoke (default: cloudwatch_agent).",
    )
    parser.add_argument(
        "--output",
        default="/tmp/agent.json",
        help=(
            "Where to write the raw SSE stream. In REPL mode each "
            "turn gets a suffix: /tmp/agent.json -> "
            "/tmp/agent-turn-1.json, -turn-2.json, ..."
        ),
    )
    parser.add_argument(
        "--raw",
        action="store_true",
        help="Stream every event verbatim instead of the summarized view.",
    )
    parser.add_argument(
        "--tf-dir",
        default="terraform",
        help=(
            "Terraform directory to read ``agent_runtime_arn`` from when "
            "the ARN is not in the environment or a .env file "
            "(default: terraform)."
        ),
    )
    parser.add_argument(
        "--export",
        action="store_true",
        help=(
            "Write a JSON session bundle (prompt + cleaned assistant text "
            "+ tool calls per turn) on exit. In REPL mode the bundle is "
            "also flushed on ``:new`` so each session is preserved."
        ),
    )
    parser.add_argument(
        "--export-dir",
        default="./exports",
        help="Directory for ``--export`` bundles (default: ./exports).",
    )
    args = parser.parse_args()

    session_id = args.session_id or _fresh_session_id()
    if len(session_id) < 33:
        parser.error(
            f"--session-id must be at least 33 chars (got {len(session_id)})"
        )

    arn = _resolve_runtime_arn(
        region=args.region,
        runtime_name_prefix=args.runtime_name,
        tf_dir=args.tf_dir,
    )

    # --- Mode selection -----------------------------------------------
    # 1. positional prompt OR piped stdin  -> one-shot
    # 2. TTY stdin with no positional prompt -> interactive REPL
    one_shot_prompt: str | None = args.prompt
    if not one_shot_prompt and not sys.stdin.isatty():
        one_shot_prompt = sys.stdin.read().strip()

    if one_shot_prompt:
        if not one_shot_prompt.strip():
            parser.error("prompt is empty")
        preview = (
            one_shot_prompt
            if len(one_shot_prompt) < 140
            else one_shot_prompt[:137] + "..."
        )
        header = Text()
        header.append("runtime  ", style="dim")
        header.append(f"{arn}\n", style="cyan")
        header.append("session  ", style="dim")
        header.append(f"{session_id}\n", style="cyan")
        header.append("user     ", style="dim")
        header.append(f"{args.user_id}\n", style="cyan")
        header.append("prompt   ", style="dim")
        header.append(f"{preview}", style="white")
        err_console.print()
        err_console.print(
            Panel(
                header,
                title="[bold cyan]⚡ CloudWatch Agent[/]",
                title_align="left",
                border_style="cyan",
                padding=(1, 2),
            )
        )
        err_console.print()
        client = boto3.client(
        "bedrock-agentcore",
        region_name=args.region,
        config=_RUNTIME_BOTO_CONFIG,
    )
        result = _invoke_once(
            client=client,
            arn=arn,
            session_id=session_id,
            user_id=args.user_id,
            prompt=one_shot_prompt,
            output_path=args.output,
            raw=args.raw,
        )
        if args.export:
            path = _write_export_bundle(
                export_dir=args.export_dir,
                session_id=session_id,
                user_id=args.user_id,
                region=args.region,
                arn=arn,
                turns=[result],
                label="oneshot",
            )
            err_console.print(
                f"[green]✓[/] [dim]exported turn →[/] [cyan]{path}[/]"
            )
        return 0

    return _interactive_repl(args, arn, session_id)


if __name__ == "__main__":
    sys.exit(main())
