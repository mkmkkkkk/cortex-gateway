#!/bin/bash
# cortex-gateway update — one command, everything synced.
# Usage:
#   ./update.sh                                        # normal update
#   ./update.sh --bootstrap-token <token>              # first-time setup (self-registration)
#   ./update.sh --hmac-secret <secret>                 # manual HMAC key (legacy)
#   ./update.sh --agent-id <id>                        # custom agent ID (default: oc)
#
# What it does:
#   1. git pull
#   2. Shows changelog
#   3. Auto-creates .env (bootstrap from Worker, or --hmac-secret, or env var)
#   4. Restarts Gateway if server code changed
#   5. Installs Board auto-poll cron
#   6. Connectivity test
#
# First-time setup:
#   1. Get a bootstrap token from Cortex admin (sent via Telegram/email)
#   2. Run: ./update.sh --bootstrap-token <token>
#   3. Done. .env is auto-created, cron installed, polling starts.
#
# OC: run this, read the output, done. No manual file reading needed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# ── Parse args ─────────────────────────────────────────────────
HMAC_SECRET_ARG=""
BOOTSTRAP_TOKEN_ARG=""
AGENT_ID_ARG="oc"
while [[ $# -gt 0 ]]; do
    case $1 in
        --hmac-secret) HMAC_SECRET_ARG="$2"; shift 2 ;;
        --bootstrap-token) BOOTSTRAP_TOKEN_ARG="$2"; shift 2 ;;
        --agent-id) AGENT_ID_ARG="$2"; shift 2 ;;
        *) shift ;;
    esac
done

WORKER_URL="https://cortex.mkyang.ai"

# ── Colors ────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

echo ""
echo -e "${CYAN}[cortex-gateway] Starting update...${NC}"
echo ""

# ── 1. Record current state ──────────────────────────────────
OLD_VERSION="unknown"
if [ -f VERSION ]; then
    OLD_VERSION=$(cat VERSION)
fi
OLD_HEAD=$(git rev-parse HEAD 2>/dev/null || echo "unknown")

# ── 2. Pull ──────────────────────────────────────────────────
echo -e "${CYAN}[1/6] Pulling latest...${NC}"
git pull --ff-only 2>&1 | head -20

NEW_HEAD=$(git rev-parse HEAD)
NEW_VERSION="unknown"
if [ -f VERSION ]; then
    NEW_VERSION=$(cat VERSION)
fi

# ── 3. Changelog ─────────────────────────────────────────────
if [ "$OLD_HEAD" != "$NEW_HEAD" ]; then
    echo ""
    echo -e "${CYAN}[2/6] Changes: v${OLD_VERSION} → v${NEW_VERSION}${NC}"
    echo "─────────────────────────────────────"
    git log --oneline "${OLD_HEAD}..${NEW_HEAD}" 2>/dev/null | head -20
    echo ""
    echo "Files changed:"
    git diff --stat "${OLD_HEAD}..${NEW_HEAD}" 2>/dev/null | tail -5
    if [ -f CHANGELOG.md ]; then
        echo ""
        echo -e "${CYAN}Release notes:${NC}"
        awk '/^## \[/{n++} n==1{print} n==2{exit}' CHANGELOG.md | head -30
    fi
    echo "─────────────────────────────────────"
    echo ""
else
    echo -e "${GREEN}  Code already up to date (v${NEW_VERSION}).${NC}"
    echo ""
fi

# ── 4. Auto-create .env ─────────────────────────────────────
echo -e "${CYAN}[3/6] Configuring .env...${NC}"

ENV_CREATED=false
ENV_OK=true

