#!/usr/bin/env python3
"""
claude_proxy.py — OpenAI-compatible proxy for the Claude Code CLI.

Exposes /v1/chat/completions and /v1/models as a drop-in replacement for the
OpenAI Chat Completions API.  All LLM calls go through the Claude Code CLI
subprocess — no Anthropic API is called directly.

Run:
    uvicorn claude_proxy:app --host 0.0.0.0 --port 8001

Key deviations from OpenAI behaviour:
- Tool arguments are injected as a fenced-JSON system block; Claude must emit
  ```json {"name":..,"arguments":..} ``` blocks to invoke tools.
- Streaming with tools buffers the full Claude response before emitting SSE,
  because tool-call blocks must be parsed atomically.
- Token counts in usage are always 0 (Claude CLI does not expose them per-call
  in a stable way).
- Session continuity is keyed on the SHA-256 of the system prompt; different
  system prompts always start a fresh Claude session.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from typing import Any, AsyncIterator, Dict, List, Optional, Union

from fastapi import FastAPI
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CLAUDE_BIN: str = os.environ.get("CLAUDE_BIN", "claude")
DEFAULT_MODEL: str = os.environ.get("DEFAULT_MODEL", "claude-sonnet-4-6")
LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "INFO").upper()
# Set LOG_PROMPTS=true to log full prompt text (may contain secrets)
LOG_PROMPTS: bool = os.environ.get("LOG_PROMPTS", "false").lower() == "true"

CLAUDE_MODELS: List[str] = [
    "claude-opus-4-6",
    "claude-sonnet-4-6",
    "claude-haiku-4-5-20251001",
    "claude-opus-4-5-20250929",
    "claude-sonnet-4-5-20250929",
    "claude-sonnet-4-20250514",
    "claude-opus-4-20250514",
    "claude-3-5-sonnet-20241022",
    "claude-3-5-haiku-20241022",
]

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("claude_proxy")


# ---------------------------------------------------------------------------
# Pydantic request models
# ---------------------------------------------------------------------------


class ToolFunction(BaseModel):
    name: str
    description: str = ""
    parameters: Dict[str, Any] = Field(default_factory=dict)


class Tool(BaseModel):
    type: str = "function"
    function: ToolFunction


class ChatRequest(BaseModel):
    model: str = DEFAULT_MODEL
    messages: List[Dict[str, Any]]
    stream: bool = False
    tools: Optional[List[Tool]] = None
    tool_choice: Optional[Any] = None
    # Accepted but ignored (Claude CLI controls these internally)
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

_TOOL_BLOCK_TEMPLATE = """\
You are operating as a pure language model via an API. You do NOT have access \
to any built-in tools, filesystem, bash, or code execution. The ONLY way to \
invoke an external capability is to emit a fenced JSON code block as described \
below. Do not attempt to execute anything yourself.

When you want to call one of the available tools, emit EXACTLY this format and \
nothing else — no prose before or after:

```json
{{
  "name": "<tool_name>",
  "arguments": {{
    "<arg>": "<value>"
  }}
}}
```

Rules:
- The key MUST be "arguments" (not "parameters" or any other key).
- Emit ONLY the fenced JSON block — no surrounding text before or after.
- You may emit multiple fenced blocks in sequence to call multiple tools.
- Stop immediately after emitting tool call block(s). Do not narrate or explain.
- If no tool is needed, respond in plain text as normal.
- NEVER say a tool is unavailable or failed — just emit the block and stop.

Available tools:
{tool_specs}
"""


def _content_to_text(content: Any) -> str:
    """Normalise OpenAI message content (str or list of blocks) to plain text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "\n".join(parts)
    return str(content)


