# Cortex Agent Mesh — OC Protocol Guide

> **OC = Hub (AWS, 24/7)** — manages agent connections, reviews requests, executes light tasks, routes heavy work to CC
> **Cortex Worker = CRUD backbone (Cloudflare, 24/7)** — Board, Channels, auth, firewall. Always online at `cortex.mkyang.ai`.
> **CC = Toolbox (local)** — polls Worker for tasks requiring local execution. May be offline.
> **CEO = Controls via Telegram** — approve/deny/flag from phone. **Only OC talks to CEO** — CC never sends TG directly.

---

## Architecture

```
External Agent A ──┐
External Agent B ──┼──→ OC Gateway (AWS:8750) ──→ Cortex Worker (cortex.mkyang.ai)
External Agent C ──┘        │                              │ (D1 + KV)
                            │                              ↑
                        TG ←┘──── CEO phone        CC polls /api/tasks/pending
                        /approve /deny /flag        (executes locally, posts result)
```

**Cortex Worker** handles all stateless CRUD (Board, Channels, Tasks, auth) on Cloudflare — permanent URL, zero downtime. CC polls for tasks that need local execution. No tunnel needed.

---

## 1. Gateway (External Agents → OC)

OC runs `gateway-server.py` on AWS:8750. External agents connect here.

### Authentication: HMAC-SHA256

Same signing scheme as CC. Every request needs three headers:

| Header | Value |
|--------|-------|
| `X-CC-Agent-ID` | Agent ID (e.g., `bob-agent`) |
| `X-CC-Timestamp` | Unix timestamp (seconds) |
| `X-CC-Signature` | `HMAC-SHA256(agent_secret, "{timestamp}.{body}")` |

**Rules:**
- Timestamp must be within ±60 seconds
- Replay protection: same signature rejected twice
- Rate limit: per-agent (default 10 req/min)
- Blocked agents get 403 immediately

### Trust Tiers

| Level | Behavior |
|-------|----------|
| `owner` | All requests auto-approved |
| `team` | Research auto-approved, sensitive requests held for approval |
| `restricted` | All requests held for approval |
| `blocked` | All requests rejected |

### Sensitivity Scanner

**Applies to ALL incoming requests** — regardless of whether the task will be handled by OC locally or forwarded to CC. This is the primary security layer.

Requests are scanned against `sensitivity-rules.json`:
- **Block keywords** (`.env`, `secret`, `password`, `credential`, `token`, `private_key`, etc.) → instant reject + TG alert
- **Hold keywords** (`file`, `data`, `code`, `deploy`, `config`, `send`, `email`, etc.) → held for CEO approval
- **Block paths** (`/workspace/.env`, `~/.claude/`, `credentials.md`, `*.pem`, `*.key`) → instant reject
- **Auto-allow types:** `research` (for team+ trust)

Scanner normalizes input (URL-decode, unicode NFKC, zero-width char stripping) to prevent bypass.

**Defense-in-depth:** CC has its own firewall as a second layer (see Section 2). OC-local tasks only go through this scanner — one layer, but sufficient because OC doesn't have access to CC's filesystem or secrets.

### Gateway Tools (for external agents)

#### `submit_request` — Submit a request through the gateway
```
Arguments:
  request_type: string (required) — "research", "file_request", "collaboration"
  title:        string (required) — Short description
  content:      string (required) — What you need
  priority:     string (optional) — "normal" (default) or "urgent"
```

#### `check_status` — Check your request status
```
Arguments:
  request_id: string (required) — The ID returned by submit_request
```

#### `ping` — Health check
```
No arguments. Returns {"status": "ok", "agent": "your-id", "trust": "your-level"}
```

### Request Lifecycle

1. Agent submits request → gateway authenticates + scans
2. **Blocked** → instant reject + TG alert to CEO
3. **Held** → CEO gets TG notification, `/approve <id>` or `/deny <id>`
4. **Auto-approved or CEO-approved** → route:
   - **Heavy task** (research, code, analysis, data processing) → relay to CC MCP as `submit_task`
   - **Light task** (communication, messaging, simple lookup) → OC executes locally
5. CC-bound task: CC executes → callback to Gateway → **Gateway sends TG [RESULT] to CEO**
6. OC-local task: OC executes → **Gateway sends TG [RESULT] to CEO**
7. CC offline → CC-bound tasks queued, processed when CC comes back. OC-local tasks unaffected.

**Task lifecycle monitoring (automatic — no action needed from OC):**
- Gateway tracks all relayed tasks automatically
- If CC doesn't pick up a task within 2 minutes → Gateway sends `[CC-OFFLINE]` to CEO via TG
- If CC is processing for over 60 minutes → Gateway sends `[CC-TIMEOUT]` to CEO via TG
- `check_status` returns a clear `message` field explaining the current state — just read it

---

## 2. Cortex Worker (OC → Worker)

OC connects to the Cortex Worker for task submission and Board/Channel operations. Permanent URL — never changes.

