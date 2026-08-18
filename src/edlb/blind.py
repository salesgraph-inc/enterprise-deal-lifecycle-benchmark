from __future__ import annotations

import hashlib
import hmac
import json
import math
import sqlite3
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from numbers import Real
from pathlib import Path
from typing import Any, Self

from .grading import CATEGORIES
from .protocol import validate_timestamp

MAX_SUBMISSIONS = 2
SUBMISSION_WINDOW = timedelta(days=30)
PUBLIC_RESULT_FIELDS = (
    "category_scores",
    "reliability",
    "cost",
    "error_summary",
)
PUBLIC_RELIABILITY_FIELDS = frozenset(
    {
        "pass_at_1",
        "pass_at_3",
        "pass_power_3",
        "official",
        "worlds",
        "incomplete_world_count",
        "duplicate_world_count",
        "incomplete_group_count",
        "duplicate_group_count",
        "confidence_interval",
    }
)
PUBLIC_ERROR_CATEGORIES = frozenset(
    {
        "agent",
        "agent_error",
        "error",
        "infrastructure",
        "infrastructure_error",
        "invalid_action",
        "judge",
        "judge_error",
        "model",
        "model_error",
        "network",
        "network_error",
        "other",
        "protocol",
        "protocol_error",
        "rate_limit",
        "runtime",
        "runtime_error",
        "timeout",
        "tool",
        "tool_error",
        "validation",
        "validation_error",
    }
)


class SubmissionLimitError(RuntimeError):
    pass


def _utc_timestamp(value: str | datetime) -> tuple[datetime, str]:
    if isinstance(value, datetime):
        parsed = value
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("timestamp must be UTC")
    else:
        validate_timestamp(value)
        parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() != timedelta(0):
        raise ValueError("timestamp must be UTC")
    normalized = parsed.astimezone(UTC)
    return normalized, normalized.isoformat().replace("+00:00", "Z")