def build_prompt(request: ChatRequest) -> tuple[str, Optional[str]]:
    """
    Convert OpenAI messages into a plain-text conversation prompt.

    Returns (conversation_prompt, system_prompt).
    - system_prompt is extracted for --system-prompt flag and session keying.
    - conversation_prompt contains User/Assistant/Tool turns only.

    If tools are provided the tool-calling instructions are appended to the
    system prompt so they appear before the conversation.
    """
    system_parts: List[str] = []
    conv_lines: List[str] = []

    for msg in request.messages:
        role = msg.get("role", "")
        text = _content_to_text(msg.get("content"))

        if role == "system":
            system_parts.append(text)
        elif role == "user":
            conv_lines.append(f"User: {text}")
        elif role == "assistant":
            # Strip any tool-call blocks already in history so Claude doesn't
            # get confused by seeing its own prior format examples
            conv_lines.append(f"Assistant: {text}")
        elif role == "tool":
            name = msg.get("name") or msg.get("tool_call_id") or "tool"
            conv_lines.append(f"Tool Result [{name}]: {text}")

    system_prompt: Optional[str] = "\n\n".join(system_parts) if system_parts else None

    # Inject tool-calling instructions into system prompt
    if request.tools:
        tool_specs = json.dumps(
            [
                {
                    "name": t.function.name,
                    "description": t.function.description,
                    "parameters": t.function.parameters,
                }
                for t in request.tools
            ],
            indent=2,
        )
        tool_block = _TOOL_BLOCK_TEMPLATE.format(tool_specs=tool_specs)
        system_prompt = (
            (system_prompt + "\n\n" + tool_block) if system_prompt else tool_block
        )

    conversation = "\n\n".join(conv_lines)

    if LOG_PROMPTS:
        logger.debug("system_prompt (%d chars):\n%s", len(system_prompt or ""), system_prompt)
        logger.debug("conversation (%d chars):\n%s", len(conversation), conversation)
    else:
        logger.debug(
            "prompt built: system=%d chars  conversation=%d chars",
            len(system_prompt or ""),
            len(conversation),
        )

    return conversation, system_prompt


# ---------------------------------------------------------------------------
# Claude CLI invocation
# ---------------------------------------------------------------------------


async def _run_claude(
    conversation: str,
    system_prompt: Optional[str],
    model: str,
    streaming: bool,
    has_tools: bool = False,
) -> AsyncIterator[str]:
    """
    Invoke the Claude CLI and yield output lines.

    - streaming=True  → --output-format stream-json --verbose (NDJSON lines)
    - streaming=False → --output-format json (single result line)
    - has_tools=True  → disables all Claude built-in tools so the model emits
                        JSON fenced blocks instead of trying to execute natively.

    Manages session resumption and caches the session_id from result events.
    Terminates the subprocess if the caller cancels (client disconnect).
    """
    base_flags = [
        "--dangerously-skip-permissions",
    ]

    if streaming:
        cmd = [
            CLAUDE_BIN, "--print", "--model", model,
            "--output-format", "stream-json",
            "--verbose",
            "--include-partial-messages",
            *base_flags,
        ]
    else:
        cmd = [
            CLAUDE_BIN, "--print", "--model", model,
            "--output-format", "json",
            *base_flags,
        ]

    # When user-provided tools are in play, strip all built-in Claude tools so
    # the model cannot attempt to execute them itself.
    if has_tools:
        cmd += ["--allowedTools", ""]

    if system_prompt:
        cmd += ["--system-prompt", system_prompt]

    logger.info(
        "Claude CLI: model=%s streaming=%s conv_chars=%d",
        model, streaming, len(conversation),
    )

    # Inherit environment and layer in perf flags.
    # CLAUDE_HOME points to a minimal config dir with no MCP servers, so the
    # proxy doesn't pay the cost of connecting to the user's cloud MCP servers.
    env = os.environ.copy()
    env["CLAUDE_HOME"] = os.environ.get("CLAUDE_PROXY_HOME", os.path.expanduser("~/.claude-proxy"))
    env["CLAUDE_BASH_NO_LOGIN"] = "1"
    env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )

    # Write conversation via stdin and close to signal EOF
    assert proc.stdin is not None
    proc.stdin.write(conversation.encode())
    await proc.stdin.drain()
    proc.stdin.close()

    try:
        assert proc.stdout is not None
        async for raw_line in proc.stdout:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                json.loads(line)
            except json.JSONDecodeError:
                logger.debug("Non-JSON line from Claude: %s", line[:120])
            yield line

        stderr_bytes = await proc.stderr.read()
        if stderr_bytes:
            logger.debug("Claude stderr: %s", stderr_bytes.decode("utf-8", errors="replace")[:500])

        await proc.wait()
        if proc.returncode not in (0, None):
            logger.warning("Claude exited with code %d", proc.returncode)

    except asyncio.CancelledError:
        logger.info("Client disconnected — terminating Claude subprocess")
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=3)
        except asyncio.TimeoutError:
            proc.kill()
        raise


