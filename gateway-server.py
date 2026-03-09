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
import secrets
import sys
import tempfile
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.error import URLError
from urllib.parse import parse_qs
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
CC_MCP_URL = os.environ.get("CC_MCP_URL", "")  # CC's Cloudflare Tunnel endpoint (dynamically updatable via /update-tunnel-url)
CC_TUNNEL_SECRET = os.environ.get("CC_TUNNEL_SECRET", "")  # Auth for /update-tunnel-url endpoint
CC_HMAC_SECRET = os.environ.get("CORTEX_HMAC_SECRET_OC", "")  # Gateway → CC auth
GW_CALLBACK_SECRET = os.environ.get("GW_CALLBACK_SECRET", "")  # CC → Gateway callback auth
GW_CALLBACK_URL = os.environ.get("GW_CALLBACK_URL", "")  # Gateway's own callback URL (e.g., http://localhost:8750/callback)
GW_PUBLIC_URL = os.environ.get("GW_PUBLIC_URL", "")  # Public-facing URL for onboard links (e.g., https://gateway.example.com)
HMAC_WINDOW_SEC = 60  # 60s replay window (H4: tightened from 300s)
DEFAULT_RATE_LIMIT = 10
RATE_WINDOW_SEC = 60
MAX_BODY_BYTES = 1_048_576  # 1MB max request body (H2)
MAX_STATE_REQUESTS = 10_000  # prune old entries beyond this (H3)
VALID_REQUEST_TYPES = {"research", "file_request", "action", "collaboration"}
VALID_PRIORITIES = {"normal", "urgent"}
REQUEST_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_\-]{1,128}$")  # H6: strict format
ONBOARD_RATE_LIMIT = 5  # max onboard attempts per minute
INVITE_DEFAULT_HOURS = 24

