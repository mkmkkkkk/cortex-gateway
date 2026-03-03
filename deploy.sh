#!/bin/bash
# Cortex Gateway — Deploy to AWS
#
# Usage:
#   ./deploy.sh <aws-host>
#   ./deploy.sh user@ip.addr.ess
#
# Prerequisites:
#   - SSH access to AWS host (key-based)
#   - Python 3.10+ on AWS host
#
# What it does:
#   1. rsync gateway files to AWS
#   2. Install Python dependencies
#   3. Create/update systemd service
#   4. Restart service

set -euo pipefail

if [ $# -lt 1 ]; then
    echo "Usage: $0 <aws-host>"
    echo "  e.g.: $0 ubuntu@52.1.2.3"
    exit 1
fi

AWS_HOST="$1"
REMOTE_DIR="/opt/cortex-gateway"

echo "=== Deploying Cortex Gateway to $AWS_HOST ==="

# 1. Sync files
echo "[1/4] Syncing files..."
ssh "$AWS_HOST" "sudo mkdir -p $REMOTE_DIR && sudo chown \$(whoami) $REMOTE_DIR"
rsync -avz --exclude='data/' --exclude='logs/' --exclude='__pycache__/' \
    --exclude='.env' --exclude='agent-registry.json' --exclude='.git/' \
    "$(dirname "$0")/" "$AWS_HOST:$REMOTE_DIR/"

# 2. Install deps
echo "[2/4] Installing dependencies..."
ssh "$AWS_HOST" "cd $REMOTE_DIR && pip3 install --user -r requirements.txt"

# 3. Create systemd service
echo "[3/4] Setting up systemd service..."
ssh "$AWS_HOST" "sudo tee /etc/systemd/system/cortex-gateway.service > /dev/null" <<'UNIT'
[Unit]
Description=Cortex Gateway — Agent Mesh Hub
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/opt/cortex-gateway
ExecStart=/usr/bin/python3 /opt/cortex-gateway/gateway-server.py
Restart=always
RestartSec=5
EnvironmentFile=/opt/cortex-gateway/.env

[Install]
WantedBy=multi-user.target
UNIT

# 4. Restart
echo "[4/4] Restarting service..."
ssh "$AWS_HOST" "sudo systemctl daemon-reload && sudo systemctl enable cortex-gateway && sudo systemctl restart cortex-gateway"

echo ""
echo "=== Deployed! ==="
echo "Check status: ssh $AWS_HOST 'sudo systemctl status cortex-gateway'"
echo "View logs:    ssh $AWS_HOST 'sudo journalctl -u cortex-gateway -f'"
echo ""
echo "IMPORTANT: Ensure $REMOTE_DIR/.env has all vars from .env.example"
echo "  and $REMOTE_DIR/agent-registry.json from agent-registry.example.json"
