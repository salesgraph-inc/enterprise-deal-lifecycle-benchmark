from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import sys
import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self

from .causal import (
    LANE_DEFAULTS,
    LANES,
    action_effects,
    digest,
    lane_status,
    normalize_official_seeds,
    public_event_effects,
    realization_cache_key,
    realization_packet,
    realize,
    select_stakeholder_act,
    terminal_outcome,
)
from .models import (
    Actor,
    Artifact,
    Checkpoint,
    Event,
    RoleGrant,
    RunManifest,
    ScenarioManifest,
    TraceEvent,
    stable_hash,
    to_json,
)
from .protocol import KINDS, validate_timestamp

SELLER_ROLES = ("account_executive", "domain_specialist", "sales_manager", "revops")
ROLE_ALIASES: dict[str, str] = {}
WRITE_ACTIONS = frozenset(
    {
        "write",
        "update",
        "merge",
        "send",
        "schedule",
        "revise",
        "create",
        "attach",
        "request",
        "respond",
        "complete",
        "advance",
    }
)
TRACE_KINDS = KINDS | {"system_error"}
TERMINAL_APPROVAL_STATUSES = frozenset({"approved", "rejected"})
SCOPED_VISIBILITIES = frozenset({"role_scoped", "internal_role_scoped", "restricted"})
SCOPE_ACCESS = {
    "run": frozenset({"current_world"}),
    "crm": frozenset({"current_world", "assigned_opportunity"}),
    "communications": frozenset({"current_world", "assigned_opportunity", "buyer_org"}),
    "calendar": frozenset({"current_world", "assigned_opportunity", "buyer_org"}),
    "documents": frozenset({"current_world", "assigned_opportunity", "seller_org"}),
    "approvals": frozenset({"current_world", "assigned_opportunity"}),
    "web": frozenset({"current_world", "buyer_org", "assigned_vertical"}),
    "team": frozenset({"current_world", "seller_org"}),
}
CANONICAL_STATE_TABLES = (
    "actors",
    "events",
    "artifacts",
    "checkpoints",
    "checkpoint_completions",
    "checkpoint_tool_usage",
    "grants",
    "crm_records",
    "crm_history",
    "communications",
    "calendar_events",
    "documents",
    "document_versions",
    "document_links",
    "approvals",
    "web_records",
    "team_messages",
    "causal_lanes",
    "causal_event_applications",
    "causal_action_applications",
    "stakeholder_acts",
    "stakeholder_realizations",
)


class EngineError(RuntimeError):
    pass


class AuthorizationError(EngineError):
    pass


class ImmutableError(EngineError):
    pass


class IdempotencyError(EngineError):
    pass


class ToolLimitError(EngineError):
    pass


def _parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(validate_timestamp(value))
    except ValueError as exc:
        raise EngineError(
            "timestamp must be an RFC3339 string with a timezone"
        ) from exc
    return parsed.astimezone(UTC)


def _time_value(value: str) -> float:
    return _parse_time(value).timestamp()


def _validated_limit(limit: int | None) -> int | None:
    if limit is None:
        return None
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise EngineError("limit must be a positive integer or null")
    return limit


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _hash(value: Any) -> str:
    return hashlib.sha256(to_json(value).encode("utf-8")).hexdigest()


def _stable_id(prefix: str, run_id: str, key: str) -> str:
    return f"{prefix}-{hashlib.sha256(f'{run_id}:{key}'.encode()).hexdigest()[:20]}"


def canonical_database_state(connection: sqlite3.Connection) -> dict[str, Any]:
    connection.row_factory = sqlite3.Row
    meta = {
        str(row["key"]): str(row["value"])
        for row in connection.execute("SELECT key, value FROM meta")
    }
    result: dict[str, Any] = {
        "manifest": json.loads(meta.get("manifest", "{}")),
        "scenario": json.loads(meta.get("scenario", "{}")),
        "current_time": meta.get("current_time"),
        "current_checkpoint": int(meta.get("current_checkpoint", -1)),
        "status": meta.get("status", "running"),
        "terminal_outcome": meta.get("terminal_outcome"),
        "terminal_support": json.loads(meta.get("terminal_support", "{}")),
        "trace_commitment": meta.get("trace_commitment"),
        "finalization_sequence": (
            int(meta["finalization_sequence"])
            if "finalization_sequence" in meta
            else None
        ),
    }
    if "resource_usage" in meta:
        result["resource_usage"] = json.loads(meta["resource_usage"])
    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    for table in CANONICAL_STATE_TABLES:
        if table in tables:
            result[table] = [
                dict(row)
                for row in connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid')
            ]
    return result


def canonical_trace_hash(connection: sqlite3.Connection) -> str:
    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    if "trace" not in tables:
        return stable_hash([])
    rows = []
    for row in connection.execute("SELECT raw FROM trace ORDER BY sequence"):
        value = json.loads(str(row[0]))
        if isinstance(value, Mapping):
            value = dict(value)
            value.pop("latency_ms", None)
        rows.append(value)
    return stable_hash(rows)


def canonical_database_hash(
    connection: sqlite3.Connection, state: Mapping[str, Any] | None = None
) -> str:
    canonical = dict(
        state if state is not None else canonical_database_state(connection)
    )
    canonical.pop("resource_usage", None)
    for table, rows in tuple(canonical.items()):
        if not isinstance(rows, list) or not all(
            isinstance(row, Mapping) for row in rows
        ):
            continue
        columns = [
            str(row[1])
            for row in connection.execute(f"PRAGMA table_info({table})")
            if int(row[5])
        ]
        canonical[table] = sorted(
            (dict(row) for row in rows),
            key=(
                lambda row: (
                    tuple(to_json(row.get(column)) for column in columns)
                    if columns
                    else to_json(row)
                )
            ),
        )
    return stable_hash(canonical)


def _list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (str, bytes)):
        return [value]
    return list(value)


_MISSING = object()


def _state_diff(before: Any, after: Any, path: str = "") -> list[dict[str, Any]]:
    if isinstance(before, Mapping) and isinstance(after, Mapping):
        result: list[dict[str, Any]] = []
        keys = sorted(set(before) | set(after), key=str)
        for key in keys:
            child = f"{path}/{str(key).replace('~', '~0').replace('/', '~1')}"
            result.extend(
                _state_diff(before.get(key, _MISSING), after.get(key, _MISSING), child)
            )
        return result
    if before is _MISSING:
        return [{"op": "set", "path": path or "/", "value": after}]
    if after is _MISSING:
        return [{"op": "remove", "path": path or "/"}]
    if before != after:
        return [{"op": "set", "path": path or "/", "value": after}]
    return []


