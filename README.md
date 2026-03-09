# Cortex Gateway

Agent Mesh Hub for the Cortex system. Runs on AWS (OC side). Accepts MCP requests from external agents, applies sensitivity scanning, routes approvals through Telegram, and relays tasks to CC (Claude Code) via Cloudflare Tunnel.

## Quick Start

```bash
git clone https://github.com/mkmkkkkk/cortex-gateway.git
cd cortex-gateway

# Setup
cp .env.example .env           # Fill in your values
cp agent-registry.example.json agent-registry.json  # Edit as needed
pip3 install -r requirements.txt

# Run
python3 gateway-server.py
# Server starts on :8750
```

## Update

```bash
git pull
# Restart your service (systemd, pm2, etc.)
```

`.env`, `agent-registry.json`, `data/`, and `logs/` are gitignored — `git pull` never overwrites your config or state.

## Systemd (Production)

```ini
[Unit]
Description=Cortex Gateway
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/opt/cortex-gateway
EnvironmentFile=/opt/cortex-gateway/.env
ExecStart=/usr/bin/python3 gateway-server.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo cp cortex-gateway.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now cortex-gateway
```

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `CORTEX_TG_BOT_TOKEN` | Yes | Telegram bot token |
| `CORTEX_TG_CHAT_ID` | Yes | Target Telegram group/chat ID |
| `CORTEX_TG_CEO_USER_ID` | No | CEO's TG user_id (command auth) |
| `CC_MCP_URL` | Yes* | CC tunnel endpoint (auto-updated via `/update-tunnel-url`) |
| `CORTEX_HMAC_SECRET_OC` | Yes | Gateway→CC HMAC shared secret |
| `CC_TUNNEL_SECRET` | Yes | Auth for `/update-tunnel-url` |
| `GW_CALLBACK_SECRET` | Yes | CC→Gateway callback auth |
| `GW_CALLBACK_URL` | No | Gateway's own callback URL |
| `GW_PUBLIC_URL` | No | Public URL for onboard links |

\* `CC_MCP_URL` can be empty at startup — CC pushes the URL automatically via `/update-tunnel-url`.

## API Endpoints

| Endpoint | Method | Auth | Description |
|----------|--------|------|-------------|
| `/mcp` | POST | HMAC | MCP over Streamable HTTP (agent tools) |
| `/callback` | POST | `X-Callback-Secret` | CC posts task results here |
| `/update-tunnel-url` | POST | `X-Tunnel-Secret` | CC pushes new tunnel URL |
| `/cc-url` | GET | None | Query current CC tunnel URL |
| `/onboard` | GET | Invite token | Agent self-service registration |

## Telegram Commands

| Command | Description |
|---------|-------------|
| `/approve <id>` | Approve pending request |
| `/deny <id>` | Deny pending request |
| `/pending` | List pending approvals |
| `/agents` | List all registered agents |
| `/flag <agent>` | Block suspicious agent |
| `/unblock <agent>` | Restore agent |
| `/invite <id>` | Generate onboard invite |
| `/invites` | List active invites |
| `/revoke-invite <id>` | Revoke invite |
| `/help` | Show all commands |

## Security

- **HMAC-SHA256** per-agent auth with replay prevention
- **Auto-ban**: 20 failures in 10min → 1h IP ban
- **Rate limiting**: Per-agent configurable limits
- **Content firewall**: Block keywords + hold keywords + path blocking
- **Fail-closed**: Missing auth config = reject all
- **Sensitivity scanning**: Tasks matched against `sensitivity-rules.json`

## Agent Onboarding

1. CEO sends `/invite myagent` in Telegram
2. Gateway returns a one-time onboard URL
3. Agent visits URL → gets assigned HMAC secret + agent ID
4. Agent uses HMAC headers for all subsequent requests

## Architecture

```
External Agents → Gateway (this server, :8750)
                    ├── Sensitivity scan
                    ├── TG approval (if needed)
                    └── Relay to CC via Cloudflare Tunnel
                         ↓
                    CC (Claude Code, local)
                         ↓
                    Results → /callback → Gateway → Agent
```
