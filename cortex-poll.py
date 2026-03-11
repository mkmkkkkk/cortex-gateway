#!/usr/bin/env python3
"""
cortex-poll — Zero-token Board poller.

Checks Cortex Board for unclaimed tasks every invocation.
Designed to run via cron every 1 minute. Zero AI tokens consumed.
Only dispatches to AI when a real task is found.

Board protocol (claim/reply) is handled HERE via curl.
AI execution backend is configurable:
  --mode cli      → spawns `claude -p` (or --cli <cmd>)
  --mode webhook  → POSTs to OpenClaw /hooks/agent endpoint

Usage:
  # CC — CLI mode (default)
  python3 cortex-poll.py

  # OC — Webhook mode (OpenClaw)
  python3 cortex-poll.py --agent-id oc --secret-env CORTEX_HMAC_SECRET_OC \\
      --mode webhook --webhook-url http://localhost:18789/hooks/agent \\
      --webhook-token-env OC_WEBHOOK_TOKEN

  # Dry run
  python3 cortex-poll.py --dry-run

  # Cron (every minute)
  * * * * * . /path/to/.env && python3 cortex-poll.py [args] >> /tmp/cortex-poll.log 2>&1
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
MAX_RESULT_LEN = 4000  # Truncate output for Board reply


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


# ── Execution backends ──────────────────────────────────────────


def _exec_cli(prompt: str, cli: str, cwd: str) -> str:
    """Execute via AI CLI (claude -p, happy -p, etc.)."""
    result = subprocess.run(
        [cli, "-p", prompt],
        capture_output=True,
        text=True,
        timeout=3600,
        cwd=cwd,
    )
    output = (result.stdout or "").strip()
    if not output:
        output = f"Task executed (exit code {result.returncode})"
        if result.stderr:
            output += f"\nstderr: {result.stderr[:500]}"
    return output


def _exec_webhook(prompt: str, webhook_url: str, webhook_token: str) -> str:
    """Execute via OpenClaw webhook (POST /hooks/agent)."""
    payload = json.dumps({"message": prompt}).encode()
    cmd = [
        "curl", "-s", "-X", "POST", webhook_url,
        "--max-time", "300",
        "-H", f"Authorization: Bearer {webhook_token}",
        "-H", "Content-Type: application/json",
        "-d", "@-",
    ]
    r = subprocess.run(cmd, input=payload, capture_output=True, timeout=600)
    if r.returncode != 0:
        return f"Webhook call failed (exit code {r.returncode})\nstderr: {(r.stderr or b'').decode()[:500]}"
    body = (r.stdout or b"").decode().strip()
    if not body:
        return "Webhook returned empty response — task may be processing async"
    # Try to extract message from JSON response
    try:
        data = json.loads(body)
        return data.get("message") or data.get("result") or data.get("text") or json.dumps(data)
    except (json.JSONDecodeError, ValueError):
        return body


# ── Main ────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="Cortex Board poller")
    parser.add_argument("--agent-id", default="cc", help="Agent ID (default: cc)")
    parser.add_argument("--secret-env", default="CORTEX_HMAC_SECRET_CC",
                        help="Env var name for HMAC secret (default: CORTEX_HMAC_SECRET_CC)")
    parser.add_argument("--dry-run", action="store_true", help="Detect only, don't execute")

    # Execution backend
    parser.add_argument("--mode", choices=["cli", "webhook"], default="cli",
                        help="Execution mode: cli (spawn AI CLI) or webhook (POST to endpoint)")
    # CLI mode args
    parser.add_argument("--cli", default="claude",
                        help="AI CLI command for cli mode (default: claude)")
    parser.add_argument("--cwd", default=None, help="Working directory for AI CLI")
    # Webhook mode args
    parser.add_argument("--webhook-url", default="http://localhost:18789/hooks/agent",
                        help="Webhook URL for webhook mode (default: OpenClaw local)")
    parser.add_argument("--webhook-token-env", default="OC_WEBHOOK_TOKEN",
                        help="Env var for webhook auth token (default: OC_WEBHOOK_TOKEN)")

    args = parser.parse_args()

    secret = os.environ.get(args.secret_env, "")
    if not secret:
        print(f"{args.secret_env} not set", file=sys.stderr)
        sys.exit(1)

    if args.mode == "webhook":
        webhook_token = os.environ.get(args.webhook_token_env, "")
        if not webhook_token:
            print(f"{args.webhook_token_env} not set", file=sys.stderr)
            sys.exit(1)

    # Lock: prevent overlapping runs
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

            # 2. Execute via chosen backend
            prompt = f"Task: {title}\n\n{body}\n\nExecute this task and provide the result."

            if args.mode == "webhook":
                print(f"[{ts}] Claimed {post_id}, POSTing to webhook...")
                output = _exec_webhook(prompt, args.webhook_url, webhook_token)
            else:
                print(f"[{ts}] Claimed {post_id}, spawning {args.cli} -p...")
                output = _exec_cli(prompt, args.cli, args.cwd or os.getcwd())

            print(f"[{ts}] Execution done for {post_id}, posting reply...")

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