# ── Auto-ban system (ported from cortex-mcp-server.py) ───────────
AUTH_FAIL_WINDOW = 600  # 10 minutes
AUTH_FAIL_THRESHOLD = 10  # failures before ban
AUTO_BAN_DURATION = 3600  # 1 hour ban
_auth_failures: dict[str, list[float]] = defaultdict(list)
_auto_bans: dict[str, float] = {}  # ip -> ban_expiry_timestamp
VALID_ROLES = {"full", "research", "readonly"}
VALID_TRUST_LEVELS = {"owner", "team", "restricted", "blocked"}
ROLE_TOOLS = {
    "full": ["submit_request", "check_status", "ping"],
    "research": ["submit_request", "check_status", "ping"],
    "readonly": ["check_status", "ping"],
}


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
    "invites": {},            # token -> {agent_id, role, trust_level, expires_at, status, ...}
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
        # T3 fix: owner still runs block_keywords scan (defense-in-depth).
        # A compromised owner identity or supply chain attack should not
        # bypass critical keyword blocking (e.g., secret exfiltration).
        action, trigger = scan_sensitivity(content, request_type)
        if action == "block":
            _log(f"SECURITY: Owner request blocked by keyword: {trigger}")
            _audit("owner_blocked", agent_id=agent_id, trigger=trigger)
            return "block", trigger
        return "allow", ""  # owner skips hold (auto-approve) but not block
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
    # T3 fix: fail-closed — if CEO user_id not configured, deny all commands
    if not TG_CEO_USER_ID:
        _log(f"SECURITY: TG command rejected — CORTEX_TG_CEO_USER_ID not configured (fail-closed)")
        _tg_send("Gateway commands disabled: CEO user_id not configured.")
        return
    # Per-user auth: only CEO can run commands (not just any group member)
    if str(from_id) != str(TG_CEO_USER_ID):
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

    elif cmd == "/invite" and arg:
        # Parse optional flags: --role, --trust, --expires
        role, trust, expires_h = "research", "team", INVITE_DEFAULT_HOURS
        i = 2
        while i < len(parts):
            if parts[i] == "--role" and i + 1 < len(parts):
                role = parts[i + 1]
                i += 2
            elif parts[i] == "--trust" and i + 1 < len(parts):
                trust = parts[i + 1]
                i += 2
            elif parts[i] == "--expires" and i + 1 < len(parts):
                try:
                    expires_h = int(parts[i + 1])
                except ValueError:
                    pass
                i += 2
            else:
                i += 1

        if role not in VALID_ROLES:
            _tg_send(f"Invalid role '{role}'. Valid: {', '.join(sorted(VALID_ROLES))}")
            return
        if trust not in VALID_TRUST_LEVELS or trust == "blocked":
            _tg_send(f"Invalid trust '{trust}'. Valid: owner, team, restricted")
            return

        # Check if agent already exists
        registry = _load_registry()
        if arg in registry.get("agents", {}):
            existing = registry["agents"][arg]
            if existing.get("status") != "revoked":
                _tg_send(f"Agent '{_tg_escape(arg)}' already registered. Revoke first or use a different ID.")
                return

        token = _create_invite(arg, role=role, trust=trust, expires_hours=expires_h, created_by=from_name)
        base_url = GW_PUBLIC_URL.rstrip("/") if GW_PUBLIC_URL else "http://&lt;gateway&gt;"
        onboard_url = f"{base_url}/onboard?token={token}"

        _tg_send(
            f"<b>[INVITE]</b> {_tg_escape(arg)}\n"
            f"Role: {role} | Trust: {trust}\n"
            f"Expires: {expires_h}h\n\n"
            f"Onboard URL:\n<code>{onboard_url}</code>\n\n"
            f"Send to the agent operator. Agent visits URL → auto-connected."
        )
        _audit("invite_sent", agent_id=arg, role=role, trust=trust, by=from_name)

    elif cmd == "/invites":
        invites = _state.get("invites", {})
        pending = {k: v for k, v in invites.items() if v.get("status") == "pending"}
        if not pending:
            _tg_send("No pending invites")
        else:
            lines = ["<b>Pending Invites:</b>"]
            for token, info in pending.items():
                expires = info.get("expires_at", "?")[:16]
                lines.append(
                    f"\n{_tg_escape(info.get('agent_id', '?'))}"
                    f" | {info.get('role', '?')} | {info.get('trust_level', '?')}"
                    f"\nExpires: {expires}"
                    f"\nToken: <code>{token[:12]}...</code>"
                )
            _tg_send("\n".join(lines))

    elif cmd == "/revoke-invite" and arg:
        # Match by token prefix
        invites = _state.get("invites", {})
        matched = [k for k in invites if k.startswith(arg) and invites[k].get("status") == "pending"]
        if not matched:
            _tg_send(f"No pending invite matching '{_tg_escape(arg)}'")
        else:
            for k in matched:
                invites[k]["status"] = "revoked"
            with _state_lock:
                _save_state()
            _tg_send(f"Revoked {len(matched)} invite(s)")
            _audit("invite_revoked", prefix=arg, count=len(matched), by=from_name)

    elif cmd == "/help":
        _tg_send(
            "<b>Gateway Commands:</b>\n"
            "/approve &lt;id&gt; — Approve request\n"
            "/deny &lt;id&gt; — Deny request\n"
            "/flag &lt;agent&gt; — Block agent\n"
            "/unblock &lt;agent&gt; — Restore to team\n"
            "/pending — List pending\n"
            "/agents — List agents\n"
            "/invite &lt;id&gt; [--role R] [--trust T] — Create invite\n"
            "/invites — List pending invites\n"
            "/revoke-invite &lt;prefix&gt; — Revoke invite"
        )


# ── CC Callback Handler ───────────────────────────────────────────

def _handle_cc_callback(body: bytes, client_ip: str) -> tuple[dict, int]:
    """Handle callback POST from CC with task results.

    Returns (response_dict, status_code).
    CC posts: {task_id, status, result, completed_at, assign_oc?}
    """
    # Auth: verify shared secret in X-Callback-Secret header
    # (simplified auth — CC and Gateway share GW_CALLBACK_SECRET)
    # Full HMAC not needed here because this endpoint is not exposed externally.

    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return {"error": "Invalid JSON"}, 400

    task_id = payload.get("task_id", "")
    status = payload.get("status", "unknown")
    result = payload.get("result", {})
    assign_oc = payload.get("assign_oc")

    if not task_id:
        return {"error": "Missing task_id"}, 400

    _log(f"Callback received: task={task_id} status={status}")
    _audit("callback_received", task_id=task_id, status=status, ip=client_ip)

    # Update request state if we have it
    with _state_lock:
        # Find the request_id that maps to this task_id
        for req_id, req_info in _state.get("requests", {}).items():
            if req_id == task_id or req_info.get("task_id") == task_id:
                req_info["status"] = f"cc_{status}"
                req_info["cc_result"] = result
                req_info["completed_at"] = _now()
                _save_state()
                break

    # Send TG [RESULT] to CEO — Gateway is the single point of contact
    summary = ""
    if isinstance(result, dict):
        summary = result.get("summary", result.get("message", str(result)[:300]))
    elif isinstance(result, str):
        summary = result[:300]
    else:
        summary = str(result)[:300]

    tg_msg = (
        f"<b>[RESULT] {task_id}</b>\n"
        f"Status: {status}\n"
        f"{summary}"
    )
    _tg_send(tg_msg)

    # Handle assign_oc handoff if present
    if assign_oc and isinstance(assign_oc, dict):
        oc_type = assign_oc.get("type", "unknown")
        oc_title = assign_oc.get("title", "")
        _log(f"CC requests OC handoff: type={oc_type} title={oc_title}")
        _tg_send(
            f"<b>[ASSIGN-OC]</b> {oc_title}\n"
            f"Type: {oc_type}\n"
            f"From task: {task_id}"
        )
        # OC picks this up from TG and handles it

    return {"status": "ok", "task_id": task_id}, 200


