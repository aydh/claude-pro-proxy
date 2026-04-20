#!/usr/bin/env python3
"""Smoke test for the running proxy. Uses only stdlib.

Usage:
    ./smoke_test.py                       # http://localhost:8001
    ./smoke_test.py http://host:port      # custom base URL
    SKIP_COMPLETION=1 ./smoke_test.py     # skip completion check (no claude binary)

Auth:
    The proxy requires API_KEY. This script reads API_KEY from the environment
    (or from ./.env) and sends it as a Bearer token. Set SKIP_AUTH=1 to omit
    the Authorization header (useful for testing 401 behavior).

Exits 0 on success, 1 on failure.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request


def _load_env_file(path: str = ".env") -> None:
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip()
                if (v.startswith('"') and v.endswith('"')) or (
                    v.startswith("'") and v.endswith("'")
                ):
                    v = v[1:-1]
                os.environ.setdefault(k, v)
    except OSError:
        pass


_load_env_file()

API_KEY = os.environ.get("API_KEY", "")
SKIP_AUTH = os.environ.get("SKIP_AUTH", "").lower() in ("1", "true", "yes")


def _headers(with_json: bool = False) -> dict:
    h: dict = {}
    if with_json:
        h["Content-Type"] = "application/json"
    if API_KEY and not SKIP_AUTH:
        h["Authorization"] = f"Bearer {API_KEY}"
    return h


def _get(url: str) -> dict:
    req = urllib.request.Request(url, headers=_headers(), method="GET")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def _post(url: str, body: dict, timeout: int = 120) -> dict:
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url, data=data, headers=_headers(with_json=True), method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _stream_post(url: str, body: dict, timeout: int = 120) -> list[str]:
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url, data=data, headers=_headers(with_json=True), method="POST",
    )
    lines: list[str] = []
    with urllib.request.urlopen(req, timeout=timeout) as r:
        for raw in r:
            line = raw.decode("utf-8", errors="replace").strip()
            if line:
                lines.append(line)
    return lines


def check(name: str, fn) -> bool:
    try:
        fn()
    except Exception as e:
        print(f"FAIL {name}: {e}")
        return False
    print(f"PASS {name}")
    return True


def main() -> int:
    base = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://localhost:8001"
    skip_completion = os.environ.get("SKIP_COMPLETION", "").lower() in ("1", "true", "yes")

    if not API_KEY and not SKIP_AUTH:
        print("WARN: API_KEY not set. Auth-gated endpoints will return 401.")
        print("      Run ./generate_api_key.py or set API_KEY in your environment.")

    results: list[bool] = []

    def health():
        body = _get(f"{base}/health")
        assert body.get("status") in ("ok", "degraded"), f"unexpected health body: {body}"

    def models():
        body = _get(f"{base}/v1/models")
        assert body.get("object") == "list", f"missing object=list: {body}"
        assert body.get("data"), "empty models list"
        assert all("id" in m for m in body["data"]), "model entry missing id"

    results.append(check("GET /health", health))
    results.append(check("GET /v1/models", models))

    if skip_completion:
        print("SKIP completion checks (SKIP_COMPLETION set)")
    else:
        def completion():
            body = _post(
                f"{base}/v1/chat/completions",
                {
                    "model": "claude-haiku-4-5-20251001",
                    "messages": [{"role": "user", "content": "Reply with the single word: pong"}],
                    "stream": False,
                },
            )
            assert body.get("object") == "chat.completion", f"wrong object: {body}"
            msg = body["choices"][0]["message"]
            assert msg["role"] == "assistant"
            assert msg.get("content"), "empty content"

        def streaming():
            lines = _stream_post(
                f"{base}/v1/chat/completions",
                {
                    "model": "claude-haiku-4-5-20251001",
                    "messages": [{"role": "user", "content": "Reply with the single word: pong"}],
                    "stream": True,
                },
            )
            assert any(l == "data: [DONE]" for l in lines), "no [DONE] sentinel"
            chunks = [l for l in lines if l.startswith("data: ") and l != "data: [DONE]"]
            assert chunks, "no data chunks received"
            for c in chunks:
                json.loads(c[len("data: "):])

        results.append(check("POST /v1/chat/completions (stream=false)", completion))
        results.append(check("POST /v1/chat/completions (stream=true)", streaming))

    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
