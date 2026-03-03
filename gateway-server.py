#!/usr/bin/env python3
"""
Cortex Gateway — Agent Mesh Hub.

Runs on AWS (OC side). Accepts requests from external agents,
scans for sensitivity, queues for approval via TG, relays to CC.

Usage:
  python3 gateway-server.py              # Start on :8750
  python3 gateway-server.py --port 9000  # Custom port

Tools exposed (MCP):
  ping            — Health check
  submit_request  — Submit task (sensitivity-scanned, may need approval)
  check_status    — Check request status

TG Commands (CEO control):
  /approve <id>   — Approve pending request
  /deny <id>      — Deny pending request
  /flag <agent>   — Block suspicious agent immediately
  /unblock <agent> — Restore agent to team trust
  /pending        — List pending approvals
  /agents         — List all agents
"""

import contextvars
import hashlib
import hmac as hmac_mod
import html as html_mod
import json
import os
import re
import sys
import tempfile
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

# ── Paths ─────────────────────────────────────────────────────────

SCRIPT_DIR = Path(__file__).resolve().parent
DATA_DIR = SCRIPT_DIR / "data"
STATE_FILE = DATA_DIR / "gateway-state.json"
RULES_FILE = SCRIPT_DIR / "sensitivity-rules.json"
REGISTRY_FILE = SCRIPT_DIR / "agent-registry.json"
LOG_DIR = SCRIPT_DIR / "logs"

DATA_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

AUDIT_LOG = LOG_DIR / "gateway-audit.jsonl"


# ── Load .env ─────────────────────────────────────────────────────

def _load_env():
    """Load .env files (gateway-local first, then /workspace fallback)."""
    for env_path in [SCRIPT_DIR / ".env", Path("/workspace/.env")]:
        if not env_path.exists():
            continue
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip())


_load_env()


# ── Config ────────────────────────────────────────────────────────

TG_BOT_TOKEN = os.environ.get("CORTEX_TG_BOT_TOKEN", "")
TG_CHAT_ID = os.environ.get("CORTEX_TG_CHAT_ID", "")
TG_CEO_USER_ID = os.environ.get("CORTEX_TG_CEO_USER_ID", "")  # CEO's TG user_id for per-user auth
CC_MCP_URL = os.environ.get("CC_MCP_URL", "")  # CC's Cloudflare Tunnel endpoint
CC_HMAC_SECRET = os.environ.get("CORTEX_HMAC_SECRET_OC", "")  # Gateway → CC auth
HMAC_WINDOW_SEC = 60  # 60s replay window (H4: tightened from 300s)
DEFAULT_RATE_LIMIT = 10
RATE_WINDOW_SEC = 60
MAX_BODY_BYTES = 1_048_576  # 1MB max request body (H2)
MAX_STATE_REQUESTS = 10_000  # prune old entries beyond this (H3)
VALID_REQUEST_TYPES = {"research", "file_request", "action", "collaboration"}
VALID_PRIORITIES = {"normal", "urgent"}
REQUEST_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_\-]{1,128}$")  # H6: strict format


# ── Logging ───────────────────────────────────────────────────────

def _now():
    return datetime.now(timezone.utc).isoformat()


def _log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{ts}] GW: {msg}", flush=True)


def _audit(event: str, **fields):
    entry = {"ts": _now(), "event": event, **fields}
    try:
        with open(AUDIT_LOG, "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


# ── State (persisted to disk) ─────────────────────────────────────

_state_lock = threading.Lock()
_state = {
    "pending_approvals": {},  # str(id) -> {request_data, agent_id, trigger, created_at}
    "relay_queue": [],        # approved tasks waiting to send to CC
    "requests": {},           # request_id -> {status, agent_id, ...}
    "next_id": 1,
    "tg_offset": 0,
}


def _load_state():
    global _state
    if STATE_FILE.exists():
        try:
            _state = json.loads(STATE_FILE.read_text())
        except Exception:
            pass


def _save_state():
    """Atomic state write: temp file + rename (H5)."""
    try:
        content = json.dumps(_state, indent=2, ensure_ascii=False)
        fd, tmp_path = tempfile.mkstemp(dir=str(DATA_DIR), suffix=".tmp")
        try:
            os.write(fd, content.encode())
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp_path, str(STATE_FILE))
    except Exception as e:
        _log(f"WARNING: Failed to save state: {e}")


