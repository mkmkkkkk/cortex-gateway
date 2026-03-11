#!/usr/bin/env python3
"""
Cortex Gateway — Canonical MCP Client.

Drop-in Python client for agents connecting to Cortex Gateway.
Handles HMAC signing, MCP protocol handshake, and tool calls.

Usage:
    from mcp_client import CortexClient

    client = CortexClient(
        gateway_url="http://localhost:8750/mcp",
        agent_id="my-agent",
        hmac_secret="my-secret",
    )

    # Submit a task
    result = client.submit_request(
        request_type="research",
        title="Find Python best practices",
        content="Research modern Python packaging approaches",
    )

    # Check status
    status = client.check_status(result["request_id"])
"""

import hashlib
import hmac as hmac_mod
import json
import time
from urllib.request import Request, urlopen


class CortexClient:
    """MCP client for Cortex Gateway with HMAC auth."""

    def __init__(self, gateway_url: str, agent_id: str, hmac_secret: str, timeout: int = 30):
        self.gateway_url = gateway_url.rstrip("/")
        self.agent_id = agent_id
        self.hmac_secret = hmac_secret
        self.timeout = timeout
        self._session_id = ""

    def _sign(self, body: bytes) -> dict:
        """Create HMAC auth headers."""
        ts = str(int(time.time()))
        msg = f"{ts}.".encode() + body
        sig = hmac_mod.new(self.hmac_secret.encode(), msg, hashlib.sha256).hexdigest()
        return {
            "X-CC-Agent-ID": self.agent_id,
            "X-CC-Timestamp": ts,
            "X-CC-Signature": sig,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }

    def _rpc(self, body: dict) -> dict:
        """Send JSON-RPC request to Gateway MCP."""
        raw = json.dumps(body).encode()
        headers = self._sign(raw)
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        req = Request(self.gateway_url, data=raw, headers=headers)
        resp = urlopen(req, timeout=self.timeout)
        self._session_id = resp.headers.get("Mcp-Session-Id", self._session_id)
        data = resp.read()
        return json.loads(data) if data else {}

    def _init_session(self):
        """MCP handshake: initialize + notifications/initialized."""
        self._session_id = ""
        self._rpc({
            "jsonrpc": "2.0",
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": f"cortex-client-{self.agent_id}", "version": "1.0"},
            },
            "id": 1,
        })
        self._rpc({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _call_tool(self, name: str, arguments: dict) -> dict:
        """Call an MCP tool. Returns parsed result."""
        self._init_session()
        resp = self._rpc({
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
            "id": 2,
        })
        content = resp.get("result", {}).get("content", [])
        if content and content[0].get("text"):
            return json.loads(content[0]["text"])
        return resp

    def ping(self) -> dict:
        """Health check."""
        return self._call_tool("ping", {})

    def submit_request(
        self,
        request_type: str,
        title: str,
        content: str,
        priority: str = "normal",
        callback_url: str = "",
    ) -> dict:
        """Submit a task. Returns {status, request_id, will_hold, message}."""
        args = {
            "request_type": request_type,
            "title": title,
            "content": content,
            "priority": priority,
        }
        if callback_url:
            args["callback_url"] = callback_url
        return self._call_tool("submit_request", args)

    def check_status(self, request_id: str) -> dict:
        """Check task status. Returns {request_id, status, message}."""
        return self._call_tool("check_status", {"request_id": request_id})