class RunEngine:
    def __init__(
        self,
        db_path: str | Path = ":memory:",
        manifest: RunManifest | None = None,
        scenario: ScenarioManifest | None = None,
        actors: Iterable[Actor] = (),
        events: Iterable[Event] = (),
        artifacts: Iterable[Artifact] = (),
        checkpoints: Iterable[Checkpoint] = (),
        grants: Iterable[RoleGrant] = (),
        trace_path: str | Path | None = None,
        stakeholder_realizer_command: Sequence[str] | None = None,
        stakeholder_timeout_seconds: float | None = None,
    ) -> None:
        self.db_path = str(db_path)
        self.trace_path = Path(trace_path) if trace_path is not None else None
        self.connection = sqlite3.connect(self.db_path, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self._transaction_depth = 0
        self._edlb_seeded_artifacts: set[str] = set()
        self._edlb_bundle: Any = None
        self.stakeholder_realizer_command = (
            tuple(stakeholder_realizer_command)
            if stakeholder_realizer_command is not None
            else None
        )
        if stakeholder_timeout_seconds is not None and (
            not isinstance(stakeholder_timeout_seconds, (int, float))
            or isinstance(stakeholder_timeout_seconds, bool)
            or not math.isfinite(stakeholder_timeout_seconds)
            or stakeholder_timeout_seconds <= 0
        ):
            raise EngineError("stakeholder timeout must be positive")
        self.stakeholder_timeout_seconds = stakeholder_timeout_seconds
        self._create_schema()
        initialized = self._meta("initialized")
        if initialized is None:
            self.manifest = manifest or self._default_manifest("run", "world")
            self.scenario = scenario or self._default_scenario(self.manifest.world_id)
            self._initialize_run(actors, events, artifacts, checkpoints, grants)
        else:
            manifest_data = json.loads(self._meta("manifest") or "{}")
            scenario_data = json.loads(self._meta("scenario") or "{}")
            self.manifest = RunManifest.from_dict(manifest_data)
            self.scenario = ScenarioManifest.from_dict(scenario_data)
            if manifest is not None and manifest.run_id != self.manifest.run_id:
                raise EngineError(
                    "existing run manifest does not match the requested run"
                )
        stakeholder_manifest = self.manifest.stakeholder_manifest
        self.official_stakeholder_seeds = normalize_official_seeds(
            stakeholder_manifest.get("official_seeds"),
            int(stakeholder_manifest.get("seed", self.manifest.seed)),
        )

    @staticmethod
    def _default_manifest(run_id: str, world_id: str) -> RunManifest:
        digest = "sha256:" + ("0" * 64)
        return RunManifest(
            run_id,
            "v1.0.0",
            world_id,
            "open_team",
            "reference",
            "v1.0.0",
            "v1.0.0",
            digest,
            digest,
            None,
            0,
            {
                "resolved": False,
                "roles": {role: "unresolved" for role in SELLER_ROLES},
                "models": {},
            },
            {
                "model_id": "deterministic",
                "model_digest": digest,
                "prompt_hash": digest,
                "seed": 0,
                "timeout_seconds": None,
            },
            {
                "tool_calls_per_checkpoint": None,
                "turns_per_checkpoint": None,
                "timeout_seconds": None,
                "retries": 0,
            },
            {
                "resolved": False,
                "runtime_version": (
                    f"{sys.implementation.name}-{sys.version_info.major}."
                    f"{sys.version_info.minor}.{sys.version_info.micro}"
                ),
                "image_digest": None,
                "git_revision": None,
                "executor_policy_digest": None,
            },
            "1970-01-01T00:00:00+00:00",
            "created",
        )

    @staticmethod
    def _default_scenario(world_id: str) -> ScenarioManifest:
        start = "1970-01-01T00:00:00+00:00"
        end = "1970-07-01T00:00:00+00:00"
        return ScenarioManifest(
            world_id,
            world_id + "-pair",
            "a",
            "dev",
            "manufacturing",
            "champion_departure",
            "seller",
            "buyer",
            "Synthetic world",
            "Synthetic benchmark world",
            start,
            end,
            181,
            (),
            (),
            (),
            (),
            (
                "call_transcript",
                "email",
                "internal_chat",
                "crm",
                "calendar",
                "document",
                "web_signal",
            ),
            "no_decision",
            0,
            {"code": "MIT", "data": "CC-BY-4.0"},
            {
                "synthetic_only": True,
                "generator": "edlb",
                "generator_version": "v1.0.0",
                "created_at": start,
                "source_policy_ids": (),
            },
            release_visibility="public",
        )

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _create_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS actors (actor_id TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (event_id TEXT PRIMARY KEY, available_at TEXT NOT NULL, visibility TEXT NOT NULL, data TEXT NOT NULL, content_hash TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS artifacts (artifact_id TEXT PRIMARY KEY, available_at TEXT NOT NULL, visibility TEXT NOT NULL, data TEXT NOT NULL, content_hash TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS checkpoints (checkpoint_id TEXT PRIMARY KEY, position INTEGER NOT NULL UNIQUE, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS checkpoint_completions (checkpoint_id TEXT NOT NULL, role TEXT NOT NULL, summary TEXT NOT NULL, completed_at TEXT NOT NULL, PRIMARY KEY (checkpoint_id, role));
            CREATE TABLE IF NOT EXISTS checkpoint_tool_usage (checkpoint_id TEXT PRIMARY KEY, attempts INTEGER NOT NULL CHECK (attempts >= 0));
            CREATE TABLE IF NOT EXISTS grants (role TEXT NOT NULL, resource TEXT NOT NULL, action TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY (role, resource, action));
            CREATE TABLE IF NOT EXISTS idempotency (key TEXT PRIMARY KEY, operation TEXT NOT NULL, result TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS crm_records (record_id TEXT PRIMARY KEY, data TEXT NOT NULL, updated_at TEXT NOT NULL, version INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS crm_history (history_id INTEGER PRIMARY KEY AUTOINCREMENT, record_id TEXT NOT NULL, changed_at TEXT NOT NULL, role TEXT NOT NULL, changes TEXT NOT NULL, snapshot TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS communications (message_id TEXT PRIMARY KEY, channel TEXT NOT NULL, direction TEXT NOT NULL, sender_role TEXT NOT NULL, recipients TEXT NOT NULL, subject TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL, available_at TEXT NOT NULL, visibility TEXT NOT NULL, metadata TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS calendar_events (calendar_id TEXT PRIMARY KEY, data TEXT NOT NULL, available_at TEXT NOT NULL, visibility TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS documents (document_id TEXT PRIMARY KEY, data TEXT NOT NULL, available_at TEXT NOT NULL, visibility TEXT NOT NULL, version INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS document_versions (document_id TEXT NOT NULL, version INTEGER NOT NULL, data TEXT NOT NULL, PRIMARY KEY (document_id, version));
            CREATE TABLE IF NOT EXISTS document_links (document_id TEXT NOT NULL, related_type TEXT NOT NULL, related_id TEXT NOT NULL, PRIMARY KEY (document_id, related_type, related_id));
            CREATE TABLE IF NOT EXISTS approvals (approval_id TEXT PRIMARY KEY, data TEXT NOT NULL, updated_at TEXT NOT NULL, visibility TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS web_records (record_id TEXT PRIMARY KEY, data TEXT NOT NULL, available_at TEXT NOT NULL, visibility TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS team_messages (message_id TEXT PRIMARY KEY, data TEXT NOT NULL, available_at TEXT NOT NULL, visibility TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS causal_lanes (lane TEXT PRIMARY KEY, score INTEGER NOT NULL CHECK (score BETWEEN -100 AND 100), status TEXT NOT NULL, sticky INTEGER NOT NULL CHECK (sticky IN (0, 1)), evidence TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS causal_event_applications (event_id TEXT PRIMARY KEY, applied_at TEXT NOT NULL, effects TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS causal_action_applications (action_key TEXT PRIMARY KEY, checkpoint INTEGER NOT NULL, tool_name TEXT NOT NULL, role TEXT NOT NULL, input_hash TEXT NOT NULL, result_hash TEXT NOT NULL, effects TEXT NOT NULL, applied_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS stakeholder_acts (act_id TEXT PRIMARY KEY, action_key TEXT NOT NULL UNIQUE, data TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS stakeholder_realizations (cache_key TEXT PRIMARY KEY, act_id TEXT NOT NULL, input_hash TEXT NOT NULL, packet TEXT NOT NULL, text TEXT NOT NULL, model_digest TEXT NOT NULL, prompt_hash TEXT NOT NULL, seed INTEGER NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS trace (sequence INTEGER PRIMARY KEY AUTOINCREMENT, raw TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS snapshots (sequence INTEGER PRIMARY KEY, timestamp TEXT NOT NULL, checkpoint INTEGER NOT NULL, state_hash TEXT NOT NULL, data TEXT NOT NULL, previous_state_hash TEXT, state_diff TEXT NOT NULL DEFAULT '[]');
            """
        )
        lane_columns = {
            str(row[1])
            for row in self.connection.execute("PRAGMA table_info(causal_lanes)")
        }
        if "sticky" not in lane_columns:
            self.connection.execute(
                "ALTER TABLE causal_lanes ADD COLUMN sticky INTEGER NOT NULL DEFAULT 0 CHECK (sticky IN (0, 1))"
            )
        action_columns = {
            str(row[1])
            for row in self.connection.execute(
                "PRAGMA table_info(causal_action_applications)"
            )
        }
        if "checkpoint" not in action_columns:
            self.connection.execute(
                "ALTER TABLE causal_action_applications ADD COLUMN checkpoint INTEGER NOT NULL DEFAULT -1"
            )
        snapshot_columns = {
            str(row[1])
            for row in self.connection.execute("PRAGMA table_info(snapshots)")
        }
        if "previous_state_hash" not in snapshot_columns:
            self.connection.execute(
                "ALTER TABLE snapshots ADD COLUMN previous_state_hash TEXT"
            )
        if "state_diff" not in snapshot_columns:
            self.connection.execute(
                "ALTER TABLE snapshots ADD COLUMN state_diff TEXT NOT NULL DEFAULT '[]'"
            )

    def _meta(self, key: str) -> str | None:
        row = self.connection.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()
        return None if row is None else str(row[0])

    def _set_meta(self, key: str, value: str) -> None:
        self.connection.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value)
        )

    def trace_manifest(self) -> dict[str, Any]:
        value = self.manifest.to_dict()
        value.pop("status", None)
        value.pop("ended_at", None)
        return value

    def trace_manifest_fingerprint(self) -> str:
        return stable_hash(self.trace_manifest())

    def persist_resource_usage(self, resource_usage: Mapping[str, Any]) -> None:
        if not isinstance(resource_usage, Mapping):
            raise EngineError("resource usage must be an object")
        self._set_meta("resource_usage", to_json(dict(resource_usage)))
        sequence = int(
            self.connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) FROM trace"
            ).fetchone()[0]
        )
        self._set_meta("trace_commitment", canonical_trace_hash(self.connection))
        self._set_meta("finalization_sequence", str(sequence))
        self._save_snapshot()

    def _initialize_run(
        self,
        actors: Iterable[Actor],
        events: Iterable[Event],
        artifacts: Iterable[Artifact],
        checkpoints: Iterable[Checkpoint],
        grants: Iterable[RoleGrant],
    ) -> None:
        checkpoint_values = sorted(checkpoints, key=lambda item: item.sequence)
        if [item.sequence for item in checkpoint_values] != list(
            range(len(checkpoint_values))
        ):
            raise EngineError(
                "checkpoint sequences must start at zero and be contiguous"
            )
        _parse_time(self.manifest.started_at)
        _parse_time(self.scenario.start_at)
        _parse_time(self.scenario.end_at)
        self._set_meta("manifest", to_json(self.manifest))
        self._set_meta("scenario", to_json(self.scenario))
        self._set_meta("current_time", self.manifest.started_at)
        self._set_meta("current_checkpoint", "-1")
        self._set_meta("status", "running")
        self._set_meta("initialized", "1")
        self._initialize_causal_lanes()
        for actor in actors:
            _parse_time(actor.active_from)
            if actor.active_until is not None:
                _parse_time(actor.active_until)
            self._validate_visibility(
                actor.visibility,
                actor.visible_roles,
                {"public", "internal_role_scoped", "restricted"},
                "actor",
            )
            self.connection.execute(
                "INSERT INTO actors(actor_id, data) VALUES (?, ?)",
                (actor.actor_id, to_json(actor)),
            )
        for event in events:
            self.append_event(event)
        for artifact in artifacts:
            self.append_artifact(artifact)
        for checkpoint in checkpoint_values:
            for timestamp in (
                checkpoint.available_at,
                checkpoint.window_start,
                checkpoint.window_end,
            ):
                _parse_time(timestamp)
            self.connection.execute(
                "INSERT INTO checkpoints(checkpoint_id, position, data) VALUES (?, ?, ?)",
                (checkpoint.checkpoint_id, checkpoint.sequence, to_json(checkpoint)),
            )
        for grant in grants:
            self.grant(grant)
        self.release_available_events()
        self._save_snapshot()
        self._trace(
            "start",
            "system",
            {
                **self.trace_manifest(),
                "manifest_fingerprint": self.trace_manifest_fingerprint(),
            },
        )

    @property
    def current_time(self) -> str:
        return self._meta("current_time") or self.manifest.started_at

    @property
    def current_checkpoint_index(self) -> int:
        return int(self._meta("current_checkpoint") or "-1")

    @property
    def status(self) -> str:
        return self._meta("status") or "running"

    def _initialize_causal_lanes(self) -> None:
        for lane, score in LANE_DEFAULTS.items():
            self.connection.execute(
                "INSERT OR IGNORE INTO causal_lanes(lane, score, status, sticky, evidence, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (lane, score, lane_status(score), 0, "[]", self.current_time),
            )

    def causal_lanes(self) -> dict[str, dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT lane, score, status, sticky, evidence, updated_at FROM causal_lanes ORDER BY lane"
        ).fetchall()
        result = {
            str(row[0]): {
                "score": int(row[1]),
                "status": str(row[2]),
                "sticky": bool(row[3]),
                "evidence": json.loads(str(row[4])),
                "updated_at": str(row[5]),
            }
            for row in rows
        }
        if set(result) != set(LANES):
            raise EngineError("causal lane state is incomplete")
        return result

    def causal_state(self) -> dict[str, Any]:
        return {
            "lanes": self.causal_lanes(),
            "event_ids": [
                str(row[0])
                for row in self.connection.execute(
                    "SELECT event_id FROM causal_event_applications ORDER BY event_id"
                )
            ],
            "action_keys": [
                str(row[0])
                for row in self.connection.execute(
                    "SELECT action_key FROM causal_action_applications ORDER BY action_key"
                )
            ],
            "terminal_outcome": self._meta("terminal_outcome"),
        }

    def causal_state_hash(self) -> str:
        return digest(self.causal_state())

    def _apply_lane_effects(
        self, effects: Mapping[str, Mapping[str, Any]], source_id: str
    ) -> None:
        for lane, effect in effects.items():
            if lane not in LANES:
                raise EngineError(f"unknown causal lane: {lane}")
            row = self.connection.execute(
                "SELECT score, status, sticky, evidence FROM causal_lanes WHERE lane = ?",
                (lane,),
            ).fetchone()
            if row is None:
                raise EngineError(f"causal lane is missing: {lane}")
            delta = effect.get("delta", 0)
            if not isinstance(delta, int) or isinstance(delta, bool):
                raise EngineError(f"causal lane delta is invalid: {lane}")
            absolute = effect.get("absolute")
            if absolute is not None and (
                not isinstance(absolute, int)
                or isinstance(absolute, bool)
                or not -100 <= absolute <= 100
            ):
                raise EngineError(f"causal lane absolute score is invalid: {lane}")
            current = int(row[0])
            proposed = absolute if absolute is not None else current + delta
            proposed = max(-100, min(100, proposed))
            was_sticky = bool(row[2])
            blocked_gain = was_sticky and proposed > current
            score = current if blocked_gain else proposed
            status = (
                str(row[1])
                if blocked_gain
                else str(effect.get("status") or lane_status(score))
            )
            sticky = was_sticky or effect.get("sticky") is True
            evidence = json.loads(str(row[3]))
            item = {
                "source_id": source_id,
                "fact": str(effect.get("fact", "state changed")),
            }
            if item not in evidence:
                evidence.append(item)
            self.connection.execute(
                "UPDATE causal_lanes SET score = ?, status = ?, sticky = ?, evidence = ?, updated_at = ? WHERE lane = ?",
                (
                    score,
                    status,
                    int(sticky),
                    to_json(evidence),
                    self.current_time,
                    lane,
                ),
            )

    def release_available_events(self) -> tuple[str, ...]:
        rows = self.connection.execute(
            "SELECT event_id, data FROM events WHERE visibility != 'oracle_only' AND event_id NOT IN (SELECT event_id FROM causal_event_applications)"
        ).fetchall()
        available = []
        for row in rows:
            event = json.loads(str(row[1]))
            if _time_value(event["available_at"]) <= _time_value(self.current_time):
                available.append((str(row[0]), event))
        events = sorted(
            available,
            key=lambda item: (
                _time_value(str(item[1]["available_at"])),
                int(item[1].get("sequence", 0)),
                item[0],
            ),
        )
        released = []
        for event_id, event in events:
            effects = public_event_effects(event)
            self._apply_lane_effects(effects, event_id)
            self.connection.execute(
                "INSERT INTO causal_event_applications(event_id, applied_at, effects) VALUES (?, ?, ?)",
                (event_id, self.current_time, to_json(effects)),
            )
            released.append(event_id)
        return tuple(released)

    def _external_action(
        self, tool_name: str, result: Mapping[str, Any]
    ) -> tuple[bool, str | None]:
        if tool_name == "communications.send":
            metadata = result.get("metadata")
            external = isinstance(metadata, Mapping) and isinstance(
                metadata.get("semantic_envelope"), Mapping
            )
            recipients = result.get("recipients")
        elif tool_name in {"calendar.schedule", "calendar.reschedule"}:
            recipients = result.get("participants")
            external = False
            for recipient in _list(recipients):
                actor = self._actor_for_recipient(str(recipient))
                if actor is not None and actor.get("kind") != "seller":
                    external = True
                    break
        else:
            return False, None
        actor_id = None
        for recipient in _list(recipients):
            actor = self._actor_for_recipient(str(recipient))
            if actor is not None and actor.get("kind") != "seller":
                actor_id = str(actor.get("actor_id"))
                break
        return external, actor_id

    def _stakeholder_settings(self) -> tuple[str, str, int]:
        manifest = self.manifest.stakeholder_manifest
        model_digest = str(manifest.get("model_digest", digest("deterministic")))
        prompt_hash = str(manifest.get("prompt_hash", digest("edlb-v1-stakeholders")))
        seed = int(manifest.get("seed", self.official_stakeholder_seeds[0]))
        if seed not in self.official_stakeholder_seeds:
            raise EngineError("stakeholder seed is not one of the official seeds")
        return model_digest, prompt_hash, seed

    def apply_agent_action(
        self,
        action_key: str,
        role: str,
        tool_name: str,
        arguments: Mapping[str, Any],
        result: Mapping[str, Any],
    ) -> dict[str, Any]:
        existing = self.connection.execute(
            "SELECT effects FROM causal_action_applications WHERE action_key = ?",
            (action_key,),
        ).fetchone()
        if existing is not None:
            return {"effects": json.loads(str(existing[0])), "cached": True}
        input_hash = digest(
            {"tool_name": tool_name, "role": role, "arguments": arguments}
        )
        result_hash = digest(result)
        external, actor_id = self._external_action(tool_name, result)
        effects = action_effects(tool_name, arguments, result, external)
        checkpoint = self.current_checkpoint_index
        if self.connection.execute(
            "SELECT 1 FROM causal_action_applications WHERE checkpoint = ? AND tool_name = ? LIMIT 1",
            (checkpoint, tool_name),
        ).fetchone():
            effects = {
                lane: effect
                for lane, effect in effects.items()
                if int(effect.get("delta", 0)) <= 0
            }
        outer = self._transaction_depth == 0
        if outer:
            self.connection.execute("BEGIN")
        try:
            self._apply_lane_effects(effects, action_key)
            response = None
            if external and actor_id is not None:
                envelope = arguments.get("semantic_envelope")
                if isinstance(envelope, Mapping):
                    act = select_stakeholder_act(
                        self.manifest.world_id,
                        action_key,
                        actor_id,
                        str(result.get("channel", "email")),
                        envelope,
                        self.causal_lanes(),
                    )
                    response = self._realize_stakeholder_act(act, input_hash)
            self.connection.execute(
                "INSERT INTO causal_action_applications(action_key, checkpoint, tool_name, role, input_hash, result_hash, effects, applied_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    action_key,
                    checkpoint,
                    tool_name,
                    role,
                    input_hash,
                    result_hash,
                    to_json(effects),
                    self.current_time,
                ),
            )
            if outer:
                self.connection.execute("COMMIT")
            return {"effects": effects, "response": response, "cached": False}
        except Exception:
            if outer:
                self.connection.execute("ROLLBACK")
            raise

    def _realize_stakeholder_act(self, act: Any, input_hash: str) -> dict[str, Any]:
        model_digest, prompt_hash, seed = self._stakeholder_settings()
        packet = realization_packet(act, prompt_hash, model_digest, seed)
        state_hash = self.causal_state_hash()
        cache_key = realization_cache_key(
            state_hash,
            input_hash,
            packet,
            prompt_hash,
            model_digest,
            seed,
        )
        row = self.connection.execute(
            "SELECT text FROM stakeholder_realizations WHERE cache_key = ?",
            (cache_key,),
        ).fetchone()
        cached = row is not None
        if row is None:
            before = self.causal_state_hash()
            text = realize(
                packet,
                self.stakeholder_realizer_command,
                self.stakeholder_timeout_seconds,
            )
            if self.causal_state_hash() != before:
                raise EngineError("stakeholder realization mutated causal state")
            self.connection.execute(
                "INSERT INTO stakeholder_realizations(cache_key, act_id, input_hash, packet, text, model_digest, prompt_hash, seed, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    cache_key,
                    act.act_id,
                    input_hash,
                    to_json(packet),
                    text,
                    model_digest,
                    prompt_hash,
                    seed,
                    self.current_time,
                ),
            )
        else:
            text = str(row[0])
        self.connection.execute(
            "INSERT OR IGNORE INTO stakeholder_acts(act_id, action_key, data, created_at) VALUES (?, ?, ?, ?)",
            (act.act_id, act.action_key, to_json(act.to_dict()), self.current_time),
        )
        message_id = f"{act.act_id}-response"
        self.seed_communication(
            message_id,
            {
                "message_id": message_id,
                "channel": act.channel,
                "direction": "inbound",
                "sender_role": act.actor_id,
                "recipients": SELLER_ROLES,
                "subject": "Re: next step",
                "body": text,
                "created_at": self.current_time,
                "available_at": self.current_time,
                "visibility": SELLER_ROLES,
                "metadata": {
                    "stakeholder_act_id": act.act_id,
                    "realization_cache_key": cache_key,
                    "model_digest": model_digest,
                    "prompt_hash": prompt_hash,
                    "seed": seed,
                },
            },
        )
        return {
            "act_id": act.act_id,
            "message_id": message_id,
            "cache_key": cache_key,
            "cached": cached,
        }

    def finalize_terminal_outcome(self) -> dict[str, Any]:
        existing = self._meta("terminal_outcome")
        if existing is not None:
            return {
                "terminal_outcome": existing,
                "support": json.loads(self._meta("terminal_support") or "{}"),
            }
        outcome, decisive_lanes = terminal_outcome(self.causal_lanes())
        lanes = self.causal_lanes()
        support_lanes = decisive_lanes or tuple(
            lane for lane in LANES if lanes[lane]["evidence"]
        )
        support = {
            lane: {
                "score": lanes[lane]["score"],
                "status": lanes[lane]["status"],
                "evidence": lanes[lane]["evidence"],
            }
            for lane in support_lanes
        }
        self._set_meta("terminal_outcome", outcome)
        self._set_meta("terminal_support", to_json(support))
        return {"terminal_outcome": outcome, "support": support}

    def _set_clock(self, timestamp: str) -> None:
        if _time_value(timestamp) < _time_value(self.current_time):
            raise EngineError("virtual time cannot move backwards")
        self._set_meta("current_time", timestamp)

    def _visible(
        self,
        visibility: str | Sequence[str],
        role: str,
        data: Mapping[str, Any] | None = None,
    ) -> bool:
        if role == "system":
            return True
        if isinstance(visibility, str):
            if visibility == "oracle_only":
                return False
            if visibility in SCOPED_VISIBILITIES:
                roles = (data or {}).get("visible_roles", ())
                return bool(roles) and role in roles
            return visibility in {"public", "agent_visible"}
        if not visibility or "*" in visibility:
            return True
        return role in visibility

    @staticmethod
    def _validate_visibility(
        visibility: str,
        visible_roles: Sequence[str],
        allowed: set[str],
        record_type: str,
    ) -> None:
        if visibility not in allowed:
            raise EngineError(f"{record_type} visibility is invalid")
        roles = tuple(visible_roles)
        if len(roles) != len(set(roles)) or any(
            role not in SELLER_ROLES for role in roles
        ):
            raise EngineError(f"{record_type} visible_roles is invalid")
        if visibility in SCOPED_VISIBILITIES and not roles:
            raise EngineError(
                f"{record_type} visible_roles must be non-empty for scoped visibility"
            )

    def _authorize(self, role: str, resource: str, action: str) -> None:
        if role == "system":
            return
        if role not in SELLER_ROLES:
            raise AuthorizationError(f"invalid external role: {role!r}")
        grant = self._role_grant(role)
        scopes = set(grant.resource_scopes)
        if not scopes.intersection(SCOPE_ACCESS.get(resource, frozenset())):
            raise AuthorizationError(f"role {role!r} has no scope for {resource}")
        rows = self.connection.execute(
            "SELECT action FROM grants WHERE role = ? AND resource = ?",
            (role, resource),
        ).fetchall()
        allowed = {str(row[0]) for row in rows}
        aliases = {
            "search": "read",
            "history": "read",
            "list": "read",
            "inbox": "read",
            "open": "read",
            "update": "write",
            "create": "write",
            "revise": "write",
            "attach": "write",
            "schedule": "write",
            "reschedule": "write",
            "cancel": "write",
            "merge": "merge",
            "send": "send_internal",
            "request": "request",
            "approve": "decide",
            "reject": "decide",
            "yield": "read",
            "complete_checkpoint": "complete_checkpoint",
        }
        required = aliases.get(action, action)
        if (
            resource == "crm"
            and action in {"update", "merge"}
            and not grant.can_write_crm
        ):
            raise AuthorizationError(f"role {role!r} cannot write CRM")
        if (
            resource == "communications"
            and action == "send_external"
            and not grant.can_contact_external
        ):
            raise AuthorizationError(
                f"role {role!r} cannot contact external recipients"
            )
        if (
            resource == "approvals"
            and action == "request"
            and not grant.can_request_approval
        ):
            raise AuthorizationError(f"role {role!r} cannot request approvals")
        if (
            resource == "approvals"
            and action in {"approve", "reject"}
            and not grant.can_approve_commercial
        ):
            raise AuthorizationError(
                f"role {role!r} cannot decide commercial approvals"
            )
        if (
            resource == "communications"
            and action == "send_internal"
            and "send" in allowed
        ):
            return
        if (
            action in {"search", "history", "list", "inbox", "read"}
            and "read" in allowed
        ):
            return
        if (
            required in allowed
            or action in allowed
            or "*" in allowed
            or (action in WRITE_ACTIONS and "write" in allowed)
        ):
            return
        raise AuthorizationError(f"role {role!r} cannot {action} on {resource}")

    def _role_grant(self, role: str) -> RoleGrant:
        row = self.connection.execute(
            "SELECT data FROM grants WHERE role = ? ORDER BY resource, action LIMIT 1",
            (role,),
        ).fetchone()
        if row is None:
            raise AuthorizationError(f"role {role!r} has no grant")
        return RoleGrant.from_dict(json.loads(str(row[0])))

    def grant(self, grant: RoleGrant) -> None:
        if (
            grant.role not in SELLER_ROLES
            or not grant.permissions
            or not grant.resource_scopes
        ):
            raise EngineError("role grant is invalid")
        flags = (
            grant.can_contact_external,
            grant.can_write_crm,
            grant.can_approve_commercial,
            grant.can_request_approval,
        )
        if any(not isinstance(flag, bool) for flag in flags):
            raise EngineError("role grant flags must be boolean")
        valid_scopes = set().union(*SCOPE_ACCESS.values())
        if any(scope not in valid_scopes for scope in grant.resource_scopes):
            raise EngineError("role grant contains an invalid resource scope")
        if grant.approval_limit_minor_units is not None and (
            isinstance(grant.approval_limit_minor_units, bool)
            or grant.approval_limit_minor_units < 0
        ):
            raise EngineError("approval limit must be a non-negative integer")
        self.connection.execute("DELETE FROM grants WHERE role = ?", (grant.role,))
        for permission in grant.permissions:
            if "." not in permission:
                raise EngineError(f"invalid permission: {permission}")
            resource, action = permission.split(".", 1)
            self.connection.execute(
                "INSERT OR REPLACE INTO grants(role, resource, action, data) VALUES (?, ?, ?, ?)",
                (grant.role, resource, action, to_json(grant)),
            )

    def _actor_for_recipient(self, recipient: str) -> dict[str, Any] | None:
        needle = recipient.casefold()
        for row in self.connection.execute("SELECT data FROM actors ORDER BY actor_id"):
            actor = json.loads(str(row[0]))
            if needle in {
                str(actor.get("actor_id", "")).casefold(),
                str(actor.get("email", "")).casefold(),
            }:
                return actor
        return None

    def _recipient_actors(
        self, role: str, recipients: Sequence[str] | str, allow_roles: bool = False
    ) -> list[dict[str, Any]]:
        values = _list(recipients)
        if not values or any(not isinstance(item, str) or not item for item in values):
            raise AuthorizationError("recipients must be a non-empty roster list")
        actors = []
        for recipient in values:
            if allow_roles and recipient in SELLER_ROLES:
                actors.append({"kind": "seller", "role_tags": [recipient]})
                continue
            actor = self._actor_for_recipient(recipient)
            if actor is None or not self._visible(
                str(actor.get("visibility", "public")), role, actor
            ):
                raise AuthorizationError("recipient is not available")
            if _time_value(str(actor["active_from"])) > _time_value(
                self.current_time
            ) or (
                actor.get("active_until")
                and _time_value(str(actor["active_until"]))
                < _time_value(self.current_time)
            ):
                raise AuthorizationError("recipient is not available")
            actors.append(actor)
        return actors

    def _require_external_contact(
        self, role: str, actors: Sequence[Mapping[str, Any]]
    ) -> None:
        if (
            any(actor.get("kind") != "seller" for actor in actors)
            and not self._role_grant(role).can_contact_external
        ):
            raise AuthorizationError(
                f"role {role!r} cannot contact external recipients"
            )

    @staticmethod
    def _semantic_envelope(value: Mapping[str, Any] | None) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise EngineError("semantic_envelope is required")
        required = {
            "purpose",
            "related_records",
            "requested_decisions",
            "commitments",
            "attachments",
        }
        if (
            set(value) != required
            or not isinstance(value.get("purpose"), str)
            or not value["purpose"]
        ):
            raise EngineError("semantic_envelope is invalid")
        for key in required - {"purpose"}:
            items = value.get(key)
            if (
                not isinstance(items, Sequence)
                or isinstance(items, (str, bytes))
                or any(not isinstance(item, str) for item in items)
            ):
                raise EngineError("semantic_envelope is invalid")
        return {
            "purpose": value["purpose"],
            **{key: list(value[key]) for key in sorted(required - {"purpose"})},
        }

    def record_tool_attempt(self, role: str) -> dict[str, Any]:
        if role not in SELLER_ROLES:
            raise AuthorizationError(f"invalid external role: {role!r}")
        checkpoint = self.current_checkpoint()
        if checkpoint is None or checkpoint.get("status") != "active":
            raise ToolLimitError("tool calls require an active checkpoint")
        checkpoint_id = str(checkpoint["checkpoint_id"])
        self.connection.execute(
            "INSERT INTO checkpoint_tool_usage(checkpoint_id, attempts) VALUES (?, 1) ON CONFLICT(checkpoint_id) DO UPDATE SET attempts = attempts + 1",
            (checkpoint_id,),
        )
        attempts = int(
            self.connection.execute(
                "SELECT attempts FROM checkpoint_tool_usage WHERE checkpoint_id = ?",
                (checkpoint_id,),
            ).fetchone()[0]
        )
        raw_limit = self.manifest.limits.get("tool_calls_per_checkpoint")
        if raw_limit is None:
            limit = None
        elif (
            isinstance(raw_limit, int)
            and not isinstance(raw_limit, bool)
            and raw_limit > 0
        ):
            limit = raw_limit
        else:
            raise EngineError("tool_calls_per_checkpoint must be positive or null")
        if limit is not None and attempts > limit:
            raise ToolLimitError(f"checkpoint tool-call cap of {limit} exceeded")
        return {"checkpoint_id": checkpoint_id, "attempts": attempts, "limit": limit}

    def append_event(self, event: Event) -> None:
        for timestamp in (event.effective_at, event.recorded_at, event.available_at):
            _parse_time(timestamp)
        self._validate_visibility(
            event.visibility,
            event.visible_roles,
            {"oracle_only", "agent_visible", "role_scoped"},
            "event",
        )
        value = to_json(event)
        digest = _hash(event)
        existing = self.connection.execute(
            "SELECT content_hash FROM events WHERE event_id = ?", (event.event_id,)
        ).fetchone()
        if existing is not None:
            if str(existing[0]) != digest:
                raise ImmutableError(
                    f"event {event.event_id!r} already exists with different content"
                )
            return
        self.connection.execute(
            "INSERT INTO events(event_id, available_at, visibility, data, content_hash) VALUES (?, ?, ?, ?, ?)",
            (event.event_id, event.available_at, event.visibility, value, digest),
        )

    def append_artifact(self, artifact: Artifact) -> None:
        _parse_time(artifact.created_at)
        _parse_time(artifact.available_at)
        self._validate_visibility(
            artifact.visibility,
            artifact.visible_roles,
            {"public", "agent_visible", "role_scoped", "oracle_only"},
            "artifact",
        )
        value = to_json(artifact)
        digest = _hash(artifact)
        existing = self.connection.execute(
            "SELECT content_hash FROM artifacts WHERE artifact_id = ?",
            (artifact.artifact_id,),
        ).fetchone()
        if existing is not None:
            if str(existing[0]) != digest:
                raise ImmutableError(
                    f"artifact {artifact.artifact_id!r} already exists with different content"
                )
            return
        self.connection.execute(
            "INSERT INTO artifacts(artifact_id, available_at, visibility, data, content_hash) VALUES (?, ?, ?, ?, ?)",
            (
                artifact.artifact_id,
                artifact.available_at,
                artifact.visibility,
                value,
                digest,
            ),
        )

    def _require_key(self, key: str | None) -> str:
        if not key:
            raise IdempotencyError("write operations require an idempotency key")
        return key

    def _idempotent(
        self, key: str | None, operation: str, callback: Callable[[], Any]
    ) -> Any:
        actual_key = self._require_key(key)
        row = self.connection.execute(
            "SELECT operation, result FROM idempotency WHERE key = ?", (actual_key,)
        ).fetchone()
        if row is not None:
            if str(row[0]) != operation:
                raise IdempotencyError(f"idempotency key already belongs to {row[0]}")
            return json.loads(str(row[1]))
        outer = self._transaction_depth == 0
        if outer:
            self.connection.execute("BEGIN")
        self._transaction_depth += 1
        committed = False
        try:
            result = callback()
            self.connection.execute(
                "INSERT INTO idempotency(key, operation, result) VALUES (?, ?, ?)",
                (
                    actual_key,
                    operation,
                    json.dumps(
                        result,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                ),
            )
            if outer:
                self.connection.execute("COMMIT")
                committed = True
                self._sync_trace_file()
            return result
        except Exception:
            if outer and not committed:
                self.connection.execute("ROLLBACK")
            raise
        finally:
            self._transaction_depth -= 1

    def _sync_trace_file(self) -> None:
        if self.trace_path is None:
            return
        self.trace_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix="trace-", suffix=".jsonl", dir=self.trace_path.parent
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                for row in self.connection.execute(
                    "SELECT raw FROM trace ORDER BY sequence"
                ):
                    stream.write(str(row[0]) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_name, self.trace_path)
        except Exception:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
            raise

    def _trace(
        self,
        kind: str,
        role: str,
        payload: Mapping[str, Any],
        idempotency_key: str | None = None,
        message_id: str | None = None,
        *,
        latency_ms: int | None = None,
        token_usage: Mapping[str, int] | None = None,
        cost_minor_units: int | None = None,
    ) -> TraceEvent:
        if kind not in TRACE_KINDS:
            raise EngineError(f"invalid trace kind: {kind!r}")
        if role not in SELLER_ROLES and role != "system":
            raise AuthorizationError(f"invalid trace actor role: {role!r}")
        if latency_ms is not None and (
            not isinstance(latency_ms, int)
            or isinstance(latency_ms, bool)
            or latency_ms < 0
        ):
            raise EngineError("trace latency_ms must be a non-negative integer")
        if cost_minor_units is not None and (
            not isinstance(cost_minor_units, int)
            or isinstance(cost_minor_units, bool)
            or cost_minor_units < 0
        ):
            raise EngineError("trace cost_minor_units must be a non-negative integer")
        if token_usage is not None:
            if any(
                not isinstance(value, int) or isinstance(value, bool) or value < 0
                for value in token_usage.values()
            ):
                raise EngineError(
                    "trace token_usage values must be non-negative integers"
                )
            token_usage = {str(key): int(value) for key, value in token_usage.items()}
        sequence = int(
            self.connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 FROM trace"
            ).fetchone()[0]
        )
        payload_value = dict(payload)
        payload_hash = (
            "sha256:"
            + hashlib.sha256(to_json(payload_value).encode("utf-8")).hexdigest()
        )
        event = TraceEvent(
            self.manifest.run_id,
            sequence,
            message_id or f"message-{sequence:06d}",
            self.current_time,
            kind,
            role,
            payload_hash,
            payload_value,
            idempotency_key,
            latency_ms,
            token_usage,
            cost_minor_units,
        )
        raw = to_json(event)
        outer = self._transaction_depth == 0
        if outer:
            self.connection.execute("BEGIN")
        try:
            self.connection.execute(
                "INSERT INTO trace(sequence, raw) VALUES (?, ?)", (event.sequence, raw)
            )
            if outer:
                self.connection.execute("COMMIT")
                self._sync_trace_file()
            return event
        except Exception:
            if outer:
                self.connection.execute("ROLLBACK")
            raise

    def trace_events(self) -> list[TraceEvent]:
        return [
            TraceEvent.from_dict(json.loads(str(row[0])))
            for row in self.connection.execute(
                "SELECT raw FROM trace ORDER BY sequence"
            )
        ]

    def dump_trace(self, path: str | Path) -> None:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("w", encoding="utf-8") as stream:
            for row in self.connection.execute(
                "SELECT raw FROM trace ORDER BY sequence"
            ):
                stream.write(str(row[0]) + "\n")

    def _canonical_state(self) -> dict[str, Any]:
        return canonical_database_state(self.connection)

    def _hash_state(self, state: Mapping[str, Any]) -> str:
        return canonical_database_hash(self.connection, state)

    def state_snapshot(self) -> dict[str, Any]:
        state = self._canonical_state()
        return {"state_hash": self._hash_state(state), "state": state}

    def state_hash(self) -> str:
        return str(self.state_snapshot()["state_hash"])

    def _save_snapshot(self) -> dict[str, Any]:
        snapshot = self.state_snapshot()
        sequence = int(
            self.connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) FROM trace"
            ).fetchone()[0]
        )
        previous = self.connection.execute(
            "SELECT state_hash, data FROM snapshots WHERE sequence < ? ORDER BY sequence DESC LIMIT 1",
            (sequence,),
        ).fetchone()
        previous_state_hash = None if previous is None else str(previous[0])
        previous_state = None if previous is None else json.loads(str(previous[1]))
        state_diff = _state_diff(previous_state, snapshot["state"])
        snapshot["previous_state_hash"] = previous_state_hash
        snapshot["state_diff"] = state_diff
        self.connection.execute(
            "INSERT OR REPLACE INTO snapshots(sequence, timestamp, checkpoint, state_hash, data, previous_state_hash, state_diff) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                sequence,
                self.current_time,
                self.current_checkpoint_index,
                snapshot["state_hash"],
                to_json(snapshot["state"]),
                previous_state_hash,
                to_json(state_diff),
            ),
        )
        return snapshot

    def snapshot(self) -> dict[str, Any]:
        snapshot = self._save_snapshot()
        self._trace(
            "observation",
            "system",
            {
                "snapshot_sequence": int(
                    self.connection.execute(
                        "SELECT COALESCE(MAX(sequence), 0) FROM trace"
                    ).fetchone()[0]
                )
            },
        )
        return snapshot

    def snapshots(self) -> list[dict[str, Any]]:
        return [
            {
                "sequence": int(row[0]),
                "timestamp": str(row[1]),
                "checkpoint": int(row[2]),
                "state_hash": str(row[3]),
                "state": json.loads(str(row[4])),
                "previous_state_hash": row[5],
                "state_diff": json.loads(str(row[6])),
            }
            for row in self.connection.execute(
                "SELECT sequence, timestamp, checkpoint, state_hash, data, previous_state_hash, state_diff FROM snapshots ORDER BY sequence"
            )
        ]

    def _rows(
        self, table: str, role: str, query: str = "", limit: int | None = None
    ) -> list[dict[str, Any]]:
        actual_limit = _validated_limit(limit)
        rows = self.connection.execute(
            f"SELECT * FROM {table} ORDER BY rowid"
        ).fetchall()
        needle = query.casefold()
        result: list[dict[str, Any]] = []
        for row in rows:
            values = dict(row)
            data = json.loads(str(values.get("data", "{}")))
            raw_visibility = values.get("visibility", data.get("visibility", "public"))
            try:
                visibility: str | Sequence[str]
                if isinstance(raw_visibility, Sequence) and not isinstance(
                    raw_visibility, (str, bytes)
                ):
                    visibility = raw_visibility
                else:
                    visibility = (
                        json.loads(str(raw_visibility))
                        if str(raw_visibility).startswith(("[", '"'))
                        else str(raw_visibility)
                    )
            except json.JSONDecodeError:
                visibility = str(raw_visibility)
            available_at = str(
                values.get(
                    "available_at",
                    data.get(
                        "available_at", values.get("updated_at", self.current_time)
                    ),
                )
            )
            if _time_value(available_at) > _time_value(
                self.current_time
            ) or not self._visible(visibility, role, data):
                continue
            haystack = json.dumps(data, ensure_ascii=False, sort_keys=True).casefold()
            if needle and needle not in haystack:
                continue
            result.append(data)
            if actual_limit is not None and len(result) >= actual_limit:
                break
        return result

    def events(
        self, role: str = "system", query: str = "", limit: int | None = None
    ) -> list[dict[str, Any]]:
        self._authorize(role, "run", "read")
        if role == "system":
            return self._rows("events", role, query, limit)
        actual_limit = _validated_limit(limit)
        rows = self._rows("events", role)
        visible = []
        for event in rows:
            payload = event.get("payload")
            if isinstance(payload, Mapping):
                event = {
                    **event,
                    "payload": {
                        key: value
                        for key, value in payload.items()
                        if key not in {"lane_effects", "causal_effects"}
                    },
                }
            visible.append(event)
        needle = query.casefold()
        if needle:
            visible = [
                event
                for event in visible
                if needle
                in json.dumps(event, ensure_ascii=False, sort_keys=True).casefold()
            ]
        return visible if actual_limit is None else visible[:actual_limit]

    def artifacts(
        self, role: str = "system", query: str = "", limit: int | None = None
    ) -> list[dict[str, Any]]:
        self._authorize(role, "documents", "read")
        return self._rows("artifacts", role, query, limit)

    def checkpoints(self) -> list[dict[str, Any]]:
        return [
            json.loads(str(row[0]))
            for row in self.connection.execute(
                "SELECT data FROM checkpoints ORDER BY position"
            )
        ]

    def current_checkpoint(self) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT data FROM checkpoints WHERE position = ?",
            (self.current_checkpoint_index,),
        ).fetchone()
        return None if row is None else json.loads(str(row[0]))

    def _set_checkpoint_status(self, position: int, status: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT data FROM checkpoints WHERE position = ?", (position,)
        ).fetchone()
        if row is None:
            raise EngineError("checkpoint does not exist")
        checkpoint = json.loads(str(row[0]))
        checkpoint["status"] = status
        self.connection.execute(
            "UPDATE checkpoints SET data = ? WHERE position = ?",
            (to_json(checkpoint), position),
        )
        return checkpoint

    def complete_checkpoint(
        self,
        role: str,
        checkpoint_id: str,
        summary: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        if role not in SELLER_ROLES:
            raise AuthorizationError(f"invalid external role: {role!r}")
        self._authorize(role, "run", "complete_checkpoint")
        if not summary:
            raise EngineError("checkpoint summary is required")

        def complete() -> dict[str, Any]:
            checkpoint = self.current_checkpoint()
            if checkpoint is None or checkpoint["checkpoint_id"] != checkpoint_id:
                raise EngineError("checkpoint is not active")
            if checkpoint["status"] != "active":
                raise EngineError("checkpoint is not active")
            if role not in checkpoint["required_roles"]:
                raise AuthorizationError("role is not required for this checkpoint")
            self.connection.execute(
                "INSERT OR REPLACE INTO checkpoint_completions(checkpoint_id, role, summary, completed_at) VALUES (?, ?, ?, ?)",
                (checkpoint_id, role, summary, self.current_time),
            )
            result = {
                "checkpoint_id": checkpoint_id,
                "role": role,
                "summary": summary,
                "completed_at": self.current_time,
            }
            self._trace("checkpoint_complete", role, result, idempotency_key)
            return result

        return self._idempotent(idempotency_key, "run.complete_checkpoint", complete)

    def _required_roles_complete(self, checkpoint: Mapping[str, Any]) -> bool:
        required = set(checkpoint.get("required_roles", ()))
        completed = {
            str(row[0])
            for row in self.connection.execute(
                "SELECT role FROM checkpoint_completions WHERE checkpoint_id = ?",
                (checkpoint["checkpoint_id"],),
            )
        }
        return required.issubset(completed)

    def advance_checkpoint(
        self, budget_exhausted: bool = False, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        if not isinstance(budget_exhausted, bool):
            raise AuthorizationError("checkpoint advancement is system-only")

        def advance() -> dict[str, Any]:
            next_position = self.current_checkpoint_index + 1
            current = self.current_checkpoint()
            if current is not None:
                if not self._required_roles_complete(current) and not budget_exhausted:
                    raise EngineError(
                        "required roles have not completed the active checkpoint"
                    )
                self._set_checkpoint_status(
                    self.current_checkpoint_index,
                    "complete" if self._required_roles_complete(current) else "failed",
                )
            row = self.connection.execute(
                "SELECT data FROM checkpoints WHERE position = ?", (next_position,)
            ).fetchone()
            if row is None:
                raise EngineError("no checkpoint remains")
            checkpoint = json.loads(str(row[0]))
            self._set_clock(str(checkpoint["available_at"]))
            self._set_meta("current_checkpoint", str(next_position))
            checkpoint = self._set_checkpoint_status(next_position, "active")
            released_event_ids = self.release_available_events()
            result = {
                "checkpoint": checkpoint,
                "current_time": self.current_time,
                "status": self.status,
                "budget_exhausted": budget_exhausted,
                "released_event_ids": released_event_ids,
            }
            self._save_snapshot()
            self._trace("observation", "system", {"checkpoint_advanced": result})
            return result

        return self._idempotent(idempotency_key, "run.advance", advance)

    def run_status(self, role: str = "system") -> dict[str, Any]:
        self._authorize(role, "run", "read")
        result = {
            "run_id": self.manifest.run_id,
            "world_id": self.manifest.world_id,
            "current_time": self.current_time,
            "current_checkpoint": self.current_checkpoint_index,
            "checkpoint": self.current_checkpoint(),
            "status": self.status,
            "state_hash": self.state_hash(),
            "terminal_outcome": self._meta("terminal_outcome"),
        }
        return result

    def run_yield(self, role: str) -> dict[str, Any]:
        self._authorize(role, "run", "yield")
        return {
            "status": "yielded",
            "current_time": self.current_time,
            "checkpoint": self.current_checkpoint_index,
        }

    def run_complete(
        self,
        status: str = "completed",
        result: Mapping[str, Any] | None = None,
        reason: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        if status not in {"completed", "failed", "invalid"}:
            raise EngineError("run status must be completed, failed, or invalid")

        def complete() -> dict[str, Any]:
            checkpoint = self.current_checkpoint()
            if (
                status == "completed"
                and checkpoint is not None
                and not checkpoint.get("terminal", False)
            ):
                raise EngineError(
                    "terminal checkpoint is required before completing the run"
                )
            terminal = self.finalize_terminal_outcome() if status == "completed" else {}
            self._set_meta("status", status)
            self._set_meta("ended_at", self.current_time)
            payload = {
                "status": status,
                "result": {**dict(result or {}), **terminal},
                "current_time": self.current_time,
            }
            if reason is not None:
                payload["reason"] = reason
            self._save_snapshot()
            self._trace("run_end", "system", payload, idempotency_key)
            return payload

        return self._idempotent(
            idempotency_key or f"run-end-{status}", "run.complete", complete
        )

    def seed_crm_record(
        self, record_id: str, data: Mapping[str, Any], updated_at: str | None = None
    ) -> dict[str, Any]:
        value = dict(data)
        value.setdefault("record_id", record_id)
        timestamp = updated_at or self.current_time
        _parse_time(timestamp)
        self.connection.execute(
            "INSERT OR REPLACE INTO crm_records(record_id, data, updated_at, version) VALUES (?, ?, ?, ?)",
            (record_id, to_json(value), timestamp, 1),
        )
        self.connection.execute(
            "INSERT INTO crm_history(record_id, changed_at, role, changes, snapshot) VALUES (?, ?, ?, ?, ?)",
            (record_id, timestamp, "system", to_json({"seed": True}), to_json(value)),
        )
        return value

    def crm_search(
        self, role: str, query: str = "", limit: int | None = None
    ) -> list[dict[str, Any]]:
        self._authorize(role, "crm", "search")
        return self._rows("crm_records", role, query, limit)

    def crm_read(self, role: str, record_id: str) -> dict[str, Any]:
        self._authorize(role, "crm", "read")
        row = self.connection.execute(
            "SELECT data, updated_at, version FROM crm_records WHERE record_id = ?",
            (record_id,),
        ).fetchone()
        if row is None:
            raise EngineError(f"CRM record {record_id!r} not found")
        data = json.loads(str(row[0]))
        visibility = data.get("visibility", "public")
        available_at = str(data.get("available_at", row[1]))
        if _time_value(available_at) > _time_value(
            self.current_time
        ) or not self._visible(visibility, role, data):
            raise EngineError(f"CRM record {record_id!r} not found")
        return {"record": data, "updated_at": str(row[1]), "version": int(row[2])}

    def crm_history(self, role: str, record_id: str) -> list[dict[str, Any]]:
        self._authorize(role, "crm", "read")
        self.crm_read(role, record_id)
        result = [
            {
                "record_id": str(row[0]),
                "changed_at": str(row[1]),
                "role": str(row[2]),
                "changes": json.loads(str(row[3])),
                "snapshot": json.loads(str(row[4])),
            }
            for row in self.connection.execute(
                "SELECT record_id, changed_at, role, changes, snapshot FROM crm_history WHERE record_id = ? ORDER BY history_id",
                (record_id,),
            )
        ]
        return result

    def crm_update(
        self,
        role: str,
        record_id: str,
        changes: Mapping[str, Any],
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        self._authorize(role, "crm", "update")

        def update() -> dict[str, Any]:
            row = self.connection.execute(
                "SELECT data, version FROM crm_records WHERE record_id = ?",
                (record_id,),
            ).fetchone()
            if row is None:
                raise EngineError(f"CRM record {record_id!r} not found")
            data = json.loads(str(row[0]))
            if "record_id" in changes and str(changes["record_id"]) != record_id:
                raise EngineError("record_id cannot be changed")
            data.update(dict(changes))
            version = int(row[1]) + 1
            self.connection.execute(
                "UPDATE crm_records SET data = ?, updated_at = ?, version = ? WHERE record_id = ?",
                (to_json(data), self.current_time, version, record_id),
            )
            self.connection.execute(
                "INSERT INTO crm_history(record_id, changed_at, role, changes, snapshot) VALUES (?, ?, ?, ?, ?)",
                (
                    record_id,
                    self.current_time,
                    role,
                    to_json(dict(changes)),
                    to_json(data),
                ),
            )
            return {"record": data, "updated_at": self.current_time, "version": version}

        return self._idempotent(idempotency_key, "crm.update", update)

    def crm_merge(
        self,
        role: str,
        source_id: str,
        target_id: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        self._authorize(role, "crm", "merge")
        if source_id == target_id:
            raise EngineError("CRM merge requires distinct records")

        def merge() -> dict[str, Any]:
            source = self.crm_read(role, source_id)["record"]
            target = self.crm_read(role, target_id)["record"]
            merged = dict(source)
            merged.update(target)
            merged["merged_from"] = source_id
            result = self.crm_update(
                role,
                target_id,
                merged,
                _stable_id(
                    "merge-update", self.manifest.run_id, idempotency_key or source_id
                ),
            )
            source_snapshot = {**source, "merged_into": target_id}
            self.connection.execute(
                "UPDATE crm_records SET data = ?, updated_at = ?, version = version + 1 WHERE record_id = ?",
                (to_json(source_snapshot), self.current_time, source_id),
            )
            self.connection.execute(
                "INSERT INTO crm_history(record_id, changed_at, role, changes, snapshot) VALUES (?, ?, ?, ?, ?)",
                (
                    source_id,
                    self.current_time,
                    role,
                    to_json({"merged_into": target_id}),
                    to_json(source_snapshot),
                ),
            )
            return {**result, "source_id": source_id, "target_id": target_id}

        return self._idempotent(idempotency_key, "crm.merge", merge)

    def seed_communication(
        self, message_id: str, data: Mapping[str, Any]
    ) -> dict[str, Any]:
        value = dict(data)
        value.setdefault("message_id", message_id)
        value.setdefault("created_at", self.current_time)
        value.setdefault("available_at", self.current_time)
        value.setdefault("visibility", [])
        _parse_time(str(value["created_at"]))
        _parse_time(str(value["available_at"]))
        self.connection.execute(
            "INSERT OR REPLACE INTO communications(message_id, channel, direction, sender_role, recipients, subject, body, created_at, available_at, visibility, metadata) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                message_id,
                value.get("channel", "email"),
                value.get("direction", "inbound"),
                value.get("sender_role", "external"),
                to_json(_list(value.get("recipients"))),
                value.get("subject", ""),
                value.get("body", ""),
                value["created_at"],
                value["available_at"],
                to_json(_list(value.get("visibility"))),
                to_json(value.get("metadata", {})),
            ),
        )
        return value

    def communications_search(
        self,
        role: str,
        query: str = "",
        channel: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        self._authorize(role, "communications", "search")
        actual_limit = _validated_limit(limit)
        clauses: list[str] = []
        params: list[Any] = []
        if channel:
            clauses.append("channel = ?")
            params.append(channel)
        rows = self.connection.execute(
            "SELECT * FROM communications"
            + (" WHERE " + " AND ".join(clauses) if clauses else "")
            + " ORDER BY created_at, message_id",
            params,
        ).fetchall()
        needle = query.casefold()
        result: list[dict[str, Any]] = []
        for row in rows:
            if _time_value(str(row["available_at"])) > _time_value(
                self.current_time
            ) or not self._visible(json.loads(str(row["visibility"])), role):
                continue
            value = {
                "message_id": row["message_id"],
                "channel": row["channel"],
                "direction": row["direction"],
                "sender_role": row["sender_role"],
                "recipients": json.loads(str(row["recipients"])),
                "subject": row["subject"],
                "body": row["body"],
                "created_at": row["created_at"],
                "available_at": row["available_at"],
                "metadata": json.loads(str(row["metadata"])),
            }
            if (
                needle
                and needle not in json.dumps(value, ensure_ascii=False).casefold()
            ):
                continue
            result.append(value)
            if actual_limit is not None and len(result) >= actual_limit:
                break
        return result

    def communications_read(self, role: str, message_id: str) -> dict[str, Any]:
        self._authorize(role, "communications", "read")
        result = self.communications_search(role, message_id, limit=1)
        if not result or result[0]["message_id"] != message_id:
            raise EngineError(f"communication {message_id!r} not found")
        return result[0]

    def communications_send(
        self,
        role: str,
        channel: str,
        recipients: Sequence[str] | str,
        subject: str,
        body: str,
        idempotency_key: str | None = None,
        visibility: Sequence[str] = SELLER_ROLES,
        metadata: Mapping[str, Any] | None = None,
        semantic_envelope: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if channel not in {"email", "internal_chat"}:
            raise EngineError("communication channel must be email or internal_chat")
        actors = self._recipient_actors(
            role, recipients, allow_roles=channel == "internal_chat"
        )
        external = any(actor.get("kind") != "seller" for actor in actors)
        if channel == "internal_chat" and external:
            raise AuthorizationError("internal chat recipients must be seller roles")
        self._authorize(
            role, "communications", "send_external" if external else "send_internal"
        )
        self._require_external_contact(role, actors)
        envelope = self._semantic_envelope(semantic_envelope) if external else None

        def send() -> dict[str, Any]:
            message_id = _stable_id(
                "message", self.manifest.run_id, idempotency_key or body
            )
            message_metadata = dict(metadata or {})
            if envelope is not None:
                message_metadata["semantic_envelope"] = envelope
            value = {
                "message_id": message_id,
                "channel": channel,
                "direction": "outbound",
                "sender_role": role,
                "recipients": _list(recipients),
                "subject": subject,
                "body": body,
                "created_at": self.current_time,
                "available_at": self.current_time,
                "visibility": list(visibility),
                "metadata": message_metadata,
            }
            self.seed_communication(message_id, value)
            return value

        return self._idempotent(idempotency_key, "communications.send", send)

    def seed_calendar_event(
        self, calendar_id: str, data: Mapping[str, Any]
    ) -> dict[str, Any]:
        value = dict(data)
        value.setdefault("calendar_id", calendar_id)
        value.setdefault("available_at", self.current_time)
        value.setdefault("visibility", [])
        _parse_time(str(value["available_at"]))
        if value.get("start_at") is not None:
            _parse_time(str(value["start_at"]))
        if value.get("end_at") is not None:
            _parse_time(str(value["end_at"]))
        self.connection.execute(
            "INSERT OR REPLACE INTO calendar_events(calendar_id, data, available_at, visibility) VALUES (?, ?, ?, ?)",
            (
                calendar_id,
                to_json(value),
                value["available_at"],
                to_json(_list(value.get("visibility"))),
            ),
        )
        return value

    def calendar_list(
        self, role: str, limit: int | None = None
    ) -> list[dict[str, Any]]:
        self._authorize(role, "calendar", "list")
        return self._rows("calendar_events", role, limit=limit)

    def calendar_schedule(
        self,
        role: str,
        subject: str,
        start_at: str,
        end_at: str,
        participants: Sequence[str] | str,
        description: str = "",
        idempotency_key: str | None = None,
        visibility: Sequence[str] = SELLER_ROLES,
        semantic_envelope: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._authorize(role, "calendar", "schedule")
        actors = self._recipient_actors(role, participants, allow_roles=True)
        self._require_external_contact(role, actors)
        envelope = self._semantic_envelope(semantic_envelope)

        def schedule() -> dict[str, Any]:
            if _time_value(end_at) < _time_value(start_at):
                raise EngineError("calendar end must not precede start")
            calendar_id = _stable_id(
                "calendar", self.manifest.run_id, idempotency_key or subject
            )
            value = {
                "calendar_id": calendar_id,
                "subject": subject,
                "start_at": start_at,
                "end_at": end_at,
                "participants": _list(participants),
                "description": description,
                "status": "scheduled",
                "organizer_role": role,
                "available_at": self.current_time,
                "visibility": list(visibility),
                "semantic_envelope": envelope,
            }
            self.seed_calendar_event(calendar_id, value)
            return value

        return self._idempotent(idempotency_key, "calendar.schedule", schedule)

    def _calendar_event(self, calendar_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT data FROM calendar_events WHERE calendar_id = ?", (calendar_id,)
        ).fetchone()
        if row is None:
            raise EngineError(f"calendar event {calendar_id!r} not found")
        return json.loads(str(row[0]))

    def calendar_reschedule(
        self,
        role: str,
        calendar_id: str,
        start_at: str,
        end_at: str,
        semantic_envelope: Mapping[str, Any],
        participants: Sequence[str] | str | None = None,
        subject: str | None = None,
        description: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        self._authorize(role, "calendar", "reschedule")
        existing = self._calendar_event(calendar_id)
        participant_values = (
            existing.get("participants", ()) if participants is None else participants
        )
        actors = self._recipient_actors(role, participant_values, allow_roles=True)
        self._require_external_contact(role, actors)
        envelope = self._semantic_envelope(semantic_envelope)

        def reschedule() -> dict[str, Any]:
            if _time_value(end_at) < _time_value(start_at):
                raise EngineError("calendar end must not precede start")
            value = dict(existing)
            value.update(
                {
                    "start_at": start_at,
                    "end_at": end_at,
                    "participants": _list(participant_values),
                    "semantic_envelope": envelope,
                    "rescheduled_at": self.current_time,
                    "rescheduled_by": role,
                }
            )
            if subject is not None:
                value["subject"] = subject
            if description is not None:
                value["description"] = description
            self.connection.execute(
                "UPDATE calendar_events SET data = ? WHERE calendar_id = ?",
                (to_json(value), calendar_id),
            )
            return value

        return self._idempotent(idempotency_key, "calendar.reschedule", reschedule)

    def calendar_cancel(
        self,
        role: str,
        calendar_id: str,
        semantic_envelope: Mapping[str, Any],
        reason: str = "",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        self._authorize(role, "calendar", "cancel")
        existing = self._calendar_event(calendar_id)
        actors = self._recipient_actors(
            role, existing.get("participants", ()), allow_roles=True
        )
        self._require_external_contact(role, actors)
        envelope = self._semantic_envelope(semantic_envelope)

        def cancel() -> dict[str, Any]:
            value = {
                **existing,
                "status": "cancelled",
                "cancel_reason": reason,
                "cancelled_at": self.current_time,
                "cancelled_by": role,
                "semantic_envelope": envelope,
            }
            self.connection.execute(
                "UPDATE calendar_events SET data = ? WHERE calendar_id = ?",
                (to_json(value), calendar_id),
            )
            return value

        return self._idempotent(idempotency_key, "calendar.cancel", cancel)

    def seed_document(
        self, document_id: str, data: Mapping[str, Any]
    ) -> dict[str, Any]:
        value = dict(data)
        value.setdefault("document_id", document_id)
        value.setdefault("version", 1)
        value.setdefault("created_at", self.current_time)
        value.setdefault("available_at", self.current_time)
        value.setdefault("visibility", [])
        _parse_time(str(value["created_at"]))
        _parse_time(str(value["available_at"]))
        self.connection.execute(
            "INSERT OR REPLACE INTO documents(document_id, data, available_at, visibility, version) VALUES (?, ?, ?, ?, ?)",
            (
                document_id,
                to_json(value),
                value["available_at"],
                to_json(_list(value.get("visibility"))),
                int(value["version"]),
            ),
        )
        self.connection.execute(
            "INSERT OR REPLACE INTO document_versions(document_id, version, data) VALUES (?, ?, ?)",
            (document_id, int(value["version"]), to_json(value)),
        )
        return value

    def documents_search(
        self, role: str, query: str = "", limit: int | None = None
    ) -> list[dict[str, Any]]:
        self._authorize(role, "documents", "search")
        return self._rows("documents", role, query, limit)

    def documents_read(self, role: str, document_id: str) -> dict[str, Any]:
        self._authorize(role, "documents", "read")
        result = self.documents_search(role, document_id, limit=1)
        if not result or result[0]["document_id"] != document_id:
            raise EngineError(f"document {document_id!r} not found")
        return result[0]

    def documents_create(
        self,
        role: str,
        title: str,
        content: str,
        kind: str = "document",
        idempotency_key: str | None = None,
        visibility: Sequence[str] = SELLER_ROLES,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._authorize(role, "documents", "create")

        def create() -> dict[str, Any]:
            document_id = _stable_id(
                "document", self.manifest.world_id, idempotency_key or title
            )
            value = {
                "document_id": document_id,
                "title": title,
                "content": content,
                "kind": kind,
                "version": 1,
                "created_at": self.current_time,
                "available_at": self.current_time,
                "visibility": list(visibility),
                "metadata": dict(metadata or {}),
                "author_role": role,
            }
            self.seed_document(document_id, value)
            return value

        return self._idempotent(idempotency_key, "documents.create", create)

    def documents_revise(
        self,
        role: str,
        document_id: str,
        content: str,
        metadata: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        self._authorize(role, "documents", "revise")

        def revise() -> dict[str, Any]:
            row = self.connection.execute(
                "SELECT data, version FROM documents WHERE document_id = ?",
                (document_id,),
            ).fetchone()
            if row is None:
                raise EngineError(f"document {document_id!r} not found")
            value = json.loads(str(row[0]))
            version = int(row[1]) + 1
            value.update(
                {
                    "content": content,
                    "version": version,
                    "revised_at": self.current_time,
                    "revised_by": role,
                }
            )
            if metadata:
                value["metadata"] = {**value.get("metadata", {}), **dict(metadata)}
            self.connection.execute(
                "UPDATE documents SET data = ?, version = ? WHERE document_id = ?",
                (to_json(value), version, document_id),
            )
            self.connection.execute(
                "INSERT INTO document_versions(document_id, version, data) VALUES (?, ?, ?)",
                (document_id, version, to_json(value)),
            )
            return value

        return self._idempotent(idempotency_key, "documents.revise", revise)

    def documents_attach(
        self,
        role: str,
        document_id: str,
        related_type: str,
        related_id: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        self._authorize(role, "documents", "attach")

        def attach() -> dict[str, Any]:
            self.documents_read(role, document_id)
            self.connection.execute(
                "INSERT OR IGNORE INTO document_links(document_id, related_type, related_id) VALUES (?, ?, ?)",
                (document_id, related_type, related_id),
            )
            result = {
                "document_id": document_id,
                "related_type": related_type,
                "related_id": related_id,
            }
            return result

        return self._idempotent(idempotency_key, "documents.attach", attach)

    def seed_approval(
        self, approval_id: str, data: Mapping[str, Any]
    ) -> dict[str, Any]:
        value = dict(data)
        value.setdefault("approval_id", approval_id)
        value.setdefault("status", "pending")
        value.setdefault("created_at", self.current_time)
        value.setdefault("visibility", [])
        _parse_time(str(value["created_at"]))
        existing = self.connection.execute(
            "SELECT data FROM approvals WHERE approval_id = ?", (approval_id,)
        ).fetchone()
        if existing is not None:
            current = json.loads(str(existing[0]))
            if current.get("status") in TERMINAL_APPROVAL_STATUSES and current != value:
                raise ImmutableError(f"approval {approval_id!r} is terminal")
            if current == value:
                return current
        self.connection.execute(
            "INSERT OR REPLACE INTO approvals(approval_id, data, updated_at, visibility) VALUES (?, ?, ?, ?)",
            (
                approval_id,
                to_json(value),
                value["created_at"],
                to_json(_list(value.get("visibility"))),
            ),
        )
        return value

    def approvals_list(
        self, role: str, status: str | None = None, limit: int | None = None
    ) -> list[dict[str, Any]]:
        self._authorize(role, "approvals", "list")
        actual_limit = _validated_limit(limit)
        result = self._rows("approvals", role)
        if status:
            result = [item for item in result if item.get("status") == status]
        return result if actual_limit is None else result[:actual_limit]

    def approvals_request(
        self,
        role: str,
        approver_role: str,
        purpose: str,
        details: Mapping[str, Any],
        idempotency_key: str | None = None,
        visibility: Sequence[str] = SELLER_ROLES,
    ) -> dict[str, Any]:
        self._authorize(role, "approvals", "request")
        if approver_role not in {*SELLER_ROLES, "system"}:
            raise EngineError("approver_role is invalid")

        def request() -> dict[str, Any]:
            approval_id = _stable_id(
                "approval", self.manifest.world_id, idempotency_key or purpose
            )
            value = {
                "approval_id": approval_id,
                "requester_role": role,
                "approver_role": approver_role,
                "purpose": purpose,
                "details": dict(details),
                "status": "pending",
                "created_at": self.current_time,
                "visibility": list(visibility),
            }
            self.seed_approval(approval_id, value)
            return value

        return self._idempotent(idempotency_key, "approvals.request", request)

    def _approvals_decide(
        self,
        role: str,
        approval_id: str,
        decision: str,
        note: str,
        idempotency_key: str | None,
    ) -> dict[str, Any]:
        action = "approve" if decision == "approved" else "reject"
        self._authorize(role, "approvals", action)

        def decide() -> dict[str, Any]:
            row = self.connection.execute(
                "SELECT data FROM approvals WHERE approval_id = ?", (approval_id,)
            ).fetchone()
            if row is None:
                raise EngineError(f"approval {approval_id!r} not found")
            value = json.loads(str(row[0]))
            if value.get("status") in TERMINAL_APPROVAL_STATUSES:
                raise ImmutableError(f"approval {approval_id!r} is terminal")
            if value.get("approver_role") not in {role, "*"}:
                raise AuthorizationError("role is not the requested approver")
            if decision == "approved":
                amount = value.get("details", {}).get("amount_minor_units", 0)
                if (
                    not isinstance(amount, int)
                    or isinstance(amount, bool)
                    or amount < 0
                ):
                    raise EngineError("approval amount is invalid")
                limit = self._role_grant(role).approval_limit_minor_units
                if limit is not None and amount > limit:
                    raise AuthorizationError("approval amount exceeds the role limit")
            value.update(
                {
                    "status": decision,
                    "decision": decision,
                    "note": note,
                    "responded_at": self.current_time,
                    "responded_by": role,
                }
            )
            self.connection.execute(
                "UPDATE approvals SET data = ?, updated_at = ? WHERE approval_id = ?",
                (to_json(value), self.current_time, approval_id),
            )
            return value

        return self._idempotent(idempotency_key, f"approvals.{action}", decide)

    def approvals_approve(
        self,
        role: str,
        approval_id: str,
        note: str = "",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return self._approvals_decide(
            role, approval_id, "approved", note, idempotency_key
        )

    def approvals_reject(
        self,
        role: str,
        approval_id: str,
        note: str = "",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return self._approvals_decide(
            role, approval_id, "rejected", note, idempotency_key
        )

    def seed_web_record(
        self, record_id: str, data: Mapping[str, Any]
    ) -> dict[str, Any]:
        value = dict(data)
        value.setdefault("record_id", record_id)
        value.setdefault("available_at", self.current_time)
        value.setdefault("visibility", [])
        _parse_time(str(value["available_at"]))
        self.connection.execute(
            "INSERT OR REPLACE INTO web_records(record_id, data, available_at, visibility) VALUES (?, ?, ?, ?)",
            (
                record_id,
                to_json(value),
                value["available_at"],
                to_json(_list(value.get("visibility"))),
            ),
        )
        return value

    def web_search(
        self, role: str, query: str = "", limit: int | None = None
    ) -> list[dict[str, Any]]:
        self._authorize(role, "web", "search")
        return self._rows("web_records", role, query, limit)

    def web_open(self, role: str, record_id: str) -> dict[str, Any]:
        self._authorize(role, "web", "open")
        result = self.web_search(role, record_id, limit=1)
        if not result or result[0]["record_id"] != record_id:
            raise EngineError(f"web record {record_id!r} not found")
        return result[0]

    def seed_team_message(
        self, message_id: str, data: Mapping[str, Any]
    ) -> dict[str, Any]:
        value = dict(data)
        value.setdefault("message_id", message_id)
        value.setdefault("created_at", self.current_time)
        value.setdefault("available_at", self.current_time)
        value.setdefault("visibility", [])
        _parse_time(str(value["created_at"]))
        _parse_time(str(value["available_at"]))
        self.connection.execute(
            "INSERT OR REPLACE INTO team_messages(message_id, data, available_at, visibility) VALUES (?, ?, ?, ?)",
            (
                message_id,
                to_json(value),
                value["available_at"],
                to_json(_list(value.get("visibility"))),
            ),
        )
        return value

    def team_search(
        self, role: str, query: str = "", limit: int | None = None
    ) -> list[dict[str, Any]]:
        self._authorize(role, "team", "search")
        return self._rows("team_messages", role, query, limit)

    def team_inbox(self, role: str, limit: int | None = None) -> list[dict[str, Any]]:
        self._authorize(role, "team", "read")
        return self._rows("team_messages", role, limit=limit)

    def team_send(
        self,
        role: str,
        recipients: Sequence[str] | str,
        body: str,
        idempotency_key: str | None = None,
        visibility: Sequence[str] = SELLER_ROLES,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._authorize(role, "team", "send")
        values = _list(recipients)
        if not values or any(item not in SELLER_ROLES for item in values):
            raise AuthorizationError("team recipients must be seller roles")

        def send() -> dict[str, Any]:
            message_id = _stable_id(
                "team", self.manifest.run_id, idempotency_key or body
            )
            value = {
                "message_id": message_id,
                "sender_role": role,
                "recipients": values,
                "body": body,
                "created_at": self.current_time,
                "available_at": self.current_time,
                "visibility": list(visibility),
                "metadata": dict(metadata or {}),
            }
            self.seed_team_message(message_id, value)
            return value

        return self._idempotent(idempotency_key, "team.send", send)


Engine = RunEngine


__all__ = [
    "SELLER_ROLES",
    "AuthorizationError",
    "Engine",
    "EngineError",
    "IdempotencyError",
    "ImmutableError",
    "RunEngine",
    "canonical_database_hash",
    "canonical_database_state",
    "canonical_trace_hash",
]
