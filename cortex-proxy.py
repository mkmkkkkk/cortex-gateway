#!/usr/bin/env python3
"""
cortex-proxy — MCP stdio-to-HTTP proxy with HMAC auth.

Transparent bridge: any MCP client (Claude Code, OpenClaw, etc.)
connects via stdio, proxy signs and forwards to Cortex Worker.

Usage:
  python cortex-proxy.py --agent-id cc --secret-env CORTEX_HMAC_SECRET_CC
  python cortex-proxy.py --agent-id oc --secret-env OC_HMAC_SECRET

MCP config (Claude Code .claude/settings.json):
  "mcpServers": {
    "cortex": {
      "command": "python3",
      "args": ["cortex-proxy.py", "--agent-id", "cc", "--secret-env", "CORTEX_HMAC_SECRET_CC"]
    }
  }
"""

import argparse
import hashlib
import hmac as hmac_mod
import json
import os
import subprocess
import sys
import time

WORKER_URL = "https://cortex.mkyang.ai/mcp"


def _sign(agent_id: str, secret: str, body: bytes) -> dict:
    ts = str(int(time.time()))
    msg = f"{ts}.".encode() + body
    sig = hmac_mod.new(secret.encode(), msg, hashlib.sha256).hexdigest()
    return {
        "X-CC-Agent-ID": agent_id,
        "X-CC-Timestamp": ts,
        "X-CC-Signature": sig,
        "Content-Type": "application/json",
    }


def _post(endpoint: str, headers: dict, body: bytes, timeout: int) -> tuple[str, str]:
    """POST via curl. Returns (response_body, session_id)."""
    cmd = ["curl", "-si", "-X", "POST", endpoint, "--max-time", str(timeout)]
    for k, v in headers.items():
        cmd += ["-H", f"{k}: {v}"]
    cmd += ["-d", "@-"]
    r = subprocess.run(cmd, input=body, capture_output=True, timeout=timeout + 5)
    raw = r.stdout.decode()
    # Split headers from body (HTTP response with -i flag)
    parts = raw.split("\r\n\r\n", 1)
    resp_headers = parts[0] if parts else ""
    resp_body = parts[1] if len(parts) > 1 else ""
    # Extract Mcp-Session-Id
    sid = ""
    for line in resp_headers.split("\r\n"):
        if line.lower().startswith("mcp-session-id:"):
            sid = line.split(":", 1)[1].strip()
    return resp_body, sid


def main():
    parser = argparse.ArgumentParser(description="Cortex MCP proxy")
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--secret", default=None, help="HMAC secret (prefer --secret-env)")
    parser.add_argument("--secret-env", default=None, help="Env var name containing HMAC secret")
    parser.add_argument("--endpoint", default=WORKER_URL)
    parser.add_argument("--timeout", type=int, default=30)
    args = parser.parse_args()

    # Resolve secret: --secret-env (safe) > --secret (visible in ps)
    secret = args.secret
    if args.secret_env:
        secret = os.environ.get(args.secret_env, "")
    if not secret:
        print("Error: provide --secret or --secret-env", file=sys.stderr)
        sys.exit(1)

    session_id = ""

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue

        is_notification = "id" not in msg
        body = line.encode()
        headers = _sign(args.agent_id, secret, body)
        if session_id:
            headers["Mcp-Session-Id"] = session_id

        try:
            data, sid = _post(args.endpoint, headers, body, args.timeout)
            if sid:
                session_id = sid
            if data and not is_notification:
                print(data, flush=True)
        except Exception as e:
            if not is_notification:
                err = {"jsonrpc": "2.0", "id": msg.get("id"),
                       "error": {"code": -32000, "message": str(e)}}
                print(json.dumps(err), flush=True)


if __name__ == "__main__":
    main()
