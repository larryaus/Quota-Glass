# ChatGPT live quota via the Codex CLI

Date: 2026-07-26
Status: approved design, not yet implemented

## Context

Quota Glass shows Claude quota live (`mode="oauth"`, read from an undocumented
Anthropic endpoint) but derives ChatGPT quota by parsing Codex rollout JSONL
files on disk. Rollout data is only as fresh as the last Codex session: on
2026-07-26 the ChatGPT card read 17% from a snapshot 130 minutes old and was
correctly flagged stale, while the account's true figure was 18%.

The goal is to give ChatGPT a live source so both providers report current
numbers.

### Why not call the HTTP endpoint directly

The Codex binary contains `/api/codex/usage` and typed responses
(`GetAccountRateLimitsResponse`, `RateLimitSnapshot`). Probing every candidate
URL under `chatgpt.com` with a valid token returned **403 with
`cf-mitigated: challenge`, `server: cloudflare`** — a bot challenge, not an auth
failure. The CLI satisfies it with a client attestation token
(`AttestationGenerateParams` in the binary).

**Replicating or bypassing that challenge is out of scope and will not be
built.** It is an access control, and circumventing it is off the table
regardless of account ownership.

### The chosen route

The Codex CLI exposes an app-server with JSON-RPC methods
`account/rateLimits/read` and `account/usage/read`. Delegating to the official
client is legitimate, and it means **Quota Glass never handles tokens at all** —
auth, refresh, and attestation stay inside Codex. This is a strictly better
security posture than the Claude path, which reads Keychain directly.

Verified working against the live account: `usedPercent: 18`,
`windowDurationMins: 10080`, `planType: plus`, plus credits and reset-credit
data. Latency ~1.1s warm; one cold start took 26s, which sets the timeout
budget.

## Architecture

### New: `app/providers/codex_cli.py`

One job: return a rate-limit snapshot from the CLI.

- `read_account_rate_limits(cli_path: str, timeout_seconds: int) -> JsonDict`
  spawns `codex app-server`, performs `initialize` → `initialized` →
  `account/rateLimits/read`, returns the raw `result`, terminates the process.
- Typed failures: `CodexCliUnavailable` (binary missing or logged out),
  `CodexCliTimeout`, `CodexCliProtocolError`.
- Never reads `~/.codex/auth.json`. Never logs response bodies.
- The subprocess is always reaped, including on timeout.

### Changed: shared live-source cache

`ClaudeOAuthCache` (`app/providers/claude.py:306`) is provider-agnostic except
for classifying HTTP 429 / `Retry-After`. Extract it as `LiveSourceCache`
(min-interval, exponential backoff, cached-payload retention, `age_seconds`,
`should_attempt`, `is_backed_off`).

Claude injects its existing 429 classifier, so current behaviour and
`tests/test_claude_oauth_cache.py` are unchanged. Codex uses a plain
failure → backoff policy.

### Changed: `parse_chatgpt`

Gains `enable_live: bool` and a `live_cache`, mirroring `parse_claude`.

1. **Disabled** → today's rollout path unchanged, `mode="local"`.
2. **Enabled, cache due** → CLI call. Success ⇒ `mode="oauth"`,
   `source="app-server"`, `stale=False`.
3. **Failure, prior success cached** → serve the cached payload and populate the
   existing `oauth_backed_off`, `oauth_backoff_reason`,
   `oauth_cache_age_seconds`, `oauth_next_retry_at` fields.
4. **Failure, no prior success** → fall back to rollout parsing,
   `mode="local"`.

`local_usage` and `model_usage` always come from rollout files; the app-server
does not expose per-model token breakdowns, so those cards behave identically in
every mode.

### Field mapping

The app-server returns camelCase; rollout files use snake_case. The parser
handles both.

| app-server | `Meter` / `Credits` |
| --- | --- |
| `rateLimits.primary.usedPercent` | `used_pct` |
| `windowDurationMins` | `window_minutes` |
| `resetsAt` | `resets_at` |
| `credits.hasCredits` / `unlimited` / `balance` | `Credits` |
| `spendControlReached` | `spend_limit_reached` |
| `planType` | `plan_type` |

