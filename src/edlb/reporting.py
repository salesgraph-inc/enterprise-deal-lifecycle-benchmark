from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .grading import CATEGORIES
from .models import aggregate_scorecard_hash, scorecard_hash


def _value(scorecard: Mapping[str, Any] | Any) -> Mapping[str, Any]:
    if isinstance(scorecard, Mapping):
        return scorecard
    if hasattr(scorecard, "to_dict"):
        value = scorecard.to_dict()
        if isinstance(value, Mapping):
            return value
    raise TypeError("scorecard must be a mapping or JSON model")


def _validate_score_hash(value: Mapping[str, Any]) -> None:
    if "score_hash" not in value:
        if "run_id" not in value and "runs" in value:
            raise ValueError("aggregate report is missing score_hash")
        return
    expected = (
        scorecard_hash(value) if "run_id" in value else aggregate_scorecard_hash(value)
    )
    if value["score_hash"] != expected:
        raise ValueError("score_hash does not match scorecard contents")


def scorecard_json(scorecard: Mapping[str, Any] | Any, indent: int = 2) -> str:
    value = _value(scorecard)
    if "run_id" in value:
        value = _schema_scorecard(value)
    _validate_score_hash(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=indent) + "\n"


def _schema_scorecard(value: Mapping[str, Any]) -> dict[str, Any]:
    fields = (
        "run_id",
        "benchmark_version",
        "world_id",
        "vertical",
        "track",
        "status",
        "execution_index",
        "strict_cycle_pass",
        "critical_violation",
        "configuration_resolved",
        "trial_seed",
        "configuration_hash",
        "manifest_hash",
        "rubric_hash",
        "oracle_hash",
        "category_scores",
        "secondary_metrics",
        "reliability",
        "resource_usage",
        "rubric_validation",
        "violations",
        "pending_judge_assertions",
        "state_hash",
        "score_hash",
        "grader_version",
        "generated_at",
    )
    result = {key: value[key] for key in fields if key in value}
    result["category_scores"] = {
        key: value.get("category_scores", {}).get(key, 0.0) for key in CATEGORIES
    }
    result["secondary_metrics"] = {
        key: value.get("secondary_metrics", {})[key]
        for key in (
            "terminal_outcome",
            "revenue_minor_units",
            "margin_minor_units",
            "cycle_days",
            "forecast_brier",
            "amount_error_minor_units",
            "close_date_error_days",
        )
        if key in value.get("secondary_metrics", {})
    }
    result["reliability"] = {
        key: value.get("reliability", {})[key]
        for key in ("pass_at_1", "pass_at_3", "pass_power_3", "confidence_interval")
        if key in value.get("reliability", {})
        and value.get("reliability", {})[key] is not None
    }
    result["resource_usage"] = {
        key: value.get("resource_usage", {})[key]
        for key in (
            "tool_calls",
            "turns",
            "retries",
            "latency_ms",
            "cost_minor_units",
            "invalid_actions",
            "errors",
            "tokens",
            "metric_availability",
        )
        if key in value.get("resource_usage", {})
    }
    if isinstance(value.get("rubric_validation"), Mapping):
        result["rubric_validation"] = dict(value["rubric_validation"])
    result["violations"] = [
        {
            key: item[key]
            for key in ("assertion_id", "severity", "message")
            if key in item
        }
        for item in value.get("violations", [])
        if isinstance(item, Mapping)
    ]
    pending = value.get("pending_judge_assertions", [])
    if pending:
        result["pending_judge_assertions"] = list(pending)
        known = {str(item.get("assertion_id")) for item in result["violations"]}
        result["violations"].extend(
            {
                "assertion_id": str(item),
                "severity": "info",
                "message": "LLM judge score is pending",
            }
            for item in pending
            if str(item) not in known
        )
    return result


def _number(value: Any, digits: int = 3) -> str:
    if value is None:
        return "pending"
    try:
        return f"{float(value):.{digits}f}"
    except TypeError, ValueError:
        return str(value)