def _prune_state():
    """Remove oldest completed requests when state exceeds limit (H3)."""
    requests = _state.get("requests", {})
    if len(requests) <= MAX_STATE_REQUESTS:
        return
    # Keep pending/approved, prune oldest completed/blocked/denied
    prunable = [
        (rid, info) for rid, info in requests.items()
        if info.get("status") in ("blocked", "denied", "relayed", "completed")
    ]
    prunable.sort(key=lambda x: x[1].get("submitted_at", ""))
    to_remove = len(requests) - MAX_STATE_REQUESTS
    for rid, _ in prunable[:to_remove]:
        del requests[rid]


_load_state()


# ── HMAC Replay Tracking (H4) ────────────────────────────────────

_seen_sigs: dict[str, float] = {}  # sig_hex -> expiry_time
_seen_sigs_lock = threading.Lock()


def _check_replay(sig_hex: str) -> bool:
    """Returns True if this signature was already used (replay attack)."""
    now = time.time()
    with _seen_sigs_lock:
        # Prune expired entries
        expired = [k for k, v in _seen_sigs.items() if v < now]
        for k in expired:
            del _seen_sigs[k]
        if sig_hex in _seen_sigs:
            return True  # replay!
        _seen_sigs[sig_hex] = now + HMAC_WINDOW_SEC
        return False


# ── Agent Registry ────────────────────────────────────────────────

_registry_lock = threading.Lock()  # H7: prevent concurrent registry corruption


def _load_registry() -> dict:
    if not REGISTRY_FILE.exists():
        return {"agents": {}}
    try:
        return json.loads(REGISTRY_FILE.read_text())
    except Exception:
        return {"agents": {}}


def _save_registry(data: dict):
    REGISTRY_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def _get_agent(agent_id: str) -> dict | None:
    return _load_registry().get("agents", {}).get(agent_id)


def _get_agent_secret(agent: dict) -> str:
    env_key = agent.get("hmac_secret_env", "")
    return os.environ.get(env_key, "") if env_key else ""


# ── Sensitivity Rules ─────────────────────────────────────────────

def _load_rules() -> dict:
    if RULES_FILE.exists():
        try:
            return json.loads(RULES_FILE.read_text())
        except Exception:
            pass
    return {"block_keywords": [], "hold_keywords": [], "block_paths": [], "auto_allow_types": []}


RULES = _load_rules()


def _normalize_for_scan(text: str) -> str:
    """Normalize text to defeat encoding bypass attempts.

    Strips zero-width chars, URL-decodes, normalizes unicode.
    """
    import unicodedata
    from urllib.parse import unquote
    # Strip zero-width characters
    zw_chars = "\u200b\u200c\u200d\u200e\u200f\ufeff\u2060\u2061\u2062\u2063\u2064"
    for c in zw_chars:
        text = text.replace(c, "")
    # URL-decode (double-decode to catch %252E → %2E → .)
    text = unquote(unquote(text))
    # Unicode NFKC normalization (fullwidth → ASCII, etc.)
    text = unicodedata.normalize("NFKC", text)
    return text


def scan_sensitivity(content: str, request_type: str = "") -> tuple[str, str]:
    """Scan content for sensitivity.

    Returns (action, trigger) where action is "allow", "hold", or "block".
    """
    # Normalize to catch encoding bypass attempts (ZWS, URL-encoding, fullwidth, etc.)
    content_lower = _normalize_for_scan(content).lower()

    # Block check — highest priority
    for kw in RULES.get("block_keywords", []):
        if kw.lower() in content_lower:
            return "block", kw
    for path_pattern in RULES.get("block_paths", []):
        if path_pattern.lower() in content_lower:
            return "block", path_pattern

    # Auto-allow certain types (e.g., pure research)
    if request_type in RULES.get("auto_allow_types", []):
        return "allow", ""

    # Hold check
    for kw in RULES.get("hold_keywords", []):
        if kw.lower() in content_lower:
            return "hold", kw

    return "allow", ""


