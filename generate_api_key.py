#!/usr/bin/env python3
"""Generate an API key for the Claude Code proxy and write it to .env.

Usage:
    ./generate_api_key.py              # create if missing, otherwise show current
    ./generate_api_key.py --force      # overwrite existing key
    ./generate_api_key.py --print-only # print a new key without writing anywhere

Exits 0 on success, 1 on error.
"""
from __future__ import annotations

import argparse
import os
import secrets
import stat
import sys
from typing import Dict, List, Tuple

ENV_PATH = ".env"
KEY_VAR = "API_KEY"


def _generate_key() -> str:
    return "sk-proxy-" + secrets.token_urlsafe(32)


def _read_env(path: str) -> Tuple[Dict[str, str], List[str]]:
    """Return ({key: value}, original_lines)."""
    if not os.path.exists(path):
        return {}, []
    with open(path, "r", encoding="utf-8") as f:
        lines = f.read().splitlines()
    values: Dict[str, str] = {}
    for line in lines:
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        k = k.strip()
        v = v.strip()
        if (v.startswith('"') and v.endswith('"')) or (
            v.startswith("'") and v.endswith("'")
        ):
            v = v[1:-1]
        values[k] = v
    return values, lines


def _write_env(path: str, lines: List[str], key_var: str, key_val: str) -> None:
    found = False
    out: List[str] = []
    for line in lines:
        s = line.strip()
        if s.startswith(f"{key_var}=") or s.startswith(f"{key_var} ="):
            out.append(f"{key_var}={key_val}")
            found = True
        else:
            out.append(line)
    if not found:
        if out and out[-1].strip() != "":
            out.append("")
        out.append(f"{key_var}={key_val}")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(out).rstrip() + "\n")
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        # Tightening permissions is best-effort (e.g. on filesystems that
        # don't support chmod); the key was still written successfully.
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--force", action="store_true",
        help="Overwrite an existing API_KEY in .env",
    )
    parser.add_argument(
        "--print-only", action="store_true",
        help="Print a new key to stdout without writing .env",
    )
    args = parser.parse_args()

    if args.print_only:
        print(_generate_key())
        return 0

    existing, lines = _read_env(ENV_PATH)
    current = existing.get(KEY_VAR)

    if current and not args.force:
        print(f"{KEY_VAR} is already set in {ENV_PATH}.")
        print(f"Current: {current}")
        print("Use --force to regenerate.")
        return 0

    new_key = _generate_key()
    _write_env(ENV_PATH, lines, KEY_VAR, new_key)
    action = "Updated" if current else "Wrote"
    print(f"{action} {KEY_VAR} in {ENV_PATH} (chmod 600).")
    print(f"Key: {new_key}")
    print()
    print("Point OpenAI-compatible clients at:")
    print("    base_url = http://localhost:8001/v1")
    print(f"    api_key  = {new_key}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
