#!/usr/bin/env python3
"""
cortex-poll — Zero-token Board poller.

Checks Cortex Board for unclaimed tasks every invocation.
Designed to run via cron every 1 minute. Zero AI tokens consumed.
Only spawns `claude -p` when a real task is found.

Usage:
  # CC (default)
  python3 cortex-poll.py

  # OC
  python3 cortex-poll.py --agent-id oc --secret-env CORTEX_HMAC_SECRET_OC

  # Dry run (detect only, no claude -p)
  python3 cortex-poll.py --dry-run

  # Cron (every minute)
  * * * * * . /path/to/.env && python3 cortex-poll.py --agent-id oc --secret-env CORTEX_HMAC_SECRET_OC >> /tmp/cortex-poll.log 2>&1
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
LOCK_FILE = "/tmp/cortex-poll.lock"


def _sign(agent_id: str, secret: str, body: bytes) -> dict:
    """HMAC-SHA256 auth headers."""
    ts = str(int(time.time()))
    msg = f"{ts}.".encode() + body
    sig = hmac_mod.new(secret.encode(), msg, hashlib.sha256).hexdigest()
    return {
        "X-CC-Agent-ID": agent_id,
        "X-CC-Timestamp": ts,
        "X-CC-Signature": sig,
        "Content-Type": "application/json",
    }


def _mcp_call(agent_id: str, secret: str, method: str, params: dict, msg_id: int) -> dict:
    """Single MCP JSON-RPC call via curl. Returns parsed response."""
    body = json.dumps({
        "jsonrpc": "2.0",
        "method": method,
        "params": params,
        "id": msg_id,
    }).encode()
    headers = _sign(agent_id, secret, body)
    cmd = ["curl", "-s", "-X", "POST", WORKER_URL, "--max-time", "10"]
    for k, v in headers.items():
        cmd += ["-H", f"{k}: {v}"]
    cmd += ["-d", "@-"]
    r = subprocess.run(cmd, input=body, capture_output=True, timeout=15)
    if r.returncode != 0:
        return {}
    try:
        return json.loads(r.stdout.decode())
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}


def _extract_posts(resp: dict) -> list:
    """Extract posts list from MCP tool response."""
    content = resp.get("result", {}).get("content", [])
    if not content:
        return []
    try:
        data = json.loads(content[0].get("text", "{}"))
        return data.get("posts", [])
    except (json.JSONDecodeError, IndexError):
        return []


def main():
    parser = argparse.ArgumentParser(description="Cortex Board poller")
    parser.add_argument("--agent-id", default="cc", help="Agent ID (default: cc)")
    parser.add_argument("--secret-env", default="CORTEX_HMAC_SECRET_CC",
                        help="Env var name for HMAC secret (default: CORTEX_HMAC_SECRET_CC)")
    parser.add_argument("--dry-run", action="store_true", help="Detect only, don't execute")
    parser.add_argument("--cwd", default=None, help="Working directory for claude -p")
    args = parser.parse_args()

    secret = os.environ.get(args.secret_env, "")
    if not secret:
        print(f"{args.secret_env} not set", file=sys.stderr)
        sys.exit(1)

    # Lock: prevent overlapping runs (if claude -p takes > 1 min)
    if not args.dry_run and os.path.exists(LOCK_FILE):
        try:
            age = time.time() - os.path.getmtime(LOCK_FILE)
            if age < 1800:
                sys.exit(0)  # Another run in progress, skip silently
        except OSError:
            pass

    # Query Board for unclaimed tasks
    resp = _mcp_call(args.agent_id, secret, "tools/call", {
        "name": "board_read",
        "arguments": {"status": "open", "limit": 5},
    }, 1)

    posts = _extract_posts(resp)

    # Filter: only tasks (type=request), not yet claimed
    tasks = [p for p in posts if p.get("type") == "request" and not p.get("claimed_by")]

    if not tasks:
        if args.dry_run:
            print("No unclaimed tasks found.")
        sys.exit(0)

    if args.dry_run:
        print(f"Found {len(tasks)} task(s):")
        for t in tasks:
            print(f"  [{t.get('post_id')}] {t.get('title')}")
        sys.exit(0)

    # Create lock
    with open(LOCK_FILE, "w") as f:
        f.write(str(os.getpid()))

    try:
        for task in tasks:
            post_id = task.get("post_id", "")
            title = task.get("title", "")
            body = task.get("body", "")

            prompt = (
                f"Cortex Board task received.\n"
                f"Post ID: {post_id}\n"
                f"Title: {title}\n"
                f"Body: {body}\n\n"
                f"Instructions:\n"
                f"1. Claim this task: board_claim(post_id=\"{post_id}\")\n"
                f"2. Execute the task\n"
                f"3. Reply with result: board_reply(post_id=\"{post_id}\", ...)\n"
            )

            subprocess.run(
                ["claude", "-p", prompt],
                timeout=3600,
                cwd=args.cwd or os.getcwd(),
            )
    finally:
        try:
            os.unlink(LOCK_FILE)
        except OSError:
            pass


if __name__ == "__main__":
    main()