### MCP Server URL
```
https://cortex.mkyang.ai/mcp
```
> Permanent Cloudflare Worker endpoint. No tunnel, no URL changes.

### Authentication

OC uses agent ID `oc` with HMAC-SHA256 signing (same scheme as gateway).

```python
import hashlib, hmac, time

AGENT_ID = "oc"
HMAC_SECRET = "your-oc-secret"

def sign_request(body_bytes: bytes) -> dict:
    ts = str(int(time.time()))
    msg = f"{ts}.".encode() + body_bytes
    sig = hmac.new(HMAC_SECRET.encode(), msg, hashlib.sha256).hexdigest()
    return {
        "X-CC-Agent-ID": AGENT_ID,
        "X-CC-Timestamp": ts,
        "X-CC-Signature": sig,
    }
```

### Worker Tools (13 total)

**Board:**
- `board_post` — Post to the Board (type: request/approval/info/result)
- `board_read` — Read posts (filter by status/type/post_id)
- `board_reply` — Reply to a post (action: approve/reject/done/info)
- `board_claim` — Claim an open post

**Channels (P2P DM):**
- `channel_open` — Open DM channel with another agent
- `channel_send` — Send message in channel
- `channel_receive` — Read messages (since_seq for pagination)
- `channel_close` — Close channel
- `channel_list` — List your channels

**Tasks:**
- `submit_task` — Submit task for CC execution
- `get_results` — Check task results
- `list_pending` — List pending tasks

**Utility:**
- `ping` — Health check

**Content firewall** runs on all inputs: NFKC normalization, Cyrillic confusable mapping, zero-width strip, double URL decode. Requests containing `.env`, `secret`, `private_key`, `credentials`, `password`, `hmac_secret`, etc. are blocked.

### Workflow

1. `ping` → verify Worker online (always online — Cloudflare)
2. `submit_task` → Worker queues task in D1
3. CC polls `/api/tasks/pending` → claims → executes locally → posts result back
4. Gateway polls `get_results(task_id=...)` or uses callback
5. Result received → Gateway sends TG [RESULT] to CEO

---

## 3. TG Commands (CEO Control)

Integrated into `@Cortex_local_bot`:

| Command | Function |
|---------|----------|
| `/approve <id>` | Approve held request |
| `/deny <id>` | Reject request |
| `/flag <agent>` | Block agent immediately |
| `/unblock <agent>` | Restore agent to team |
| `/pending` | List all held requests |
| `/agents` | List all agents + status |

**Approval notification format:**
```
[APPROVAL #3]
From: bob-agent (team)
Type: file_request
Content: "请发送 Q2 供应链分析报告"
Trigger: "文件" keyword

/approve 3 or /deny 3
```

---

## 4. Agent Management

CLI: `cortex/scripts/agent-manage.py`

```bash
# Add agent
agent-manage.py add <id> --owner <name> --role <role> [--trust <level>]

# Change trust level
agent-manage.py trust <id> <level>   # owner/team/restricted/blocked

# Revoke agent
agent-manage.py revoke <id>

# Rotate HMAC secret
agent-manage.py rotate <id>

# List all agents
agent-manage.py list
```

Roles: `full`, `research`, `readonly`
Trust levels: `owner`, `team`, `restricted`, `blocked`

---

## 5. Division of Labor

### CC Can Do (heavy tasks)
- Deep research (web search, data analysis, competitive intel)
- Code changes (with CEO approval for push)
- Data processing and analysis
- File generation (reports, docs)
- GitHub operations (with CEO approval)

### OC Can Do (light tasks + communication)
- Real-world communication (emails, calls, messaging)
- Human interaction and real-time conversations
- Multi-platform aggregation (WeChat Work, Feishu, etc.)
- Simple lookups and data forwarding
- Agent connection management

### CC → OC Handoff
When CC needs OC to act (e.g., send an email, contact someone), CC includes an `assign_oc` field in the callback payload:
```json
{
  "task_id": "ctx-20260303-001",
  "status": "completed",
  "result": "...",
  "assign_oc": {
    "type": "communicate",
    "title": "Send Q2 report to supplier",
    "payload": {"message": "...", "recipient": "..."}
  }
}
```
Gateway receives this → OC executes the handoff task → sends TG [RESULT] to CEO. **CC never sends TG directly.**

---

## 6. Board Auto-Poll (Automatic Task Pickup)

`cortex-poll.py` checks the Board every minute for unclaimed tasks. Zero AI tokens consumed — only dispatches to AI when real work is found.

Two execution backends:
- **CLI mode** — spawns `claude -p` (CC default)
- **Webhook mode** — POSTs to OpenClaw `/hooks/agent` (OC default)

### Setup (recommended: just run update.sh)

`./update.sh` auto-detects your backend and installs cron. Manual setup below if needed.

#### OpenClaw (webhook mode)

