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
import time
import uuid
from pathlib import Path
from typing import Any

import boto3
from botocore.config import Config
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
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
# Streaming-text renderer
# ---------------------------------------------------------------------------
#
# Assistant text arrives token-by-token over the SSE stream. We write
# each chunk as plain text directly to the console (no live re-render,
# so duplication is impossible regardless of buffer size). When the
# block ends we walk the cursor back over the rows we just printed and
# replace them with a single ``rich.Markdown`` render of the same
# content, so bold / lists / code blocks come through formatted while
# avoiding ``rich.Live``'s known repaint issues on content taller than
# the terminal.

_md_buffer: str = ""
# Number of physical terminal rows we have written for the current
# in-progress text block — used to ``\033[<n>F`` back up and erase the
# plain-text stream before the rendered Markdown takes its place.
_md_rows_printed: int = 0

# Export collector. Every assistant text block closed via ``_md_close``
# is appended here (in order) so ``--export`` can serialize the cleaned
# conversation alongside the raw SSE dump. Reset at the start of each
# turn by ``_invoke_once``.
_turn_text_blocks: list[str] = []
# Tool names invoked during the current turn, in call order. Populated
# from contentBlockStart events.
_turn_tool_calls: list[str] = []


def _terminal_width() -> int:
    # Falls back to a reasonable default if stdout is not a TTY.
    return console.size.width or 80


def _physical_rows(text: str) -> int:
    """How many terminal rows a chunk of text will occupy when printed.

    Counts wrapped lines using the current terminal width. Tabs and
    other control chars are not handled precisely; close enough for
    the cursor-up cleanup, which only needs to overestimate to be
    safe.
    """
    width = _terminal_width()
    rows = 0
    for line in text.split("\n"):
        # Empty lines still occupy a row.
        rows += max(1, (len(line) + width - 1) // width)
    return rows


def _md_open() -> None:
    global _md_buffer, _md_rows_printed
    _md_buffer = ""
    _md_rows_printed = 0


def _md_append(text: str) -> None:
    global _md_buffer, _md_rows_printed
    if not text:
        return
    if _md_buffer == "" and _md_rows_printed == 0:
        _md_open()
    _md_buffer += text
    _md_rows_printed += _physical_rows(text)
    # ``end=""`` + ``soft_wrap=True`` keeps rich from injecting its own
    # newlines: we want the raw token stream to land verbatim so our
    # row counter stays accurate.
    console.print(text, end="", soft_wrap=True, markup=False, highlight=False)


def _md_close() -> None:
    """Replace the streamed plain text with its rendered Markdown form.

    If nothing was streamed since the last close, this is a no-op.
    Otherwise we move the cursor up over the rows we wrote, clear from
    there to the end of the screen, and re-print the buffer once as
    Markdown.
    """
    global _md_buffer, _md_rows_printed
    if not _md_buffer:
        _md_rows_printed = 0
        return
    # Snapshot the completed block for --export before we tear the
    # buffer down. Keeping every block as its own entry preserves the
    # tool-call boundaries (one block per model round).
    _turn_text_blocks.append(_md_buffer)
    # Move cursor up over the streamed rows and clear to end of screen.
    # \033[<n>F = cursor up <n> lines, column 0. \033[J = clear to end.
    # Guard n>=1 because \033[0F is undefined on some terminals.
    n = max(1, _md_rows_printed)
    sys.stdout.write(f"\r\033[{n}F\033[J")
    sys.stdout.flush()
    console.print(Markdown(_md_buffer, code_theme="monokai", justify="left"))
    _md_buffer = ""
    _md_rows_printed = 0

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

    Text deltas accumulate in the streaming-text buffer (printed as
    plain text token-by-token, see _md_append above). Any other event
    closes the buffer first so the streamed plain text is replaced
    with its rendered Markdown form before the next event prints.
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
                name = tu.get("name", "?")
                _turn_tool_calls.append(name)
                console.print(f"  [yellow]⚡[/] [bold cyan]{name}[/]")
            return

        # Incremental deltas. Text deltas feed the streaming-text buffer;
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
            _md_close()  # flush the partial text block before exiting
            err_console.print(
                "\n[yellow]⚠ stream interrupted by Ctrl-C[/]"
            )
        # Flush any tail event that didn't end with a blank line.
        if not raw and buffer.strip():
            _print_block(buffer)
        # Final safety net: flush any in-flight text block if the
        # response ended without an explicit contentBlockStop event
        # for the last text block.
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