# ── Trust + Sensitivity Check ─────────────────────────────────────

def check_trust(agent_id: str, content: str, request_type: str) -> tuple[str, str]:
    """Combined trust level + content sensitivity check.

    Returns (action, trigger).
    """
    agent = _get_agent(agent_id)
    if not agent:
        return "block", "unknown_agent"

    trust = agent.get("trust_level", "team")

    if trust == "blocked":
        return "block", "agent_blocked"
    if trust == "owner":
        return "allow", ""  # owner bypasses all checks
    if trust == "restricted":
        return "hold", "restricted_agent"

    # team level — run sensitivity scanner
    return scan_sensitivity(content, request_type)


# ── Telegram Helpers ──────────────────────────────────────────────

def _tg_escape(text: str) -> str:
    """Escape untrusted text for TG HTML messages (C2: prevent injection)."""
    return html_mod.escape(str(text), quote=True)


def _tg_send(text: str, parse_mode: str = "HTML"):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        _log(f"[TG-SKIP] {text}")
        return
    data = json.dumps({
        "chat_id": TG_CHAT_ID, "text": text,
        "parse_mode": parse_mode,
    }).encode()
    req = Request(
        f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
        data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        urlopen(req, timeout=10)
    except Exception as e:
        _log(f"[TG-ERR] {e}")


def _tg_get_updates(offset: int = 0) -> tuple[list, int]:
    if not TG_BOT_TOKEN:
        return [], offset
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/getUpdates?offset={offset}&timeout=5"
    try:
        resp = urlopen(Request(url), timeout=15)
        data = json.loads(resp.read())
    except Exception:
        return [], offset

    if not data.get("ok"):
        return [], offset

    results = data.get("result", [])
    if not results:
        return [], offset

    messages = []
    max_uid = offset
    for update in results:
        uid = update.get("update_id", 0)
        if uid > max_uid:
            max_uid = uid
        msg = update.get("message")
        if not msg:
            continue
        from_user = msg.get("from", {})
        messages.append({
            "text": msg.get("text", ""),
            "from_id": from_user.get("id"),
            "from_name": from_user.get("first_name", ""),
            "chat_id": msg.get("chat", {}).get("id"),
            "is_bot": from_user.get("is_bot", False),
        })

    new_offset = max_uid + 1 if max_uid >= offset else offset
    return messages, new_offset


# ── TG Command Handler ────────────────────────────────────────────

def _handle_tg_command(text: str, from_name: str, chat_id: int = 0, from_id: int = 0):
    """Process /commands from Telegram. Only accepts from authorized chat + user."""
    if TG_CHAT_ID and str(chat_id) != str(TG_CHAT_ID):
        _audit("tg_unauthorized", chat_id=chat_id, from_name=from_name, text=text[:50])
        _log(f"[TG-AUTH] Unauthorized chat_id={chat_id}, from={from_name}")
        return  # silently ignore
    # Per-user auth: only CEO can run commands (not just any group member)
    if TG_CEO_USER_ID and str(from_id) != str(TG_CEO_USER_ID):
        _audit("tg_unauthorized_user", chat_id=chat_id, from_id=from_id, from_name=from_name, text=text[:50])
        _log(f"[TG-AUTH] Non-CEO user {from_name}(id={from_id}) tried: {text[:30]}")
        _tg_send(f"Unauthorized: only CEO can run gateway commands.")
        return
    parts = text.strip().split()
    cmd = parts[0].lower() if parts else ""
    arg = parts[1] if len(parts) > 1 else ""

    if cmd == "/approve" and arg:
        with _state_lock:
            if arg in _state["pending_approvals"]:
                approval = _state["pending_approvals"].pop(arg)
                task_data = approval["request_data"]
                task_data["status"] = "approved"
                task_data["approved_by"] = from_name
                task_data["approved_at"] = _now()
                _state["relay_queue"].append(task_data)
                req_id = approval["request_id"]
                _state["requests"][req_id]["status"] = "approved"
                _save_state()
            else:
                _tg_send(f"#{arg} not found in pending")
                return
        _tg_send(f"Approved #{arg}")
        _audit("approval", approval_id=arg, by=from_name)

    elif cmd == "/deny" and arg:
        with _state_lock:
            if arg in _state["pending_approvals"]:
                approval = _state["pending_approvals"].pop(arg)
                req_id = approval["request_id"]
                _state["requests"][req_id]["status"] = "denied"
                _state["requests"][req_id]["denied_by"] = from_name
                _save_state()
            else:
                _tg_send(f"#{arg} not found in pending")
                return
        _tg_send(f"Denied #{arg}")
        _audit("denial", approval_id=arg, by=from_name)

    elif cmd == "/flag" and arg:
        with _registry_lock:
            registry = _load_registry()
            agents = registry.get("agents", {})
            if arg in agents:
                agents[arg]["trust_level"] = "blocked"
                agents[arg]["blocked_at"] = _now()
                agents[arg]["blocked_by"] = from_name
                _save_registry(registry)
                _tg_send(f"Agent '{_tg_escape(arg)}' BLOCKED")
                _audit("agent_flagged", agent_id=arg, by=from_name)
            else:
                _tg_send(f"Agent '{_tg_escape(arg)}' not found")

    elif cmd == "/unblock" and arg:
        with _registry_lock:
            registry = _load_registry()
            agents = registry.get("agents", {})
            if arg in agents:
                agents[arg]["trust_level"] = "team"
                agents[arg].pop("blocked_at", None)
                agents[arg].pop("blocked_by", None)
                _save_registry(registry)
                _tg_send(f"Agent '{_tg_escape(arg)}' unblocked (team)")
                _audit("agent_unblocked", agent_id=arg, by=from_name)
            else:
                _tg_send(f"Agent '{_tg_escape(arg)}' not found")

    elif cmd == "/pending":
        pending = _state["pending_approvals"]
        if not pending:
            _tg_send("No pending approvals")
        else:
            lines = ["<b>Pending Approvals:</b>"]
            for rid, info in pending.items():
                rd = info.get("request_data", {})
                lines.append(
                    f"\n#{rid} | {_tg_escape(info.get('agent_id', '?'))}"
                    f"\n  {_tg_escape(rd.get('title', '?'))}"
                    f"\n  Trigger: {_tg_escape(info.get('trigger', '?'))}"
                )
            _tg_send("\n".join(lines))

    elif cmd == "/agents":
        registry = _load_registry()
        agents = registry.get("agents", {})
        if not agents:
            _tg_send("No agents")
        else:
            lines = ["<b>Agents:</b>"]
            for aid, info in agents.items():
                trust = info.get("trust_level", "team")
                status = info.get("status", "?")
                lines.append(f"  <code>{aid}</code>: {trust} ({status})")
            _tg_send("\n".join(lines))

    elif cmd == "/help":
        _tg_send(
            "<b>Gateway Commands:</b>\n"
            "/approve &lt;id&gt; — Approve request\n"
            "/deny &lt;id&gt; — Deny request\n"
            "/flag &lt;agent&gt; — Block agent\n"
            "/unblock &lt;agent&gt; — Restore to team\n"
            "/pending — List pending\n"
            "/agents — List agents"
        )


# ── CC Relay ──────────────────────────────────────────────────────

def _sign_for_cc(body: bytes) -> dict:
    """Create HMAC auth headers for CC MCP server."""
    if not CC_HMAC_SECRET:
        return {}
    ts = str(int(time.time()))
    msg = f"{ts}.".encode() + body
    sig = hmac_mod.new(CC_HMAC_SECRET.encode(), msg, hashlib.sha256).hexdigest()
    return {
        "X-CC-Agent-ID": "oc",
        "X-CC-Timestamp": ts,
        "X-CC-Signature": sig,
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }


def _cc_mcp_request(rpc_body: dict, session_id: str = "") -> tuple[dict, str]:
    """Send a single JSON-RPC request to CC MCP. Returns (response, session_id)."""
    body = json.dumps(rpc_body).encode()
    headers = _sign_for_cc(body)
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    req = Request(CC_MCP_URL, data=body, headers=headers)
    resp = urlopen(req, timeout=30)
    sid = resp.headers.get("Mcp-Session-Id", session_id)
    resp_body = resp.read()
    if resp_body:
        return json.loads(resp_body), sid
    return {}, sid


def relay_to_cc(task_data: dict) -> dict:
    """Forward a task to CC's MCP server (full protocol handshake)."""
    if not CC_MCP_URL:
        return {"status": "error", "message": "CC_MCP_URL not configured"}

    try:
        # Step 1: Initialize session
        init_resp, session_id = _cc_mcp_request({
            "jsonrpc": "2.0",
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "cortex-gateway", "version": "1.0"},
            },
            "id": 1,
        })

        # Step 2: Send initialized notification
        _cc_mcp_request(
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            session_id=session_id,
        )

        # Step 3: Call submit_task
        result, _ = _cc_mcp_request({
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {
                "name": "submit_task",
                "arguments": {
                    "task_id": task_data.get("request_id", ""),
                    "task_type": task_data.get("request_type", "research"),
                    "title": task_data.get("title", ""),
                    "context": task_data.get("content", ""),
                    "priority": task_data.get("priority", "normal"),
                },
            },
            "id": 2,
        }, session_id=session_id)

        return {"status": "relayed", "cc_response": result}

    except Exception as e:
        return {"status": "cc_offline", "error": str(e)}