if [ ! -f .env ]; then
    echo "  .env not found — creating..."

    # --- Detect OpenClaw webhook token ---
    OC_TOKEN=""
    for cfg in \
        "$HOME/.clawdbot/config.json" \
        "$HOME/.config/openclaw/config.json" \
        "$HOME/.openclaw/config.json" \
        "$HOME/.clawdbot/config.yaml" \
        "$HOME/.clawdbot/config.yml"; do
        if [ -f "$cfg" ]; then
            if [[ "$cfg" == *.json ]]; then
                OC_TOKEN=$(python3 -c "
import json
try:
    with open('$cfg') as f:
        c = json.load(f)
    print(c.get('hooks',{}).get('token','') or c.get('hooks',{}).get('secret','') or c.get('api',{}).get('token',''))
except: pass
" 2>/dev/null)
            fi
            if [[ "$cfg" == *.yaml || "$cfg" == *.yml ]]; then
                OC_TOKEN=$(python3 -c "
try:
    import yaml
    with open('$cfg') as f:
        c = yaml.safe_load(f)
    print(c.get('hooks',{}).get('token','') or '')
except: pass
" 2>/dev/null)
            fi
            if [ -n "$OC_TOKEN" ]; then
                echo -e "  ${GREEN}Auto-detected OpenClaw webhook token from ${cfg}${NC}"
                break
            fi
        fi
    done

    # --- HMAC secret (priority: arg > env > bootstrap) ---
    HMAC_SECRET=""
    if [ -n "$HMAC_SECRET_ARG" ]; then
        HMAC_SECRET="$HMAC_SECRET_ARG"
        echo -e "  ${GREEN}Using HMAC secret from --hmac-secret arg${NC}"
    elif [ -n "${CORTEX_HMAC_SECRET_OC:-}" ]; then
        HMAC_SECRET="$CORTEX_HMAC_SECRET_OC"
        echo -e "  ${GREEN}Using HMAC secret from environment${NC}"
    elif [ -n "$BOOTSTRAP_TOKEN_ARG" ]; then
        # Self-registration via /bootstrap endpoint
        echo "  Bootstrapping with Cortex Worker..."
        BOOTSTRAP_RESP=$(curl -s -X POST "${WORKER_URL}/bootstrap" \
            --max-time 10 \
            -H "Content-Type: application/json" \
            -d "{\"agent_id\":\"${AGENT_ID_ARG}\",\"token\":\"${BOOTSTRAP_TOKEN_ARG}\"}" 2>/dev/null)
        BOOTSTRAP_OK=$(echo "$BOOTSTRAP_RESP" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('ok',''))" 2>/dev/null)
        if [ "$BOOTSTRAP_OK" = "True" ]; then
            HMAC_SECRET=$(echo "$BOOTSTRAP_RESP" | python3 -c "import json,sys; print(json.load(sys.stdin)['hmac_secret'])" 2>/dev/null)
            echo -e "  ${GREEN}Bootstrap successful — HMAC secret received${NC}"
        else
            BOOTSTRAP_ERR=$(echo "$BOOTSTRAP_RESP" | python3 -c "import json,sys; print(json.load(sys.stdin).get('error','unknown'))" 2>/dev/null || echo "connection failed")
            echo -e "  ${RED}Bootstrap failed: ${BOOTSTRAP_ERR}${NC}"
        fi
    fi

    if [ -z "$HMAC_SECRET" ]; then
        echo -e "  ${RED}HMAC secret not found.${NC}"
        echo "  First-time setup: ./update.sh --bootstrap-token <token>"
        echo "  Get your token from the Cortex admin."
        ENV_OK=false
    fi

    if [ -z "$OC_TOKEN" ]; then
        echo -e "  ${YELLOW}Could not auto-detect OpenClaw webhook token${NC}"
        echo "  Will try to read from OpenClaw config at runtime"
        OC_TOKEN="PLACEHOLDER_RUN_UPDATE_AGAIN_AFTER_OPENCLAW_STARTS"
    fi

    # --- Write .env ---
    if [ -n "$HMAC_SECRET" ]; then
        cat > .env << ENVEOF
# Auto-generated by update.sh — $(date -u +%Y-%m-%dT%H:%M:%SZ)
CORTEX_HMAC_SECRET_OC=${HMAC_SECRET}
OC_WEBHOOK_TOKEN=${OC_TOKEN}
ENVEOF
        chmod 600 .env
        ENV_CREATED=true
        echo -e "  ${GREEN}.env created${NC}"
    fi
else
    echo "  .env exists"
fi

# Validate required vars
REQUIRED_VARS=("CORTEX_HMAC_SECRET_OC")
if [ -f .env ]; then
    for var in "${REQUIRED_VARS[@]}"; do
        val=$(grep "^${var}=" .env 2>/dev/null | cut -d= -f2-)
        if [ -z "$val" ] || [[ "$val" == PLACEHOLDER* ]]; then
            echo -e "  ${RED}MISSING or placeholder: ${var}${NC}"
            ENV_OK=false
        fi
    done
fi

if [ "$ENV_OK" = true ] && [ -f .env ]; then
    echo -e "  ${GREEN}.env OK${NC}"
else
    echo -e "  ${YELLOW}WARNING: Fix .env issues above${NC}"
fi

# ── 5. Restart if server code changed ────────────────────────
echo ""
echo -e "${CYAN}[4/6] Checking if restart needed...${NC}"

SERVER_CHANGED=false
if [ "$OLD_HEAD" != "$NEW_HEAD" ]; then
    if git diff --name-only "${OLD_HEAD}..${NEW_HEAD}" 2>/dev/null | grep -qE '(gateway-server\.py|sensitivity-rules\.json)'; then
        SERVER_CHANGED=true
    fi
fi

if [ "$SERVER_CHANGED" = true ]; then
    echo "  gateway-server.py or rules changed — restarting..."
    if systemctl is-active --quiet cortex-gateway 2>/dev/null; then
        sudo systemctl restart cortex-gateway
        sleep 2
        if systemctl is-active --quiet cortex-gateway; then
            echo -e "${GREEN}  Gateway restarted OK${NC}"
        else
            echo -e "${RED}  Gateway failed to start! Check: sudo journalctl -u cortex-gateway -n 20${NC}"
        fi
    else
        echo -e "${YELLOW}  Gateway not running as systemd service — restart manually${NC}"
    fi
else
    echo -e "${GREEN}  No server code changes — no restart needed${NC}"
fi

# ── 6. Board Auto-Poll (cron) ────────────────────────────────
echo ""
echo -e "${CYAN}[5/6] Checking Board auto-poll cron...${NC}"

# Detect execution backend: webhook (OpenClaw) > CLI (claude/happy)
POLL_MODE=""
POLL_ARGS=""
if curl -s -o /dev/null -w "%{http_code}" http://localhost:18789/hooks/agent 2>/dev/null | grep -qE "^(401|403|405|200)"; then
    POLL_MODE="webhook"
    POLL_ARGS="--mode webhook --webhook-url http://localhost:18789/hooks/agent --webhook-token-env OC_WEBHOOK_TOKEN"
    echo "  Detected: OpenClaw webhook (localhost:18789)"
else
    for cli_candidate in claude happy; do
        if command -v "$cli_candidate" &>/dev/null; then
            POLL_MODE="cli"
            POLL_ARGS="--mode cli --cli ${cli_candidate}"
            echo "  Detected: AI CLI ($cli_candidate)"
            break
        fi
    done
fi

POLL_INSTALLED=false
if crontab -l 2>/dev/null | grep -q "cortex-poll"; then
    # Update existing cron if mode changed
    CURRENT_CRON=$(crontab -l 2>/dev/null | grep "cortex-poll")
    if [ -n "$POLL_MODE" ] && ! echo "$CURRENT_CRON" | grep -q "$POLL_MODE"; then
        echo "  Updating cron to mode=$POLL_MODE..."
        CRON_LINE="* * * * * set -a && . ${SCRIPT_DIR}/.env && set +a && python3 ${SCRIPT_DIR}/cortex-poll.py --agent-id ${AGENT_ID_ARG} --secret-env CORTEX_HMAC_SECRET_OC ${POLL_ARGS} >> /tmp/cortex-poll.log 2>&1"
        (crontab -l 2>/dev/null | grep -v "cortex-poll"; echo "SHELL=/bin/bash"; echo "$CRON_LINE") | crontab -
        echo -e "${GREEN}  cortex-poll cron: updated (mode=$POLL_MODE)${NC}"
    else
        echo -e "${GREEN}  cortex-poll cron: already installed${NC}"
    fi
    POLL_INSTALLED=true
elif [ -z "$POLL_MODE" ]; then
    echo -e "${YELLOW}  No AI backend found (OpenClaw webhook / claude / happy)${NC}"
    echo "  Start OpenClaw or install claude CLI, then re-run ./update.sh"
else
    echo "  Installing cortex-poll cron (every minute, mode=$POLL_MODE)..."
    CRON_LINE="* * * * * set -a && . ${SCRIPT_DIR}/.env && set +a && python3 ${SCRIPT_DIR}/cortex-poll.py --agent-id ${AGENT_ID_ARG} --secret-env CORTEX_HMAC_SECRET_OC ${POLL_ARGS} >> /tmp/cortex-poll.log 2>&1"
    (crontab -l 2>/dev/null | grep -v "cortex-poll"; echo "SHELL=/bin/bash"; echo "$CRON_LINE") | crontab -
    if crontab -l 2>/dev/null | grep -q "cortex-poll"; then
        POLL_INSTALLED=true
        echo -e "${GREEN}  cortex-poll cron: installed OK (mode=$POLL_MODE)${NC}"
    else
        echo -e "${RED}  cortex-poll cron: install failed${NC}"
    fi
fi

# ── 7. Connectivity test ─────────────────────────────────────
echo ""
echo -e "${CYAN}[6/6] Connectivity test...${NC}"

WORKER_OK=false
if curl -sf --max-time 5 "https://cortex.mkyang.ai/health" > /dev/null 2>&1; then
    WORKER_OK=true
    echo -e "${GREEN}  Worker (cortex.mkyang.ai): OK${NC}"
else
    if curl -sf --max-time 5 -X POST "https://cortex.mkyang.ai/mcp" \
        -H "Content-Type: application/json" \
        -d '{"jsonrpc":"2.0","method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"update-test","version":"1.0"}},"id":1}' \
        > /dev/null 2>&1; then
        WORKER_OK=true
        echo -e "${GREEN}  Worker (cortex.mkyang.ai): OK${NC}"
    else
        echo -e "${YELLOW}  Worker (cortex.mkyang.ai): unreachable (may be network issue)${NC}"
    fi
fi

GW_OK=false
if curl -sf --max-time 3 "http://127.0.0.1:8750/health" > /dev/null 2>&1; then
    GW_OK=true
    echo -e "${GREEN}  Gateway (localhost:8750): OK${NC}"
else
    echo -e "${YELLOW}  Gateway (localhost:8750): not responding${NC}"
fi

# ── Summary ──────────────────────────────────────────────────
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo -e "${CYAN}[cortex-gateway]${NC} v${NEW_VERSION}"
if [ "$OLD_HEAD" != "$NEW_HEAD" ]; then
    echo -e "  Updated:  v${OLD_VERSION} → v${NEW_VERSION} ($(git log --oneline "${OLD_HEAD}..${NEW_HEAD}" 2>/dev/null | wc -l | tr -d ' ') commits)"
fi
echo -e "  Env:      $([ "$ENV_OK" = true ] && echo -e "${GREEN}OK${NC}" || echo -e "${RED}NEEDS FIX${NC}")$([ "$ENV_CREATED" = true ] && echo " (auto-created)")"
echo -e "  Restart:  $([ "$SERVER_CHANGED" = true ] && echo "yes" || echo "no")"
echo -e "  Worker:   $([ "$WORKER_OK" = true ] && echo -e "${GREEN}OK${NC}" || echo -e "${YELLOW}?${NC}")"
echo -e "  Gateway:  $([ "$GW_OK" = true ] && echo -e "${GREEN}OK${NC}" || echo -e "${YELLOW}?${NC}")"
echo -e "  Poll:     $([ "$POLL_INSTALLED" = true ] && echo -e "${GREEN}OK (${POLL_MODE:-unknown})${NC}" || echo -e "${YELLOW}NOT SET UP${NC}")"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