# ---------------------------------------------------------------------------
# Output parsing
# ---------------------------------------------------------------------------

# Matches ```json ... ``` fenced blocks that contain a tool call object.
_JSON_FENCE_RE = re.compile(r"```json\s*\n([\s\S]*?)\n```", re.MULTILINE)


def _extract_tool_calls(text: str) -> tuple[List[Dict[str, Any]], str]:
    """
    Parse ```json tool-call fenced blocks from Claude output.

    Returns (tool_calls_in_openai_format, text_with_blocks_removed).
    Blocks that do not match {name, arguments} are left in the text.
    """
    tool_calls: List[Dict[str, Any]] = []
    removed_spans: List[tuple[int, int]] = []

    for match in _JSON_FENCE_RE.finditer(text):
        body = match.group(1).strip()
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            continue
        if not isinstance(parsed, dict):
            continue
        # Accept both "arguments" (spec) and "parameters" (common model mistake)
        if "name" not in parsed:
            continue
        if "arguments" not in parsed and "parameters" not in parsed:
            continue

        args = parsed.get("arguments") or parsed.get("parameters", {})
        tool_calls.append(
            {
                "id": f"call_{uuid.uuid4().hex[:8]}",
                "type": "function",
                "function": {
                    "name": parsed["name"],
                    "arguments": json.dumps(args) if not isinstance(args, str) else args,
                },
            }
        )
        removed_spans.append((match.start(), match.end()))

    if not removed_spans:
        return [], text

    # Remove matched spans in reverse order to preserve indices
    cleaned = text
    for start, end in reversed(removed_spans):
        cleaned = cleaned[:start] + cleaned[end:]
    return tool_calls, cleaned.strip()


def _collect_result_text(lines: List[str]) -> str:
    """
    Extract the final assistant text from buffered stream-json lines.

    Prefers the 'result' field of the result event (clean, de-duplicated).
    Falls back to concatenating text blocks from assistant events.
    """
    for line in reversed(lines):
        try:
            event = json.loads(line)
            if event.get("type") == "result" and "result" in event:
                return event["result"]
        except json.JSONDecodeError:
            continue

    # Fallback: concatenate text from the last assistant event
    for line in reversed(lines):
        try:
            event = json.loads(line)
            if event.get("type") == "assistant":
                msg = event.get("message", {})
                parts = [
                    b.get("text", "")
                    for b in msg.get("content", [])
                    if b.get("type") == "text"
                ]
                if parts:
                    return "".join(parts)
        except json.JSONDecodeError:
            continue
    return ""


# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------


def _completion_id() -> str:
    return f"chatcmpl-{uuid.uuid4().hex[:12]}"