# ── Context Variable (pass agent_id from middleware to tools) ─────

_current_agent_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "agent_id", default="unknown"
)


# ── Auth Helpers ──────────────────────────────────────────────────

_request_log: dict[str, list[float]] = defaultdict(list)


def _verify_hmac(timestamp_str: str, body: bytes, signature_hex: str,
                 secret: str = "") -> bool:
    if not secret:
        return False
    try:
        ts = int(timestamp_str)
    except (ValueError, TypeError):
        return False
    if abs(int(time.time()) - ts) > HMAC_WINDOW_SEC:
        return False
    msg = f"{timestamp_str}.".encode() + body
    expected = hmac_mod.new(secret.encode(), msg, hashlib.sha256).hexdigest()
    return hmac_mod.compare_digest(expected, signature_hex)


def _check_rate_limit(identifier: str, limit: int = 0) -> bool:
    max_req = limit or DEFAULT_RATE_LIMIT
    now = time.time()
    _request_log[identifier] = [t for t in _request_log[identifier] if now - t < RATE_WINDOW_SEC]
    if len(_request_log[identifier]) >= max_req:
        return False
    _request_log[identifier].append(now)
    # Prune stale keys (prevent unbounded memory growth)
    if len(_request_log) > 1000:
        stale = [k for k, v in _request_log.items() if not v or now - v[-1] > RATE_WINDOW_SEC * 10]
        for k in stale:
            del _request_log[k]
    return True


