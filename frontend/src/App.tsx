import { useCallback, useEffect, useMemo, useState } from "react";

type Meter = {
  key: string;
  provider: "chatgpt" | "claude";
  label: string;
  used_pct: number | null;
  window_minutes: number | null;
  resets_at: number | null;
  has_quota: boolean;
  source: "rollout" | "oauth" | "local";
  stale: boolean;
};

type LocalUsage = {
  label: string;
  input_tokens: number;
  cached_input_tokens: number;
  cache_creation_input_tokens: number;
  cache_read_input_tokens: number;
  output_tokens: number;
  reasoning_output_tokens: number;
  total_tokens: number;
  estimated_cost_usd: number | null;
};

type ModelUsageWindow = {
  label: string;
  window_minutes: number | null;
  total_tokens: number;
  models: {
    model: string;
    tokens: number;
    percentage: number;
  }[];
};

type Provider = {
  key: "chatgpt" | "claude";
  label: string;
  mode: string;
  meters: Meter[];
  credits: {
    has_credits: boolean;
    unlimited: boolean;
    balance: string;
    currency: string | null;
    is_enabled: boolean | null;
    spend_limit_reached: boolean;
  };
  plan_type: string | null;
  error: string | null;
  last_updated: string | null;
  oauth_backed_off: boolean;
  oauth_backoff_reason: string | null;
  oauth_cache_age_seconds: number | null;
  oauth_next_retry_at: string | null;
  local_usage: LocalUsage[];
  model_usage: ModelUsageWindow[];
};

type DashboardState = {
  providers: Provider[];
  poller: {
    status: string;
    running: boolean;
    background_task_alive: boolean;
    last_poll_completed: string | null;
    last_poll_completed_age_seconds: number | null;
    last_poll_duration_ms: number | null;
    last_error: string | null;
    poll_count: number;
  };
  generated_at: string;
};

type AlertEvent = {
  id: number;
  event_type: "EXHAUSTED" | "REFRESHED";
  meter_key: string;
  provider: string;
  label: string;
  used_pct: number | null;
  created_at: number;
};

function timeAgo(value: string | null): string {
  if (!value) return "waiting for data";
  const seconds = Math.max(0, Math.floor((Date.now() - Date.parse(value)) / 1000));
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  return `${Math.floor(seconds / 3600)}h ago`;
}

function countdown(epoch: number | null, now: number): string {
  if (!epoch) return "Reset time unavailable";
  const remaining = Math.max(0, epoch * 1000 - now);
  if (remaining === 0) return "Refresh due";
  const totalMinutes = Math.floor(remaining / 60_000);
  const days = Math.floor(totalMinutes / 1440);
  const hours = Math.floor((totalMinutes % 1440) / 60);
  const minutes = totalMinutes % 60;
  if (days) return `${days}d ${hours}h ${minutes}m`;
  if (hours) return `${hours}h ${minutes}m`;
  return `${minutes}m`;
}

function windowLabel(minutes: number | null): string {
  if (!minutes) return "Rolling window";
  if (minutes % 10080 === 0) return `${minutes / 10080} week window`;
  if (minutes % 1440 === 0) return `${minutes / 1440} day window`;
  if (minutes % 60 === 0) return `${minutes / 60} hour window`;
  return `${minutes} minute window`;
}

function colorFor(pct: number | null): string {
  if (pct === null) return "var(--muted)";
  if (pct > 90) return "var(--red)";
  if (pct >= 70) return "var(--amber)";
  return "var(--green)";
}

function creditValue(balance: string, currency: string | null): string {
  return currency ? `${balance} ${currency}` : balance;
}

