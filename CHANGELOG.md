# Changelog

## [7.1.0] — 2026-03-11

### Added
- **[D53] Task lifecycle manager** — Gateway auto-detects CC offline (pending > 2min) and CC timeout (processing > 60min). TG alerts CEO automatically. OC just reads `check_status`.
- **`check_status` message field** — returns human-readable status explanation (`cc_offline`, `cc_timeout`, `cc_done`, `relayed`).
- **`update.sh`** — one-command update: pull + validate + restart + test.

### Changed
- `get_results` now returns `submitted_at`, `claimed_at` for pending/processing tasks.
- Protocol version: v7.1

### Migration
- No env changes. No tool schema changes. Just `./update.sh`.

---

## [7.0.0] — 2026-03-11

### Added
- **[D50] Cortex Worker migration** — all CRUD moved to Cloudflare Worker at `cortex.mkyang.ai`. Permanent URL, zero downtime.
- **[D51] Tunnel deprecated** — no more `tunnel-manager.sh`, DNS TXT sync, or `/update-tunnel-url`.
- **[D52] D1 + KV backend** — Board, Channels, Tasks, Agents in D1; rate limits + replay detection in KV.
- 13 MCP tools: Board (4), Channels (5), Tasks (3), ping (1).
- HMAC-SHA256 auth with Web Crypto API + replay detection (KV TTL 65s).
- Content firewall: NFKC + Cyrillic confusables + zero-width strip + SSRF block.

### Changed
- `CC_MCP_URL` default: `https://cortex.mkyang.ai/mcp` (was tunnel URL).
- Gateway DNS sync disabled — Worker URL is permanent.
- `/update-tunnel-url` returns deprecation notice.

### Removed
- Tunnel dependency.
- DNS TXT encrypted sync.

### Migration
- Update `.env`: remove `CC_TUNNEL_SECRET` (optional, ignored if present).
- `CC_MCP_URL` defaults to Worker — no change needed unless overridden.

---

## [6.0.0] — 2026-03-09

### Added
- Gateway relay architecture — OC no longer connects directly to CC.
- Encrypted DNS TXT tunnel URL sync.
- Sensitivity rules: block/hold keywords, auto-allow types.
- HMAC-SHA256 auth (replaced Bearer token).
- Auto-ban + alert escalation.
- TG commands: /approve, /deny, /flag, /unblock, /pending, /agents.

### Notes
- First stable OC↔CC communication via Gateway.