# ── MCP Server ────────────────────────────────────────────────────

from mcp.server.fastmcp import FastMCP


mcp = FastMCP(
    "Cortex Gateway",
    host="127.0.0.1",
    port=8750,
    # DNS rebinding protection removed — redundant with 127.0.0.1 bind + HMAC auth
    instructions=(
        "Cortex Gateway — Agent Mesh Hub. "
        "Use submit_request to send tasks. "
        "Use check_status to check your request. "
        "Use ping for health check."
    ),
)


@mcp.tool()
def ping() -> str:
    """Health check — verify gateway is alive."""
    cc_status = "configured" if CC_MCP_URL else "not_configured"
    return json.dumps({
        "status": "ok",
        "server": "Cortex Gateway",
        "cc_relay": cc_status,
        "pending_approvals": len(_state["pending_approvals"]),
        "relay_queue": len(_state["relay_queue"]),
        "timestamp": _now(),
    })


@mcp.tool()
def submit_request(
    request_id: str,
    request_type: str,
    title: str,
    content: str,
    priority: str = "normal",
) -> str:
    """Submit a request to the Cortex network.

    Requests go through sensitivity scanning. Sensitive requests
    require CEO approval via Telegram before processing.

    Args:
        request_id: Unique ID (e.g., "req-20260303-001")
        request_type: "research", "file_request", "action", "collaboration"
        title: Short description
        content: Full request details
        priority: "normal" or "urgent"

    Returns:
        JSON with status: "accepted", "pending_approval", or "blocked"
    """
    agent_id = _current_agent_id.get()

    # Input validation (H6)
    if not REQUEST_ID_PATTERN.match(request_id):
        return json.dumps({"status": "error", "message": "Invalid request_id format (alphanumeric, -, _, max 128 chars)"})
    if request_type not in VALID_REQUEST_TYPES:
        return json.dumps({"status": "error", "message": f"Invalid request_type. Valid: {', '.join(sorted(VALID_REQUEST_TYPES))}"})
    if priority not in VALID_PRIORITIES:
        return json.dumps({"status": "error", "message": f"Invalid priority. Valid: {', '.join(sorted(VALID_PRIORITIES))}"})
    if len(title) > 500:
        return json.dumps({"status": "error", "message": "Title too long (max 500 chars)"})
    if len(content) > 50_000:
        return json.dumps({"status": "error", "message": "Content too long (max 50000 chars)"})

    # Duplicate check
    if request_id in _state.get("requests", {}):
        return json.dumps({"status": "error", "message": "Duplicate request_id"})

    # Trust + sensitivity check
    action, trigger = check_trust(agent_id, content, request_type)

    request_data = {
        "request_id": request_id,
        "request_type": request_type,
        "title": title,
        "content": content,
        "priority": priority,
        "agent_id": agent_id,
        "submitted_at": _now(),
    }

    _audit("request_submitted", request_id=request_id, agent_id=agent_id,
           request_type=request_type, action=action, trigger=trigger)

    if action == "block":
        with _state_lock:
            _state["requests"][request_id] = {
                "status": "blocked", "trigger": trigger, "agent_id": agent_id,
            }
            _prune_state()
            _save_state()
        _tg_send(
            f"[BLOCKED]\n"
            f"Agent: {_tg_escape(agent_id)}\n"
            f"Trigger: {_tg_escape(trigger)}\n"
            f"Content: {_tg_escape(content[:200])}"
        )
        return json.dumps({"status": "blocked", "reason": "Content blocked by policy"})

    if action == "hold":
        with _state_lock:
            approval_id = str(_state["next_id"])
            _state["next_id"] += 1
            _state["pending_approvals"][approval_id] = {
                "request_id": request_id,
                "request_data": request_data,
                "agent_id": agent_id,
                "trigger": trigger,
                "created_at": _now(),
            }
            _state["requests"][request_id] = {
                "status": "pending_approval",
                "approval_id": approval_id,
                "agent_id": agent_id,
            }
            _prune_state()
            _save_state()

        _tg_send(
            f"[APPROVAL #{approval_id}]\n"
            f"From: {_tg_escape(agent_id)}\n"
            f"Type: {_tg_escape(request_type)}\n"
            f"Title: {_tg_escape(title)}\n"
            f"Content: {_tg_escape(content[:300])}\n"
            f"Trigger: \"{_tg_escape(trigger)}\"\n\n"
            f"/approve {approval_id} or /deny {approval_id}"
        )
        return json.dumps({
            "status": "pending_approval",
            "approval_id": approval_id,
            "message": "Request held for CEO approval.",
        })

    # action == "allow" — auto-approve, add to relay queue
    with _state_lock:
        _state["relay_queue"].append(request_data)
        _state["requests"][request_id] = {"status": "approved", "agent_id": agent_id}
        _prune_state()
        _save_state()

    return json.dumps({
        "status": "accepted",
        "message": "Request approved, relaying to CC.",
    })