def render_markdown(scorecard: Mapping[str, Any] | Any) -> str:
    value = _value(scorecard)
    _validate_score_hash(value)
    if "run_id" not in value and "runs" in value:
        return render_aggregate_markdown(value)
    lines = [
        f"# EDLB scorecard: {value.get('run_id', 'run')}",
        "",
        f"World `{value.get('world_id', 'unknown')}`, track `{value.get('track', 'unknown')}`.",
        "",
        f"Status: **{value.get('status', 'unknown')}**",
        f"Execution index: **{_number(value.get('execution_index'), 2)}/100**",
        f"Strict cycle pass: **{'yes' if value.get('strict_cycle_pass') else 'no'}**",
        f"Critical violation: **{'yes' if value.get('critical_violation') else 'no'}**",
        f"State hash: `{value.get('state_hash') or 'unavailable'}`",
        f"Score hash: `{value.get('score_hash') or 'unavailable'}`",
        "",
        "## Category scores",
        "",
        "| Category | Score |",
        "| --- | ---: |",
    ]
    categories = value.get("category_scores", {})
    for category in CATEGORIES:
        lines.append(f"| {category} | {_number(categories.get(category, 0.0))} |")
    secondary = value.get("secondary_metrics", {})
    if secondary:
        lines.extend(
            ("", "## Secondary metrics", "", "| Metric | Value |", "| --- | ---: |")
        )
        for key in (
            "terminal_outcome",
            "revenue_minor_units",
            "margin_minor_units",
            "cycle_days",
            "forecast_brier",
            "amount_error_minor_units",
            "close_date_error_days",
        ):
            if key in secondary:
                lines.append(f"| {key} | {secondary[key]} |")
    reliability = value.get("reliability", {})
    if reliability:
        lines.extend(("", "## Reliability", "", "| Metric | Value |", "| --- | ---: |"))
        for key in ("pass_at_1", "pass_at_3", "pass_power_3"):
            if key in reliability:
                lines.append(f"| {key} | {_number(reliability[key])} |")
        if "confidence_interval" in reliability:
            lines.append(
                f"| confidence interval | {reliability['confidence_interval']} |"
            )
    usage = value.get("resource_usage", {})
    if usage:
        lines.extend(("", "## Resources", "", "| Metric | Value |", "| --- | ---: |"))
        for key in (
            "tool_calls",
            "turns",
            "retries",
            "latency_ms",
            "cost_minor_units",
            "invalid_actions",
            "errors",
            "tokens",
        ):
            if key in usage:
                lines.append(f"| {key} | {usage[key]} |")
    rubric_validation = value.get("rubric_validation", {})
    if rubric_validation:
        lines.extend(
            (
                "",
                "## Rubric validation",
                "",
                f"Deterministic weight: **{_number(rubric_validation.get('deterministic_fraction'))}**",
                f"Valid: **{'yes' if rubric_validation.get('valid') else 'no'}**",
            )
        )
        for error in rubric_validation.get("errors", ()):
            lines.append(f"- {error}")
    violations = value.get("violations", [])
    if violations:
        lines.extend(("", "## Violations", ""))
        lines.extend(
            f"- `{item.get('assertion_id', 'assertion')}` {item.get('message', '')}"
            for item in violations
        )
    pending = value.get("pending_judge_assertions", [])
    if pending:
        lines.extend(
            (
                "",
                f"LLM judge scores pending for: {', '.join(f'`{item}`' for item in pending)}.",
            )
        )
    return "\n".join(lines).rstrip() + "\n"