def _non_streaming_response(
    cid: str,
    model: str,
    content: str,
    tool_calls: List[Dict[str, Any]],
) -> Dict[str, Any]:
    message: Dict[str, Any] = {"role": "assistant", "content": content or None}
    finish_reason = "stop"
    if tool_calls:
        message["tool_calls"] = tool_calls
        finish_reason = "tool_calls"

    return {
        "id": cid,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        # Token counts unavailable without internal API access
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def _sse(payload: Dict[str, Any]) -> str:
    return f"data: {json.dumps(payload)}\n\n"


def _chunk(
    cid: str,
    model: str,
    delta: Dict[str, Any],
    finish_reason: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "id": cid,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }


# ---------------------------------------------------------------------------
# Streaming generators
# ---------------------------------------------------------------------------


async def _stream_text_only(
    conversation: str,
    system_prompt: Optional[str],
    model: str,
    cid: str,
) -> AsyncIterator[str]:
    """
    True incremental streaming for text-only (no-tool) responses.

    Uses --include-partial-messages so Claude emits partial content events.
    Tracks the last emitted character position to derive per-chunk deltas.
    """
    yield _sse(_chunk(cid, model, {"role": "assistant"}))

    emitted_len = 0
    async for line in _run_claude(
        conversation, system_prompt, model, streaming=True, has_tools=False
    ):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        if event.get("type") != "assistant":
            continue

        msg = event.get("message", {})
        full_text = "".join(
            b.get("text", "")
            for b in msg.get("content", [])
            if b.get("type") == "text"
        )
        if len(full_text) > emitted_len:
            delta_text = full_text[emitted_len:]
            emitted_len = len(full_text)
            yield _sse(_chunk(cid, model, {"content": delta_text}))

    yield _sse(_chunk(cid, model, {}, finish_reason="stop"))
    yield "data: [DONE]\n\n"


async def _stream_with_tools(
    conversation: str,
    system_prompt: Optional[str],
    model: str,
    cid: str,
) -> AsyncIterator[str]:
    """
    Streaming for requests that include tools.

    Text content is streamed incrementally via partial messages (same approach
    as _stream_text_only). Tool-call blocks are buffered and emitted at the end
    since they must be parsed atomically from the full response.

    In practice the orchestrator either outputs a pure JSON tool-call block
    (cleaned_text ends up empty) or a pure text final answer (no tool calls),
    so streaming text in real-time is safe and gives live UI updates.
    """
    yield _sse(_chunk(cid, model, {"role": "assistant"}))

    lines: List[str] = []
    emitted_len = 0

    async for line in _run_claude(
        conversation, system_prompt, model, streaming=True, has_tools=True
    ):
        lines.append(line)
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "assistant":
            continue
        msg = event.get("message", {})
        full_text = "".join(
            b.get("text", "")
            for b in msg.get("content", [])
            if b.get("type") == "text"
        )
        if len(full_text) > emitted_len:
            delta_text = full_text[emitted_len:]
            emitted_len = len(full_text)
            yield _sse(_chunk(cid, model, {"content": delta_text}))

    full_text = _collect_result_text(lines)
    tool_calls, cleaned_text = _extract_tool_calls(full_text)
    finish_reason = "tool_calls" if tool_calls else "stop"

    logger.info("Tool calls detected: %d", len(tool_calls))
    logger.info("Response id=%s tool_calls=%d text_chars=%d\n%s", cid, len(tool_calls), len(cleaned_text), cleaned_text)

    if tool_calls:
        yield _sse(_chunk(cid, model, {"tool_calls": tool_calls}))

    yield _sse(_chunk(cid, model, {}, finish_reason=finish_reason))
    yield "data: [DONE]\n\n"


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------

app = FastAPI(title="Claude Code Proxy", version="1.0.0")


@app.get("/v1/models")
async def list_models() -> Dict[str, Any]:
    return {
        "object": "list",
        "data": [
            {
                "id": m,
                "object": "model",
                "created": int(time.time()),
                "owned_by": "anthropic",
            }
            for m in CLAUDE_MODELS
        ],
    }


@app.post("/v1/chat/completions")
async def chat_completions(request: ChatRequest):
    conversation, system_prompt = build_prompt(request)
    model = request.model or DEFAULT_MODEL
    cid = _completion_id()
    has_tools = bool(request.tools)

    logger.info(
        "Request id=%s model=%s stream=%s tools=%d",
        cid, model, request.stream, len(request.tools or []),
    )

    if request.stream:
        if has_tools:
            gen = _stream_with_tools(conversation, system_prompt, model, cid)
        else:
            gen = _stream_text_only(conversation, system_prompt, model, cid)
        return StreamingResponse(gen, media_type="text/event-stream")

    # ── Non-streaming ──────────────────────────────────────────────────────
    lines: List[str] = []
    async for line in _run_claude(
        conversation, system_prompt, model, streaming=False, has_tools=has_tools
    ):
        lines.append(line)

    full_text = _collect_result_text(lines)
    tool_calls, cleaned_text = _extract_tool_calls(full_text)

    logger.info("Response id=%s tool_calls=%d text_chars=%d\n%s", cid, len(tool_calls), len(cleaned_text), cleaned_text)
    return JSONResponse(
        _non_streaming_response(cid, model, cleaned_text, tool_calls)
    )


@app.get("/health")
async def health() -> Dict[str, str]:
    return {"status": "ok"}
