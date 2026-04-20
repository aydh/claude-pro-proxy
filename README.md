# Claude Pro Proxy

An OpenAI-compatible HTTP proxy that forwards `/v1/chat/completions` and
`/v1/models` requests to the [Claude Code](https://docs.claude.com/en/docs/claude-code)
CLI, letting any OpenAI-compatible client talk to Claude through your existing
Claude Pro / Claude Code subscription — no Anthropic API key required.

## How it works

- The proxy is a FastAPI app (`claude_proxy.py`) exposing:
  - `POST /v1/chat/completions` — chat completions, streaming or not
  - `GET /v1/models` — list of supported Claude model IDs
  - `GET /health` — liveness check
- Every request spawns a `claude` subprocess with `--print --output-format json`
  (or `stream-json` for streaming). The subprocess handles auth via your
  existing Claude Code login — no API calls are made directly.
- OpenAI `messages` are flattened into a plaintext `User:` / `Assistant:` /
  `Tool Result:` transcript fed over stdin. `system` messages become
  `--system-prompt`.
- Tool calling is emulated: when the request includes `tools`, Claude's built-in
  tools are disabled and a system instruction tells Claude to emit
  ` ```json {"name": ..., "arguments": ...} ``` ` fenced blocks. The proxy
  parses those blocks back into OpenAI-format `tool_calls`.

## Requirements

- Python 3.10+
- The [Claude Code CLI](https://docs.claude.com/en/docs/claude-code) installed
  and logged in (`claude` on `PATH`)

## Quick start

```bash
./start.sh
```

`start.sh` creates a `.venv` if missing, installs `requirements.txt`, then runs
`uvicorn claude_proxy:app` on `0.0.0.0:8001`. Extra args are forwarded to
`uvicorn`:

```bash
./start.sh --reload          # dev auto-reload
PORT=9000 ./start.sh         # custom port
HOST=127.0.0.1 ./start.sh    # bind to loopback
```

## Usage

Point any OpenAI-compatible client at `http://localhost:8001/v1` with any
non-empty API key:

```bash
curl http://localhost:8001/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "claude-sonnet-4-6",
    "messages": [{"role": "user", "content": "Hello"}],
    "stream": false
  }'
```

Python (`openai` SDK):

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8001/v1", api_key="unused")
resp = client.chat.completions.create(
    model="claude-sonnet-4-6",
    messages=[{"role": "user", "content": "Hello"}],
)
print(resp.choices[0].message.content)
```

## Configuration

Environment variables:

| Var | Default | Purpose |
| --- | --- | --- |
| `CLAUDE_BIN` | `claude` | Path to the Claude Code binary |
| `DEFAULT_MODEL` | `claude-sonnet-4-6` | Model used when the request omits one |
| `LOG_LEVEL` | `INFO` | Python logging level |
| `LOG_PROMPTS` | `false` | If `true`, log full prompt text (may contain secrets) |
| `CLAUDE_PROXY_HOME` | `~/.claude-proxy` | `CLAUDE_HOME` passed to the CLI (keep minimal to skip MCP startup cost) |
| `HOST` | `0.0.0.0` | Bind host for `start.sh` |
| `PORT` | `8001` | Bind port for `start.sh` |

## Smoke test

With the proxy running, `./smoke_test.py` hits `/health`, `/v1/models`, and
runs one streaming + one non-streaming completion. Uses only the Python
stdlib, so no venv needed:

```bash
./smoke_test.py                           # default http://localhost:8001
./smoke_test.py http://host:port          # custom URL
SKIP_COMPLETION=1 ./smoke_test.py         # endpoints only, no claude binary
```

Exits 0 on success, 1 on failure.

## Limitations

- `usage` token counts are always `0` — the CLI does not expose per-call counts.
- `temperature` and `max_tokens` are accepted but ignored.
- Streaming with tools streams text incrementally but buffers the final
  tool-call block until the response completes (blocks must be parsed whole).