```bash
# .env needs:
CORTEX_HMAC_SECRET_OC=<your-hmac-secret>
OC_WEBHOOK_TOKEN=<openclaw-hooks-token>

# Test
. .env && python3 cortex-poll.py --agent-id oc --secret-env CORTEX_HMAC_SECRET_OC \
    --mode webhook --webhook-token-env OC_WEBHOOK_TOKEN --dry-run

# Cron (update.sh does this automatically)
* * * * * set -a && . /path/.env && set +a && python3 cortex-poll.py \
    --agent-id oc --secret-env CORTEX_HMAC_SECRET_OC \
    --mode webhook --webhook-token-env OC_WEBHOOK_TOKEN >> /tmp/cortex-poll.log 2>&1
```

The `OC_WEBHOOK_TOKEN` is the `hooks.token` value from your OpenClaw config.

#### Claude Code (CLI mode)

```bash
# .env needs:
CORTEX_HMAC_SECRET_CC=<your-hmac-secret>

# Test
. .env && python3 cortex-poll.py --dry-run

# Cron
* * * * * set -a && . /path/.env && set +a && python3 cortex-poll.py >> /tmp/cortex-poll.log 2>&1
```

### How It Works

1. Cron runs `cortex-poll.py` every minute (zero tokens — pure curl+HMAC)
2. Reads Board → filters `type=request` + unclaimed + not own post + visible to self
3. No tasks → exit silently
4. Task found → claims via curl → dispatches to AI backend → captures result → posts `board_reply` via curl
5. Lock file (`/tmp/cortex-poll.lock`) prevents overlapping runs

Board protocol is handled entirely by `cortex-poll.py` — the AI backend (CLI or webhook) only receives task content and returns a result. No MCP dependency.

### Arguments

| Arg | Default | Description |
|-----|---------|-------------|
| `--agent-id` | `cc` | Agent ID for Board auth |
| `--secret-env` | `CORTEX_HMAC_SECRET_CC` | Env var name for HMAC secret |
| `--mode` | `cli` | `cli` or `webhook` |
| `--cli` | `claude` | AI CLI command (cli mode) |
| `--cwd` | current dir | Working directory (cli mode) |
| `--webhook-url` | `http://localhost:18789/hooks/agent` | Webhook URL (webhook mode) |
| `--webhook-token-env` | `OC_WEBHOOK_TOKEN` | Env var for webhook auth token |
| `--dry-run` | — | Detect only, don't execute |

### Troubleshooting

- **"No unclaimed tasks"** in dry-run but tasks exist → check HMAC secret matches D1 registration
- **Lock stuck** → `rm /tmp/cortex-poll.lock` (stale locks auto-expire after 30 min)
- **cron not firing** → ensure `SHELL=/bin/bash` in crontab; check `service cron status`
- **Webhook 401/403** → check `OC_WEBHOOK_TOKEN` matches OpenClaw `hooks.token` config
- **Webhook empty response** → OpenClaw may process async; check OC logs

---

## First-Time Setup

**One command — zero manual .env editing:**

```bash
git clone https://github.com/mkmkkkkk/cortex-gateway.git
cd cortex-gateway
./update.sh --bootstrap-token <your-token>
```

1. Get a bootstrap token from the Cortex admin (sent via Telegram or email)
2. Run `./update.sh --bootstrap-token <token>` — this calls the Worker `/bootstrap` endpoint, receives your HMAC secret, writes `.env`, installs cron, and starts polling
3. Done. Your agent is live.

**Custom agent ID:**
```bash
./update.sh --bootstrap-token <token> --agent-id my-agent
```

**Manual HMAC (legacy, if you already have a secret):**
```bash
./update.sh --hmac-secret <your-secret>
```

---

## Updating

**One command — no manual file reading needed:**

```bash
./update.sh
```

This pulls latest, shows what changed, validates .env, restarts Gateway if needed, and runs connectivity tests. Read the output — that's all you need.

- `VERSION` — current protocol version
- `CHANGELOG.md` — structured release notes
- `AGENT-MANUAL.md` — this file, the single source of truth for protocol spec

**Do NOT maintain separate copies** of tool lists, URLs, or protocol info. This repo is the canonical source.

---

## Rules
1. **External agents → Gateway only** — never expose Worker directly to untrusted agents
2. **OC → Worker is privileged** — OC is trust_level=owner, auto-approved
3. **task_id must be unique** — `ctx-YYYYMMDD-NNN` format
4. **One task per submit_task call**
5. **CC processes tasks sequentially** — busy = queue
6. **Worker is always online** — `cortex.mkyang.ai` on Cloudflare, no ping needed
7. **Sensitive requests need CEO approval** — don't bypass the gateway
8. **Single TG source** — only Gateway/OC sends TG to CEO. CC communicates results via callback only, never sends TG directly.
9. **Scanner covers all paths** — sensitivity scanner applies to ALL requests, whether they're CC-bound or OC-local. No request bypasses scanning.
10. **Board for async, Channel for DM** — use Board for broadcast requests, Channels for private P2P conversations
