"""Shared model-and-effort aggregation for the local usage windows.

Both providers count tokens per (model, effort) pair and turn those counts
into the same shape, so the arithmetic lives here once. Effort is whatever
the local record said — never inferred — and records that carry no effort
land under `UNSPECIFIED_EFFORT`.
"""

from typing import Any, Dict, Mapping, Optional, Tuple

from app.models import EffortUsage, ModelUsage, ModelUsageWindow


UNSPECIFIED_EFFORT = "unspecified"

# (model, effort) -> tokens
ModelEffortCounts = Mapping[Tuple[str, str], int]


def normalize_effort(value: Any) -> str:
    """Return a comparable effort label, or `unspecified` when there is none.

    Codex writes `"effort": null` for models that take no reasoning effort,
    and older Claude Code records predate the field entirely; both mean the
    same thing here.
    """
    if not isinstance(value, str):
        return UNSPECIFIED_EFFORT
    cleaned = value.strip().lower()
    return cleaned or UNSPECIFIED_EFFORT


def _percentage(part: int, whole: int) -> float:
    return round(part * 100.0 / whole, 1) if whole else 0.0


def model_usage_window(
    label: str,
    window_minutes: Optional[int],
    counts: ModelEffortCounts,
) -> ModelUsageWindow:
    """Build one window's model mix, each model split by effort.

    Model percentages are shares of the window; effort percentages are shares
    of their own model, so a model's efforts always add up to 100%.
    """
    model_totals: Dict[str, int] = {}
    effort_totals: Dict[str, Dict[str, int]] = {}
    for (model, effort), tokens in counts.items():
        if tokens <= 0:
            continue
        model_totals[model] = model_totals.get(model, 0) + tokens
        efforts = effort_totals.setdefault(model, {})
        efforts[effort] = efforts.get(effort, 0) + tokens

    total = sum(model_totals.values())
    models = []
    for model, tokens in sorted(
        model_totals.items(),
        key=lambda item: (-item[1], item[0]),
    ):
        models.append(
            ModelUsage(
                model=model,
                tokens=tokens,
                percentage=_percentage(tokens, total),
                efforts=[
                    EffortUsage(
                        effort=effort,
                        tokens=effort_tokens,
                        percentage=_percentage(effort_tokens, tokens),
                    )
                    for effort, effort_tokens in sorted(
                        effort_totals[model].items(),
                        key=lambda item: (-item[1], item[0]),
                    )
                ],
            )
        )
    return ModelUsageWindow(
        label=label,
        window_minutes=window_minutes,
        total_tokens=total,
        models=models,
    )