@mcp.tool()
def check_status(request_id: str) -> str:
    """Check status of a submitted request.

    Args:
        request_id: The request ID to check.

    Returns:
        JSON with request status and details.
    """
    agent_id = _current_agent_id.get()
    info = _state["requests"].get(request_id)

    if not info:
        return json.dumps({"status": "not_found", "request_id": request_id})

    # Agents can only see their own requests (owner sees all)
    agent = _get_agent(agent_id)
    trust = agent.get("trust_level", "team") if agent else "team"
    if trust != "owner" and info.get("agent_id") != agent_id:
        return json.dumps({"status": "not_found", "request_id": request_id})

    return json.dumps({"request_id": request_id, **info})


# ── Background Workers ────────────────────────────────────────────

def _tg_poller():
    """Background thread: poll TG for /commands."""
    _log("TG poller started")
    consecutive_errors = 0
    while True:
        try:
            messages, new_offset = _tg_get_updates(_state.get("tg_offset", 0))
            if new_offset > _state.get("tg_offset", 0):
                with _state_lock:
                    _state["tg_offset"] = new_offset
                    _save_state()

            for msg in messages:
                if msg.get("is_bot"):
                    continue  # ignore bot messages
                text = msg.get("text", "")
                if text.startswith("/"):
                    _handle_tg_command(text, msg.get("from_name", "?"), msg.get("chat_id", 0), msg.get("from_id", 0))
            consecutive_errors = 0
        except Exception as e:
            consecutive_errors += 1
            _log(f"TG poller error ({consecutive_errors}): {e}")
            if consecutive_errors >= 10:
                _log("TG poller: 10 consecutive errors, backing off 60s")
                time.sleep(60)
                consecutive_errors = 0
                continue

        time.sleep(3)


