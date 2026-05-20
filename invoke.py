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
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import boto3
from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text

# stdout console for assistant output (tool calls, results, final text).
# stderr console for the banner / status / errors so the assistant
# response can be cleanly redirected (e.g. ``invoke.py "…" > out.txt``).
console = Console(highlight=False)
err_console = Console(stderr=True, highlight=False)


# ---------------------------------------------------------------------------
# Streaming-markdown renderer
# ---------------------------------------------------------------------------
#
# The agent's assistant text arrives token-by-token over the SSE stream.
# We accumulate it in a buffer and re-render the buffer as Markdown via
# rich.Live a few times per second, so the user sees the response
# appearing live AND formatted (bold, lists, headers, code blocks) —
# the same UX Claude.ai / ChatGPT have, instead of literal ``**bold**``.
#
# The live region is opened lazily on the first text delta and closed
# whenever a non-text event interrupts the text block (a tool starts,
# the message ends, the turn metadata arrives, or the stream is
# cancelled). That guarantees no other ``console.print`` call clashes
# with the in-place updating Live region.

_md_buffer: str = ""
_md_live: Live | None = None


def _md_open() -> None:
    global _md_live, _md_buffer
    if _md_live is not None:
        return
    _md_buffer = ""
    _md_live = Live(
        Markdown(""),
        console=console,
        refresh_per_second=12,
        vertical_overflow="visible",
        transient=False,
    )
    _md_live.start()


def _md_append(text: str) -> None:
    global _md_buffer
    if _md_live is None:
        _md_open()
    _md_buffer += text
    # ``code_theme`` could be customized; the default monokai-style
    # works great on dark terminals. justify="left" prevents rich from
    # centering short final lines.
    _md_live.update(Markdown(_md_buffer, code_theme="monokai", justify="left"))


def _md_close() -> None:
    global _md_live, _md_buffer
    if _md_live is None:
        return
    _md_live.stop()
    _md_live = None
    _md_buffer = ""

# ---------------------------------------------------------------------------
# Discovery + helpers
# ---------------------------------------------------------------------------

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

    Text deltas accumulate in the streaming-markdown buffer (rendered
    via rich.Live above). Any other event closes the live region first
    so its own ``console.print`` call doesn't clash with the
    in-place-updating Markdown.
    """
    if not isinstance(payload, dict):
        return

    # --- Bedrock Converse stream events --------------------------------
    if "event" in payload:
        ev = payload["event"]

        # Tool-use block opens here. We print just the name now (no
        # args yet); the args/input arrive as deltas we skip, and the
        # result is rendered when the matching message snapshot lands.
        if "contentBlockStart" in ev:
            _md_close()  # any preceding text block ends before this tool starts
            tu = ev["contentBlockStart"].get("start", {}).get("toolUse", {})
            if tu:
                console.print(
                    f"  [yellow]⚡[/] [bold cyan]{tu.get('name', '?')}[/]"
                )
            return

        # Incremental deltas. Text deltas feed the live-markdown stream;
        # tool-input deltas are ignored (rendering the model assembling
        # tool args adds noise without value).
        if "contentBlockDelta" in ev:
            delta = ev["contentBlockDelta"].get("delta", {})
            if "text" in delta:
                _md_append(delta["text"])
            return

        # End of any content block — explicitly close the markdown
        # stream so the next event (tool call or stats) draws cleanly.
        if "contentBlockStop" in ev:
            _md_close()
            return

        # End-of-turn stats.
        if "metadata" in ev:
            _md_close()
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
            return
        return

    # --- Tool-result message snapshots ---------------------------------
    if "message" in payload:
        _md_close()
        for block in payload["message"].get("content", []):
            if "toolResult" not in block:
                continue  # toolUse blocks are already shown by contentBlockStart
            tr = block["toolResult"]
            status = tr.get("status", "?")
            body = tr.get("content", []) or []
            if status == "success":
                size = sum(len(b.get("text", "")) for b in body)
                console.print(f"     [green]✓[/] [dim]{size:,} b[/]")
            else:
                first = body[0].get("text", "")[:200] if body else ""
                console.print(f"     [bold red]✗[/] [red]{first}[/]")
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
) -> int:
    """Send one prompt, stream the response, write the raw SSE to a file.

    Returns the byte count streamed. On streaming errors (incl. user
    Ctrl-C mid-stream) the partial stream is still flushed and the
    function returns the bytes received so far rather than raising.
    """
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
        return 0

    body = response["response"]
    total = 0
    buffer = b""
    interrupted = False
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
            _md_close()  # don't leave the Live region dangling
            err_console.print(
                "\n[yellow]⚠ stream interrupted by Ctrl-C[/]"
            )
        # Flush any tail event that didn't end with a blank line.
        if not raw and buffer.strip():
            _print_block(buffer)
        # Final safety net: close the markdown stream if it's still
        # open (e.g. the response ended without an explicit
        # contentBlockStop event for the text block).
        _md_close()

    elapsed = time.time() - t0
    suffix = "   [yellow][partial][/]" if interrupted else ""
    err_console.print(
        f"[dim]──  {total:,} bytes   ·   {elapsed:.1f}s   ·   "
        f"raw → {output_path}[/]{suffix}"
    )
    return total


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
    ``session_id``. ``:new`` rotates it. Ctrl-D exits; Ctrl-C cancels
    the current input (or the current in-flight stream) without
    exiting the REPL.
    """
    # readline gives input() history + arrow-key editing for free.
    try:
        import readline  # noqa: F401
    except ImportError:
        pass

    client = boto3.client("bedrock-agentcore", region_name=args.region)
    raw = args.raw

    err_console.print()
    err_console.print(_banner_panel(arn, session_id, args.user_id, raw))

    turn = 0
    while True:
        try:
            line = console.input("\n[bold magenta]▸[/] ").strip()
        except EOFError:
            err_console.print("\n[dim cyan]bye 👋[/]")
            return 0
        except KeyboardInterrupt:
            err_console.print()  # break the line, fresh prompt
            continue

        if not line:
            continue

        # --- Special commands -----------------------------------------
        if line in (":exit", ":quit", "exit", "quit"):
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
            session_id = _fresh_session_id()
            turn = 0
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
        _invoke_once(
            client=client,
            arn=arn,
            session_id=session_id,
            user_id=args.user_id,
            prompt=line,
            output_path=out_path,
            raw=raw,
        )


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
    args = parser.parse_args()

    session_id = args.session_id or _fresh_session_id()
    if len(session_id) < 33:
        parser.error(
            f"--session-id must be at least 33 chars (got {len(session_id)})"
        )

    arn = _find_runtime_arn(args.region, args.runtime_name)

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
        client = boto3.client("bedrock-agentcore", region_name=args.region)
        _invoke_once(
            client=client,
            arn=arn,
            session_id=session_id,
            user_id=args.user_id,
            prompt=one_shot_prompt,
            output_path=args.output,
            raw=args.raw,
        )
        return 0

    return _interactive_repl(args, arn, session_id)


if __name__ == "__main__":
    sys.exit(main())
