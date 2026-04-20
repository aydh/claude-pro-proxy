# CLAUDE.md

Guidance for Claude Code when working in this repository.

## Project

OpenAI-compatible HTTP proxy that wraps the `claude` Code CLI as a subprocess,
exposing `/v1/chat/completions` and `/v1/models`. No Anthropic API is called
directly — auth flows through the user's existing Claude Code login.

## Layout

- `claude_proxy.py` — the entire app. FastAPI with three endpoints
  (`/v1/chat/completions`, `/v1/models`, `/health`).
- `requirements.txt` — `fastapi`, `uvicorn`.
- `start.sh` — creates `.venv`, installs deps, runs `uvicorn claude_proxy:app`.
  Honors `HOST`, `PORT`, and forwards extra args to uvicorn.

## Architecture notes

`claude_proxy.py` is one file organized in these sections (in order):

1. **Config** — env vars (`CLAUDE_BIN`, `DEFAULT_MODEL`, `LOG_LEVEL`,
   `LOG_PROMPTS`) and the `CLAUDE_MODELS` list advertised by `/v1/models`.
2. **Pydantic request models** — `ChatRequest`, `Tool`, `ToolFunction`.
3. **Prompt construction** — `build_prompt` flattens OpenAI `messages` into a
   `User:` / `Assistant:` / `Tool Result:` transcript. `system` messages are
   concatenated and returned separately to be passed via `--system-prompt`.
   Tool specs are injected into the system prompt using `_TOOL_BLOCK_TEMPLATE`,
   which instructs Claude to emit ` ```json {"name","arguments"} ``` ` blocks.
4. **CLI invocation** — `_run_claude` spawns `claude --print` with either
   `--output-format json` (non-streaming) or `stream-json --verbose
   --include-partial-messages` (streaming). When `has_tools=True` it passes
   `--allowedTools ""` so Claude cannot execute its own built-in tools.
   Handles client disconnect by terminating the subprocess.
5. **Output parsing** — `_extract_tool_calls` pulls `json` fenced blocks out of
   Claude's text and converts them to OpenAI `tool_calls`. Accepts both
   `arguments` (spec) and `parameters` (common model mistake).
   `_collect_result_text` prefers the `result` field of the stream's `result`
   event, falling back to concatenating assistant text blocks.
6. **Response helpers / streaming generators / FastAPI app** — SSE formatting,
   `_stream_text_only` for no-tool streaming (true incremental deltas),
   `_stream_with_tools` (incremental text, buffered tool calls at the end).

## Important behaviors

- `usage` token counts are always `0` — the CLI doesn't expose per-call counts.
- `temperature` and `max_tokens` are accepted on the request but ignored.
- `CLAUDE_HOME` is set to `~/.claude-proxy` (override with `CLAUDE_PROXY_HOME`)
  so the subprocess doesn't pay startup cost for the user's MCP servers.
- `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` and `CLAUDE_BASH_NO_LOGIN=1` are
  set for speed.
- The CLI is invoked with `--dangerously-skip-permissions` — this is
  intentional since the proxy runs under the user's own account and any
  built-in tools are already disabled for tool-calling requests.

## Running

```bash
./start.sh                   # venv + deps + uvicorn on :8001
./start.sh --reload          # forwarded to uvicorn for dev
PORT=9000 ./start.sh         # override port
```

There is no test suite and no linter configured. When adding behavior, exercise
it manually against `http://localhost:8001/v1/chat/completions` with both
`stream: true` and `stream: false`, and with/without `tools`.

## Conventions

- Keep the app in one file unless something grows large enough to justify a
  split.
- Preserve the OpenAI wire format exactly — clients expect it.
- Log at `INFO` for request/response boundaries; keep prompt bodies behind
  `LOG_PROMPTS=true` since they may contain user secrets.
