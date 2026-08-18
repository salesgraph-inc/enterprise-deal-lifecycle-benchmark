from __future__ import annotations

import math
import random
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import date, datetime
from statistics import fmean
from typing import Any


def _finite(value: Any) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("value must be finite")
    return number


def _probability(value: Any) -> float:
    number = _finite(value)
    if not 0 <= number <= 1:
        raise ValueError("probability must be between zero and one")
    return number


def brier_score(
    probabilities: Iterable[float], outcomes: Iterable[bool | int | float]
) -> float:
    values = [
        (_probability(probability), 1.0 if bool(outcome) else 0.0)
        for probability, outcome in zip(probabilities, outcomes, strict=True)
    ]
    return (
        0.0
        if not values
        else fmean((probability - outcome) ** 2 for probability, outcome in values)
    )


def absolute_error(predicted: float, actual: float) -> float:
    return abs(_finite(predicted) - _finite(actual))


def _parse_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    return parsed.date()


def date_error_days(predicted: Any, actual: Any) -> int:
    return abs((_parse_date(predicted) - _parse_date(actual)).days)


def _groups(values: Sequence[Any]) -> list[list[bool]]:
    if not values:
        return []
    first = values[0]
    if isinstance(first, (str, bytes)) or not isinstance(first, Sequence):
        return [[bool(item) for item in values]]
    return [[bool(item) for item in group] for group in values]


def pass_at_k(trials: Sequence[Any], k: int = 1) -> float:
    if k < 1:
        raise ValueError("k must be positive")
    groups = _groups(trials)
    if not groups:
        return 0.0
    if k == 1:
        values = [value for group in groups for value in group]
        return 0.0 if not values else fmean(values)
    if any(len(group) != k for group in groups):
        raise ValueError(f"each world must have exactly {k} trials")
    return fmean(any(group) for group in groups)


def pass_power_k(trials: Sequence[Any], k: int = 3) -> float:
    if k < 1:
        raise ValueError("k must be positive")
    groups = _groups(trials)
    if not groups:
        return 0.0
    if any(len(group) != k for group in groups):
        raise ValueError(f"each world must have exactly {k} trials")
    return fmean(all(group) for group in groups)


def _percentile(values: Sequence[float], probability: float) -> float:
    if not values:
        raise ValueError("values must not be empty")
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def bootstrap_ci(
    values: Sequence[float],
    statistic: Callable[[Sequence[float]], float] | None = None,
    replicates: int = 10_000,
    seed: int = 0,
    alpha: float = 0.05,
) -> tuple[float, float]:
    if not values:
        raise ValueError("values must not be empty")
    if replicates < 1:
        raise ValueError("replicates must be positive")
    if not 0 < alpha < 1:
        raise ValueError("alpha must be between zero and one")
    statistic = statistic or fmean
    randomizer = random.Random(seed)
    samples = []
    for _ in range(replicates):
        sample = [values[randomizer.randrange(len(values))] for _ in values]
        samples.append(_finite(statistic(sample)))
    return (_percentile(samples, alpha / 2), _percentile(samples, 1 - alpha / 2))


def paired_bootstrap_ci(
    first: Sequence[float],
    second: Sequence[float],
    statistic: Callable[[Sequence[float]], float] | None = None,
    replicates: int = 10_000,
    seed: int = 0,
    alpha: float = 0.05,
) -> tuple[float, float]:
    if len(first) != len(second) or not first:
        raise ValueError("paired samples must have equal non-zero length")
    deltas = [
        _finite(right) - _finite(left)
        for left, right in zip(first, second, strict=True)
    ]
    return bootstrap_ci(
        deltas, statistic=statistic, replicates=replicates, seed=seed, alpha=alpha
    )


def execution_index_ci(
    pairs_by_vertical: Mapping[str, Sequence[Sequence[float]]],
    replicates: int = 10_000,
    seed: int = 0,
    alpha: float = 0.05,
) -> tuple[float, float]:
    groups = []
    for vertical in sorted(pairs_by_vertical):
        normalized = sorted(
            tuple(_finite(score) for score in pair)
            for pair in pairs_by_vertical[vertical]
        )
        if any(len(pair) != 2 for pair in normalized):
            raise ValueError("each counterfactual pair must contain two worlds")
        if normalized:
            groups.append(normalized)
    if not groups:
        raise ValueError("counterfactual pairs must not be empty")
    if replicates < 1:
        raise ValueError("replicates must be positive")
    if not 0 < alpha < 1:
        raise ValueError("alpha must be between zero and one")
    randomizer = random.Random(seed)
    samples = []
    for _ in range(replicates):
        vertical_scores = []
        for pairs in groups:
            selected = [pairs[randomizer.randrange(len(pairs))] for _ in pairs]
            vertical_scores.append(fmean(score for pair in selected for score in pair))
        samples.append(fmean(vertical_scores))
    return (_percentile(samples, alpha / 2), _percentile(samples, 1 - alpha / 2))