def render_aggregate_markdown(report: Mapping[str, Any]) -> str:
    _validate_score_hash(report)
    lines = [
        "# EDLB aggregate report",
        "",
        f"Runs: **{report.get('runs', 0)}**",
        f"Official: **{'yes' if report.get('official') else 'no'}**",
        f"Execution index: **{_number(report.get('execution_index'), 2)}/100**",
        f"Critical violation rate: **{_number(report.get('critical_violation_rate'))}**",
        f"State hash: `{report.get('state_hash') or 'unavailable'}`",
        f"Score hash: `{report.get('score_hash') or 'unavailable'}`",
        "",
        "## Category scores",
        "",
        "| Category | Score |",
        "| --- | ---: |",
    ]
    validation = report.get("input_validation", {})
    if isinstance(validation, Mapping) and validation:
        lines.extend(
            (
                f"Input validation: **{'passed' if validation.get('valid') else 'failed'}**",
                "",
            )
        )
    for category in CATEGORIES:
        lines.append(
            f"| {category} | {_number(report.get('category_scores', {}).get(category, 0.0))} |"
        )
    reliability = report.get("reliability", {})
    if reliability:
        lines.extend(("", "## Reliability", "", "| Metric | Value |", "| --- | ---: |"))
        for key in ("pass_at_1", "pass_at_3", "pass_power_3"):
            if key in reliability:
                lines.append(f"| {key} | {_number(reliability[key])} |")
        lines.append(f"| official | {'yes' if reliability.get('official') else 'no'} |")
        for key in ("worlds", "incomplete_world_count", "duplicate_world_count"):
            if key in reliability:
                lines.append(f"| {key} | {reliability[key]} |")
    interval = report.get("execution_index_confidence_interval")
    lines.extend(
        (
            "",
            "## Execution index uncertainty",
            "",
            f"95% paired-world interval: `{interval if interval is not None else 'unavailable'}`",
        )
    )
    sensitivity = report.get("counterfactual_sensitivity")
    if sensitivity:
        lines.extend(
            (
                "",
                "## Counterfactual sensitivity",
                "",
                f"Mean B minus A: **{_number(sensitivity.get('mean_delta_b_minus_a'))}**",
                f"95% interval: `{sensitivity.get('confidence_interval')}`",
            )
        )
    else:
        lines.extend(
            (
                "",
                "## Counterfactual sensitivity",
                "",
                "Unavailable. Explicit pair metadata is required.",
            )
        )
    resources = report.get("resource_usage")
    if isinstance(resources, Mapping) and resources:
        lines.extend(
            (
                "",
                "## Resources",
                "",
                "| Metric | Total | Mean |",
                "| --- | ---: | ---: |",
            )
        )
        totals = resources.get("totals", {})
        totals = totals if isinstance(totals, Mapping) else {}
        means = resources.get("means", {})
        means = means if isinstance(means, Mapping) else {}
        for key in (
            "tool_calls",
            "turns",
            "retries",
            "latency_ms",
            "cost_minor_units",
            "invalid_actions",
            "errors",
            "tokens",
        ):
            if key in totals:
                lines.append(f"| {key} | {totals[key]} | {_number(means.get(key))} |")
    ordering_keys = report.get("ordering_keys")
    if ordering_keys:
        lines.extend(
            (
                "",
                "## Ranking order",
                "",
                ", ".join(str(key) for key in ordering_keys),
            )
        )
    return "\n".join(lines).rstrip() + "\n"


def write_report(
    scorecard: Mapping[str, Any] | Any,
    json_path: str | Path,
    markdown_path: str | Path | None = None,
) -> tuple[Path, Path]:
    value = _value(scorecard)
    _validate_score_hash(value)
    destination = Path(json_path)
    markdown_destination = (
        Path(markdown_path)
        if markdown_path is not None
        else destination.with_suffix(".md")
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    markdown_destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(scorecard_json(value), encoding="utf-8")
    markdown_destination.write_text(render_markdown(value), encoding="utf-8")
    return destination, markdown_destination


def report_scorecard(*args: Any, **kwargs: Any) -> tuple[Path, Path]:
    return write_report(*args, **kwargs)


render_scorecard = render_markdown
serialize_scorecard = scorecard_json


__all__ = [
    "render_aggregate_markdown",
    "render_markdown",
    "render_scorecard",
    "report_scorecard",
    "scorecard_json",
    "serialize_scorecard",
    "write_report",
]