Windows map to the existing `labels` dict (`app/providers/chatgpt.py:332`),
which is keyed in snake_case. The live reader normalizes camelCase window names
to those keys before lookup, so `individualLimit` resolves to `individual_limit`
and produces meter key `chatgpt.individual_limit` — identical to the rollout
path. Meter keys must not differ between modes, or history rows and alert state
would split across two key namespaces for the same limit.

### Bundled fix

`_latest_snapshot` (`app/providers/chatgpt.py:100`) caps by *files examined*
rather than *usable snapshots found*, so sessions that never emitted
`rate_limits` consume candidate slots. Demonstrated: five recent snapshot-less
sessions plus one older session with data returns `None`, surfacing "No Codex
session usage snapshots found" while good data sits one slot below the cutoff.

Fix: collect until `CHATGPT_CANDIDATE_FILES` *usable* snapshots are found,
scanning at most `10 × CHATGPT_CANDIDATE_FILES` files (50 by default) so large
session directories stay cheap. Included because the fallback path now depends
on rollout parsing being reliable.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `ENABLE_CHATGPT_LIVE` | `0` | Opt into the Codex CLI live source. |
| `CHATGPT_LIVE_MIN_INTERVAL_SECONDS` | `300` | Minimum interval between CLI calls. |
| `CODEX_CLI_PATH` | `codex` | Injectable CLI path (tests use a fake). |
| `CODEX_CLI_TIMEOUT_SECONDS` | `20` | Per-call timeout; set from the observed 26s cold start. |

Off by default, matching `ENABLE_CLAUDE_OAUTH`. A subprocess spawn and network
call are never part of default behaviour.

### Naming

`ProviderState.mode` reports `"oauth"` so both providers render symmetrically
and the existing `oauth_*` fields carry backoff state. The env var is
`ENABLE_CHATGPT_LIVE` rather than `..._OAUTH` because Quota Glass performs no
OAuth flow itself — it delegates to the CLI.

## Frontend

`frontend/src/App.tsx`:

- Add `"app-server"` to the `source` union (line 11).
- The local-only note (line 359) and the OAuth backoff block (line 336) are
  hardcoded to `provider.key === "claude"`. Generalize both so ChatGPT gets the
  same treatment, with copy pointing at `ENABLE_CHATGPT_LIVE`.

## Error handling

| Condition | Behaviour |
| --- | --- |
| `codex` not installed | `CodexCliUnavailable` → fall back to rollout, `mode="local"` |
| Logged out of Codex | JSON-RPC error → same fallback, reason surfaced |
| CLI hangs | killed at `CODEX_CLI_TIMEOUT_SECONDS`, process reaped, backoff |
| Malformed / changed protocol | `CodexCliProtocolError` → fallback, reason surfaced |
| Repeated failures | exponential backoff up to the max, cached reading served meanwhile |

A live failure must never make the card worse than today's rollout-only
behaviour.

## Testing

All tests stay offline: no network, no Keychain, no notifications, no real CLI —
preserving the promise in README.md.

- A fake `codex` executable injected via `CODEX_CLI_PATH` emits canned JSON-RPC:
  success, logged-out error, hang (timeout), malformed JSON, error response.
- A committed redacted fixture of a real `account/rateLimits/read` payload under
  `tests/fixtures/chatgpt/`.
- Cache and backoff tests mirroring `tests/test_claude_oauth_cache.py`.
- Fallback test: live fails with no cached payload ⇒ rollout meters still
  render and `mode == "local"`.
- Regression test for the `_latest_snapshot` candidate-window fix.
- Frontend: `npx tsc --noEmit` and `npm run build` stay clean.

## Out of scope

- `rateLimitResetCredits` (`availableCount: 3` on this account) — a new UI
  surface, not needed for the stated goal.
- `account/usage/read` lifetime/streak stats — no place in the current UI.
- Any Cloudflare challenge handling.