# ── Agent Onboarding ─────────────────────────────────────────────

def _create_invite(agent_id: str, role: str = "research", trust: str = "team",
                   expires_hours: int = INVITE_DEFAULT_HOURS, created_by: str = "CEO") -> str:
    """Create an invite token for a new agent. Returns the token."""
    token = "inv_" + secrets.token_hex(32)
    now = datetime.now(timezone.utc)
    with _state_lock:
        if "invites" not in _state:
            _state["invites"] = {}
        _state["invites"][token] = {
            "agent_id": agent_id,
            "role": role,
            "trust_level": trust,
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(hours=expires_hours)).isoformat(),
            "created_by": created_by,
            "status": "pending",
            "used_at": None,
            "used_from_ip": None,
        }
        _save_state()
    _audit("invite_created", agent_id=agent_id, role=role, trust=trust, expires_hours=expires_hours)
    return token


def _handle_onboard(query_string: str, client_ip: str) -> tuple[dict, int]:
    """Handle GET /onboard?token=inv_xxx — provision a new agent."""
    params = parse_qs(query_string)
    token = params.get("token", [""])[0]

    if not token:
        return {"error": "Missing token parameter"}, 400

    with _state_lock:
        invites = _state.get("invites", {})
        invite = invites.get(token)

        if not invite:
            return {"error": "Invalid or expired token"}, 404

        # Check expiry
        expires_at = datetime.fromisoformat(invite["expires_at"])
        if datetime.now(timezone.utc) > expires_at:
            invite["status"] = "expired"
            _save_state()
            return {"error": "Token expired"}, 404

        # Check already used
        if invite["status"] == "used":
            return {"error": "Token already used"}, 410

        agent_id = invite["agent_id"]

        # Check if agent_id already exists in registry
        registry = _load_registry()
        if agent_id in registry.get("agents", {}):
            existing = registry["agents"][agent_id]
            if existing.get("status") != "revoked":
                return {"error": f"Agent '{agent_id}' already registered"}, 409

        # Generate HMAC secret
        hmac_secret = secrets.token_hex(32)
        env_key = f"GW_HMAC_{agent_id.upper().replace('-', '_')}"

        # Write to registry
        with _registry_lock:
            registry = _load_registry()
            registry["agents"][agent_id] = {
                "name": agent_id,
                "owner": invite.get("created_by", "CEO"),
                "role": invite["role"],
                "trust_level": invite["trust_level"],
                "hmac_secret_env": env_key,
                "rate_limit": DEFAULT_RATE_LIMIT,
                "status": "active",
                "created_at": _now(),
                "onboarded_from_ip": client_ip,
            }
            _save_registry(registry)

        # Set env var in running process (hot reload — no restart needed)
        os.environ[env_key] = hmac_secret

        # Also append to .env file for persistence across restarts
        env_file = SCRIPT_DIR / ".env"
        try:
            with open(env_file, "a") as f:
                f.write(f"{env_key}={hmac_secret}\n")
        except Exception as e:
            _log(f"WARNING: Failed to write {env_key} to .env: {e}")

        # Mark invite as used
        invite["status"] = "used"
        invite["used_at"] = _now()
        invite["used_from_ip"] = client_ip
        _save_state()

    _audit("agent_onboarded", agent_id=agent_id, ip=client_ip, role=invite["role"], trust=invite["trust_level"])
    _log(f"Agent '{agent_id}' onboarded from {client_ip}")

    # TG notification
    _tg_send(
        f"<b>[ONBOARD]</b> {_tg_escape(agent_id)}\n"
        f"Role: {_tg_escape(invite['role'])} | Trust: {_tg_escape(invite['trust_level'])}\n"
        f"IP: {_tg_escape(client_ip)}"
    )

    # Build gateway URL for the response
    gateway_url = GW_PUBLIC_URL.rstrip("/") + "/mcp" if GW_PUBLIC_URL else "http://<gateway-host>:8750/mcp"

    return {
        "status": "ok",
        "agent_id": agent_id,
        "hmac_secret": hmac_secret,
        "gateway_url": gateway_url,
        "trust_level": invite["trust_level"],
        "role": invite["role"],
        "instructions": (
            "Use HMAC-SHA256 to sign every request. "
            "Headers: X-CC-Agent-ID (your agent_id), "
            "X-CC-Timestamp (unix seconds), "
            "X-CC-Signature (HMAC-SHA256 of '{timestamp}.{body}' using hmac_secret)"
        ),
    }, 200


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

        # Step 3: Call submit_task with callback_url so CC posts results back
        submit_args = {
            "task_id": task_data.get("request_id", ""),
            "task_type": task_data.get("request_type", "research"),
            "title": task_data.get("title", ""),
            "context": task_data.get("content", ""),
            "priority": task_data.get("priority", "normal"),
        }
        if GW_CALLBACK_URL:
            submit_args["callback_url"] = GW_CALLBACK_URL

        result, _ = _cc_mcp_request({
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {
                "name": "submit_task",
                "arguments": submit_args,
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


def _check_auto_ban(client_ip: str) -> bool:
    """Return True if IP is currently auto-banned."""
    expiry = _auto_bans.get(client_ip)
    if expiry and time.time() < expiry:
        return True
    if expiry:
        del _auto_bans[client_ip]  # Ban expired
    return False


def _record_auth_failure(client_ip: str, context: str = ""):
    """Record an auth failure. Auto-bans after threshold."""
    now = time.time()
    _auth_failures[client_ip] = [t for t in _auth_failures[client_ip] if now - t < AUTH_FAIL_WINDOW]
    _auth_failures[client_ip].append(now)
    count = len(_auth_failures[client_ip])

    if count >= AUTH_FAIL_THRESHOLD:
        _auto_bans[client_ip] = now + AUTO_BAN_DURATION
        _log(f"AUTO-BAN: {client_ip} banned for {AUTO_BAN_DURATION}s ({count} failures in {AUTH_FAIL_WINDOW}s) [{context}]")
        _audit("auto_ban", ip=client_ip, failures=count, context=context)
        _tg_send(f"<b>[AUTO-BAN]</b> {_tg_escape(client_ip)}\n{count} auth failures → banned {AUTO_BAN_DURATION}s")
    elif count >= AUTH_FAIL_THRESHOLD // 2:
        _log(f"AUTH-WARN: {client_ip} at {count}/{AUTH_FAIL_THRESHOLD} failures [{context}]")


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

        path = scope.get("path", "")
        headers = dict(
            (k.decode("latin-1").lower(), v.decode("latin-1"))
            for k, v in scope.get("headers", [])
        )
        client_ip = scope.get("client", ("unknown", 0))[0]

        # Auto-ban check — applies to all endpoints
        if _check_auto_ban(client_ip):
            _log(f"AUTO-BAN: Rejected request from banned IP {client_ip}")
            resp = JSONResponse({"error": "Temporarily banned"}, status_code=403)
            await resp(scope, receive, send)
            return

        # ── /callback endpoint — CC posts task results here ──
        if path == "/callback" and scope.get("method", "GET") == "POST":
            body = await _read_body(receive)
            # Auth: verify shared secret (T3 fix: fail-closed + timing-safe)
            cb_secret = headers.get("x-callback-secret", "")
            if not GW_CALLBACK_SECRET:
                _log(f"SECURITY: Callback rejected — GW_CALLBACK_SECRET not configured (fail-closed)")
                resp = JSONResponse({"error": "Server misconfigured"}, status_code=500)
                await resp(scope, receive, send)
                return
            if not hmac_mod.compare_digest(cb_secret, GW_CALLBACK_SECRET):
                _log(f"Callback auth failed from {client_ip}")
                _record_auth_failure(client_ip, "callback")
                resp = JSONResponse({"error": "Unauthorized"}, status_code=401)
                await resp(scope, receive, send)
                return
            result, status_code = _handle_cc_callback(body, client_ip)
            resp = JSONResponse(result, status_code=status_code)
            await resp(scope, receive, send)
            return

        # ── /update-tunnel-url endpoint — CC pushes new tunnel URL here ──
        if path == "/update-tunnel-url" and scope.get("method", "GET") == "POST":
            global CC_MCP_URL
            body = await _read_body(receive)
            # Auth: shared secret (same pattern as callback)
            tunnel_secret = headers.get("x-tunnel-secret", "")
            if not CC_TUNNEL_SECRET:
                _log(f"SECURITY: /update-tunnel-url rejected — CC_TUNNEL_SECRET not configured")
                resp = JSONResponse({"error": "Server misconfigured"}, status_code=500)
                await resp(scope, receive, send)
                return
            if not hmac_mod.compare_digest(tunnel_secret, CC_TUNNEL_SECRET):
                _log(f"/update-tunnel-url auth failed from {client_ip}")
                _record_auth_failure(client_ip, "tunnel_url_update")
                resp = JSONResponse({"error": "Unauthorized"}, status_code=401)
                await resp(scope, receive, send)
                return
            try:
                data = json.loads(body)
                new_url = data.get("url", "").strip()
                if not new_url or not new_url.startswith("https://"):
                    resp = JSONResponse({"error": "Invalid URL (must be HTTPS)"}, status_code=400)
                    await resp(scope, receive, send)
                    return
                old_url = CC_MCP_URL
                CC_MCP_URL = new_url
                _log(f"CC_MCP_URL updated: {old_url} → {new_url}")
                _audit("tunnel_url_updated", old_url=old_url, new_url=new_url, ip=client_ip)
                resp = JSONResponse({"status": "ok", "url": new_url})
                await resp(scope, receive, send)
            except Exception as e:
                resp = JSONResponse({"error": str(e)}, status_code=400)
                await resp(scope, receive, send)
            return

        # ── /cc-url endpoint — OC sessions query current CC tunnel URL ──
        if path == "/cc-url" and scope.get("method", "GET") == "GET":
            resp = JSONResponse({
                "url": CC_MCP_URL,
                "status": "configured" if CC_MCP_URL else "not_configured",
            })
            await resp(scope, receive, send)
            return

        # ── /onboard endpoint — self-service agent provisioning ──
        if path == "/onboard" and scope.get("method", "GET") == "GET":
            # Rate limit onboard attempts
            if not _check_rate_limit(f"onboard:{client_ip}", limit=ONBOARD_RATE_LIMIT):
                resp = JSONResponse({"error": "Rate limit exceeded"}, status_code=429)
                await resp(scope, receive, send)
                return
            query_string = scope.get("query_string", b"").decode("latin-1")
            result, status_code = _handle_onboard(query_string, client_ip)
            if status_code in (401, 403, 404):
                _record_auth_failure(client_ip, "onboard")
            resp = JSONResponse(result, status_code=status_code)
            await resp(scope, receive, send)
            return

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
            _record_auth_failure(client_ip, "mcp_missing_creds")
            resp = JSONResponse({"error": "Unauthorized"}, status_code=401)
            await resp(scope, receive, send)
            return

        agent = _get_agent(agent_id)
        if not agent:
            _audit("auth_fail", ip=client_ip, agent_id=agent_id, reason="unknown_agent")
            _record_auth_failure(client_ip, f"mcp_unknown_agent:{agent_id}")
            resp = JSONResponse({"error": "Unauthorized"}, status_code=401)
            await resp(scope, receive, send)
            return

        if agent.get("status") == "revoked":
            _audit("auth_fail", ip=client_ip, agent_id=agent_id, reason="revoked")
            _record_auth_failure(client_ip, f"mcp_revoked:{agent_id}")
            resp = JSONResponse({"error": "Unauthorized"}, status_code=403)
            await resp(scope, receive, send)
            return

        if agent.get("trust_level") == "blocked":
            _audit("auth_fail", ip=client_ip, agent_id=agent_id, reason="blocked")
            _record_auth_failure(client_ip, f"mcp_blocked:{agent_id}")
            resp = JSONResponse({"error": "Unauthorized"}, status_code=403)
            await resp(scope, receive, send)
            return

        agent_secret = _get_agent_secret(agent)
        if not _verify_hmac(hmac_ts, body, hmac_sig, secret=agent_secret):
            _audit("auth_fail", ip=client_ip, agent_id=agent_id, reason="invalid_hmac")
            _record_auth_failure(client_ip, f"mcp_bad_hmac:{agent_id}")
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
