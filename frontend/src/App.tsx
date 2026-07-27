import { useCallback, useEffect, useMemo, useState } from "react";

type MeterProjection = {
  burn_rate_pct_per_hour: number;
  projected_exhaustion_at: number | null;
  exhausts_before_reset: boolean;
  sample_count: number;
  span_seconds: number;
};

type Meter = {
  key: string;
  provider: "chatgpt" | "claude";
  label: string;
  used_pct: number | null;
  window_minutes: number | null;
  resets_at: number | null;
  has_quota: boolean;
  source: "rollout" | "oauth" | "local" | "app-server";
  stale: boolean;
  projection: MeterProjection | null;
};

type HistorySample = {
  sampled_at: number;
  meter_key: string;
  provider: string;
  used_pct: number | null;
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

type EffortUsage = {
  effort: string;
  tokens: number;
  percentage: number;
};

type ModelUsage = {
  model: string;
  tokens: number;
  percentage: number;
  efforts: EffortUsage[];
};

type ModelUsageWindow = {
  label: string;
  window_minutes: number | null;
  total_tokens: number;
  models: ModelUsage[];
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
  event_type: "EXHAUSTED" | "REFRESHED" | "PROJECTED_EXHAUSTION";
  meter_key: string;
  provider: string;
  label: string;
  used_pct: number | null;
  created_at: number;
};

// How much history the sparkline covers, and how coarsely the backend buckets
// it. Six hours at five-minute buckets is ~72 points per meter -- enough shape
// to read a trend, small enough to re-fetch on every 15s poll.
const HISTORY_HOURS = 6;
const HISTORY_BUCKET_SECONDS = 300;

// Matches app/providers/usage.py: the label for records that named no effort.
const UNSPECIFIED_EFFORT = "unspecified";

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

function shortDuration(seconds: number): string {
  const totalMinutes = Math.max(0, Math.floor(seconds / 60));
  const hours = Math.floor(totalMinutes / 60);
  const minutes = totalMinutes % 60;
  if (hours && minutes) return `${hours}h ${minutes}m`;
  if (hours) return `${hours}h`;
  return `${minutes}m`;
}

// A polyline over the meter's own min/max, so a series that only moves a few
// percent still shows its shape. The width is normalised to the full time span
// rather than to sample index, so a gap in polling reads as a gap.
function Sparkline({ points, label }: { points: HistorySample[]; label: string }) {
  const usable = points.filter(
    (point): point is HistorySample & { used_pct: number } => point.used_pct !== null,
  );
  if (usable.length < 2) return null;

  const values = usable.map((point) => point.used_pct);
  const times = usable.map((point) => point.sampled_at);
  const lowest = Math.min(...values);
  const highest = Math.max(...values);
  const earliest = Math.min(...times);
  const latest = Math.max(...times);
  const timeSpan = latest - earliest || 1;
  // Keep a floor on the vertical range so a flat series draws a flat line
  // through the middle instead of amplifying rounding noise into a sawtooth.
  const valueSpan = Math.max(highest - lowest, 5);
  const floor = highest - valueSpan;

  const coordinates = usable
    .map((point) => {
      const x = ((point.sampled_at - earliest) / timeSpan) * 100;
      const y = 22 - ((point.used_pct - floor) / valueSpan) * 20;
      return `${x.toFixed(2)},${y.toFixed(2)}`;
    })
    .join(" ");

  return (
    <svg
      className="sparkline"
      viewBox="0 0 100 24"
      preserveAspectRatio="none"
      role="img"
      aria-label={`${label}: ${lowest.toFixed(0)} to ${highest.toFixed(
        0,
      )} percent over the last ${shortDuration(timeSpan)}`}
    >
      <polyline points={coordinates} />
    </svg>
  );
}

function BurnRate({ meter, now }: { meter: Meter; now: number }) {
  const projection = meter.projection;
  if (!projection) {
    return (
      <div className="burn-rate pending">
        <span>Burn rate</span>
        <strong>Gathering data…</strong>
      </div>
    );
  }
  if (projection.burn_rate_pct_per_hour <= 0) {
    return (
      <div className="burn-rate">
        <span>Burn rate</span>
        <strong>Idle</strong>
      </div>
    );
  }

  const rate = `${projection.burn_rate_pct_per_hour.toFixed(1)}%/hr`;
  const projectedAt = projection.projected_exhaustion_at;
  const remaining =
    projectedAt === null ? null : Math.max(0, projectedAt * 1000 - now);

  return (
    <div className={`burn-rate ${projection.exhausts_before_reset ? "warning" : ""}`}>
      <span>↗ {rate}</span>
      {remaining !== null && (
        <strong>
          {projection.exhausts_before_reset ? "Runs out in " : "Full in "}
          {shortDuration(remaining / 1000)}
        </strong>
      )}
      {projection.exhausts_before_reset && (
        <small>before this window resets</small>
      )}
    </div>
  );
}

function Gauge({
  meter,
  now,
  history,
}: {
  meter: Meter;
  now: number;
  history: HistorySample[];
}) {
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
      <div className="meter-trend">
        <Sparkline points={history} label={meter.label} />
        <BurnRate meter={meter} now={now} />
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

type EffortSlice = EffortUsage & {
  // Width as a share of the whole window, so segments stack into the model bar.
  width: number;
  color: string;
};

// Each effort is a flat sample of the same green-to-amber ramp the solid bars
// use, taken at the segment's midpoint, so a legend dot and its bar segment are
// always the same colour.
function effortSlices(model: ModelUsage): EffortSlice[] {
  let consumed = 0;
  return (model.efforts ?? []).map((effort) => {
    const share = effort.percentage / 100;
    const midpoint = Math.min(1, consumed + share / 2);
    consumed += share;
    return {
      ...effort,
      width: model.percentage * share,
      color: `color-mix(in srgb, var(--green), var(--amber) ${(
        midpoint * 100
      ).toFixed(1)}%)`,
    };
  });
}

function effortSummary(efforts: EffortUsage[]): string {
  return efforts
    .map((effort) => `${effort.effort} ${effort.percentage.toFixed(1)} percent`)
    .join(", ");
}

function ModelRow({ model }: { model: ModelUsage }) {
  // A model whose records never named an effort keeps the plain bar it always
  // had: a lone "unspecified 100%" row would be noise, not detail.
  const efforts = model.efforts ?? [];
  const detailed = efforts.some((effort) => effort.effort !== UNSPECIFIED_EFFORT);
  const slices = detailed ? effortSlices(model) : [];
  const share = `${model.percentage.toFixed(1)} percent`;

  return (
    <li>
      <div className="model-row">
        <strong title={model.model}>{model.model}</strong>
        <span>{model.percentage.toFixed(1)}%</span>
      </div>
      <div
        className="model-bar"
        role="img"
        aria-label={
          detailed
            ? `${model.model}: ${share}, split by effort ${effortSummary(efforts)}`
            : `${model.model}: ${share}`
        }
      >
        {detailed ? (
          slices.map((slice) => (
            <span
              key={slice.effort}
              className="model-bar-segment"
              style={{ width: `${slice.width}%`, background: slice.color }}
            />
          ))
        ) : (
          <span style={{ width: `${model.percentage}%` }} />
        )}
      </div>
      <small>{model.tokens.toLocaleString()} tokens</small>
      {detailed && (
        <ul className="effort-list">
          {slices.map((slice) => (
            <li key={slice.effort}>
              <span className="effort-swatch" style={{ background: slice.color }} />
              <span className="effort-name" title={slice.effort}>
                {slice.effort}
              </span>
              <span className="effort-share">{slice.percentage.toFixed(1)}%</span>
              <span className="effort-tokens">{slice.tokens.toLocaleString()}</span>
            </li>
          ))}
        </ul>
      )}
    </li>
  );
}

function ModelUsageCard({ usage }: { usage: ModelUsageWindow }) {
  const hasEffortDetail = usage.models.some((model) =>
    (model.efforts ?? []).some((effort) => effort.effort !== UNSPECIFIED_EFFORT),
  );
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
            <ModelRow key={model.model} model={model} />
          ))}
        </ol>
      )}
      <footer>
        {usage.total_tokens.toLocaleString()} attributed tokens
        {hasEffortDetail && " · effort percentages are per model"}
      </footer>
    </article>
  );
}

