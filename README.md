# Quota Glass

Quota Glass is a private, single-user macOS dashboard for Claude and ChatGPT
subscription usage. A React frontend displays quota gauges, refresh countdowns,
local token estimates, model-by-model token percentages, credits, provider
errors, and recent alert events.
A FastAPI process reads local usage files, persists samples and alert state in
SQLite, and uses built-in `osascript` notifications when a quota is exhausted
and when it refreshes.

Everything runs locally. No API key or vendor billing organization is needed.

## Data sources

- **ChatGPT:** reads the newest usable `token_count` snapshot from
  `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`. The percentage comes directly
  from Codex's local rate-limit snapshot. Because Codex only updates it during a
  turn, readings older than 30 minutes are marked stale and never alert. Model
  mix is calculated from each turn's token delta and active rollout model.
- **Claude (default):** reads assistant usage records under
  `~/.claude/projects/**/*.jsonl` and aggregates rolling 5-hour and 7-day token
  totals and model mix. These local records do not contain subscription quota
  percentages, so the dashboard labels them as estimates and never alerts on
  them.

The SQLite database is created at `./data/usage.db`. Alert latches survive app
restarts, preventing duplicate notifications.

## Requirements

- macOS with `/usr/bin/osascript`
- Python 3.9
- Node.js and npm

No Homebrew, `uv`, Poetry, `pipx`, `terminal-notifier`, API key, or system-wide
Python package installation is required.

## Setup

From this repository:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cd frontend
npm install
cd ..
```

## Run

```bash
./run.sh
```

Open [http://localhost:5173](http://localhost:5173). The backend listens only on
`127.0.0.1:8000`; Vite proxies `/api` to it. Stop both processes with `Ctrl-C`.

## Configuration

Set environment variables before running `./run.sh`:

| Variable | Default | Meaning |
| --- | --- | --- |
| `ENABLE_CLAUDE_OAUTH` | `0` | Opt into Claude's undocumented live quota source. |
| `POLL_INTERVAL_SECONDS` | `60` | Backend poll interval. |
| `OAUTH_MIN_INTERVAL_SECONDS` | `300` | Minimum interval between Claude OAuth usage requests. |
| `OAUTH_MAX_BACKOFF_SECONDS` | `3600` | Ceiling for exponential Claude OAuth rate-limit backoff. |
| `ALERT_THRESHOLD_PCT` | `100` | Percentage crossing that fires `EXHAUSTED`. |
| `ALERT_RESET_PCT` | `5` | Low-water percentage that confirms `REFRESHED`. |
| `STALE_AFTER_MINUTES` | `30` | Age after which a Codex snapshot is stale. |
| `CHATGPT_CANDIDATE_FILES` | `5` | Recent rollout files compared by snapshot timestamp. |
| `ENABLE_CHATGPT_LIVE` | `0` | Opt into live ChatGPT quota via the Codex CLI. |
| `CHATGPT_LIVE_MIN_INTERVAL_SECONDS` | `300` | Minimum interval between Codex CLI usage reads. |
| `CODEX_CLI_PATH` | `codex` | Path to the Codex CLI executable. |
| `CODEX_CLI_TIMEOUT_SECONDS` | `20` | Timeout for a single Codex CLI read. |
| `HISTORY_RETENTION_DAYS` | `30` | Retention period for SQLite samples and events. |
| `MAX_NOTIFICATION_ATTEMPTS` | `3` | Delivery attempts allowed for each notification. |
| `CODEX_SESSIONS_DIR` | `~/.codex/sessions` | Injectable Codex rollout root. |
| `CLAUDE_PROJECTS_DIR` | `~/.claude/projects` | Injectable Claude project root. |
| `NOTIFICATIONS_ENABLED` | `1` | Set to `0` to suppress macOS notifications. |
| `DATABASE_PATH` | `./data/usage.db` | SQLite file location. |

Example:

```bash
POLL_INTERVAL_SECONDS=30 NOTIFICATIONS_ENABLED=0 ./run.sh
```

## Claude OAuth opt-in: unsupported and fragile

Live Claude quota percentages are **off by default**. If explicitly enabled,
the backend caches successful usage responses in memory for at least five
minutes by default. When a request is due, it re-reads the rotating Claude Code
token from macOS Keychain immediately before calling:

```text
GET https://api.anthropic.com/api/oauth/usage
```

This is an undocumented internal endpoint. It may change or stop working after
any Claude Code update. Enabling it sends the request to Anthropic and permits
the app to ask Keychain for the `Claude Code-credentials` item:

```bash
ENABLE_CLAUDE_OAUTH=1 ./run.sh
```

The token is held only for the request and is never cached or written to disk.
Rate limits pause further OAuth requests using `Retry-After` when supplied, or
capped exponential backoff otherwise. A previously successful response remains
visible as stale OAuth data during backoff; only a provider that has never
succeeded falls back to local-only estimates. With the default
`ENABLE_CLAUDE_OAUTH=0`, Quota Glass makes no Claude network call and never
attempts a Keychain read.

## ChatGPT live quota opt-in

Live ChatGPT percentages are **off by default**. When enabled, the backend runs
`codex app-server` and calls its `account/rateLimits/read` JSON-RPC method,
caching the result for at least five minutes by default:

```bash
ENABLE_CHATGPT_LIVE=1 ./run.sh
```

Quota Glass never reads `~/.codex/auth.json` and never handles Codex tokens.
Authentication, token refresh, and request attestation stay inside the Codex
CLI. This does mean the feature depends on the CLI being installed and logged
in, and on `app-server` — an experimental interface — keeping its current shape.

Any failure (CLI missing, logged out, timeout, or a protocol change) falls back
to parsing Codex session files on disk, which is the default behaviour. A
previously successful reading stays visible as a cached value during backoff.
With `ENABLE_CHATGPT_LIVE=0`, no subprocess is spawned and no network call is
made.

## Cost estimates and billing

Claude dollar figures are rough API-equivalent estimates calculated from a
small hardcoded per-model price table. They are not subscription spend and may
not match current pricing. **Quota Glass does not use a vendor billing API** and
does not have access to either provider's billing records.

## API

- `GET /api/state` — providers, meters, credits, errors, and poller health
- `GET /api/history?hours=24` — sampled SQLite history
- `GET /api/events?limit=50` — recent fired alerts
- `POST /api/refresh` — immediate poll
- `GET /api/health` — lightweight process health

## Tests

```bash
.venv/bin/python -m pytest
cd frontend
npx tsc --noEmit
npm run build
```

Tests use committed JSONL fixtures and a recording notifier. They do not read
real usage directories, access Keychain, make a network call, or display a
macOS notification.