function durationLabel(seconds: number | null): string {
  if (seconds === null) return "age unavailable";
  if (seconds < 60) return `${seconds}s old`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m old`;
  return `${Math.floor(seconds / 3600)}h old`;
}

function retryTime(value: string | null): string {
  if (!value) return "the next scheduled poll";
  return new Intl.DateTimeFormat(undefined, {
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

function Gauge({ meter, now }: { meter: Meter; now: number }) {
  const value = meter.used_pct === null ? 0 : Math.min(100, Math.max(0, meter.used_pct));
  const gaugeStyle = {
    "--meter-value": `${value * 3.6}deg`,
    "--meter-color": colorFor(meter.used_pct),
  } as React.CSSProperties;

  return (
    <article className="meter-card">
      <div className="meter-topline">
        <span className="source">{meter.source}</span>
        <div className="badges">
          {meter.stale && <span className="badge warning">Stale</span>}
          {!meter.has_quota && <span className="badge">Estimate only</span>}
        </div>
      </div>
      <div className="meter-body">
        <div
          className="gauge"
          style={gaugeStyle}
          role="img"
          aria-label={
            meter.used_pct === null
              ? `${meter.label}: percentage unavailable`
              : `${meter.label}: ${meter.used_pct.toFixed(1)} percent used`
          }
        >
          <div className="gauge-center">
            {meter.used_pct === null ? (
              <span className="unknown">—</span>
            ) : (
              <>
                <strong>{meter.used_pct.toFixed(meter.used_pct % 1 ? 1 : 0)}</strong>
                <span>%</span>
              </>
            )}
          </div>
        </div>
        <div className="meter-copy">
          <h3>{meter.label}</h3>
          <p>{windowLabel(meter.window_minutes)}</p>
          <div className="countdown">
            <span>{meter.resets_at ? "Refreshes in" : "Status"}</span>
            <strong>{countdown(meter.resets_at, now)}</strong>
          </div>
        </div>
      </div>
    </article>
  );
}

function LocalUsageCard({
  usage,
  providerKey,
}: {
  usage: LocalUsage;
  providerKey: Provider["key"];
}) {
  return (
    <article className="local-card">
      <div>
        <span className="source">local estimate</span>
        <h3>{usage.label}</h3>
      </div>
      <strong className="token-total">{usage.total_tokens.toLocaleString()}</strong>
      <span className="token-caption">tokens observed</span>
      <dl>
        <div>
          <dt>Input</dt>
          <dd>{usage.input_tokens.toLocaleString()}</dd>
        </div>
        <div>
          <dt>Output</dt>
          <dd>{usage.output_tokens.toLocaleString()}</dd>
        </div>
        {providerKey === "chatgpt" ? (
          <>
            <div>
              <dt>Cached input</dt>
              <dd>{usage.cached_input_tokens.toLocaleString()}</dd>
            </div>
            <div>
              <dt>Reasoning output</dt>
              <dd>{usage.reasoning_output_tokens.toLocaleString()}</dd>
            </div>
          </>
        ) : (
          <>
            <div>
              <dt>Cache write</dt>
              <dd>{usage.cache_creation_input_tokens.toLocaleString()}</dd>
            </div>
            <div>
              <dt>Cache read</dt>
              <dd>{usage.cache_read_input_tokens.toLocaleString()}</dd>
            </div>
          </>
        )}
      </dl>
      {typeof usage.estimated_cost_usd === "number" && (
        <div className="cost">
          <span>Estimated API-equivalent cost</span>
          <strong>${usage.estimated_cost_usd.toFixed(2)}</strong>
        </div>
      )}
    </article>
  );
}

function ModelUsageCard({ usage }: { usage: ModelUsageWindow }) {
  return (
    <article className="model-card">
      <header>
        <div>
          <span className="source">token mix</span>
          <h3>Models used</h3>
        </div>
        <span className="model-window">{usage.label}</span>
      </header>
      {usage.models.length === 0 ? (
        <p className="model-empty">No model-attributed tokens observed yet.</p>
      ) : (
        <ol className="model-list">
          {usage.models.map((model) => (
            <li key={model.model}>
              <div className="model-row">
                <strong title={model.model}>{model.model}</strong>
                <span>{model.percentage.toFixed(1)}%</span>
              </div>
              <div
                className="model-bar"
                role="img"
                aria-label={`${model.model}: ${model.percentage.toFixed(1)} percent`}
              >
                <span style={{ width: `${model.percentage}%` }} />
              </div>
              <small>{model.tokens.toLocaleString()} tokens</small>
            </li>
          ))}
        </ol>
      )}
      <footer>{usage.total_tokens.toLocaleString()} attributed tokens</footer>
    </article>
  );
}

function ProviderPanel({ provider, now }: { provider: Provider; now: number }) {
  const initials = provider.key === "chatgpt" ? "C" : "A";
  const oauthCacheAge =
    provider.oauth_cache_age_seconds === null
      ? null
      : provider.last_updated
        ? Math.max(0, Math.floor((now - Date.parse(provider.last_updated)) / 1000))
        : provider.oauth_cache_age_seconds;
  return (
    <section className={`provider provider-${provider.key}`}>
      <header className="provider-header">
        <div className="provider-identity">
          <span className="provider-mark">{initials}</span>
          <div>
            <div className="provider-title">
              <h2>{provider.label}</h2>
              <span className="mode">{provider.mode}</span>
            </div>
            <p>Updated {timeAgo(provider.last_updated)}</p>
          </div>
        </div>
        <div className="account-meta">
          {provider.plan_type && <span>{provider.plan_type} plan</span>}
          <span>
            {provider.credits.unlimited
              ? "Unlimited credits"
              : provider.key === "claude" && provider.credits.has_credits
                ? `Credits used ${creditValue(
                    provider.credits.balance,
                    provider.credits.currency,
                  )}`
                : `Credit balance ${creditValue(
                    provider.credits.balance,
                    provider.credits.currency,
                  )}`}
          </span>
          {provider.credits.is_enabled !== null && (
            <span>
              Extra usage {provider.credits.is_enabled ? "enabled" : "disabled"}
            </span>
          )}
          {provider.credits.spend_limit_reached && <span>Spend limit reached</span>}
          {provider.key === "claude" && oauthCacheAge !== null && (
            <span>OAuth reading {durationLabel(oauthCacheAge)}</span>
          )}
        </div>
      </header>

      {provider.key === "claude" && provider.oauth_backed_off && (
        <div className="oauth-status" role="status">
          <strong>
            {provider.oauth_backoff_reason?.startsWith("Rate limited")
              ? "OAuth rate limited"
              : "OAuth retry delayed"}
          </strong>
          <p>
            {provider.oauth_backoff_reason}
            {oauthCacheAge !== null &&
              ` Cached reading is ${durationLabel(oauthCacheAge)}.`}
            {` Retrying at ${retryTime(provider.oauth_next_retry_at)}.`}
          </p>
        </div>
      )}

      {provider.error && !provider.oauth_backed_off && (
        <div className="provider-error" role="status">
          <span>!</span>
          <p>{provider.error}</p>
        </div>
      )}

      {provider.key === "claude" &&
        provider.mode === "local" &&
        !provider.oauth_backed_off && (
          <div className="local-note">
            <strong>Local-only mode</strong>
            <p>
              Claude’s local records provide token and cost estimates, not quota
              percentages. Set <code>ENABLE_CLAUDE_OAUTH=1</code> before starting
              the app to opt into the fragile live-percentage source.
            </p>
          </div>
        )}

      <div className="meter-grid">
        {provider.meters
          .filter((meter) => meter.has_quota)
          .map((meter) => (
            <Gauge key={meter.key} meter={meter} now={now} />
          ))}
        {provider.local_usage.map((usage) => (
          <LocalUsageCard
            key={usage.label}
            usage={usage}
            providerKey={provider.key}
          />
        ))}
        {provider.model_usage.map((usage) => (
          <ModelUsageCard key={usage.label} usage={usage} />
        ))}
      </div>

      {provider.meters.length === 0 &&
        provider.local_usage.length === 0 &&
        provider.model_usage.length === 0 && (
        <div className="empty-state">No usage readings are available yet.</div>
      )}
    </section>
  );
}

function Events({ events }: { events: AlertEvent[] }) {
  return (
    <section className="events-panel">
      <header>
        <div>
          <span className="eyebrow">macOS notifications</span>
          <h2>Recent events</h2>
        </div>
        <span className="event-count">{events.length}</span>
      </header>
      {events.length === 0 ? (
        <div className="events-empty">
          <span className="quiet-dot" />
          <div>
            <strong>All quiet</strong>
            <p>Exhausted and refreshed quota events will appear here.</p>
          </div>
        </div>
      ) : (
        <ol className="events-list">
          {events.map((event) => (
            <li key={event.id}>
              <span className={`event-icon ${event.event_type.toLowerCase()}`}>
                {event.event_type === "REFRESHED" ? "↻" : "!"}
              </span>
              <div>
                <strong>
                  {event.provider} · {event.label}
                </strong>
                <p>
                  {event.event_type === "REFRESHED"
                    ? "Quota refreshed"
                    : "Quota exhausted"}
                  {event.used_pct !== null && ` at ${event.used_pct.toFixed(0)}%`}
                </p>
              </div>
              <time dateTime={new Date(event.created_at * 1000).toISOString()}>
                {timeAgo(new Date(event.created_at * 1000).toISOString())}
              </time>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}

export default function App() {
  const [state, setState] = useState<DashboardState | null>(null);
  const [events, setEvents] = useState<AlertEvent[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [now, setNow] = useState(Date.now());

  const load = useCallback(async () => {
    try {
      const [stateResponse, eventsResponse] = await Promise.all([
        fetch("/api/state"),
        fetch("/api/events?limit=20"),
      ]);
      if (!stateResponse.ok || !eventsResponse.ok) {
        throw new Error("The local backend returned an error.");
      }
      const [nextState, nextEvents] = await Promise.all([
        stateResponse.json() as Promise<DashboardState>,
        eventsResponse.json() as Promise<{ events: AlertEvent[] }>,
      ]);
      setState(nextState);
      setEvents(nextEvents.events);
      setError(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Unable to reach the backend.");
    }
  }, []);

  useEffect(() => {
    void load();
    const poll = window.setInterval(() => void load(), 15_000);
    return () => window.clearInterval(poll);
  }, [load]);

  useEffect(() => {
    const ticker = window.setInterval(() => setNow(Date.now()), 1_000);
    return () => window.clearInterval(ticker);
  }, []);

  async function forceRefresh() {
    setRefreshing(true);
    try {
      const response = await fetch("/api/refresh", { method: "POST" });
      if (!response.ok) throw new Error("Refresh failed.");
      await load();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Refresh failed.");
    } finally {
      setRefreshing(false);
    }
  }

  const healthClass = useMemo(() => {
    if (
      error ||
      state?.poller.status === "degraded" ||
      (state !== null && !state.poller.background_task_alive)
    )
      return "degraded";
    if (!state || state.poller.status === "starting") return "starting";
    return "healthy";
  }, [error, state]);

  return (
    <main>
      <div className="ambient ambient-one" />
      <div className="ambient ambient-two" />
      <header className="app-header">
        <div className="brand">
          <div className="brand-symbol">
            <i />
            <i />
            <i />
          </div>
          <div>
            <span className="eyebrow">Local usage monitor</span>
            <h1>Quota Glass</h1>
          </div>
        </div>
        <div className="header-actions">
          <div className={`health ${healthClass}`}>
            <span />
            <div>
              <strong>{healthClass === "healthy" ? "Watching" : healthClass}</strong>
              <small>
                {state?.poller.last_poll_completed
                  ? `Checked ${timeAgo(state.poller.last_poll_completed)}`
                  : "Initial poll"}
              </small>
            </div>
          </div>
          <button onClick={() => void forceRefresh()} disabled={refreshing}>
            <span className={refreshing ? "spin" : ""}>↻</span>
            {refreshing ? "Refreshing" : "Refresh now"}
          </button>
        </div>
      </header>

      {error && (
        <div className="connection-error" role="alert">
          <strong>Backend unavailable.</strong> {error} Make sure <code>./run.sh</code>{" "}
          is running.
        </div>
      )}

      {!state ? (
        <section className="loading">
          <div className="loading-orbit" />
          <p>Reading local usage snapshots…</p>
        </section>
      ) : (
        <>
          <div className="intro">
            <div>
              <span className="eyebrow">Subscription headroom</span>
              <h2>Know what’s left before the limit hits.</h2>
            </div>
            <p>
              Data stays on this Mac. Quota alerts are edge-triggered and
              remembered across restarts.
            </p>
          </div>
          <div className="provider-stack">
            {state.providers.map((provider) => (
              <ProviderPanel key={provider.key} provider={provider} now={now} />
            ))}
          </div>
          <Events events={events} />
        </>
      )}

      <footer>
        <span>Private by design · Local files + SQLite</span>
        <span>Percentages refresh every 15 seconds</span>
      </footer>
    </main>
  );
}