def counterfactual_sensitivity(
    pairs: Mapping[str, Mapping[str, float] | Sequence[float]]
    | Sequence[Mapping[str, Any] | Sequence[float]],
    replicates: int = 10_000,
    seed: int = 0,
) -> dict[str, Any]:
    first: list[float] = []
    second: list[float] = []
    items = (
        pairs.items()
        if isinstance(pairs, Mapping)
        else ((str(index), pair) for index, pair in enumerate(pairs))
    )
    for _, pair in items:
        if isinstance(pair, Mapping):
            left = pair.get("variant_a", pair.get("a"))
            right = pair.get("variant_b", pair.get("b"))
        else:
            if len(pair) != 2:
                raise ValueError("counterfactual pairs must contain two scores")
            left, right = pair
        if left is None or right is None:
            raise ValueError("counterfactual pairs require both variants")
        first.append(_finite(left))
        second.append(_finite(right))
    deltas = [
        _finite(right) - _finite(left)
        for left, right in zip(first, second, strict=True)
    ]
    interval = paired_bootstrap_ci(first, second, replicates=replicates, seed=seed)
    return {
        "pairs": len(deltas),
        "mean_delta_b_minus_a": 0.0 if not deltas else fmean(deltas),
        "confidence_interval": [interval[0], interval[1]],
        "replicates": replicates,
        "seed": seed,
    }


def macro_average_vertical(
    scores: Mapping[str, float | Sequence[float]],
    verticals: Sequence[str] | None = None,
) -> float:
    names = tuple(verticals or scores.keys())
    values = []
    for name in names:
        if name not in scores:
            continue
        value = scores[name]
        values.append(
            _finite(
                fmean(value)
                if isinstance(value, Sequence) and not isinstance(value, (str, bytes))
                else value
            )
        )
    return 0.0 if not values else fmean(values)


def resource_summary(resources: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    rows = list(resources)
    fields = (
        "tool_calls",
        "turns",
        "retries",
        "latency_ms",
        "cost_minor_units",
        "invalid_actions",
        "errors",
        "tokens",
    )
    totals = {
        field: sum(max(0, int(row.get(field, 0) or 0)) for row in rows)
        for field in fields
    }
    means = {field: (totals[field] / len(rows) if rows else 0.0) for field in fields}
    return {"runs": len(rows), "totals": totals, "means": means}


def reliability_metrics(trials: Sequence[Any], k: int = 3) -> dict[str, Any]:
    duplicate_count = 0
    groups: list[list[bool]]
    if trials and all(isinstance(item, Mapping) for item in trials):
        by_world: dict[str, dict[str, bool]] = {}
        for index, item in enumerate(trials):
            world_id = str(item.get("world_id", "world"))
            run_id = item.get("run_id")
            run_key = str(run_id) if run_id is not None else f"__row_{index}"
            group = by_world.setdefault(world_id, {})
            if run_key in group:
                duplicate_count += 1
                continue
            group[run_key] = bool(item.get("strict_cycle_pass"))
        groups = [list(group.values()) for group in by_world.values()]
    else:
        groups = _groups(trials)
    incomplete_count = sum(len(group) != k for group in groups)
    official = bool(groups) and not incomplete_count and not duplicate_count
    return {
        "pass_at_1": pass_at_k(groups, 1),
        "pass_at_3": pass_at_k(groups, k) if official else None,
        "pass_power_3": pass_power_k(groups, k) if official else None,
        "official": official,
        "incomplete_group_count": incomplete_count,
        "duplicate_group_count": duplicate_count,
    }


def pass_at_3(trials: Sequence[Any]) -> float:
    return pass_at_k(trials, 3)


def pass_power_3(trials: Sequence[Any]) -> float:
    return pass_power_k(trials, 3)


paired_bootstrap = paired_bootstrap_ci


__all__ = [
    "absolute_error",
    "bootstrap_ci",
    "brier_score",
    "counterfactual_sensitivity",
    "date_error_days",
    "execution_index_ci",
    "macro_average_vertical",
    "paired_bootstrap",
    "paired_bootstrap_ci",
    "pass_at_3",
    "pass_at_k",
    "pass_power_3",
    "pass_power_k",
    "reliability_metrics",
    "resource_summary",
]
