#!/bin/bash
# cortex-gateway update — one command, everything synced.
# Usage: ./update.sh
#
# What it does:
#   1. Records current version
#   2. git pull
#   3. Shows structured changelog
#   4. Validates .env
#   5. Restarts Gateway if server code changed
#   6. Connectivity test
#
# OC: run this, read the output, done. No manual file reading needed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# ── Colors ────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

echo ""
echo -e "${CYAN}[cortex-gateway] Starting update...${NC}"
echo ""

# ── 1. Record current state ──────────────────────────────────────
OLD_VERSION="unknown"
if [ -f VERSION ]; then
    OLD_VERSION=$(cat VERSION)
fi
OLD_HEAD=$(git rev-parse HEAD 2>/dev/null || echo "unknown")

# ── 2. Pull ──────────────────────────────────────────────────────
echo -e "${CYAN}[1/5] Pulling latest...${NC}"
git pull --ff-only 2>&1 | head -20

NEW_HEAD=$(git rev-parse HEAD)
NEW_VERSION="unknown"
if [ -f VERSION ]; then
    NEW_VERSION=$(cat VERSION)
fi

if [ "$OLD_HEAD" = "$NEW_HEAD" ]; then
    echo ""
    echo -e "${GREEN}Already up to date (v${NEW_VERSION}).${NC}"
    echo ""
    exit 0
fi

# ── 3. Changelog ─────────────────────────────────────────────────
echo ""
echo -e "${CYAN}[2/5] Changes: v${OLD_VERSION} → v${NEW_VERSION}${NC}"
echo "─────────────────────────────────────"

# Show commit messages since last version
git log --oneline "${OLD_HEAD}..${NEW_HEAD}" 2>/dev/null | head -20

# Show which files changed
echo ""
echo "Files changed:"
git diff --stat "${OLD_HEAD}..${NEW_HEAD}" 2>/dev/null | tail -5

# Extract latest changelog entry (between first two ## headers)
if [ -f CHANGELOG.md ]; then
    echo ""
    echo -e "${CYAN}Release notes:${NC}"
    awk '/^## \[/{n++} n==1{print} n==2{exit}' CHANGELOG.md | head -30
fi

echo "─────────────────────────────────────"
echo ""

# ── 4. Validate .env ─────────────────────────────────────────────
echo -e "${CYAN}[3/5] Validating .env...${NC}"

REQUIRED_VARS=(
    "CORTEX_TG_BOT_TOKEN"
    "CORTEX_TG_CHAT_ID"
    "CORTEX_HMAC_SECRET_OC"
)

ENV_OK=true
if [ ! -f .env ]; then
    echo -e "${RED}  MISSING: .env file not found${NC}"
    echo "  Run: cp .env.example .env && edit values"
    ENV_OK=false
else
    for var in "${REQUIRED_VARS[@]}"; do
        if ! grep -q "^${var}=" .env 2>/dev/null; then
            echo -e "${RED}  MISSING: ${var}${NC}"
            ENV_OK=false
        fi
    done
fi

if [ "$ENV_OK" = true ]; then
    echo -e "${GREEN}  .env OK${NC}"
else
    echo -e "${YELLOW}  WARNING: Fix .env before restarting${NC}"
fi

# ── 5. Restart if server code changed ────────────────────────────
echo ""
echo -e "${CYAN}[4/5] Checking if restart needed...${NC}"

SERVER_CHANGED=false
if git diff --name-only "${OLD_HEAD}..${NEW_HEAD}" 2>/dev/null | grep -qE '(gateway-server\.py|sensitivity-rules\.json)'; then
    SERVER_CHANGED=true
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
        echo "  e.g.: pkill -f gateway-server.py; nohup python3 gateway-server.py &"
    fi
else
    echo -e "${GREEN}  No server code changes — no restart needed${NC}"
fi

# ── 6. Board Auto-Poll (cron) ────────────────────────────────────
echo -e "${CYAN}[5/6] Checking Board auto-poll cron...${NC}"

POLL_INSTALLED=false
if crontab -l 2>/dev/null | grep -q "cortex-poll"; then
    POLL_INSTALLED=true
    echo -e "${GREEN}  cortex-poll cron: installed${NC}"
else
    echo -e "${YELLOW}  cortex-poll cron: NOT installed${NC}"
    echo ""
    echo "  To enable automatic Board task pickup, run:"
    echo ""
    echo "    (crontab -l 2>/dev/null; echo 'SHELL=/bin/bash'; echo '* * * * * set -a && . ${SCRIPT_DIR}/.env && set +a && python3 ${SCRIPT_DIR}/cortex-poll.py --agent-id oc --secret-env CORTEX_HMAC_SECRET_OC >> /tmp/cortex-poll.log 2>&1') | crontab -"
    echo ""
    echo "  See AGENT-MANUAL.md Section 6 for details."
fi

# ── 7. Connectivity test ─────────────────────────────────────────
echo ""
echo -e "${CYAN}[6/6] Connectivity test...${NC}"

# Test Worker
WORKER_OK=false
if curl -sf --max-time 5 "https://cortex.mkyang.ai/health" > /dev/null 2>&1; then
    WORKER_OK=true
    echo -e "${GREEN}  Worker (cortex.mkyang.ai): OK${NC}"
else
    # Try MCP ping as fallback
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

# Test Gateway local
GW_OK=false
if curl -sf --max-time 3 "http://127.0.0.1:8750/health" > /dev/null 2>&1; then
    GW_OK=true
    echo -e "${GREEN}  Gateway (localhost:8750): OK${NC}"
else
    if [ "$SERVER_CHANGED" = true ]; then
        echo -e "${YELLOW}  Gateway (localhost:8750): not responding (may still be starting)${NC}"
    else
        echo -e "${YELLOW}  Gateway (localhost:8750): not responding${NC}"
    fi
fi

# ── Summary ──────────────────────────────────────────────────────
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo -e "${CYAN}[cortex-gateway]${NC} v${OLD_VERSION} → v${NEW_VERSION}"
echo -e "  Commits:  $(git log --oneline "${OLD_HEAD}..${NEW_HEAD}" 2>/dev/null | wc -l | tr -d ' ')"
echo -e "  Restart:  $([ "$SERVER_CHANGED" = true ] && echo "yes" || echo "no")"
echo -e "  Env:      $([ "$ENV_OK" = true ] && echo -e "${GREEN}OK${NC}" || echo -e "${RED}NEEDS FIX${NC}")"
echo -e "  Worker:   $([ "$WORKER_OK" = true ] && echo -e "${GREEN}OK${NC}" || echo -e "${YELLOW}?${NC}")"
echo -e "  Gateway:  $([ "$GW_OK" = true ] && echo -e "${GREEN}OK${NC}" || echo -e "${YELLOW}?${NC}")"
echo -e "  Poll:     $([ "$POLL_INSTALLED" = true ] && echo -e "${GREEN}OK${NC}" || echo -e "${YELLOW}NOT SET UP${NC}")"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
