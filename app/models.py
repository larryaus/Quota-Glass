from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class MeterProjection(BaseModel):
    """Forward projection for one meter, derived from stored samples."""

    burn_rate_pct_per_hour: float
    # None when the burn rate is zero: nothing is being consumed, so there is
    # no finite time at which the window runs out.
    projected_exhaustion_at: Optional[int] = None
    exhausts_before_reset: bool = False
    sample_count: int
    span_seconds: int


class Meter(BaseModel):
    key: str
    provider: str
    label: str
    used_pct: Optional[float] = None
    window_minutes: Optional[int] = None
    resets_at: Optional[int] = None
    has_quota: bool
    source: str
    stale: bool = False
    projection: Optional[MeterProjection] = None


class Credits(BaseModel):
    has_credits: bool = False
    unlimited: bool = False
    balance: str = "0"
    currency: Optional[str] = None
    is_enabled: Optional[bool] = None
    spend_limit_reached: bool = False


class LocalUsageWindow(BaseModel):
    label: str
    input_tokens: int = 0
    cached_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_output_tokens: int = 0
    total_tokens: int = 0
    estimated_cost_usd: Optional[float] = None


class EffortUsage(BaseModel):
    effort: str
    tokens: int
    # Share of the parent model's tokens in the window, not of the window.
    percentage: float


class ModelUsage(BaseModel):
    model: str
    tokens: int
    percentage: float
    efforts: List[EffortUsage] = Field(default_factory=list)


class ModelUsageWindow(BaseModel):
    label: str
    window_minutes: Optional[int] = None
    total_tokens: int = 0
    models: List[ModelUsage] = Field(default_factory=list)


class ProviderState(BaseModel):
    key: str
    label: str
    mode: str
    meters: List[Meter] = Field(default_factory=list)
    credits: Credits = Field(default_factory=Credits)
    plan_type: Optional[str] = None
    error: Optional[str] = None
    last_updated: Optional[str] = None
    oauth_backed_off: bool = False
    oauth_backoff_reason: Optional[str] = None
    oauth_cache_age_seconds: Optional[int] = None
    oauth_next_retry_at: Optional[str] = None
    local_usage: List[LocalUsageWindow] = Field(default_factory=list)
    model_usage: List[ModelUsageWindow] = Field(default_factory=list)


class PollerHealth(BaseModel):
    status: str = "starting"
    running: bool = False
    background_task_alive: bool = False
    last_poll_started: Optional[str] = None
    last_poll_completed: Optional[str] = None
    last_poll_completed_age_seconds: Optional[int] = None
    last_poll_duration_ms: Optional[int] = None
    last_error: Optional[str] = None
    poll_count: int = 0


class DashboardState(BaseModel):
    providers: List[ProviderState] = Field(default_factory=list)
    poller: PollerHealth = Field(default_factory=PollerHealth)
    generated_at: str


class HistorySample(BaseModel):
    sampled_at: int
    meter_key: str
    provider: str
    used_pct: Optional[float] = None
    stale: bool = False


class HistoryResponse(BaseModel):
    hours: int
    # 0 means the rows are raw samples rather than time buckets.
    bucket_seconds: int = 0
    samples: List[HistorySample] = Field(default_factory=list)


JsonDict = Dict[str, Any]