function ProviderPanel({
  provider,
  now,
  history,
}: {
  provider: Provider;
  now: number;
  history: Map<string, HistorySample[]>;
}) {
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
          {oauthCacheAge !== null && (
            <span>Live reading {durationLabel(oauthCacheAge)}</span>
          )}
        </div>
      </header>

      {provider.oauth_backed_off && (
        <div className="oauth-status" role="status">
          <strong>
            {provider.oauth_backoff_reason?.startsWith("Rate limited")
              ? "Live source rate limited"
              : "Live source retry delayed"}
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

      {provider.mode === "local" && !provider.oauth_backed_off && (
        <div className="local-note">
          <strong>Local-only mode</strong>
          {provider.key === "claude" ? (
            <p>
              Claude’s local records provide token and cost estimates, not quota
              percentages. Set <code>ENABLE_CLAUDE_OAUTH=1</code> before starting
              the app to opt into the fragile live-percentage source.
            </p>
          ) : (
            <p>
              ChatGPT percentages come from Codex session files on disk, so they
              are only as fresh as your last Codex session. Set{" "}
              <code>ENABLE_CHATGPT_LIVE=1</code> before starting the app to read
              live figures through the Codex CLI.
            </p>
          )}
        </div>
      )}

      <div className="meter-grid">
        {provider.meters
          .filter((meter) => meter.has_quota)
          .map((meter) => (
            <Gauge
              key={meter.key}
              meter={meter}
              now={now}
              history={history.get(meter.key) ?? []}
            />
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

function eventGlyph(eventType: AlertEvent["event_type"]): string {
  if (eventType === "REFRESHED") return "↻";
  if (eventType === "PROJECTED_EXHAUSTION") return "↗";
  return "!";
}

function eventSummary(eventType: AlertEvent["event_type"]): string {
  if (eventType === "REFRESHED") return "Quota refreshed";
  if (eventType === "PROJECTED_EXHAUSTION") return "On track to run out early";
  return "Quota exhausted";
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
                {eventGlyph(event.event_type)}
              </span>
              <div>
                <strong>
                  {event.provider} · {event.label}
                </strong>
                <p>
                  {eventSummary(event.event_type)}
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
  const [history, setHistory] = useState<HistorySample[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [now, setNow] = useState(Date.now());

  const load = useCallback(async () => {
    try {
      // One bucketed request covers every meter; grouping happens below.
      const [stateResponse, eventsResponse, historyResponse] = await Promise.all([
        fetch("/api/state"),
        fetch("/api/events?limit=20"),
        fetch(
          `/api/history?hours=${HISTORY_HOURS}&bucket_seconds=${HISTORY_BUCKET_SECONDS}`,
        ),
      ]);
      if (!stateResponse.ok || !eventsResponse.ok || !historyResponse.ok) {
        throw new Error("The local backend returned an error.");
      }
      const [nextState, nextEvents, nextHistory] = await Promise.all([
        stateResponse.json() as Promise<DashboardState>,
        eventsResponse.json() as Promise<{ events: AlertEvent[] }>,
        historyResponse.json() as Promise<{ samples: HistorySample[] }>,
      ]);
      setState(nextState);
      setEvents(nextEvents.events);
      setHistory(nextHistory.samples);
      setError(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Unable to reach the backend.");
    }
  }, []);

  const historyByMeter = useMemo(() => {
    const grouped = new Map<string, HistorySample[]>();
    for (const sample of history) {
      const existing = grouped.get(sample.meter_key);
      if (existing) existing.push(sample);
      else grouped.set(sample.meter_key, [sample]);
    }
    return grouped;
  }, [history]);

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
              <ProviderPanel
                key={provider.key}
                provider={provider}
                now={now}
                history={historyByMeter}
              />
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