def _relay_worker():
    """Background thread: drain relay queue to CC MCP."""
    _log("Relay worker started")
    while True:
        time.sleep(10)

        with _state_lock:
            queue = list(_state["relay_queue"])

        if not queue:
            continue

        for task in queue:
            req_id = task.get("request_id", "?")
            _log(f"Relaying {req_id} to CC...")
            result = relay_to_cc(task)

            if result.get("status") == "relayed":
                with _state_lock:
                    # Remove from queue by matching request_id
                    _state["relay_queue"] = [
                        t for t in _state["relay_queue"]
                        if t.get("request_id") != req_id
                    ]
                    _state["requests"][req_id]["status"] = "relayed"
                    _state["requests"][req_id]["relayed_at"] = _now()
                    _save_state()
                _log(f"Relayed {req_id} to CC")
                _audit("relayed", request_id=req_id)
            else:
                # CC offline — stop processing, retry later
                _log(f"CC relay failed for {req_id}: {result.get('error', '?')}")
                break


# ── Entry Point ───────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    from starlette.responses import JSONResponse

    port = 8750
    if "--port" in sys.argv:
        idx = sys.argv.index("--port")
        port = int(sys.argv[idx + 1])

    registry = _load_registry()
    agent_count = len(registry.get("agents", {}))
    _log(f"Starting Cortex Gateway on :{port}")
    _log(f"Agents: {agent_count} | CC: {'configured' if CC_MCP_URL else 'NOT configured'}")
    _log(f"TG: {'configured' if TG_BOT_TOKEN else 'NOT configured'} | CEO user_id: {'set' if TG_CEO_USER_ID else 'NOT set (any group member can command)'}")
    _log(f"Sensitivity rules: {len(RULES.get('block_keywords', []))} block, "
         f"{len(RULES.get('hold_keywords', []))} hold")

    app = mcp.streamable_http_app()
    original_app = app.middleware_stack or app.build_middleware_stack()

    async def _read_body(receive):
        parts = []
        while True:
            message = await receive()
            body = message.get("body", b"")
            if body:
                parts.append(body)
            if not message.get("more_body", False):
                break
        return b"".join(parts)

    def _make_body_receive(body: bytes, original_receive):
        sent = False

        async def body_receive():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await original_receive()
        return body_receive

    async def auth_middleware(scope, receive, send):
        if scope["type"] != "http":
            await original_app(scope, receive, send)
            return

        headers = dict(
            (k.decode("latin-1").lower(), v.decode("latin-1"))
            for k, v in scope.get("headers", [])
        )
        client_ip = scope.get("client", ("unknown", 0))[0]

        # H2: Check Content-Length before reading body
        content_length = headers.get("content-length", "0")
        try:
            cl = int(content_length)
        except ValueError:
            cl = 0
        if cl > MAX_BODY_BYTES:
            resp = JSONResponse({"error": "Request too large"}, status_code=413)
            await resp(scope, receive, send)
            return

        body = await _read_body(receive)
        if len(body) > MAX_BODY_BYTES:
            resp = JSONResponse({"error": "Request too large"}, status_code=413)
            await resp(scope, receive, send)
            return

        replay = _make_body_receive(body, receive)

        agent_id = headers.get("x-cc-agent-id", "")
        hmac_sig = headers.get("x-cc-signature", "")
        hmac_ts = headers.get("x-cc-timestamp", "")

        if not (agent_id and hmac_sig and hmac_ts):
            _audit("auth_fail", ip=client_ip, reason="missing_credentials")
            resp = JSONResponse({"error": "Unauthorized"}, status_code=401)
            await resp(scope, receive, send)
            return

        agent = _get_agent(agent_id)
        if not agent:
            _audit("auth_fail", ip=client_ip, agent_id=agent_id, reason="unknown_agent")
            resp = JSONResponse({"error": "Unauthorized"}, status_code=401)
            await resp(scope, receive, send)
            return

        if agent.get("status") == "revoked":
            _audit("auth_fail", ip=client_ip, agent_id=agent_id, reason="revoked")
            resp = JSONResponse({"error": "Unauthorized"}, status_code=403)
            await resp(scope, receive, send)
            return

        if agent.get("trust_level") == "blocked":
            _audit("auth_fail", ip=client_ip, agent_id=agent_id, reason="blocked")
            resp = JSONResponse({"error": "Unauthorized"}, status_code=403)
            await resp(scope, receive, send)
            return

        agent_secret = _get_agent_secret(agent)
        if not _verify_hmac(hmac_ts, body, hmac_sig, secret=agent_secret):
            _audit("auth_fail", ip=client_ip, agent_id=agent_id, reason="invalid_hmac")
            resp = JSONResponse({"error": "Unauthorized"}, status_code=401)
            await resp(scope, receive, send)
            return

        # H4: Replay attack prevention — reject reused signatures
        if _check_replay(hmac_sig):
            _audit("auth_fail", ip=client_ip, agent_id=agent_id, reason="replay_attack")
            resp = JSONResponse({"error": "Unauthorized"}, status_code=401)
            await resp(scope, receive, send)
            return

        agent_limit = agent.get("rate_limit", DEFAULT_RATE_LIMIT)
        if not _check_rate_limit(f"agent:{agent_id}", limit=agent_limit):
            resp = JSONResponse({"error": "Rate limit exceeded"}, status_code=429)
            await resp(scope, receive, send)
            return

        # Set agent_id for tool functions
        _current_agent_id.set(agent_id)
        _audit("auth_ok", ip=client_ip, agent_id=agent_id)
        await original_app(scope, replay, send)

    # Start background workers
    threading.Thread(target=_tg_poller, daemon=True).start()
    threading.Thread(target=_relay_worker, daemon=True).start()

    uvicorn.run(auth_middleware, host="127.0.0.1", port=port)
