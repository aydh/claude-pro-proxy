# Claude Pro Proxy

An OpenAI-compatible HTTP proxy that forwards `/v1/chat/completions` and
`/v1/models` requests to the [Claude Code](https://docs.claude.com/en/docs/claude-code)
CLI, letting any OpenAI-compatible client talk to Claude through your existing
Claude Pro / Claude Code subscription — no Anthropic API key required.

## How it works

- FastAPI app (`claude_proxy.py`) exposing:
  - `POST /v1/chat/completions` — chat completions, streaming or not
  - `GET /v1/models` — list of supported Claude model IDs
  - `GET /health` — liveness check (verifies the `claude` CLI responds)
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
- Sessions are cached by SHA-256 of the system prompt: repeat requests with the
  same system prompt resume the same Claude session via `--resume`, avoiding
  cold-start cost and preserving context.

## Requirements

- Python 3.10+
- The [Claude Code CLI](https://docs.claude.com/en/docs/claude-code) installed
  and logged in (`claude` on `PATH`)

## Quick start

```bash
python3 generate_api_key.py   # writes API_KEY to .env (chmod 600)
./start.sh
```

`start.sh` creates a `.venv` if missing, installs `requirements.txt`, then runs
`uvicorn claude_proxy:app` on `127.0.0.1:8001`. Extra args are forwarded to
`uvicorn`:

```bash
./start.sh --reload          # dev auto-reload
PORT=9000 ./start.sh         # custom port
HOST=0.0.0.0 ./start.sh      # bind on LAN (auth still required)
SKIP_INSTALL=1 ./start.sh    # skip pip install (faster restarts)
```

The default bind is loopback; the proxy is safe to expose on `0.0.0.0` because
every request requires `Authorization: Bearer <API_KEY>`.

## Authentication

The proxy behaves like a real OpenAI endpoint: clients must send
`Authorization: Bearer <key>` on every `/v1/*` request, where `<key>` matches
the `API_KEY` env var (loaded from `.env` on startup).

```bash
python3 generate_api_key.py              # create a new key in .env
python3 generate_api_key.py --force      # rotate the existing key
python3 generate_api_key.py --print-only # print a key without writing
```

Keys are of the form `sk-proxy-<32 url-safe chars>`. `.env` is written with
mode `0600` and is gitignored.

## Usage

```bash
curl http://localhost:8001/v1/chat/completions \
  -H "Authorization: Bearer $API_KEY" \
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

client = OpenAI(
    base_url="http://localhost:8001/v1",
    api_key="sk-proxy-...",  # from .env
)
resp = client.chat.completions.create(
    model="claude-sonnet-4-6",
    messages=[{"role": "user", "content": "Hello"}],
)
print(resp.choices[0].message.content)
```

## Configuration

Environment variables (loaded from `.env` on startup if present):

| Var | Default | Purpose |
| --- | --- | --- |
| `API_KEY` | *(required)* | Bearer token clients must send |
| `CLAUDE_BIN` | `claude` | Path to the Claude Code binary |
| `DEFAULT_MODEL` | `claude-sonnet-4-6` | Model used when the request omits one |
| `CLAUDE_MODELS` | *(built-in list)* | Comma-separated model allowlist override |
| `LOG_LEVEL` | `INFO` | Python logging level |
| `LOG_PROMPTS` | `false` | If `true`, log full prompt + response text |
| `CLAUDE_PROXY_HOME` | `~/.claude-proxy` | `CLAUDE_HOME` passed to the CLI |
| `CLI_TIMEOUT_SECONDS` | `300` | Per-request subprocess timeout |
| `MAX_REQUEST_BYTES` | `10485760` (10 MiB) | Reject larger request bodies with 413 |
| `MAX_SYSTEM_PROMPT_BYTES` | `98304` (96 KiB) | Guard against argv `E2BIG` |
| `SESSION_CACHE_SIZE` | `256` | Max cached sessions (LRU) |
| `SESSION_CACHE_TTL_SECONDS` | `3600` | Cached session TTL |
| `HOST` | `127.0.0.1` | Bind host for `start.sh` |
| `PORT` | `8001` | Bind port for `start.sh` |
| `SKIP_INSTALL` | `0` | `start.sh` skips `pip install` when `1` |

## Smoke test

With the proxy running, `./smoke_test.py` hits `/health`, `/v1/models`, and
runs one streaming + one non-streaming completion. It reads `API_KEY` from the
environment (or `.env`) and sends it as a Bearer token.

```bash
./smoke_test.py                           # default http://localhost:8001
./smoke_test.py http://host:port          # custom URL
SKIP_COMPLETION=1 ./smoke_test.py         # endpoints only, no claude binary
SKIP_AUTH=1 ./smoke_test.py               # omit Authorization (expects 401)
```

Exits 0 on success, 1 on failure.

## Limitations

- `usage` token counts are always `0` — the CLI does not expose per-call counts.
- `temperature` and `max_tokens` are accepted but ignored.
- Streaming with tools buffers the full response before emitting SSE (tool-call
  JSON fences must be parsed atomically, streaming them raw would leak to the
  client).
- Only `text` content blocks are supported. Image / audio / document blocks
  return HTTP 400.
- `tool_choice="none"` is respected; `tool_choice={"type":"function",...}` is
  treated like `"auto"` (any tool may be called or none).