def _manifest_bytes(
    manifest: bytes | bytearray | memoryview | Mapping[str, Any] | Path | str,
) -> bytes:
    if isinstance(manifest, bytes):
        return manifest
    if isinstance(manifest, (bytearray, memoryview)):
        return bytes(manifest)
    if isinstance(manifest, Path):
        return manifest.read_bytes()
    if isinstance(manifest, Mapping):
        return json.dumps(
            manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    if isinstance(manifest, str):
        return manifest.encode("utf-8")
    raise TypeError("run manifest must be bytes, text, a path, or an object")


def manifest_hash(
    manifest: bytes | bytearray | memoryview | Mapping[str, Any] | Path | str,
) -> str:
    return "sha256:" + hashlib.sha256(_manifest_bytes(manifest)).hexdigest()


hash_run_manifest = manifest_hash


def _secret_bytes(secret: bytes | bytearray | memoryview | str) -> bytes:
    if isinstance(secret, str):
        value = secret.encode("utf-8")
    elif isinstance(secret, (bytes, bytearray, memoryview)):
        value = bytes(secret)
    else:
        raise TypeError("signing secret must be bytes or text")
    if not value:
        raise ValueError("signing secret must not be empty")
    return value


def sign_result(result: Any, secret: bytes | bytearray | memoryview | str) -> str:
    return hmac.new(
        _secret_bytes(secret), _manifest_bytes(result), hashlib.sha256
    ).hexdigest()


def verify_result(
    result: Any,
    signature: str,
    secret: bytes | bytearray | memoryview | str,
) -> bool:
    if not isinstance(signature, str):
        return False
    signature = signature.removeprefix("sha256:")
    try:
        expected = sign_result(result, secret)
    except TypeError, ValueError:
        return False
    return hmac.compare_digest(expected, signature)


def _number(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, Real):
        return None
    try:
        value = int(value) if isinstance(value, int) else float(value)
        finite = math.isfinite(value)
    except OverflowError, ValueError:
        return None
    if not finite:
        return None
    return value


def _count(value: Any) -> int | None:
    number = _number(value)
    if number is None or number < 0 or int(number) != number:
        return None
    return int(number)


def _unit_number(value: Any) -> int | float | None:
    number = _number(value)
    return number if number is not None and 0 <= number <= 1 else None


def _nonnegative_number(value: Any) -> int | float | None:
    number = _number(value)
    return number if number is not None and number >= 0 else None


def _category_scores(value: Any) -> dict[str, int | float]:
    if not isinstance(value, Mapping):
        return {}
    return {
        category: number
        for category in CATEGORIES
        if (number := _unit_number(value.get(category))) is not None
    }


def _reliability(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    filtered: dict[str, Any] = {}
    for key in PUBLIC_RELIABILITY_FIELDS:
        if key not in value:
            continue
        item = value[key]
        if key == "official":
            if isinstance(item, bool):
                filtered[key] = item
            continue
        if key == "confidence_interval":
            if isinstance(item, (list, tuple)) and len(item) == 2:
                interval = [_unit_number(part) for part in item]
                if all(part is not None for part in interval):
                    filtered[key] = interval
            continue
        number = (
            _unit_number(item)
            if key in {"pass_at_1", "pass_at_3", "pass_power_3"}
            else _count(item)
            if key
            in {
                "worlds",
                "incomplete_world_count",
                "duplicate_world_count",
                "incomplete_group_count",
                "duplicate_group_count",
            }
            else None
        )
        if number is not None:
            filtered[key] = number
    return {key: filtered[key] for key in sorted(filtered)}


def _error_category(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    category = value.casefold()
    return category if category in PUBLIC_ERROR_CATEGORIES else None


def _error_item_category(value: Any) -> str | None:
    if isinstance(value, Mapping):
        for key in ("category", "type", "code"):
            category = _error_category(value.get(key))
            if category is not None:
                return category
        return None
    return _error_category(value)


def _error_summary(value: Any) -> dict[str, Any]:
    total: int | None = None
    categories: dict[str, int] = {}
    source: Any = None
    if isinstance(value, Mapping):
        for key in ("count", "total", "error_count"):
            if key in value:
                total = _count(value[key])
                if total is None:
                    return {}
                break
        source = value.get("categories")
        if source is None:
            source = value.get("items", value.get("errors"))
        if source is None:
            source = value
    elif isinstance(value, (list, tuple)):
        total = len(value)
        source = value
    elif value is not None:
        total = _count(value)
        if total is None:
            return {} if _number(value) is not None else {"count": 1}
        source = value if isinstance(value, str) else None
    if isinstance(source, Mapping):
        for raw_category, raw_count in source.items():
            category = _error_category(raw_category)
            count = _count(raw_count)
            if category is not None and count:
                categories[category] = categories.get(category, 0) + count
    elif isinstance(source, (list, tuple)):
        for item in source:
            category = _error_item_category(item)
            if category is not None:
                categories[category] = categories.get(category, 0) + 1
    else:
        category = _error_item_category(source)
        if category is not None:
            categories[category] = 1
    if total is None:
        total = sum(categories.values())
    if total < 0:
        return {}
    result: dict[str, Any] = {"count": total}
    if categories:
        result["categories"] = {
            category: categories[category] for category in sorted(categories)
        }
    return result


def filter_public_result(result: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(result, Mapping):
        raise TypeError("result must be an object")
    filtered: dict[str, Any] = {}
    if "category_scores" in result:
        filtered["category_scores"] = _category_scores(result["category_scores"])
    if "reliability" in result:
        filtered["reliability"] = _reliability(result["reliability"])
    cost = _nonnegative_number(result.get("cost"))
    if cost is None and isinstance(result.get("cost"), Mapping):
        cost = _nonnegative_number(result["cost"].get("minor_units"))
    resource_usage = result.get("resource_usage")
    if cost is None and isinstance(resource_usage, Mapping):
        cost = _nonnegative_number(resource_usage.get("cost_minor_units"))
    if cost is not None:
        filtered["cost"] = cost
    error_value = result.get("error_summary")
    if error_value is None and isinstance(resource_usage, Mapping):
        error_value = resource_usage.get("errors")
    if error_value is None:
        error_value = result.get("errors", result.get("error"))
    if error_value is not None:
        filtered["error_summary"] = _error_summary(error_value)
    return filtered


public_result = filter_public_result
public_result_filter = filter_public_result


def scan_canaries(
    output: bytes | bytearray | memoryview,
    trace: bytes | bytearray | memoryview,
    tokens: Iterable[bytes | bytearray | memoryview | str],
) -> tuple[str, ...]:
    output_bytes = bytes(output)
    trace_bytes = bytes(trace)
    found: list[str] = []
    for token in tokens:
        token_bytes = token.encode("utf-8") if isinstance(token, str) else bytes(token)
        if not token_bytes:
            raise ValueError("canary tokens must not be empty")
        if token_bytes in output_bytes or token_bytes in trace_bytes:
            found.append(
                token_bytes.decode("utf-8", errors="replace")
                if not isinstance(token, str)
                else token
            )
    return tuple(dict.fromkeys(found))


scan_canary_tokens = scan_canaries


class SubmissionLedger:
    def __init__(
        self,
        db_path: str | Path = ":memory:",
        max_submissions: int = MAX_SUBMISSIONS,
        window: timedelta = SUBMISSION_WINDOW,
    ) -> None:
        if max_submissions < 1:
            raise ValueError("max_submissions must be positive")
        if window <= timedelta(0):
            raise ValueError("submission window must be positive")
        self.connection = sqlite3.connect(str(db_path), isolation_level=None)
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS submissions (submission_id INTEGER PRIMARY KEY AUTOINCREMENT, team_id TEXT NOT NULL, run_id TEXT, submitted_at TEXT NOT NULL, submitted_at_us INTEGER NOT NULL, manifest_hash TEXT NOT NULL)"
        )
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS submissions_team_time ON submissions(team_id, submitted_at_us)"
        )
        self.max_submissions = max_submissions
        self.window = window

    def register_submission(
        self,
        team_id: str,
        submitted_at: str | datetime,
        manifest: bytes | bytearray | memoryview | Mapping[str, Any] | Path | str = b"",
        run_id: str | None = None,
    ) -> str:
        if not isinstance(team_id, str) or not team_id:
            raise ValueError("team_id must not be empty")
        timestamp, normalized = _utc_timestamp(submitted_at)
        digest = manifest_hash(manifest)
        timestamp_us = int(timestamp.timestamp() * 1_000_000)
        cutoff_us = int((timestamp - self.window).timestamp() * 1_000_000)
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            count = self.connection.execute(
                "SELECT COUNT(*) FROM submissions WHERE team_id = ? AND submitted_at_us BETWEEN ? AND ?",
                (team_id, cutoff_us, timestamp_us),
            ).fetchone()[0]
            if count >= self.max_submissions:
                raise SubmissionLimitError(
                    f"team {team_id!r} exceeded the submission limit"
                )
            self.connection.execute(
                "INSERT INTO submissions(team_id, run_id, submitted_at, submitted_at_us, manifest_hash) VALUES (?, ?, ?, ?, ?)",
                (team_id, run_id, normalized, timestamp_us, digest),
            )
            self.connection.execute("COMMIT")
        except Exception:
            self.connection.execute("ROLLBACK")
            raise
        return digest

    record_submission = register_submission

    def register_manifest_hash(
        self,
        team_id: str,
        run_id: str,
        manifest: bytes | bytearray | memoryview | Mapping[str, Any] | Path | str,
        submitted_at: str | datetime,
    ) -> str:
        return self.register_submission(team_id, submitted_at, manifest, run_id)

    def recent_count(self, team_id: str, submitted_at: str | datetime) -> int:
        timestamp, _ = _utc_timestamp(submitted_at)
        now_us = int(timestamp.timestamp() * 1_000_000)
        cutoff_us = int((timestamp - self.window).timestamp() * 1_000_000)
        return int(
            self.connection.execute(
                "SELECT COUNT(*) FROM submissions WHERE team_id = ? AND submitted_at_us BETWEEN ? AND ?",
                (team_id, cutoff_us, now_us),
            ).fetchone()[0]
        )

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def register_manifest_hash(
    ledger: SubmissionLedger,
    team_id: str,
    run_id: str,
    manifest: bytes | bytearray | memoryview | Mapping[str, Any] | Path | str,
    submitted_at: str | datetime,
) -> str:
    return ledger.register_manifest_hash(team_id, run_id, manifest, submitted_at)


__all__ = [
    "MAX_SUBMISSIONS",
    "PUBLIC_RESULT_FIELDS",
    "SUBMISSION_WINDOW",
    "SubmissionLedger",
    "SubmissionLimitError",
    "filter_public_result",
    "hash_run_manifest",
    "manifest_hash",
    "public_result",
    "public_result_filter",
    "register_manifest_hash",
    "scan_canaries",
    "scan_canary_tokens",
    "sign_result",
    "verify_result",
]
