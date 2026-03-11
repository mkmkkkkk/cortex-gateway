#!/usr/bin/env python3
"""
cortex-poll — Zero-token Board poller.

Checks Cortex Board for unclaimed tasks every invocation.
Designed to run via cron every 1 minute. Zero AI tokens consumed.
Only spawns `claude -p` when a real task is found.

Board protocol (claim/reply) is handled HERE via curl — claude -p
only receives the task content and executes it. This avoids MCP
dependency issues in cron environments.

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
MAX_RESULT_LEN = 4000  # Truncate claude output for Board reply


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


def _board_claim(agent_id: str, secret: str, post_id: str) -> bool:
    """Claim a Board post. Returns True if successful."""
    resp = _mcp_call(agent_id, secret, "tools/call", {
        "name": "board_claim",
        "arguments": {"post_id": post_id},
    }, 100)
    # Check for success (no error in response)
    return bool(resp.get("result"))


def _board_reply(agent_id: str, secret: str, post_id: str, body: str, action: str = "done") -> bool:
    """Reply to a Board post. Returns True if successful."""
    resp = _mcp_call(agent_id, secret, "tools/call", {
        "name": "board_reply",
        "arguments": {
            "post_id": post_id,
            "body": body[:MAX_RESULT_LEN],
            "action": action,
        },
    }, 101)
    return bool(resp.get("result"))


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

    # Filter: type=request, not claimed, not posted by self, visible to self
    tasks = []
    for p in posts:
        if p.get("type") != "request" or p.get("claimed_by"):
            continue
        if p.get("from_agent") == args.agent_id:
            continue  # Skip own posts — prevent self-claim
        vis = p.get("visible_to")
        if vis and args.agent_id not in vis:
            continue  # Not visible to this agent
        tasks.append(p)

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
            ts = time.strftime("%H:%M:%S")

            print(f"[{ts}] Processing: [{post_id}] {title}")

            # 1. Claim the task via curl (Board protocol handled here)
            if not _board_claim(args.agent_id, secret, post_id):
                print(f"[{ts}] Failed to claim {post_id}, skipping")
                continue

            print(f"[{ts}] Claimed {post_id}, spawning claude -p...")

            # 2. Spawn claude -p with ONLY the task content
            #    No Board instructions — claude just executes the task.
            prompt = f"Task: {title}\n\n{body}\n\nExecute this task and provide the result."

            result = subprocess.run(
                ["claude", "-p", prompt],
                capture_output=True,
                text=True,
                timeout=3600,
                cwd=args.cwd or os.getcwd(),
            )

            output = (result.stdout or "").strip()
            if not output:
                output = f"Task executed (exit code {result.returncode})"
                if result.stderr:
                    output += f"\nstderr: {result.stderr[:500]}"

            print(f"[{ts}] claude -p done for {post_id}, posting reply...")

            # 3. Post result back to Board via curl
            _board_reply(args.agent_id, secret, post_id, output)

            print(f"[{ts}] Done: [{post_id}] {title}")

    finally:
        try:
            os.unlink(LOCK_FILE)
        except OSError:
            pass


if __name__ == "__main__":
    main()
