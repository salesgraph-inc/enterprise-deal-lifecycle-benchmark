from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import sys
import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Self

from .causal import (
    LANE_DEFAULTS,
    LANES,
    ActionEffectRule,
    BranchDefinition,
    BranchResolution,
    MilestoneDefinition,
    MilestoneResolution,
    action_effect_rule,
    branch_definition,
    branch_resolution,
    digest,
    lane_status,
    milestone_definition,
    milestone_resolution,
    normalize_official_seeds,
    realization_cache_key,
    realization_packet,
    realize,
    select_stakeholder_act,
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
SEMANTIC_PURPOSE_LABELS = {
    "advance_gate": "advance the supported gate",
    "close_won": "record the accepted close and delivery handoff",
    "coordinate_meeting": "coordinate a meeting",
    "record_closed_lost": "record the supported closed-lost disposition",
    "record_disqualified": "record the supported disqualification",
    "record_no_decision": "record the supported no-decision disposition",
    "recover_gate": "request an evidence-backed remediation decision",
    "request_information": "request missing information",
    "share_document": "share supporting material",
    "update_account": "update the account record",
}
WRITE_SCOPE_CLASSIFICATIONS = frozenset(
    {"checkpoint_completion", "checkpoint_coordination"}
)
WRITE_SCOPE_MODES = {
    "approvals.approve": "result_envelope",
    "approvals.reject": "result_envelope",
    "approvals.request": "argument_envelope",
    "calendar.cancel": "argument_envelope",
    "calendar.reschedule": "argument_envelope",
    "calendar.schedule": "argument_envelope",
    "communications.send": "argument_envelope",
    "crm.merge": "argument_records",
    "crm.update": "argument_record",
    "documents.attach": "argument_related",
    "documents.create": "argument_envelope",
    "documents.revise": "argument_envelope",
    "run.complete_checkpoint": "checkpoint_completion",
    "team.send": "checkpoint_coordination",
}
SEMANTIC_DECISION_LABELS = {
    "confirm_attendance": "confirm attendance",
    "confirm_closing_authority": "confirm closing authority",
    "confirm_deferred_disposition": "confirm the deferred disposition",
    "confirm_gate_authority": "confirm accountable gate authority",
    "confirm_remedied_disposition": "confirm the remedied disposition",
    "confirm_rejected_disposition": "confirm the rejected disposition",
    "request_information": "provide the requested information",
    "request_remediation_decision": "confirm the remediation decision",
}
SEMANTIC_COMMITMENT_LABELS = {
    "defer_outreach": "defer outreach",
    "follow_up": "follow up",
    "handoff_delivery": "hand the accepted scope to delivery",
    "provide_information": "provide the requested information",
    "record_before_advancing": "record the decision before advancing",
    "complete_remediation": "complete the documented remediation",
    "stop_pursuit": "stop active pursuit",
}
AGENT_HIDDEN_FIELDS = frozenset(
    {
        "allowed_state_diff_targets",
        "approval_exception",
        "approval_required",
        "branch_id",
        "branch_ids",
        "branch_option",
        "artifact_key",
        "artifact_role",
        "author_role_id",
        "authoritative_for",
        "authority_actor_id",
        "authority_actor_ids",
        "authority_role_ids",
        "authority_rights",
        "causal_effects",
        "causal_skeleton",
        "decision_owner_actor_id",
        "decision_owner_actor_ids",
        "decision_route",
        "effect_id",
        "effect_ids",
        "evidence_refs",
        "family",
        "forecast_cutoff_at",
        "lane_effects",
        "oracle_hash",
        "outcome",
        "reference_outcome",
        "recovery_decisions",
        "required_artifact_keys",
        "required_artifact_roles",
        "required_signer_actor_ids",
        "required_signer_role_ids",
        "selected_decision_artifact_ids",
        "source_ids",
        "source_policy_ids",
        "source_fact_ids",
        "success_decision_artifact_ids",
        "fallback_decision_artifact_ids",
        "success_if_any",
        "fact_ids",
        "terminal_outcome",
        "trigger_event_id",
        "variant",
        "verification_basis",
    }
)
AGENT_CHECKPOINT_FIELDS = (
    "checkpoint_id",
    "sequence",
    "available_at",
    "window_start",
    "window_end",
    "visible_gate",
    "label",
    "business_objective",
    "role_deliverables",
    "completion_conditions",
    "policy_entrypoints",
)
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
    "causal_branch_resolutions",
    "milestone_resolutions",
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


def _crm_projection_fields_match(
    requirement: Mapping[str, Any], values: Mapping[str, Any], fields: set[str]
) -> bool:
    exact = dict(requirement["exact_fields"])
    nonempty = {str(item) for item in requirement["nonempty_fields"]}
    if any(
        key in fields and values.get(key) != expected for key, expected in exact.items()
    ) or any(
        key in fields
        and (not isinstance(values.get(key), str) or not str(values[key]).strip())
        for key in nonempty
    ):
        return False
    for key, bounds in requirement["number_ranges"].items():
        if key not in fields:
            continue
        value = values.get(key)
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or value < bounds["minimum"]
            or value > bounds["maximum"]
        ):
            return False
    for key, bounds in requirement["date_ranges"].items():
        if key not in fields:
            continue
        try:
            parsed = date.fromisoformat(str(values.get(key)))
            not_before = date.fromisoformat(str(bounds["not_before"]))
            not_after = date.fromisoformat(str(bounds["not_after"]))
        except ValueError:
            return False
        if parsed < not_before or parsed > not_after:
            return False
    for field, references in requirement["text_reference_fields"].items():
        if field not in fields or not set(references) <= fields:
            continue
        if any(
            _normalized_text(values.get(reference, ""))
            not in _normalized_text(values.get(field, ""))
            for reference in references
        ):
            return False
    return True


def _agent_safe(value: Any, key: str | None = None) -> Any:
    if isinstance(value, Mapping):
        return {
            str(name): _agent_safe(item, str(name))
            for name, item in value.items()
            if str(name) not in AGENT_HIDDEN_FIELDS
        }
    if isinstance(value, list):
        return [_agent_safe(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_agent_safe(item) for item in value)
    if key in {"body", "content"} and isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return value
        if isinstance(parsed, (Mapping, list)):
            return json.dumps(
                _agent_safe(parsed), ensure_ascii=False, sort_keys=True, indent=2
            )
    return value


def _agent_checkpoint(value: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    return {key: value[key] for key in AGENT_CHECKPOINT_FIELDS}


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


def _normalized_text(value: Any) -> str:
    return " ".join(str(value).casefold().replace("_", " ").replace("-", " ").split())


def semantic_envelope_summary(envelope: Mapping[str, Any], target_label: str) -> str:
    gate = str(envelope.get("gate_id", "")).replace("_", " ")
    resolution = str(envelope.get("resolution", "")).replace("_", " ")
    purpose = SEMANTIC_PURPOSE_LABELS[str(envelope["purpose_code"])]
    lines = [f"Purpose: {purpose} for {gate} ({resolution})."]
    decision_codes = _list(envelope.get("decision_codes"))
    if decision_codes:
        decisions = ", ".join(
            SEMANTIC_DECISION_LABELS[str(code)] for code in decision_codes
        )
        lines.append(
            f"Decision requested from {target_label} by {envelope['decision_due_at']}: {decisions}."
        )
    commitment_codes = _list(envelope.get("commitment_codes"))
    if commitment_codes:
        commitments = ", ".join(
            SEMANTIC_COMMITMENT_LABELS[str(code)] for code in commitment_codes
        )
        owner = str(envelope["commitment_owner_role"]).replace("_", " ")
        lines.append(
            f"Commitment by {owner} due {envelope['commitment_due_at']}: {commitments}."
        )
    claims = _list(envelope.get("evidence_claims"))
    basis = sum(
        1
        for claim in claims
        if isinstance(claim, Mapping)
        and claim.get("claim_type") == "supports_gate_basis"
    )
    decision_count = sum(
        1
        for claim in claims
        if isinstance(claim, Mapping)
        and claim.get("claim_type") == "supports_gate_resolution"
    )
    lines.append(
        f"Evidence: {basis} supporting record(s), {decision_count} authoritative decision record(s), {len(_list(envelope.get('attachments')))} attachment(s)."
    )
    return "\n".join(lines)


def brokered_document_payload(
    semantic_summary: str, remediation: Mapping[str, Any] | None = None
) -> dict[str, str]:
    sections = [semantic_summary]
    if remediation is not None:
        sections.append(
            "\n".join(
                (
                    f"Gate: {remediation['gate_id']}",
                    f"Owner: {remediation['owner_role']}",
                    "Cure data: "
                    + json.dumps(remediation["cure_data"], sort_keys=True),
                )
            )
        )
    return {
        "title": semantic_summary.splitlines()[0],
        "content": "\n\n".join(sections),
    }


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
        milestones: Iterable[Mapping[str, Any]] = (),
        action_effect_rules: Iterable[Mapping[str, Any]] = (),
        branches: Iterable[Mapping[str, Any]] = (),
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
        self._edlb_seeded_policy_documents: set[str] = set()
        self._edlb_bundle: Any = None
        actor_values = tuple(actors)
        event_values = tuple(events)
        artifact_values = tuple(artifacts)
        checkpoint_values = tuple(checkpoints)
        grant_values = tuple(grants)
        milestone_values = tuple(milestone_definition(value) for value in milestones)
        if len({value.milestone_id for value in milestone_values}) != len(
            milestone_values
        ):
            raise EngineError("milestone ids must be unique")
        self.milestone_definitions = {
            value.milestone_id: value for value in milestone_values
        }
        effect_values = tuple(
            action_effect_rule(value) for value in action_effect_rules
        )
        if len({value.effect_id for value in effect_values}) != len(effect_values):
            raise EngineError("action effect ids must be unique")
        self.action_effect_rules = {value.effect_id: value for value in effect_values}
        branch_values = tuple(branch_definition(value) for value in branches)
        if len({value.branch_id for value in branch_values}) != len(branch_values):
            raise EngineError("branch ids must be unique")
        self.branch_definitions = {value.branch_id: value for value in branch_values}
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
            self._initialize_run(
                actor_values,
                event_values,
                artifact_values,
                checkpoint_values,
                grant_values,
            )
        else:
            manifest_data = json.loads(self._meta("manifest") or "{}")
            scenario_data = json.loads(self._meta("scenario") or "{}")
            self.manifest = RunManifest.from_dict(manifest_data)
            self.scenario = ScenarioManifest.from_dict(scenario_data)
            if manifest is not None and manifest.run_id != self.manifest.run_id:
                raise EngineError(
                    "existing run manifest does not match the requested run"
                )
        self._validate_milestone_contract()
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
            CREATE TABLE IF NOT EXISTS checkpoint_completions (checkpoint_id TEXT NOT NULL, role TEXT NOT NULL, completed_at TEXT NOT NULL, PRIMARY KEY (checkpoint_id, role));
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
            CREATE TABLE IF NOT EXISTS milestone_resolutions (milestone_id TEXT PRIMARY KEY, resolution TEXT NOT NULL CHECK (resolution IN ('accepted', 'rejected', 'deferred', 'inapplicable', 'remedied')), decision_artifact_ids TEXT NOT NULL, evidence_ids TEXT NOT NULL, authority_resolutions TEXT NOT NULL, business_effects TEXT NOT NULL, effective_at TEXT NOT NULL, remedy_of TEXT);
            CREATE TABLE IF NOT EXISTS causal_branch_resolutions (branch_id TEXT PRIMARY KEY, option TEXT NOT NULL CHECK (option IN ('success', 'fallback')), effect_ids TEXT NOT NULL, action_keys TEXT NOT NULL, selected_decision_artifact_ids TEXT NOT NULL, resolved_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS stakeholder_acts (act_id TEXT PRIMARY KEY, action_key TEXT NOT NULL UNIQUE, data TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS stakeholder_realizations (cache_key TEXT PRIMARY KEY, act_id TEXT NOT NULL, input_hash TEXT NOT NULL, packet TEXT NOT NULL, text TEXT NOT NULL, model_digest TEXT NOT NULL, prompt_hash TEXT NOT NULL, seed INTEGER NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS authority_decision_observations (decision_id TEXT PRIMARY KEY, effect_id TEXT NOT NULL, action_key TEXT NOT NULL UNIQUE, actor_id TEXT NOT NULL, resolution TEXT NOT NULL, request_id TEXT NOT NULL, data TEXT NOT NULL, created_at TEXT NOT NULL);
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
            self._validate_actor_scope(actor.to_dict())
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
                checkpoint.forecast_cutoff_at,
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

    def _organization_scope(self, actor: Mapping[str, Any]) -> str:
        organization_id = actor.get("organization_id")
        if organization_id == self.scenario.seller_org_id:
            return "seller"
        if organization_id == self.scenario.buyer_org_id:
            return "buyer"
        return "third_party"

    def _validate_actor_scope(self, actor: Mapping[str, Any]) -> None:
        attributes = actor.get("attributes")
        authored = (
            attributes.get("organization_scope")
            if isinstance(attributes, Mapping)
            else None
        )
        derived = self._organization_scope(actor)
        if authored is not None and authored != derived:
            raise EngineError("actor organization scope is inconsistent")

    def _external_actor(self, actor: Mapping[str, Any]) -> bool:
        return self._organization_scope(actor) != "seller"

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

    def _validate_milestone_contract(self) -> None:
        if not self.milestone_definitions:
            if self.action_effect_rules or self.branch_definitions:
                raise EngineError("causal branches require milestone definitions")
            return
        checkpoints = {
            str(row[0]): json.loads(str(row[1]))
            for row in self.connection.execute(
                "SELECT checkpoint_id, data FROM checkpoints"
            )
        }
        artifacts = {
            str(row[0]): json.loads(str(row[1]))
            for row in self.connection.execute(
                "SELECT artifact_id, data FROM artifacts"
            )
        }
        actors = {
            str(row[0]): json.loads(str(row[1]))
            for row in self.connection.execute("SELECT actor_id, data FROM actors")
        }
        checkpoint_ids = [
            definition.checkpoint_id
            for definition in self.milestone_definitions.values()
        ]
        if len(checkpoint_ids) != len(set(checkpoint_ids)) or set(
            checkpoint_ids
        ) != set(checkpoints):
            raise EngineError("checkpoint milestone definitions must be complete")
        for definition in self.milestone_definitions.values():
            if set(definition.business_effect_requirements_by_resolution) != (
                set(definition.allowed_resolutions) - {"inapplicable"}
            ):
                raise EngineError("milestone business effect resolutions are invalid")
            if "inapplicable" in definition.terminal_outcome_by_resolution:
                raise EngineError("inapplicable milestone cannot be terminal")
            checkpoint = checkpoints.get(definition.checkpoint_id)
            if checkpoint is None or checkpoint.get("gate_id") != definition.gate_id:
                raise EngineError("milestone checkpoint or gate is invalid")
            evidence_by_role = definition.evidence_requirements_by_role
            if set(evidence_by_role) != set(checkpoint.get("required_roles", ())):
                raise EngineError("milestone role evidence requirements are invalid")
            assigned_evidence = {
                artifact_id
                for evidence_ids in evidence_by_role.values()
                for artifact_id in evidence_ids
            }
            if assigned_evidence | set(definition.decision_artifact_ids) != set(
                definition.evidence_ids
            ) or not set(definition.evidence_ids) <= set(artifacts):
                raise EngineError("milestone evidence contract is invalid")
            authority_actor_ids = {
                str(requirement["actor_id"])
                for requirement in definition.authority_requirements
            }
            if not authority_actor_ids:
                raise EngineError("milestone authority requirements are invalid")
            for requirement in definition.authority_requirements:
                actor = actors.get(str(requirement["actor_id"]))
                authority = actor.get("authority") if actor is not None else None
                if (
                    actor is None
                    or self._organization_scope(actor)
                    != requirement["organization_scope"]
                    or not isinstance(authority, Mapping)
                    or definition.gate_id not in set(authority.get("gate_ids", ()))
                    or not set(requirement["rights"])
                    <= set(authority.get("rights", ()))
                ):
                    raise EngineError("milestone authority is invalid")
            for artifact_id in definition.decision_artifact_ids:
                payload = artifacts[artifact_id].get("structured_payload")
                owners = [
                    str(requirement["actor_id"])
                    for requirement in definition.authority_requirements
                    if artifact_id in set(requirement["decision_artifact_ids"])
                ]
                if (
                    len(owners) != 1
                    or not isinstance(payload, Mapping)
                    or payload.get("checkpoint_id") != definition.checkpoint_id
                    or payload.get("gate_id") != definition.gate_id
                    or list(payload.get("decision_owner_actor_ids", ())) != owners
                    or list(payload.get("required_signer_actor_ids", ())) != owners
                    or payload.get("author_actor_id") != owners[0]
                    or list(artifacts[artifact_id].get("source_actor_ids", ()))
                    != owners
                ):
                    raise EngineError("milestone decision evidence is invalid")
            for (
                resolution,
                effects,
            ) in definition.business_effect_requirements_by_resolution.items():
                followup = effects["decision_followup"]
                crm = effects["crm_projection"]
                deliverable = effects["deliverable"]
                semantic = followup.get("semantic_requirements", {})
                evidence_resolution = (
                    "accepted" if resolution == "remedied" else resolution
                )
                resolution_decisions = {
                    artifact_id
                    for artifact_id in definition.decision_artifact_ids
                    if artifacts[artifact_id]
                    .get("structured_payload", {})
                    .get("decision_state")
                    == evidence_resolution
                }
                expected_effect_evidence = assigned_evidence | resolution_decisions
                if (
                    followup.get("recipient_actor_id") not in authority_actor_ids
                    or set(followup.get("required_evidence_ids", ()))
                    != expected_effect_evidence
                    or set(deliverable.get("required_evidence_ids", ()))
                    != expected_effect_evidence
                    or followup.get("sender_role")
                    not in set(checkpoint.get("required_roles", ()))
                    or crm.get("writer_role")
                    not in set(checkpoint.get("required_roles", ()))
                    or deliverable.get("author_role")
                    not in set(checkpoint.get("required_roles", ()))
                    or followup.get("related_record_id") != crm.get("record_id")
                    or followup.get("related_record_id")
                    != deliverable.get("related_id")
                    or followup.get("semantic_requirements")
                    != deliverable.get("semantic_requirements")
                    or semantic.get("authority_actor_id")
                    != followup.get("recipient_actor_id")
                    or semantic.get("gate_id") != definition.gate_id
                    or semantic.get("resolution") != resolution
                    or semantic.get("commitment_owner_role")
                    not in set(checkpoint.get("required_roles", ()))
                    or str(checkpoint.get("visible_gate", "")).casefold()
                    not in {
                        str(term).casefold()
                        for term in deliverable.get("required_content_terms", ())
                    }
                ):
                    raise EngineError("milestone business effect contract is invalid")
            chronology = definition.chronology
            if (
                chronology.get("sequence") != checkpoint.get("sequence")
                or chronology.get("available_at") != checkpoint.get("available_at")
                or set(chronology.get("decision_times", {}))
                != set(definition.decision_artifact_ids)
            ):
                raise EngineError("milestone chronology is invalid")
            for artifact_id in definition.decision_artifact_ids:
                decision = artifacts[artifact_id]
                times = chronology["decision_times"][artifact_id]
                if (
                    times.get("created_at") != decision.get("created_at")
                    or times.get("available_at") != decision.get("available_at")
                    or _time_value(str(decision["created_at"]))
                    > _time_value(str(decision["available_at"]))
                    or _time_value(str(decision["available_at"]))
                    > _time_value(str(chronology["available_at"]))
                ):
                    raise EngineError("milestone chronology is invalid")
            if definition.approval_requirement is not None:
                approvers = set(definition.approval_requirement["approver_actor_ids"])
                for actor_id in approvers:
                    actor = actors.get(actor_id)
                    authority = actor.get("authority") if actor is not None else None
                    if (
                        actor is None
                        or self._organization_scope(actor) != "seller"
                        or not isinstance(authority, Mapping)
                        or not str(authority.get("role_id", "")).startswith("seller.")
                        or definition.gate_id not in set(authority.get("gate_ids", ()))
                        or not set(authority.get("rights", ()))
                    ):
                        raise EngineError("milestone approval authority is invalid")
                if approvers & authority_actor_ids:
                    raise EngineError("decision and approval authorities must differ")
            for prerequisite_id in definition.prerequisite_milestone_ids:
                prerequisite = self.milestone_definitions.get(prerequisite_id)
                if prerequisite is None:
                    raise EngineError("milestone prerequisite is invalid")
                prerequisite_checkpoint = checkpoints[prerequisite.checkpoint_id]
                if int(prerequisite_checkpoint["sequence"]) >= int(
                    checkpoint["sequence"]
                ):
                    raise EngineError("milestone prerequisite chronology is invalid")
            if definition.remedy_of is not None and (
                definition.remedy_of not in definition.prerequisite_milestone_ids
                or "remedied" not in definition.allowed_resolutions
            ):
                raise EngineError("milestone remedy is invalid")
        for effect in self.action_effect_rules.values():
            checkpoint = checkpoints.get(effect.checkpoint_id)
            branch = self.branch_definitions.get(effect.branch_id)
            resolution_checkpoint = (
                checkpoints.get(branch.resolution_checkpoint_id)
                if branch is not None
                else None
            )
            authority_actor = (
                actors.get(effect.authority_actor_id)
                if effect.authority_actor_id is not None
                else None
            )
            authority = (
                authority_actor.get("authority")
                if isinstance(authority_actor, Mapping)
                else None
            )
            if (
                checkpoint is None
                or branch is None
                or effect.checkpoint_id != branch.action_checkpoint_id
                or effect.gate_id
                not in {
                    checkpoint.get("gate_id"),
                    resolution_checkpoint.get("gate_id")
                    if resolution_checkpoint is not None
                    else None,
                }
                or effect.role not in set(checkpoint.get("required_roles", ()))
                or not set(effect.required_evidence_ids) <= set(artifacts)
                or effect.authority_actor_id is not None
                and effect.authority_actor_id not in actors
                or effect.fact_type == "authority_decision_observed"
                and (
                    not isinstance(authority, Mapping)
                    or effect.gate_id not in set(authority.get("gate_ids", ()))
                    or not set(effect.authority_rights)
                    <= set(authority.get("rights", ()))
                    or effect.purpose_code not in SEMANTIC_PURPOSE_LABELS
                    or effect.decision_code not in SEMANTIC_DECISION_LABELS
                    or effect.commitment_code not in SEMANTIC_COMMITMENT_LABELS
                    or effect.resolution != "pending"
                    or effect.document_kind != "remediation_plan"
                    or effect.response_resolution
                    not in {"accepted", "rejected", "deferred"}
                    or effect.remediation_requirements is None
                )
                or effect.fact_type == "crm_transition"
                and effect.next_step_type != "remediation_decision"
            ):
                raise EngineError("action effect contract is invalid")
        used_effect_ids: set[str] = set()
        for branch in self.branch_definitions.values():
            action_checkpoint = checkpoints.get(branch.action_checkpoint_id)
            resolution_checkpoint = checkpoints.get(branch.resolution_checkpoint_id)
            milestone = self.milestone_definitions.get(branch.remedy_milestone_id)
            option_effects = {
                effect_id for option in branch.success_if_any for effect_id in option
            }
            selected_artifacts = set(branch.success_decision_artifact_ids) | set(
                branch.fallback_decision_artifact_ids
            )
            authority_ids = (
                {
                    str(requirement["actor_id"])
                    for requirement in milestone.authority_requirements
                }
                if milestone is not None
                else set()
            )
            option_authority_ids = {
                option: {
                    str(self.action_effect_rules[effect_id].authority_actor_id)
                    for effect_id in option
                    if effect_id in self.action_effect_rules
                    and self.action_effect_rules[effect_id].fact_type
                    == "authority_decision_observed"
                }
                for option in branch.success_if_any
            }
            decision_authority_ids = (
                {
                    option: {
                        str(requirement["actor_id"])
                        for requirement in milestone.authority_requirements
                        if set(requirement["decision_artifact_ids"]) & set(artifact_ids)
                    }
                    for option, artifact_ids in (
                        ("success", branch.success_decision_artifact_ids),
                        ("fallback", branch.fallback_decision_artifact_ids),
                    )
                }
                if milestone is not None
                else {}
            )
            if (
                action_checkpoint is None
                or resolution_checkpoint is None
                or int(action_checkpoint["sequence"])
                >= int(resolution_checkpoint["sequence"])
                or milestone is None
                or milestone.checkpoint_id != branch.resolution_checkpoint_id
                or milestone.branch_id != branch.branch_id
                or set(milestone.decision_artifact_ids) != selected_artifacts
                or not option_effects <= set(self.action_effect_rules)
                or any(
                    self.action_effect_rules[effect_id].branch_id != branch.branch_id
                    for effect_id in option_effects
                )
                or not authority_ids
                or any(
                    targeted != authority_ids
                    for targeted in option_authority_ids.values()
                )
                or any(
                    selected != authority_ids
                    for selected in decision_authority_ids.values()
                )
            ):
                raise EngineError("causal branch contract is invalid")
            used_effect_ids.update(option_effects)
        if used_effect_ids != set(self.action_effect_rules):
            raise EngineError("action effect rules must belong to one branch option")

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
            "authority_decisions": [
                json.loads(str(row[0]))
                for row in self.connection.execute(
                    "SELECT data FROM authority_decision_observations ORDER BY decision_id"
                )
            ],
            "branch_resolutions": self.branch_resolutions(),
            "milestone_resolutions": self.milestone_resolutions(),
            "terminal_outcome": self._meta("terminal_outcome"),
        }

    def causal_state_hash(self) -> str:
        return digest(self.causal_state())

    def milestone_resolutions(self) -> list[dict[str, Any]]:
        return [
            {
                "milestone_id": str(row[0]),
                "resolution": str(row[1]),
                "decision_artifact_ids": json.loads(str(row[2])),
                "evidence_ids": json.loads(str(row[3])),
                "authority_resolutions": json.loads(str(row[4])),
                "business_effects": json.loads(str(row[5])),
                "effective_at": str(row[6]),
                "remedy_of": row[7],
            }
            for row in self.connection.execute(
                "SELECT milestone_id, resolution, decision_artifact_ids, evidence_ids, authority_resolutions, business_effects, effective_at, remedy_of FROM milestone_resolutions ORDER BY rowid"
            )
        ]

    def branch_resolutions(self) -> list[dict[str, Any]]:
        return [
            BranchResolution(
                str(row[0]),
                str(row[1]),
                tuple(json.loads(str(row[2]))),
                tuple(json.loads(str(row[3]))),
                tuple(json.loads(str(row[4]))),
                str(row[5]),
            ).to_dict()
            for row in self.connection.execute(
                "SELECT branch_id, option, effect_ids, action_keys, selected_decision_artifact_ids, resolved_at FROM causal_branch_resolutions ORDER BY rowid"
            )
        ]

    def _rebuild_causal_lanes(self) -> None:
        state: dict[str, dict[str, Any]] = {
            lane: {
                "score": score,
                "status": lane_status(score),
                "sticky": False,
                "evidence": [],
            }
            for lane, score in LANE_DEFAULTS.items()
        }
        for resolution in self.milestone_resolutions():
            definition = self.milestone_definitions.get(resolution["milestone_id"])
            if definition is None:
                raise EngineError("milestone resolution has no definition")
            effects = definition.lane_effects_by_resolution.get(
                resolution["resolution"], {}
            )
            for lane, effect in effects.items():
                current = state[lane]
                score = int(
                    effect.get(
                        "absolute", current["score"] + int(effect.get("delta", 0))
                    )
                )
                current["score"] = max(-100, min(100, score))
                current["status"] = str(
                    effect.get("status") or lane_status(current["score"])
                )
                current["sticky"] = bool(current["sticky"] or effect.get("sticky"))
                current["evidence"].append(
                    {
                        "source_id": resolution["milestone_id"],
                        "fact": str(effect.get("fact", "milestone resolved")),
                    }
                )
        for lane, value in state.items():
            self.connection.execute(
                "UPDATE causal_lanes SET score = ?, status = ?, sticky = ?, evidence = ?, updated_at = ? WHERE lane = ?",
                (
                    value["score"],
                    value["status"],
                    int(value["sticky"]),
                    to_json(value["evidence"]),
                    self.current_time,
                    lane,
                ),
            )

    def release_available_events(self) -> tuple[str, ...]:
        if self._supported_terminal_outcome() is not None:
            return ()
        rows = self.connection.execute(
            "SELECT event_id, data FROM events WHERE visibility != 'oracle_only' AND event_id NOT IN (SELECT event_id FROM causal_event_applications)"
        ).fetchall()
        available = []
        for row in rows:
            event = json.loads(str(row[1]))
            if _time_value(event["available_at"]) <= _time_value(
                self.current_time
            ) and self._condition_is_selected(event):
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
            self.connection.execute(
                "INSERT INTO causal_event_applications(event_id, applied_at, effects) VALUES (?, ?, ?)",
                (event_id, self.current_time, "{}"),
            )
            released.append(event_id)
        return tuple(released)

    def _supported_terminal_outcome(self) -> str | None:
        outcomes = [
            self.milestone_definitions[
                item["milestone_id"]
            ].terminal_outcome_by_resolution.get(item["resolution"])
            for item in self.milestone_resolutions()
        ]
        supported = [outcome for outcome in outcomes if outcome is not None]
        if len(supported) > 1:
            raise EngineError("multiple terminal milestone outcomes are supported")
        return supported[0] if supported else None

    def _external_action(
        self, tool_name: str, result: Mapping[str, Any]
    ) -> tuple[bool, str | None]:
        if tool_name == "communications.send":
            metadata = result.get("metadata")
            envelope = (
                metadata.get("semantic_envelope")
                if isinstance(metadata, Mapping)
                else None
            )
        elif tool_name in {"calendar.schedule", "calendar.reschedule"}:
            envelope = result.get("semantic_envelope")
        else:
            return False, None
        if not isinstance(envelope, Mapping):
            return False, None
        actor = self._actor_for_recipient(str(envelope.get("target_actor_id", "")))
        if actor is None or not self._external_actor(actor):
            return False, None
        return True, str(actor["actor_id"])

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
        effects = self._derive_action_effects(
            action_key, role, tool_name, arguments, result, input_hash
        )
        checkpoint = self.current_checkpoint_index
        outer = self._transaction_depth == 0
        if outer:
            self.connection.execute("BEGIN")
        try:
            response = None
            observed = self.connection.execute(
                "SELECT 1 FROM authority_decision_observations WHERE action_key = ?",
                (action_key,),
            ).fetchone()
            if external and actor_id is not None and observed is None:
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

    def _rule_evidence_is_grounded(self, rule: ActionEffectRule) -> bool:
        reads = self._successful_evidence_reads().get(rule.role, set())
        if not set(rule.required_evidence_ids) <= reads:
            return False
        try:
            for artifact_id in rule.required_evidence_ids:
                self._artifact_row(artifact_id)
        except EngineError:
            return False
        return True

    @staticmethod
    def _action_semantics_support(
        rule: ActionEffectRule,
        envelope: Mapping[str, Any],
        additional_attachment_ids: Sequence[str] = (),
        evidence_ids: Sequence[str] | None = None,
    ) -> bool:
        expected_evidence_ids = tuple(
            rule.required_evidence_ids if evidence_ids is None else evidence_ids
        )
        claims = envelope.get("evidence_claims")
        expected_claims = {
            (
                artifact_id,
                "supports_gate_basis",
                rule.gate_id,
                rule.resolution,
            )
            for artifact_id in expected_evidence_ids
        }
        actual_claims = (
            {
                (
                    str(claim.get("artifact_id")),
                    str(claim.get("claim_type")),
                    str(claim.get("gate_id")),
                    str(claim.get("resolution")),
                )
                for claim in claims
                if isinstance(claim, Mapping)
            }
            if isinstance(claims, Sequence) and not isinstance(claims, (str, bytes))
            else set()
        )
        return bool(
            envelope.get("target_actor_id") == rule.authority_actor_id
            and envelope.get("purpose_code") == rule.purpose_code
            and envelope.get("gate_id") == rule.gate_id
            and envelope.get("resolution") == rule.resolution
            and _list(envelope.get("decision_codes")) == [rule.decision_code]
            and _list(envelope.get("commitment_codes")) == [rule.commitment_code]
            and set(_list(envelope.get("related_records"))) == {rule.record_id}
            and set(_list(envelope.get("attachments")))
            == set(expected_evidence_ids) | set(additional_attachment_ids)
            and actual_claims == expected_claims
        )

    def _remediation_evidence_basis(
        self,
        owner_role: str,
        gate_id: str,
        cure_data: Mapping[str, Any],
        evidence_ids: Sequence[str],
        expected_checksums: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        selected = tuple(str(artifact_id) for artifact_id in evidence_ids)
        if not selected or len(set(selected)) != len(selected):
            raise EngineError("remediation plan is not grounded in current evidence")
        owner_reads = self._successful_evidence_reads().get(owner_role, set())
        supported: set[str] = set()
        checksums: dict[str, str] = {}
        for artifact_id in selected:
            artifact = self._artifact_row(artifact_id)
            payload = artifact.get("structured_payload")
            if (
                artifact_id not in owner_reads
                or artifact.get("gate_id") != gate_id
                or not self._visible(
                    str(artifact.get("visibility", "oracle_only")),
                    owner_role,
                    artifact,
                )
                or not isinstance(payload, Mapping)
            ):
                raise EngineError(
                    "remediation plan is not grounded in current evidence"
                )
            checksums[artifact_id] = str(artifact["checksum"])
            for field, expected in cure_data.items():
                if field not in payload:
                    continue
                if payload[field] != expected:
                    raise EngineError(
                        "remediation plan is not grounded in current evidence"
                    )
                supported.add(str(field))
        if supported != set(cure_data) or (
            expected_checksums is not None and checksums != dict(expected_checksums)
        ):
            raise EngineError("remediation plan is not grounded in current evidence")
        return {
            "evidence_checksums": checksums,
            "evidence_ids": sorted(selected),
        }

    def _communication_support(
        self, rule: ActionEffectRule, result: Mapping[str, Any]
    ) -> str | None:
        if rule.authority_actor_id is None:
            return None
        actor = self._actor_for_recipient(rule.authority_actor_id)
        message_id = result.get("message_id")
        row = self.connection.execute(
            "SELECT sender_role, recipients, body, created_at, metadata FROM communications WHERE message_id = ?",
            (message_id,),
        ).fetchone()
        if actor is None or row is None:
            return None
        authority = actor.get("authority")
        metadata = json.loads(str(row[4]))
        envelope = metadata.get("semantic_envelope")
        recipients = {str(item).casefold() for item in json.loads(str(row[1]))}
        if (
            row[0] != rule.role
            or str(actor.get("email", "")).casefold() not in recipients
            and rule.authority_actor_id.casefold() not in recipients
            or not str(row[2]).strip()
            or _time_value(str(row[3])) > _time_value(self.current_time)
            or not isinstance(authority, Mapping)
            or rule.gate_id not in set(authority.get("gate_ids", ()))
            or not set(rule.authority_rights) <= set(authority.get("rights", ()))
            or not isinstance(envelope, Mapping)
            or not self._action_semantics_support(rule, envelope)
        ):
            return None
        return str(message_id)

    def _authority_request_support(
        self, rule: ActionEffectRule, result: Mapping[str, Any]
    ) -> tuple[str, str] | None:
        if rule.authority_actor_id is None or rule.remediation_requirements is None:
            return None
        actor = self._actor_for_recipient(rule.authority_actor_id)
        message_id = result.get("message_id")
        row = self.connection.execute(
            "SELECT sender_role, recipients, body, created_at, metadata FROM communications WHERE message_id = ?",
            (message_id,),
        ).fetchone()
        if actor is None or row is None:
            return None
        metadata = json.loads(str(row[4]))
        envelope = metadata.get("semantic_envelope")
        if not isinstance(envelope, Mapping):
            return None
        attachments = set(_list(envelope.get("attachments")))
        plan_ids: set[str] = set()
        for attachment_id in attachments:
            candidate = self.connection.execute(
                "SELECT data FROM documents WHERE document_id = ?", (attachment_id,)
            ).fetchone()
            if (
                candidate is not None
                and json.loads(str(candidate[0])).get("kind") == rule.document_kind
            ):
                plan_ids.add(str(attachment_id))
        if len(plan_ids) != 1:
            return None
        document_id = next(iter(plan_ids))
        document_row = self.connection.execute(
            "SELECT data FROM documents WHERE document_id = ?", (document_id,)
        ).fetchone()
        linked = self.connection.execute(
            "SELECT 1 FROM document_links WHERE document_id = ? AND related_type = 'opportunity' AND related_id = ?",
            (document_id, rule.record_id),
        ).fetchone()
        document = json.loads(str(document_row[0])) if document_row is not None else {}
        document_metadata = document.get("metadata")
        remediation = (
            document_metadata.get("remediation")
            if isinstance(document_metadata, Mapping)
            else None
        )
        verification_basis = (
            document_metadata.get("verification_basis")
            if isinstance(document_metadata, Mapping)
            else None
        )
        action_checkpoint = next(
            (
                checkpoint
                for checkpoint in self.checkpoints()
                if checkpoint["checkpoint_id"] == rule.checkpoint_id
            ),
            None,
        )
        expected_remediation = {
            "cure_data": dict(rule.remediation_requirements["cure_data"]),
            "gate_id": (
                action_checkpoint.get("gate_id")
                if action_checkpoint is not None
                else None
            ),
            "owner_role": rule.remediation_requirements["owner_role"],
        }
        owner_role = str(rule.remediation_requirements["owner_role"])
        selected_evidence = (
            tuple(_list(verification_basis.get("evidence_ids")))
            if isinstance(verification_basis, Mapping)
            else ()
        )
        try:
            self._remediation_evidence_basis(
                owner_role,
                str(expected_remediation["gate_id"]),
                expected_remediation["cure_data"],
                selected_evidence,
                (
                    verification_basis.get("evidence_checksums")
                    if isinstance(verification_basis, Mapping)
                    else None
                ),
            )
        except EngineError, KeyError:
            return None
        authority = actor.get("authority")
        recipients = {str(item).casefold() for item in json.loads(str(row[1]))}
        if (
            row[0] != rule.role
            or str(actor.get("email", "")).casefold() not in recipients
            and rule.authority_actor_id.casefold() not in recipients
            or not str(row[2]).strip()
            or _time_value(str(row[3])) > _time_value(self.current_time)
            or not isinstance(authority, Mapping)
            or rule.gate_id not in set(authority.get("gate_ids", ()))
            or not set(rule.authority_rights) <= set(authority.get("rights", ()))
            or document.get("author_role") != owner_role
            or document.get("kind") != rule.document_kind
            or document.get("version") != 1
            or not self._document_is_brokered(document)
            or remediation != expected_remediation
            or not isinstance(verification_basis, Mapping)
            or set(verification_basis)
            != {"due_at", "evidence_checksums", "evidence_ids"}
            or verification_basis.get("due_at")
            != rule.remediation_requirements["due_at"]
            or linked is None
            or not self._action_semantics_support(
                rule, envelope, (document_id,), selected_evidence
            )
        ):
            return None
        return str(message_id), str(document_id)

    def _observe_authority_decision(
        self,
        rule: ActionEffectRule,
        action_key: str,
        request_id: str,
        document_id: str,
        input_hash: str,
        envelope: Mapping[str, Any],
    ) -> dict[str, Any]:
        existing = self.connection.execute(
            "SELECT data FROM authority_decision_observations WHERE action_key = ?",
            (action_key,),
        ).fetchone()
        if existing is not None:
            return json.loads(str(existing[0]))
        actor_id = str(rule.authority_actor_id)
        resolution = str(rule.response_resolution)
        act = select_stakeholder_act(
            self.manifest.world_id,
            action_key,
            actor_id,
            "email",
            envelope,
            self.causal_lanes(),
            resolution,
        )
        response = self._realize_stakeholder_act(act, input_hash)
        decision_id = _stable_id(
            "authority-decision",
            self.manifest.world_id,
            f"{rule.effect_id}:{action_key}",
        )
        value = {
            "decision_id": decision_id,
            "effect_id": rule.effect_id,
            "action_key": action_key,
            "actor_id": actor_id,
            "resolution": resolution,
            "request_id": request_id,
            "document_id": document_id,
            "response_message_id": response["message_id"],
            "gate_id": rule.gate_id,
            "rights": list(rule.authority_rights),
            "created_at": self.current_time,
        }
        self.connection.execute(
            "INSERT INTO authority_decision_observations(decision_id, effect_id, action_key, actor_id, resolution, request_id, data, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                decision_id,
                rule.effect_id,
                action_key,
                actor_id,
                resolution,
                request_id,
                to_json(value),
                self.current_time,
            ),
        )
        return value

    def _authority_decision_support(
        self,
        rule: ActionEffectRule,
        result: Mapping[str, Any],
        action_key: str | None = None,
        input_hash: str | None = None,
    ) -> dict[str, Any] | None:
        support = self._authority_request_support(rule, result)
        if support is None:
            return None
        request_id, document_id = support
        if action_key is not None and input_hash is not None:
            metadata = result.get("metadata")
            envelope = (
                metadata.get("semantic_envelope")
                if isinstance(metadata, Mapping)
                else None
            )
            if not isinstance(envelope, Mapping):
                return None
            decision = self._observe_authority_decision(
                rule,
                action_key,
                request_id,
                document_id,
                input_hash,
                envelope,
            )
        else:
            row = self.connection.execute(
                "SELECT data FROM authority_decision_observations WHERE effect_id = ? AND request_id = ?",
                (rule.effect_id, request_id),
            ).fetchone()
            if row is None:
                return None
            decision = json.loads(str(row[0]))
        if (
            decision.get("actor_id") != rule.authority_actor_id
            or decision.get("resolution") != rule.response_resolution
            or decision.get("document_id") != document_id
            or decision.get("gate_id") != rule.gate_id
            or set(decision.get("rights", ())) != set(rule.authority_rights)
        ):
            return None
        return decision

    def _calendar_support(
        self, rule: ActionEffectRule, result: Mapping[str, Any]
    ) -> str | None:
        if rule.authority_actor_id is None:
            return None
        actor = self._actor_for_recipient(rule.authority_actor_id)
        calendar_id = result.get("calendar_id")
        row = self.connection.execute(
            "SELECT data FROM calendar_events WHERE calendar_id = ?", (calendar_id,)
        ).fetchone()
        if actor is None or row is None:
            return None
        event = json.loads(str(row[0]))
        authority = actor.get("authority")
        envelope = event.get("semantic_envelope")
        participants = {str(item).casefold() for item in event.get("participants", ())}
        if (
            event.get("status") == "cancelled"
            or event.get("organizer_role") != rule.role
            or str(actor.get("email", "")).casefold() not in participants
            and rule.authority_actor_id.casefold() not in participants
            or not isinstance(authority, Mapping)
            or rule.gate_id not in set(authority.get("gate_ids", ()))
            or not set(rule.authority_rights) <= set(authority.get("rights", ()))
            or not isinstance(envelope, Mapping)
            or not self._action_semantics_support(rule, envelope)
        ):
            return None
        return str(calendar_id)

    def _deliverable_support(
        self, rule: ActionEffectRule, result: Mapping[str, Any]
    ) -> str | None:
        if rule.authority_actor_id is None:
            return None
        actor = self._actor_for_recipient(rule.authority_actor_id)
        message_id = result.get("message_id")
        message_row = self.connection.execute(
            "SELECT sender_role, recipients, metadata FROM communications WHERE message_id = ?",
            (message_id,),
        ).fetchone()
        if actor is None or message_row is None or not self._external_actor(actor):
            return None
        recipients = {str(item).casefold() for item in json.loads(str(message_row[1]))}
        metadata = json.loads(str(message_row[2]))
        envelope = metadata.get("semantic_envelope")
        attachments = (
            set(_list(envelope.get("attachments")))
            if isinstance(envelope, Mapping)
            else set()
        )
        document_ids = {
            str(row[0])
            for row in self.connection.execute(
                "SELECT document_id, data FROM documents"
            )
            if str(row[0]) in attachments
            and json.loads(str(row[1])).get("kind") == rule.document_kind
            and json.loads(str(row[1])).get("author_role") == rule.role
        }
        if len(document_ids) != 1:
            return None
        document_id = next(iter(document_ids))
        document_row = self.connection.execute(
            "SELECT data FROM documents WHERE document_id = ?", (document_id,)
        ).fetchone()
        linked = self.connection.execute(
            "SELECT 1 FROM document_links WHERE document_id = ? AND related_id = ?",
            (document_id, rule.record_id),
        ).fetchone()
        document = json.loads(str(document_row[0])) if document_row is not None else {}
        if (
            message_row[0] != rule.role
            or str(actor.get("email", "")).casefold() not in recipients
            or document.get("author_role") != rule.role
            or document.get("kind") != rule.document_kind
            or document.get("version") != 1
            or not self._document_is_brokered(document)
            or not isinstance(envelope, Mapping)
            or linked is None
            or not self._action_semantics_support(rule, envelope, (document_id,))
        ):
            return None
        return str(message_id)

    def _crm_transition_support(
        self, rule: ActionEffectRule, result: Mapping[str, Any]
    ) -> str | None:
        result_record = result.get("record")
        result_record_id = result.get("record_id")
        if isinstance(result_record, Mapping):
            result_record_id = result_record.get("record_id", result_record_id)
        if result_record_id != rule.record_id:
            return None
        row = self.connection.execute(
            "SELECT data FROM crm_records WHERE record_id = ?", (rule.record_id,)
        ).fetchone()
        if row is None:
            return None
        record = json.loads(str(row[0]))
        history = self.connection.execute(
            "SELECT history_id FROM crm_history WHERE record_id = ? AND role = ? AND changed_at = ? AND json_extract(changes, '$.next_step_gate_id') = ? AND json_extract(changes, '$.next_step_type') = ? ORDER BY history_id DESC LIMIT 1",
            (
                rule.record_id,
                rule.role,
                self.current_time,
                rule.next_gate_id,
                rule.next_step_type,
            ),
        ).fetchone()
        if (
            record.get("next_step_gate_id") != rule.next_gate_id
            or record.get("next_step_type") != rule.next_step_type
            or history is None
        ):
            return None
        return str(history[0])

    def _approval_support(
        self, rule: ActionEffectRule, result: Mapping[str, Any]
    ) -> str | None:
        approval_id = result.get("approval_id")
        row = self.connection.execute(
            "SELECT data FROM approvals WHERE approval_id = ?", (approval_id,)
        ).fetchone()
        if row is None:
            return None
        approval = json.loads(str(row[0]))
        details = approval.get("details")
        if (
            approval.get("status") != "approved"
            or rule.authority_actor_id is not None
            and rule.authority_actor_id
            not in set(approval.get("responded_by_actor_ids", ()))
            or not isinstance(details, Mapping)
            or details.get("checkpoint_id") != rule.checkpoint_id
            or details.get("gate") != rule.gate_id
            or details.get("record_id") != rule.record_id
        ):
            return None
        return str(approval_id)

    def _derive_action_effects(
        self,
        action_key: str,
        role: str,
        tool_name: str,
        arguments: Mapping[str, Any],
        result: Mapping[str, Any],
        input_hash: str,
    ) -> dict[str, Any]:
        checkpoint = self.current_checkpoint()
        if checkpoint is None:
            return {}
        matched: dict[str, Any] = {}
        for rule in self.action_effect_rules.values():
            if (
                rule.checkpoint_id != checkpoint.get("checkpoint_id")
                or rule.role != role
                or tool_name not in set(rule.tool_names)
                or rule.fact_type != "authority_decision_observed"
                and not self._rule_evidence_is_grounded(rule)
            ):
                continue
            support = (
                self._authority_decision_support(rule, result, action_key, input_hash)
                if rule.fact_type == "authority_decision_observed"
                else {
                    "crm_transition": self._crm_transition_support,
                    "internal_approval": self._approval_support,
                }[rule.fact_type](rule, result)
            )
            if support is not None and (
                rule.fact_type != "authority_decision_observed"
                or isinstance(support, Mapping)
                and support.get("resolution") in {"accepted", "remedied"}
            ):
                matched[rule.effect_id] = {
                    "action_key": action_key,
                    "fact_type": rule.fact_type,
                    "support_id": (
                        support["decision_id"]
                        if isinstance(support, Mapping)
                        else support
                    ),
                    **(
                        {"request_id": support["request_id"]}
                        if isinstance(support, Mapping)
                        else {}
                    ),
                    "tool_name": tool_name,
                }
        return matched

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

    def _milestone_for_checkpoint(
        self, checkpoint_id: str
    ) -> MilestoneDefinition | None:
        matches = [
            value
            for value in self.milestone_definitions.values()
            if value.checkpoint_id == checkpoint_id
        ]
        if len(matches) > 1:
            raise EngineError("checkpoint has multiple milestone definitions")
        return matches[0] if matches else None

    def _successful_evidence_reads(self) -> dict[str, set[str]]:
        calls: dict[str, tuple[str, str]] = {}
        result: dict[str, set[str]] = {role: set() for role in SELLER_ROLES}
        keys = ("message_id", "record_id", "document_id", "artifact_id")
        for event in self.trace_events():
            payload = event.payload
            if event.kind == "tool_call" and payload.get("tool_name") in {
                "communications.read",
                "crm.read",
                "crm.history",
                "documents.read",
                "web.open",
            }:
                arguments = payload.get("arguments")
                if not isinstance(arguments, Mapping):
                    continue
                identifier = next(
                    (
                        str(arguments[key])
                        for key in keys
                        if arguments.get(key) is not None
                    ),
                    "",
                )
                if identifier:
                    calls[str(payload.get("call_id", event.message_id))] = (
                        event.actor_role,
                        identifier,
                    )
            elif event.kind == "tool_result" and payload.get("ok") is True:
                call = calls.get(str(payload.get("call_id", "")))
                if call is not None:
                    result[call[0]].add(call[1])
        return result

    def _branch_for_artifact(self, artifact_id: str) -> BranchDefinition | None:
        return next(
            (
                branch
                for branch in self.branch_definitions.values()
                if artifact_id
                in {
                    *branch.success_decision_artifact_ids,
                    *branch.fallback_decision_artifact_ids,
                }
            ),
            None,
        )

    def _artifact_is_selected(self, artifact_id: str) -> bool:
        branch = self._branch_for_artifact(artifact_id)
        if branch is None:
            return True
        row = self.connection.execute(
            "SELECT selected_decision_artifact_ids FROM causal_branch_resolutions WHERE branch_id = ?",
            (branch.branch_id,),
        ).fetchone()
        return row is not None and artifact_id in set(json.loads(str(row[0])))

    def _condition_is_selected(self, value: Mapping[str, Any]) -> bool:
        payload = value.get("payload")
        source = payload if isinstance(payload, Mapping) else value
        branch_id = source.get("branch_id")
        branch_option = source.get("branch_option")
        if branch_id is None and branch_option is None:
            artifact_ids = tuple(_list(value.get("artifact_ids")))
            conditional = tuple(
                artifact_id
                for artifact_id in artifact_ids
                if self._branch_for_artifact(artifact_id) is not None
            )
            return not conditional or all(
                self._artifact_is_selected(artifact_id) for artifact_id in conditional
            )
        if branch_id not in self.branch_definitions or branch_option not in {
            "success",
            "fallback",
        }:
            raise EngineError("conditional source is invalid")
        row = self.connection.execute(
            "SELECT option FROM causal_branch_resolutions WHERE branch_id = ?",
            (branch_id,),
        ).fetchone()
        return row is not None and row[0] == branch_option

    def _artifact_row(self, artifact_id: str) -> dict[str, Any]:
        if not self._artifact_is_selected(artifact_id):
            raise EngineError("checkpoint evidence is incomplete or invalid")
        row = self.connection.execute(
            "SELECT data FROM artifacts WHERE artifact_id = ?", (artifact_id,)
        ).fetchone()
        if row is None:
            raise EngineError("checkpoint evidence is incomplete or invalid")
        artifact = json.loads(str(row[0]))
        created_at = str(artifact["created_at"])
        available_at = str(artifact["available_at"])
        if _time_value(created_at) > _time_value(available_at) or _time_value(
            available_at
        ) > _time_value(self.current_time):
            raise EngineError("checkpoint evidence is incomplete or invalid")
        superseded = any(
            self._artifact_is_selected(str(candidate[0]))
            for candidate in self.connection.execute(
                "SELECT artifact_id FROM artifacts WHERE json_extract(data, '$.supersedes_artifact_id') = ? AND available_at <= ?",
                (artifact_id, self.current_time),
            )
        )
        logical = artifact.get("logical_document_id")
        version = artifact.get("version")
        if logical is not None and version is not None:
            newer = any(
                self._artifact_is_selected(str(candidate[0]))
                for candidate in self.connection.execute(
                    "SELECT artifact_id FROM artifacts WHERE json_extract(data, '$.logical_document_id') = ? AND CAST(json_extract(data, '$.version') AS INTEGER) > ? AND available_at <= ?",
                    (logical, int(version), self.current_time),
                )
            )
            superseded = superseded or newer
        if superseded:
            raise EngineError("checkpoint evidence is incomplete or invalid")
        return artifact

    def _matching_approval_id(
        self, requirement: Mapping[str, Any], available_at: str
    ) -> str | None:
        for row in self.connection.execute(
            "SELECT approval_id, data FROM approvals ORDER BY approval_id"
        ):
            approval = json.loads(str(row[1]))
            details = approval.get("details")
            if not isinstance(details, Mapping):
                continue
            created_at = str(approval.get("created_at", self.current_time))
            responded_at = str(approval.get("responded_at", self.current_time))
            if (
                approval.get("status") == "approved"
                and set(approval.get("approver_actor_ids", ()))
                == set(requirement.get("approver_actor_ids", ()))
                and set(approval.get("responded_by_actor_ids", ()))
                == set(requirement.get("approver_actor_ids", ()))
                and details.get("checkpoint_id") == requirement.get("checkpoint_id")
                and details.get("gate") == requirement.get("gate_id")
                and details.get("amount_minor_units")
                == requirement.get("amount_minor_units")
                and details.get("basis") == requirement.get("basis")
                and details.get("policy_limit_minor_units")
                == requirement.get("policy_limit_minor_units")
                and details.get("policy_owner") == requirement.get("policy_owner")
                and details.get("policy_evidence") == requirement.get("policy_evidence")
                and details.get("trigger") == requirement.get("trigger")
                and _time_value(available_at) <= _time_value(created_at)
                and _time_value(created_at) <= _time_value(responded_at)
                and _time_value(responded_at) <= _time_value(self.current_time)
            ):
                return str(row[0])
        return None

    def _approval_satisfies(
        self, requirement: Mapping[str, Any], available_at: str
    ) -> bool:
        return self._matching_approval_id(requirement, available_at) is not None

    def _envelope_supports_business_effect(
        self,
        envelope: Any,
        related_record_id: str,
        evidence_ids: set[str],
        semantic_requirements: Mapping[str, Any],
        semantic_summary: Any,
        visible_text: str,
    ) -> bool:
        if not isinstance(envelope, Mapping):
            return False
        related = {
            str(item)
            for item in _list(envelope.get("related_records"))
            if isinstance(item, str)
        }
        attachments = {
            str(item)
            for item in _list(envelope.get("attachments"))
            if isinstance(item, str)
        }
        decisions = [
            item
            for item in _list(envelope.get("requested_decisions"))
            if isinstance(item, str) and item.strip()
        ]
        commitments = [
            item
            for item in _list(envelope.get("commitments"))
            if isinstance(item, str) and item.strip()
        ]
        claims = {
            (
                str(item.get("artifact_id", "")),
                str(item.get("claim_type", "")),
                str(item.get("gate_id", "")),
                str(item.get("resolution", "")),
            )
            for item in _list(envelope.get("evidence_claims"))
            if isinstance(item, Mapping)
        }
        expected_claims = {
            (
                str(item["artifact_id"]),
                str(item["claim_type"]),
                str(item["gate_id"]),
                str(item["resolution"]),
            )
            for item in semantic_requirements["evidence_claims"]
        }
        normalized_visible = _normalized_text(visible_text)
        try:
            expected_summary = self._semantic_summary(envelope)
        except EngineError:
            return False
        return (
            related == {related_record_id}
            and attachments == evidence_ids
            and claims == expected_claims
            and len(decisions) == 1
            and len(commitments) == 1
            and isinstance(semantic_summary, str)
            and bool(_normalized_text(semantic_summary))
            and semantic_summary == expected_summary
            and _normalized_text(semantic_summary) in normalized_visible
            and envelope.get("target_actor_id")
            == semantic_requirements["authority_actor_id"]
            and envelope.get("commitment_owner_role")
            == semantic_requirements["commitment_owner_role"]
            and envelope.get("gate_id") == semantic_requirements["gate_id"]
            and envelope.get("purpose_code") == semantic_requirements["purpose_code"]
            and envelope.get("resolution") == semantic_requirements["resolution"]
            and _list(envelope.get("decision_codes"))
            == [semantic_requirements["decision_code"]]
            and _list(envelope.get("commitment_codes"))
            == [semantic_requirements["commitment_code"]]
        )

    def _decision_followup_effect(
        self, requirement: Mapping[str, Any], available_at: str
    ) -> str | None:
        actor_row = self.connection.execute(
            "SELECT data FROM actors WHERE actor_id = ?",
            (requirement["recipient_actor_id"],),
        ).fetchone()
        if actor_row is None:
            return None
        actor = json.loads(str(actor_row[0]))
        recipient = str(actor.get("email", ""))
        role = str(requirement["sender_role"])
        related = str(requirement["related_record_id"])
        evidence = {str(item) for item in requirement["required_evidence_ids"]}
        channels = {str(item) for item in requirement["allowed_channels"]}
        semantic = requirement["semantic_requirements"]
        message_facts = requirement["required_message_facts"]
        if "email" in channels:
            for row in self.connection.execute(
                "SELECT message_id, channel, direction, sender_role, recipients, subject, body, created_at, metadata FROM communications ORDER BY message_id"
            ):
                metadata = json.loads(str(row[8]))
                message = "\n".join((str(row[5]), str(row[6])))
                if (
                    row[1] == "email"
                    and row[2] == "outbound"
                    and row[3] == role
                    and recipient in set(json.loads(str(row[4])))
                    and _time_value(available_at) <= _time_value(str(row[7]))
                    and _time_value(str(row[7])) <= _time_value(self.current_time)
                    and all(
                        _normalized_text(fact) in _normalized_text(message)
                        for fact in message_facts
                    )
                    and self._envelope_supports_business_effect(
                        metadata.get("semantic_envelope"),
                        related,
                        evidence,
                        semantic,
                        metadata.get("semantic_summary"),
                        message,
                    )
                ):
                    return str(row[0])
        if "calendar" in channels:
            for row in self.connection.execute(
                "SELECT calendar_id, data FROM calendar_events ORDER BY calendar_id"
            ):
                event = json.loads(str(row[1]))
                created_at = str(event.get("available_at", self.current_time))
                message = "\n".join(
                    (str(event.get("subject", "")), str(event.get("description", "")))
                )
                if (
                    event.get("status") != "cancelled"
                    and event.get("organizer_role") == role
                    and recipient in set(_list(event.get("participants")))
                    and _time_value(available_at) <= _time_value(created_at)
                    and _time_value(created_at) <= _time_value(self.current_time)
                    and all(
                        _normalized_text(fact) in _normalized_text(message)
                        for fact in message_facts
                    )
                    and self._envelope_supports_business_effect(
                        event.get("semantic_envelope"),
                        related,
                        evidence,
                        semantic,
                        event.get("semantic_summary"),
                        message,
                    )
                ):
                    return str(row[0])
        return None

    def _crm_projection_effect(
        self, requirement: Mapping[str, Any], available_at: str
    ) -> str | None:
        record_id = str(requirement["record_id"])
        row = self.connection.execute(
            "SELECT data FROM crm_records WHERE record_id = ?", (record_id,)
        ).fetchone()
        if row is None:
            return None
        current = json.loads(str(row[0]))
        exact = dict(requirement["exact_fields"])
        nonempty = {str(item) for item in requirement["nonempty_fields"]}
        number_ranges = dict(requirement["number_ranges"])
        date_ranges = dict(requirement["date_ranges"])
        required_fields = {str(item) for item in requirement["write_fields"]}
        recovery_overwrite: set[str] = set()
        for application in self.connection.execute(
            "SELECT effects FROM causal_action_applications WHERE checkpoint = ? ORDER BY action_key",
            (self.current_checkpoint_index,),
        ):
            effects = json.loads(str(application[0]))
            if not isinstance(effects, Mapping):
                continue
            for effect_id, support in effects.items():
                rule = self.action_effect_rules.get(str(effect_id))
                if (
                    rule is None
                    or rule.fact_type != "crm_transition"
                    or rule.record_id != record_id
                    or not isinstance(support, Mapping)
                    or not self._effect_still_valid(rule, support)
                ):
                    continue
                support_row = self.connection.execute(
                    "SELECT changes FROM crm_history WHERE history_id = ?",
                    (support.get("support_id"),),
                ).fetchone()
                changes = (
                    json.loads(str(support_row[0])) if support_row is not None else None
                )
                if isinstance(changes, Mapping) and all(
                    current.get(key) == value for key, value in changes.items()
                ):
                    recovery_overwrite.update(str(key) for key in changes)

        current_fields = (
            set(exact)
            | nonempty
            | set(number_ranges)
            | set(date_ranges)
            | set(requirement["text_reference_fields"])
            | {
                str(reference)
                for references in requirement["text_reference_fields"].values()
                for reference in references
            }
        ) - recovery_overwrite
        if not _crm_projection_fields_match(requirement, current, current_fields):
            return None
        history_ids: list[int] = []
        for history in self.connection.execute(
            "SELECT history_id, changed_at, role, changes FROM crm_history WHERE record_id = ? ORDER BY history_id",
            (record_id,),
        ):
            if (
                history[2] != requirement["writer_role"]
                or _time_value(str(history[1])) < _time_value(available_at)
                or _time_value(str(history[1])) > _time_value(self.current_time)
            ):
                continue
            changes = json.loads(str(history[3]))
            if (
                isinstance(changes, Mapping)
                and required_fields <= set(changes)
                and _crm_projection_fields_match(requirement, changes, required_fields)
            ):
                history_ids.append(int(history[0]))
        if not history_ids:
            return None
        return digest({"record_id": record_id, "history_ids": history_ids})

    def _deliverable_effect(
        self, requirement: Mapping[str, Any], available_at: str
    ) -> str | None:
        related_id = str(requirement["related_id"])
        evidence = {str(item) for item in requirement["required_evidence_ids"]}
        semantic = requirement["semantic_requirements"]
        for row in self.connection.execute(
            "SELECT document_id, data, version FROM documents ORDER BY document_id"
        ):
            document = json.loads(str(row[1]))
            created_at = str(document.get("created_at", self.current_time))
            content = "\n".join(
                (str(document.get("title", "")), str(document.get("content", "")))
            ).casefold()
            metadata = document.get("metadata")
            envelope = (
                metadata.get("semantic_envelope")
                if isinstance(metadata, Mapping)
                else None
            )
            linked = self.connection.execute(
                "SELECT 1 FROM document_links WHERE document_id = ? AND related_type = ? AND related_id = ?",
                (row[0], requirement["related_type"], related_id),
            ).fetchone()
            if (
                document.get("author_role") == requirement["author_role"]
                and document.get("kind") == requirement["kind"]
                and int(row[2]) >= int(requirement["minimum_version"])
                and _time_value(available_at) <= _time_value(created_at)
                and _time_value(created_at) <= _time_value(self.current_time)
                and linked is not None
                and self._document_is_brokered(document)
                and self._envelope_supports_business_effect(
                    envelope,
                    related_id,
                    evidence,
                    semantic,
                    metadata.get("semantic_summary")
                    if isinstance(metadata, Mapping)
                    else None,
                    content,
                )
            ):
                return str(row[0])
        return None

    def _milestone_business_effects(
        self, definition: MilestoneDefinition, resolution: str
    ) -> dict[str, str]:
        requirements = definition.business_effect_requirements_by_resolution.get(
            resolution
        )
        if requirements is None:
            raise EngineError("checkpoint business effect resolution is invalid")
        available_at = str(definition.chronology["available_at"])
        effects = {
            "decision_followup": self._decision_followup_effect(
                requirements["decision_followup"], available_at
            ),
            "crm_projection": self._crm_projection_effect(
                requirements["crm_projection"], available_at
            ),
            "deliverable": self._deliverable_effect(
                requirements["deliverable"], available_at
            ),
        }
        if (
            resolution in {"accepted", "remedied"}
            and (requirement := definition.approval_requirement) is not None
        ):
            effects["approval"] = self._matching_approval_id(requirement, available_at)
        if any(value is None for value in effects.values()):
            raise EngineError("checkpoint business effects are incomplete or invalid")
        return {key: str(value) for key, value in effects.items()}

    def _insert_milestone_resolution(self, value: Mapping[str, Any]) -> None:
        resolution = milestone_resolution(value)
        existing = self.connection.execute(
            "SELECT resolution, decision_artifact_ids, evidence_ids, authority_resolutions, business_effects, effective_at, remedy_of FROM milestone_resolutions WHERE milestone_id = ?",
            (resolution.milestone_id,),
        ).fetchone()
        serialized = (
            resolution.resolution,
            to_json(resolution.decision_artifact_ids),
            to_json(resolution.evidence_ids),
            to_json(resolution.authority_resolutions),
            to_json(resolution.business_effects),
            resolution.effective_at,
            resolution.remedy_of,
        )
        if existing is not None:
            if tuple(existing) != serialized:
                raise ImmutableError("milestone resolution is immutable")
            return
        self.connection.execute(
            "INSERT INTO milestone_resolutions(milestone_id, resolution, decision_artifact_ids, evidence_ids, authority_resolutions, business_effects, effective_at, remedy_of) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (resolution.milestone_id, *serialized),
        )
        self._rebuild_causal_lanes()

    def _selected_decision_ids(
        self, definition: MilestoneDefinition
    ) -> tuple[str, ...]:
        if definition.branch_id is None:
            return definition.decision_artifact_ids
        row = self.connection.execute(
            "SELECT selected_decision_artifact_ids FROM causal_branch_resolutions WHERE branch_id = ?",
            (definition.branch_id,),
        ).fetchone()
        if row is None:
            raise EngineError("causal branch is unresolved")
        selected = tuple(json.loads(str(row[0])))
        if not set(selected) <= set(definition.decision_artifact_ids):
            raise EngineError("causal branch evidence is invalid")
        return selected

    def _authority_resolutions(
        self,
        definition: MilestoneDefinition,
        decision_artifact_ids: tuple[str, ...],
    ) -> tuple[tuple[dict[str, Any], ...], str]:
        decisions = {
            artifact_id: self._artifact_row(artifact_id)
            for artifact_id in decision_artifact_ids
        }
        authority_resolutions: list[dict[str, Any]] = []
        for requirement in definition.authority_requirements:
            actor_id = str(requirement["actor_id"])
            matching = [
                artifact_id
                for artifact_id in decision_artifact_ids
                if artifact_id in set(requirement["decision_artifact_ids"])
            ]
            if len(matching) != 1:
                raise EngineError("checkpoint authority evidence is incomplete")
            artifact_id = matching[0]
            payload = decisions[artifact_id].get("structured_payload")
            authored = (
                payload.get("authority_decisions")
                if isinstance(payload, Mapping)
                else None
            )
            if not isinstance(authored, Sequence) or isinstance(authored, (str, bytes)):
                raise EngineError("checkpoint authority evidence is invalid")
            entries = [
                entry
                for entry in authored
                if isinstance(entry, Mapping) and entry.get("actor_id") == actor_id
            ]
            if len(entries) != 1:
                raise EngineError("checkpoint authority evidence is incomplete")
            entry = entries[0]
            resolution = str(entry.get("resolution", ""))
            effective_at = str(entry.get("effective_at", ""))
            actor_row = self.connection.execute(
                "SELECT data FROM actors WHERE actor_id = ?", (actor_id,)
            ).fetchone()
            actor = json.loads(str(actor_row[0])) if actor_row is not None else None
            authority = actor.get("authority") if isinstance(actor, Mapping) else None
            if (
                resolution not in {"accepted", "rejected", "deferred", "remedied"}
                or len(authored) != 1
                or set(_list(entry.get("rights"))) != set(requirement["rights"])
                or not isinstance(actor, Mapping)
                or self._organization_scope(actor) != requirement["organization_scope"]
                or not isinstance(authority, Mapping)
                or not isinstance(payload, Mapping)
                or payload.get("decision_state") != resolution
                or payload.get("author_actor_id") != actor_id
                or list(decisions[artifact_id].get("source_actor_ids", ()))
                != [actor_id]
                or definition.gate_id not in set(authority.get("gate_ids", ()))
                or not set(requirement["rights"]) <= set(authority.get("rights", ()))
                or _time_value(effective_at) < _time_value(str(actor["active_from"]))
                or actor.get("active_until") is not None
                and _time_value(effective_at) >= _time_value(str(actor["active_until"]))
                or _time_value(effective_at)
                > _time_value(str(decisions[artifact_id]["created_at"]))
                or _time_value(str(decisions[artifact_id]["available_at"]))
                > _time_value(self.current_time)
            ):
                raise EngineError("checkpoint authority is invalid")
            authority_resolutions.append(
                {
                    "actor_id": actor_id,
                    "decision_artifact_id": artifact_id,
                    "organization_scope": requirement["organization_scope"],
                    "resolution": resolution,
                    "rights": tuple(requirement["rights"]),
                }
            )
        states = {item["resolution"] for item in authority_resolutions}
        if "rejected" in states:
            resolution = "rejected"
        elif "deferred" in states:
            resolution = "deferred"
        elif states and states <= {"accepted", "remedied"}:
            resolution = "accepted"
        else:
            raise EngineError("checkpoint authority decision is incomplete")
        return tuple(authority_resolutions), resolution

    def _resolve_checkpoint_milestone(self, checkpoint_id: str) -> None:
        definition = self._milestone_for_checkpoint(checkpoint_id)
        if definition is None:
            return
        if self.connection.execute(
            "SELECT 1 FROM milestone_resolutions WHERE milestone_id = ?",
            (definition.milestone_id,),
        ).fetchone():
            return
        prerequisite_rows = {
            str(row[0]): str(row[1])
            for row in self.connection.execute(
                "SELECT milestone_id, resolution FROM milestone_resolutions"
            )
        }
        if not set(definition.prerequisite_milestone_ids) <= set(prerequisite_rows):
            raise EngineError("checkpoint milestone prerequisites are unresolved")
        blocked = next(
            (
                milestone_id
                for milestone_id in definition.prerequisite_milestone_ids
                if prerequisite_rows[milestone_id] not in {"accepted", "remedied"}
            ),
            None,
        )
        if (
            blocked is not None
            and definition.remedy_of == blocked
            and self.milestone_definitions[blocked].terminal_outcome_by_resolution.get(
                prerequisite_rows[blocked]
            )
            is not None
        ):
            raise EngineError("terminal milestone cannot be remedied")
        if blocked is not None and definition.remedy_of != blocked:
            self._insert_milestone_resolution(
                {
                    "milestone_id": definition.milestone_id,
                    "resolution": "inapplicable",
                    "decision_artifact_ids": [],
                    "evidence_ids": [],
                    "authority_resolutions": [],
                    "business_effects": {},
                    "effective_at": self.current_time,
                    "remedy_of": None,
                }
            )
            return
        reads = self._successful_evidence_reads()
        selected_decisions = self._selected_decision_ids(definition)
        submitted_by_role: dict[str, set[str]] = {}
        for role in definition.evidence_requirements_by_role:
            submitted: set[str] = set()
            for artifact_id in reads.get(role, set()):
                row = self.connection.execute(
                    "SELECT data FROM artifacts WHERE artifact_id = ?", (artifact_id,)
                ).fetchone()
                if row is None:
                    continue
                artifact = json.loads(str(row[0]))
                if artifact.get("gate_id") != definition.gate_id:
                    continue
                self._artifact_row(artifact_id)
                submitted.add(artifact_id)
            submitted_by_role[role] = submitted
        if any(
            required and not submitted_by_role.get(role)
            for role, required in definition.evidence_requirements_by_role.items()
        ) or not set(selected_decisions) <= submitted_by_role.get(
            definition.decision_evidence_role, set()
        ):
            raise EngineError("checkpoint evidence is incomplete or invalid")
        submitted_evidence = set().union(*submitted_by_role.values())
        authority_resolutions, resolution = self._authority_resolutions(
            definition, selected_decisions
        )
        if (
            blocked is not None
            and definition.remedy_of == blocked
            and resolution == "accepted"
        ):
            resolution = "remedied"
        if resolution not in definition.allowed_resolutions:
            raise EngineError("checkpoint decision evidence is invalid")
        checkpoint = self.current_checkpoint()
        if (
            checkpoint is None
            or checkpoint.get("checkpoint_id") != definition.checkpoint_id
            or checkpoint.get("sequence") != definition.chronology.get("sequence")
            or checkpoint.get("available_at")
            != definition.chronology.get("available_at")
        ):
            raise EngineError("checkpoint authority is invalid")
        if (
            resolution in {"accepted", "remedied"}
            and definition.approval_requirement is not None
            and not self._approval_satisfies(
                definition.approval_requirement,
                str(definition.chronology["available_at"]),
            )
        ):
            raise EngineError("checkpoint approval is incomplete or invalid")
        business_effects = self._milestone_business_effects(definition, resolution)
        self._insert_milestone_resolution(
            MilestoneResolution(
                definition.milestone_id,
                resolution,
                selected_decisions,
                tuple(sorted(submitted_evidence)),
                authority_resolutions,
                business_effects,
                self.current_time,
                definition.remedy_of if resolution == "remedied" else None,
            ).to_dict()
        )
        if definition.terminal_outcome_by_resolution.get(resolution) is not None:
            self._seal_terminal_path(definition)

    def _seal_terminal_path(self, definition: MilestoneDefinition) -> None:
        terminal_position = int(definition.chronology["sequence"])
        row = self.connection.execute(
            "SELECT data FROM checkpoints WHERE position = ?", (terminal_position,)
        ).fetchone()
        if row is None:
            raise EngineError("terminal checkpoint does not exist")
        checkpoint = json.loads(str(row[0]))
        checkpoint["status"] = "complete"
        checkpoint["terminal"] = True
        self.connection.execute(
            "UPDATE checkpoints SET data = ? WHERE position = ?",
            (to_json(checkpoint), terminal_position),
        )
        descendants = sorted(
            (
                candidate
                for candidate in self.milestone_definitions.values()
                if int(candidate.chronology["sequence"]) > terminal_position
            ),
            key=lambda candidate: int(candidate.chronology["sequence"]),
        )
        for candidate in descendants:
            position = int(candidate.chronology["sequence"])
            self._set_checkpoint_status(position, "skipped")
            if self.connection.execute(
                "SELECT 1 FROM milestone_resolutions WHERE milestone_id = ?",
                (candidate.milestone_id,),
            ).fetchone():
                continue
            self._insert_milestone_resolution(
                MilestoneResolution(
                    candidate.milestone_id,
                    "inapplicable",
                    (),
                    (),
                    (),
                    {},
                    self.current_time,
                    None,
                ).to_dict()
            )
        self.finalize_terminal_outcome()

    def finalize_terminal_outcome(self) -> dict[str, Any]:
        existing = self._meta("terminal_outcome")
        if existing is not None:
            return {
                "terminal_outcome": existing,
                "support": json.loads(self._meta("terminal_support") or "{}"),
            }
        terminal: list[tuple[str, dict[str, Any]]] = []
        for resolution in self.milestone_resolutions():
            definition = self.milestone_definitions.get(resolution["milestone_id"])
            if definition is None:
                raise EngineError("milestone resolution has no definition")
            outcome = definition.terminal_outcome_by_resolution.get(
                resolution["resolution"]
            )
            if outcome is not None:
                terminal.append((outcome, resolution))
        if len(terminal) != 1:
            raise EngineError("run requires exactly one supported terminal resolution")
        outcome, resolution = terminal[0]
        support = {"milestone": resolution}
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

    def _all_actors(self) -> list[dict[str, Any]]:
        return [
            json.loads(str(row[0]))
            for row in self.connection.execute(
                "SELECT data FROM actors ORDER BY actor_id"
            )
        ]

    def _recipient_actors(
        self, role: str, recipients: Sequence[str] | str, allow_roles: bool = False
    ) -> list[dict[str, Any]]:
        values = _list(recipients)
        if not values or any(not isinstance(item, str) or not item for item in values):
            raise AuthorizationError("recipients must be a non-empty roster list")
        actors = []
        for recipient in values:
            if allow_roles and recipient in SELLER_ROLES:
                actors.append(
                    {
                        "organization_id": self.scenario.seller_org_id,
                        "role_tags": [recipient],
                    }
                )
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
                <= _time_value(self.current_time)
            ):
                raise AuthorizationError("recipient is not available")
            actors.append(actor)
        return actors

    def _require_external_contact(
        self, role: str, actors: Sequence[Mapping[str, Any]]
    ) -> None:
        if (
            any(self._external_actor(actor) for actor in actors)
            and self._supported_terminal_outcome() is not None
        ):
            raise AuthorizationError(
                "external contact is unavailable after disposition"
            )
        if (
            any(self._external_actor(actor) for actor in actors)
            and not self._role_grant(role).can_contact_external
        ):
            raise AuthorizationError(
                f"role {role!r} cannot contact external recipients"
            )

    def _semantic_envelope(self, value: Mapping[str, Any] | None) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise EngineError("semantic_envelope is required")
        required = {
            "target_actor_id",
            "purpose",
            "purpose_code",
            "gate_id",
            "resolution",
            "related_records",
            "requested_decisions",
            "decision_codes",
            "commitments",
            "commitment_codes",
            "commitment_owner_role",
            "commitment_due_at",
            "decision_due_at",
            "attachments",
            "evidence_claims",
        }
        scalar_fields = {
            "target_actor_id",
            "commitment_owner_role",
            "gate_id",
            "purpose",
            "purpose_code",
            "resolution",
        }
        list_fields = {
            "attachments",
            "commitment_codes",
            "commitments",
            "decision_codes",
            "related_records",
            "requested_decisions",
        }
        if set(value) != required or any(
            not isinstance(value.get(field), str) or not value[field]
            for field in scalar_fields
        ):
            raise EngineError("semantic_envelope is invalid")
        for key in list_fields:
            items = value.get(key)
            if (
                not isinstance(items, Sequence)
                or isinstance(items, (str, bytes))
                or any(not isinstance(item, str) for item in items)
                or (key == "related_records" and not items)
            ):
                raise EngineError("semantic_envelope is invalid")
        claims = value.get("evidence_claims")
        if (
            not isinstance(claims, Sequence)
            or isinstance(claims, (str, bytes))
            or any(
                not isinstance(claim, Mapping)
                or set(claim) != {"artifact_id", "claim_type", "gate_id", "resolution"}
                or any(not isinstance(item, str) or not item for item in claim.values())
                for claim in claims
            )
        ):
            raise EngineError("semantic_envelope is invalid")
        decisions = list(value["requested_decisions"])
        decision_codes = list(value["decision_codes"])
        commitments = list(value["commitments"])
        commitment_codes = list(value["commitment_codes"])
        decision_due_at = value["decision_due_at"]
        commitment_due_at = value["commitment_due_at"]
        checkpoint = self.current_checkpoint()
        due_at_values = (
            (decisions, decision_codes, decision_due_at),
            (commitments, commitment_codes, commitment_due_at),
        )
        if (
            value["purpose_code"] not in SEMANTIC_PURPOSE_LABELS
            or any(code not in SEMANTIC_DECISION_LABELS for code in decision_codes)
            or any(code not in SEMANTIC_COMMITMENT_LABELS for code in commitment_codes)
            or any(
                len(texts) != len(codes)
                or bool(texts) != (due_at is not None)
                or due_at is not None
                and (
                    not isinstance(due_at, str)
                    or _time_value(due_at) < _time_value(self.current_time)
                    or checkpoint is not None
                    and _time_value(due_at) > _time_value(str(checkpoint["window_end"]))
                )
                for texts, codes, due_at in due_at_values
            )
        ):
            raise EngineError("semantic_envelope is invalid")
        if any(
            not self._related_entity_exists(str(record_id))
            for record_id in value["related_records"]
        ):
            raise EngineError("semantic_envelope related record does not exist")
        return {
            **{key: value[key] for key in sorted(scalar_fields)},
            **{key: list(value[key]) for key in sorted(list_fields)},
            "commitment_due_at": commitment_due_at,
            "decision_due_at": decision_due_at,
            "evidence_claims": [dict(claim) for claim in claims],
        }

    def _related_entity_exists(self, identifier: str) -> bool:
        if identifier in {
            self.scenario.seller_org_id,
            self.scenario.buyer_org_id,
        }:
            return True
        return bool(
            self.connection.execute(
                "SELECT 1 FROM crm_records WHERE record_id = ?", (identifier,)
            ).fetchone()
        )

    def _semantic_summary(self, envelope: Mapping[str, Any]) -> str:
        row = self.connection.execute(
            "SELECT data FROM actors WHERE actor_id = ?",
            (envelope["target_actor_id"],),
        ).fetchone()
        if row is None:
            raise EngineError("semantic target is not available")
        actor = json.loads(str(row[0]))
        label = str(actor.get("display_name") or actor.get("email") or "recipient")
        return semantic_envelope_summary(envelope, label)

    @staticmethod
    def _semantic_target_is_recipient(
        envelope: Mapping[str, Any], actors: Sequence[Mapping[str, Any]]
    ) -> bool:
        return str(envelope["target_actor_id"]) in {
            str(actor.get("actor_id", "")) for actor in actors
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
        effective_at, recorded_at, available_at = (
            _time_value(timestamp)
            for timestamp in (
                event.effective_at,
                event.recorded_at,
                event.available_at,
            )
        )
        if not effective_at <= recorded_at <= available_at:
            raise EngineError("event chronology is invalid")
        for actor_id in event.actor_ids:
            row = self.connection.execute(
                "SELECT data FROM actors WHERE actor_id = ?", (actor_id,)
            ).fetchone()
            if row is None:
                raise EngineError("event actor is unavailable")
            actor = json.loads(str(row[0]))
            active_until = actor.get("active_until")
            if _time_value(str(actor["active_from"])) > effective_at or (
                event.kind == "stakeholder_departed"
                and (
                    active_until is None
                    or _time_value(str(active_until)) != effective_at
                )
                or event.kind != "stakeholder_departed"
                and active_until is not None
                and _time_value(str(active_until)) <= available_at
            ):
                raise EngineError("event actor chronology is invalid")
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
        created_at = _time_value(artifact.created_at)
        available_at = _time_value(artifact.available_at)
        if created_at > available_at:
            raise EngineError("artifact chronology is invalid")
        for actor_id in {
            *artifact.source_actor_ids,
            *artifact.recipient_actor_ids,
        }:
            row = self.connection.execute(
                "SELECT data FROM actors WHERE actor_id = ?", (actor_id,)
            ).fetchone()
            if row is None:
                raise EngineError("artifact actor is unavailable")
            actor = json.loads(str(row[0]))
            if _time_value(str(actor["active_from"])) > created_at or (
                actor.get("active_until") is not None
                and _time_value(str(actor["active_until"])) <= available_at
            ):
                raise EngineError("artifact actor chronology is invalid")
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

    def execute_agent_write(
        self,
        action_key: str,
        role: str,
        tool_name: str,
        arguments: Mapping[str, Any],
        callback: Callable[[], Any],
    ) -> Any:
        outer = self._transaction_depth == 0
        if outer:
            self.connection.execute("BEGIN")
        self._transaction_depth += 1
        committed = False
        try:
            result = callback()
            if not isinstance(result, Mapping):
                raise EngineError("write result must be an object")
            value = {
                **result,
                "write_scope": self._write_scope(tool_name, arguments, result),
            }
            self.apply_agent_action(action_key, role, tool_name, arguments, value)
            if outer:
                self.connection.execute("COMMIT")
                committed = True
                self._sync_trace_file()
            return value
        except Exception:
            if outer and not committed:
                self.connection.execute("ROLLBACK")
            raise
        finally:
            self._transaction_depth -= 1

    def _write_scope(
        self,
        tool_name: str,
        arguments: Mapping[str, Any],
        result: Mapping[str, Any],
    ) -> dict[str, Any]:
        related: list[str] = []
        classification: str | None = None
        mode = WRITE_SCOPE_MODES.get(tool_name)
        if mode is None:
            raise EngineError("write scope mode is not defined")
        if mode == "argument_record":
            related = [str(arguments.get("record_id", ""))]
        elif mode == "argument_records":
            related = [
                str(arguments.get("source_id", "")),
                str(arguments.get("target_id", "")),
            ]
        elif mode == "argument_related":
            related = [str(arguments.get("related_id", ""))]
        elif mode == "argument_envelope":
            envelope = arguments.get("semantic_envelope")
            if isinstance(envelope, Mapping):
                related = [str(item) for item in _list(envelope.get("related_records"))]
        elif mode == "result_envelope":
            envelope = result.get("semantic_envelope")
            if isinstance(envelope, Mapping):
                related = [str(item) for item in _list(envelope.get("related_records"))]
        elif mode == "checkpoint_coordination":
            checkpoint = self.current_checkpoint()
            if checkpoint is not None and arguments.get(
                "checkpoint_id"
            ) == checkpoint.get("checkpoint_id"):
                classification = mode
        elif mode == "checkpoint_completion" and arguments.get(
            "checkpoint_id"
        ) == result.get("checkpoint_id"):
            classification = mode
        related = sorted(set(filter(None, related)))
        if not related and classification not in WRITE_SCOPE_CLASSIFICATIONS:
            raise EngineError("write must be linked or classified")
        return {"related_records": related, "classification": classification}

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
            if table == "events" and not self._condition_is_selected(data):
                continue
            if table == "artifacts" and not self._artifact_is_selected(
                str(values.get("artifact_id", data.get("artifact_id", "")))
            ):
                continue
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
            visible_data = data if role == "system" else _agent_safe(data)
            haystack = json.dumps(
                visible_data, ensure_ascii=False, sort_keys=True
            ).casefold()
            if needle and needle not in haystack:
                continue
            result.append(visible_data)
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
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        if role not in SELLER_ROLES:
            raise AuthorizationError(f"invalid external role: {role!r}")
        self._authorize(role, "run", "complete_checkpoint")

        def complete() -> dict[str, Any]:
            checkpoint = self.current_checkpoint()
            if checkpoint is None or checkpoint["checkpoint_id"] != checkpoint_id:
                raise EngineError("checkpoint is not active")
            if checkpoint["status"] != "active":
                raise EngineError("checkpoint is not active")
            if role not in checkpoint["required_roles"]:
                raise AuthorizationError("role is not required for this checkpoint")
            self.connection.execute(
                "INSERT INTO checkpoint_completions(checkpoint_id, role, completed_at) VALUES (?, ?, ?)",
                (checkpoint_id, role, self.current_time),
            )
            if self._required_roles_complete(checkpoint):
                self._resolve_checkpoint_milestone(checkpoint_id)
            result = {
                "checkpoint_id": checkpoint_id,
                "role": role,
                "completed_at": self.current_time,
            }
            if terminal_outcome := self._supported_terminal_outcome():
                result["terminal_outcome"] = terminal_outcome
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

    def _effect_still_valid(
        self, rule: ActionEffectRule, support: Mapping[str, Any]
    ) -> bool:
        if support.get("fact_type") != rule.fact_type or (
            rule.fact_type != "authority_decision_observed"
            and not self._rule_evidence_is_grounded(rule)
        ):
            return False
        support_id = support.get("support_id")
        action_checkpoint = self.connection.execute(
            "SELECT position, data FROM checkpoints WHERE checkpoint_id = ?",
            (rule.checkpoint_id,),
        ).fetchone()
        if (
            rule.fact_type == "crm_transition"
            and action_checkpoint is not None
            and self.current_checkpoint_index > int(action_checkpoint[0])
        ):
            history = self.connection.execute(
                "SELECT record_id, role, changed_at, changes FROM crm_history WHERE history_id = ?",
                (support_id,),
            ).fetchone()
            if history is None:
                return False
            changes = json.loads(str(history[3]))
            checkpoint = json.loads(str(action_checkpoint[1]))
            later = self.connection.execute(
                "SELECT changes FROM crm_history WHERE record_id = ? AND history_id > ? AND changed_at <= ? ORDER BY history_id",
                (rule.record_id, support_id, checkpoint["available_at"]),
            ).fetchall()
            return bool(
                history[0] == rule.record_id
                and history[1] == rule.role
                and history[2] == checkpoint["available_at"]
                and changes.get("next_step_gate_id") == rule.next_gate_id
                and changes.get("next_step_type") == rule.next_step_type
                and all(
                    not (
                        "next_step_gate_id"
                        in (later_changes := json.loads(str(row[0])))
                        and later_changes["next_step_gate_id"] != rule.next_gate_id
                        or "next_step_type" in later_changes
                        and later_changes["next_step_type"] != rule.next_step_type
                    )
                    for row in later
                )
            )
        result = {
            "message_id": support.get("request_id", support_id),
            "calendar_id": support_id,
            "document_id": support_id,
            "approval_id": support_id,
            "record_id": rule.record_id,
        }
        if rule.fact_type == "authority_decision_observed":
            decision = self._authority_decision_support(rule, result)
            return bool(
                decision is not None and decision.get("decision_id") == support_id
            )
        return {
            "crm_transition": self._crm_transition_support,
            "internal_approval": self._approval_support,
        }[rule.fact_type](rule, result) is not None

    def _insert_branch_resolution(self, value: Mapping[str, Any]) -> None:
        resolution = branch_resolution(value)
        existing = self.connection.execute(
            "SELECT option, effect_ids, action_keys, selected_decision_artifact_ids, resolved_at FROM causal_branch_resolutions WHERE branch_id = ?",
            (resolution.branch_id,),
        ).fetchone()
        serialized = (
            resolution.option,
            to_json(resolution.effect_ids),
            to_json(resolution.action_keys),
            to_json(resolution.selected_decision_artifact_ids),
            resolution.resolved_at,
        )
        if existing is not None:
            if tuple(existing) != serialized:
                raise ImmutableError("causal branch resolution is immutable")
            return
        self.connection.execute(
            "INSERT INTO causal_branch_resolutions(branch_id, option, effect_ids, action_keys, selected_decision_artifact_ids, resolved_at) VALUES (?, ?, ?, ?, ?, ?)",
            (resolution.branch_id, *serialized),
        )

    def _resolve_branches_from_checkpoint(self, checkpoint_id: str) -> None:
        branches = sorted(
            (
                branch
                for branch in self.branch_definitions.values()
                if branch.resolution_checkpoint_id == checkpoint_id
            ),
            key=lambda item: item.branch_id,
        )
        for branch in branches:
            if self.connection.execute(
                "SELECT 1 FROM causal_branch_resolutions WHERE branch_id = ?",
                (branch.branch_id,),
            ).fetchone():
                continue
            support_by_effect: dict[str, tuple[str, Mapping[str, Any]]] = {}
            action_position = self.connection.execute(
                "SELECT position FROM checkpoints WHERE checkpoint_id = ?",
                (branch.action_checkpoint_id,),
            ).fetchone()
            if action_position is None:
                raise EngineError("branch action checkpoint does not exist")
            for row in self.connection.execute(
                "SELECT action_key, effects FROM causal_action_applications WHERE checkpoint = ? ORDER BY action_key",
                (int(action_position[0]),),
            ):
                effects = json.loads(str(row[1]))
                if not isinstance(effects, Mapping):
                    continue
                for effect_id, support in effects.items():
                    rule = self.action_effect_rules.get(str(effect_id))
                    if (
                        rule is not None
                        and rule.branch_id == branch.branch_id
                        and isinstance(support, Mapping)
                        and self._effect_still_valid(rule, support)
                    ):
                        support_by_effect[str(effect_id)] = (str(row[0]), support)
            selected_option = next(
                (
                    option
                    for option in branch.success_if_any
                    if set(option) <= set(support_by_effect)
                ),
                None,
            )
            success = branch.recoverable and selected_option is not None
            selected_effects = tuple(selected_option or ()) if success else ()
            action_keys = (
                tuple(
                    sorted(
                        {
                            support_by_effect[effect_id][0]
                            for effect_id in selected_effects
                        }
                    )
                )
                if success
                else ()
            )
            selected_artifacts = (
                branch.success_decision_artifact_ids
                if success
                else branch.fallback_decision_artifact_ids
            )
            self._insert_branch_resolution(
                BranchResolution(
                    branch.branch_id,
                    "success" if success else "fallback",
                    selected_effects,
                    action_keys,
                    selected_artifacts,
                    self.current_time,
                ).to_dict()
            )

    def _resolve_terminal_fallbacks(self, checkpoint_id: str) -> None:
        for branch in self.branch_definitions.values():
            if branch.resolution_checkpoint_id != checkpoint_id:
                continue
            row = self.connection.execute(
                "SELECT option, selected_decision_artifact_ids FROM causal_branch_resolutions WHERE branch_id = ?",
                (branch.branch_id,),
            ).fetchone()
            if row is None or row[0] != "fallback":
                continue
            definition = self.milestone_definitions[branch.remedy_milestone_id]
            if self.connection.execute(
                "SELECT 1 FROM milestone_resolutions WHERE milestone_id = ?",
                (definition.milestone_id,),
            ).fetchone():
                continue
            decisions = tuple(json.loads(str(row[1])))
            authority_resolutions, resolution = self._authority_resolutions(
                definition, decisions
            )
            if definition.terminal_outcome_by_resolution.get(resolution) is None:
                continue
            prerequisites = {
                str(value[0]): str(value[1])
                for value in self.connection.execute(
                    "SELECT milestone_id, resolution FROM milestone_resolutions"
                )
            }
            if not set(definition.prerequisite_milestone_ids) <= set(prerequisites):
                raise EngineError("terminal fallback prerequisites are unresolved")
            self._insert_milestone_resolution(
                MilestoneResolution(
                    definition.milestone_id,
                    resolution,
                    decisions,
                    decisions,
                    authority_resolutions,
                    {},
                    self.current_time,
                    None,
                ).to_dict()
            )
            self._seal_terminal_path(definition)

    def advance_checkpoint(
        self, budget_exhausted: bool = False, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        if not isinstance(budget_exhausted, bool):
            raise AuthorizationError("checkpoint advancement is system-only")

        def advance() -> dict[str, Any]:
            if self._supported_terminal_outcome() is not None:
                raise EngineError("terminal outcome is immutable")
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
            forecast_observations = (
                [
                    {
                        "record_id": str(record[0]),
                        "cutoff_sequence": int(checkpoint["sequence"]),
                        "cutoff_at": str(checkpoint["forecast_cutoff_at"]),
                        "forecast_probability": json.loads(str(record[1])).get(
                            "forecast_probability"
                        ),
                    }
                    for record in self.connection.execute(
                        "SELECT record_id, data FROM crm_records ORDER BY record_id"
                    )
                ]
                if current is not None
                else []
            )
            self._set_clock(str(checkpoint["available_at"]))
            self._set_meta("current_checkpoint", str(next_position))
            checkpoint = self._set_checkpoint_status(next_position, "active")
            self._resolve_branches_from_checkpoint(str(checkpoint["checkpoint_id"]))
            released_event_ids = self.release_available_events()
            self._resolve_terminal_fallbacks(str(checkpoint["checkpoint_id"]))
            checkpoint = self.current_checkpoint() or checkpoint
            result = {
                "checkpoint": checkpoint,
                "current_time": self.current_time,
                "status": self.status,
                "budget_exhausted": budget_exhausted,
                "released_event_ids": released_event_ids,
                "forecast_observations": forecast_observations,
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
            "checkpoint": self.current_checkpoint()
            if role == "system"
            else _agent_checkpoint(self.current_checkpoint()),
            "status": self.status,
            "state_hash": self.state_hash(),
        }
        if role == "system":
            result["terminal_outcome"] = self._meta("terminal_outcome")
        else:
            now = _time_value(self.current_time)
            contacts = []
            for actor in self._all_actors():
                active_until = actor.get("active_until")
                if (
                    _time_value(str(actor["active_from"])) <= now
                    and (active_until is None or now < _time_value(str(active_until)))
                    and self._visible(str(actor["visibility"]), role, actor)
                ):
                    authority = actor.get("authority")
                    attributes = actor.get("attributes")
                    contacts.append(
                        {
                            "actor_id": actor["actor_id"],
                            "display_name": actor["display_name"],
                            "email": actor.get("email"),
                            "kind": actor["kind"],
                            "organization_id": actor["organization_id"],
                            "authority": {
                                "role_id": authority.get("role_id")
                                if isinstance(authority, Mapping)
                                else None
                            },
                            "job_title": attributes.get("job_title")
                            if isinstance(attributes, Mapping)
                            else None,
                        }
                    )
            result["active_contacts"] = contacts
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
                and self._supported_terminal_outcome() is None
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

    def seed_crm_projection(
        self, record_id: str, data: Mapping[str, Any], updated_at: str
    ) -> dict[str, Any]:
        _parse_time(updated_at)
        row = self.connection.execute(
            "SELECT data, version FROM crm_records WHERE record_id = ?", (record_id,)
        ).fetchone()
        value = dict(data)
        if row is None:
            return self.seed_crm_record(record_id, value, updated_at)
        previous = json.loads(str(row[0]))
        system_row = self.connection.execute(
            "SELECT snapshot FROM crm_history WHERE record_id = ? AND role = 'system' ORDER BY history_id DESC LIMIT 1",
            (record_id,),
        ).fetchone()
        system_snapshot = (
            json.loads(str(system_row[0])) if system_row is not None else previous
        )
        override_values: dict[str, Any] = {}
        override_history: dict[str, int] = {}
        system_history: dict[str, int] = {}
        history_rows = self.connection.execute(
            "SELECT history_id, role, changes, snapshot FROM crm_history WHERE record_id = ? ORDER BY history_id",
            (record_id,),
        )
        for history_row in history_rows:
            history_id = int(history_row[0])
            role = str(history_row[1])
            changes_row = json.loads(str(history_row[2]))
            snapshot_row = json.loads(str(history_row[3]))
            if not isinstance(changes_row, Mapping) or not isinstance(
                snapshot_row, Mapping
            ):
                continue
            if role == "system":
                for key in changes_row:
                    if key not in {"projection", "seed"}:
                        system_history[str(key)] = history_id
            else:
                for key in changes_row:
                    override_history[str(key)] = history_id
                    override_values[str(key)] = snapshot_row.get(key)
        for key, history_id in override_history.items():
            if history_id <= system_history.get(key, -1):
                override_values.pop(key, None)
        merged = dict(previous)
        changes: dict[str, Any] = {}
        for key, item in value.items():
            if key in override_values:
                if merged.get(key) != override_values[key]:
                    merged[key] = override_values[key]
                continue
            if (
                key not in previous or previous.get(key) == system_snapshot.get(key)
            ) and previous.get(key) != item:
                merged[key] = item
                changes[key] = item
        self.connection.execute(
            "INSERT INTO crm_history(record_id, changed_at, role, changes, snapshot) VALUES (?, ?, ?, ?, ?)",
            (
                record_id,
                updated_at,
                "system",
                to_json({"projection": True, **changes}),
                to_json(merged),
            ),
        )
        self.connection.execute(
            "UPDATE crm_records SET data = ?, updated_at = ?, version = version + 1 WHERE record_id = ?",
            (to_json(merged), updated_at, record_id),
        )
        return merged

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
        return {
            "record": data if role == "system" else _agent_safe(data),
            "updated_at": str(row[1]),
            "version": int(row[2]),
        }

    def crm_history(self, role: str, record_id: str) -> list[dict[str, Any]]:
        self._authorize(role, "crm", "read")
        self.crm_read(role, record_id)
        result = [
            {
                "record_id": str(row[0]),
                "changed_at": str(row[1]),
                "role": str(row[2]),
                "changes": json.loads(str(row[3]))
                if role == "system"
                else _agent_safe(json.loads(str(row[3]))),
                "snapshot": json.loads(str(row[4]))
                if role == "system"
                else _agent_safe(json.loads(str(row[4]))),
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
        if changes.get("stage") == "closed_won" and idempotency_key:
            cached = self.connection.execute(
                "SELECT 1 FROM idempotency WHERE key = ?", (idempotency_key,)
            ).fetchone()
            if cached is not None:
                return self._idempotent(idempotency_key, "crm.update", dict)
        if "forecast_probability" in changes:
            probability = changes["forecast_probability"]
            if (
                not isinstance(probability, (int, float))
                or isinstance(probability, bool)
                or not math.isfinite(probability)
                or not 0 <= probability <= 1
            ):
                raise EngineError(
                    "forecast_probability must be a finite number from 0 to 1"
                )

        if changes.get("stage") == "closed_won":
            checkpoint = self.current_checkpoint()
            definition = next(
                (
                    value
                    for value in self.milestone_definitions.values()
                    if checkpoint is not None
                    and value.checkpoint_id == checkpoint.get("checkpoint_id")
                ),
                None,
            )
            projections = (
                [
                    definition.business_effect_requirements_by_resolution[resolution][
                        "crm_projection"
                    ]
                    for resolution, outcome in definition.terminal_outcome_by_resolution.items()
                    if outcome == "closed_won"
                ]
                if definition is not None
                else []
            )
            current_row = self.connection.execute(
                "SELECT data FROM crm_records WHERE record_id = ?", (record_id,)
            ).fetchone()
            current = json.loads(str(current_row[0])) if current_row is not None else {}
            requirement = projections[0] if len(projections) == 1 else None
            required_fields = (
                {str(item) for item in requirement["write_fields"]}
                if requirement is not None
                else set()
            )
            constraint_fields = (
                {
                    *requirement["exact_fields"],
                    *requirement["nonempty_fields"],
                    *requirement["number_ranges"],
                    *requirement["date_ranges"],
                    *requirement["text_reference_fields"],
                    *(
                        reference
                        for references in requirement["text_reference_fields"].values()
                        for reference in references
                    ),
                }
                if requirement is not None
                else set()
            )
            projected = {**current, **changes}
            if (
                requirement is None
                or current.get("stage") == "closed_won"
                or requirement["record_id"] != record_id
                or requirement["writer_role"] != role
                or not required_fields <= set(changes)
                or not _crm_projection_fields_match(
                    requirement, changes, required_fields
                )
                or not _crm_projection_fields_match(
                    requirement, projected, constraint_fields
                )
            ):
                raise EngineError("closed_won CRM update is not grounded")

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
            value: dict[str, Any] = {
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
            visible_value = value if role == "system" else _agent_safe(value)
            if (
                needle
                and needle
                not in json.dumps(visible_value, ensure_ascii=False).casefold()
            ):
                continue
            result.append(visible_value)
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
        external = any(self._external_actor(actor) for actor in actors)
        if channel == "internal_chat" and external:
            raise AuthorizationError("internal chat recipients must be seller roles")
        self._authorize(
            role, "communications", "send_external" if external else "send_internal"
        )
        self._require_external_contact(role, actors)
        envelope = (
            self._semantic_envelope(semantic_envelope)
            if semantic_envelope is not None
            else None
        )
        if external and envelope is None:
            raise EngineError("semantic_envelope is required")
        if envelope is not None and not self._semantic_target_is_recipient(
            envelope, actors
        ):
            raise EngineError("semantic target must be a recipient")
        semantic_summary = self._semantic_summary(envelope) if envelope else None
        if external and envelope is not None:
            self._require_brokered_document_attachments(envelope)

        def send() -> dict[str, Any]:
            message_id = _stable_id(
                "message", self.manifest.run_id, idempotency_key or body
            )
            message_metadata = dict(metadata or {})
            if envelope is not None:
                message_metadata["semantic_envelope"] = envelope
                message_metadata["semantic_summary"] = semantic_summary
            value = {
                "message_id": message_id,
                "channel": channel,
                "direction": "outbound",
                "sender_role": role,
                "recipients": _list(recipients),
                "subject": (
                    semantic_summary.splitlines()[0]
                    if external and semantic_summary
                    else subject
                ),
                "body": semantic_summary if external and semantic_summary else body,
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
        external = any(self._external_actor(actor) for actor in actors)
        envelope = self._semantic_envelope(semantic_envelope)
        if not self._semantic_target_is_recipient(envelope, actors):
            raise EngineError("semantic target must be a calendar participant")
        semantic_summary = self._semantic_summary(envelope)

        def schedule() -> dict[str, Any]:
            if _time_value(end_at) < _time_value(start_at):
                raise EngineError("calendar end must not precede start")
            calendar_id = _stable_id(
                "calendar", self.manifest.run_id, idempotency_key or subject
            )
            value = {
                "calendar_id": calendar_id,
                "subject": semantic_summary.splitlines()[0] if external else subject,
                "start_at": start_at,
                "end_at": end_at,
                "participants": _list(participants),
                "description": (
                    semantic_summary
                    if external
                    else f"{description}\n\n{semantic_summary}"
                ),
                "status": "scheduled",
                "organizer_role": role,
                "available_at": self.current_time,
                "visibility": list(visibility),
                "semantic_envelope": envelope,
                "semantic_summary": semantic_summary,
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
        external = any(self._external_actor(actor) for actor in actors)
        envelope = self._semantic_envelope(semantic_envelope)
        if not self._semantic_target_is_recipient(envelope, actors):
            raise EngineError("semantic target must be a calendar participant")
        semantic_summary = self._semantic_summary(envelope)

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
                    "semantic_summary": semantic_summary,
                    "rescheduled_at": self.current_time,
                    "rescheduled_by": role,
                }
            )
            if external:
                value["subject"] = semantic_summary.splitlines()[0]
                value["description"] = semantic_summary
            else:
                if subject is not None:
                    value["subject"] = subject
                if description is not None:
                    value["description"] = f"{description}\n\n{semantic_summary}"
                elif semantic_summary not in str(value.get("description", "")):
                    value["description"] = (
                        f"{value.get('description', '')}\n\n{semantic_summary}"
                    )
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
        external = any(self._external_actor(actor) for actor in actors)
        envelope = self._semantic_envelope(semantic_envelope)
        if not self._semantic_target_is_recipient(envelope, actors):
            raise EngineError("semantic target must be a calendar participant")
        semantic_summary = self._semantic_summary(envelope)

        def cancel() -> dict[str, Any]:
            value = {
                **existing,
                "status": "cancelled",
                "cancel_reason": semantic_summary if external else reason,
                "cancelled_at": self.current_time,
                "cancelled_by": role,
                "semantic_envelope": envelope,
                "semantic_summary": semantic_summary,
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

    def _document_is_brokered(self, document: Mapping[str, Any]) -> bool:
        metadata = document.get("metadata")
        if not isinstance(metadata, Mapping) or metadata.get("brokered") is not True:
            return False
        envelope = metadata.get("semantic_envelope")
        if not isinstance(envelope, Mapping):
            return False
        try:
            summary = self._semantic_summary(envelope)
        except EngineError:
            return False
        remediation = (
            metadata.get("remediation")
            if document.get("kind") == "remediation_plan"
            else None
        )
        if remediation is not None and not isinstance(remediation, Mapping):
            return False
        payload = brokered_document_payload(summary, remediation)
        return bool(
            metadata.get("semantic_summary") == summary
            and document.get("title") == payload["title"]
            and document.get("content") == payload["content"]
        )

    def _require_brokered_document_attachments(
        self, envelope: Mapping[str, Any]
    ) -> None:
        for document_id in _list(envelope.get("attachments")):
            row = self.connection.execute(
                "SELECT data FROM documents WHERE document_id = ?", (document_id,)
            ).fetchone()
            if row is None:
                continue
            document = json.loads(str(row[0]))
            agent_authored = (
                document.get("author_role") in SELLER_ROLES
                or document.get("revised_by") in SELLER_ROLES
            )
            if agent_authored and not self._document_is_brokered(document):
                raise EngineError("external document attachment is not brokered")

    def _structured_remediation(
        self,
        value: Mapping[str, Any] | None,
        envelope: Mapping[str, Any] | None,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        fields = {"cure_data", "gate_id", "owner_role"}
        if not isinstance(value, Mapping) or set(value) != fields:
            raise EngineError("remediation plan structure is invalid")
        cure_data = value["cure_data"]
        checkpoint = self.current_checkpoint()
        if (
            not isinstance(envelope, Mapping)
            or not isinstance(cure_data, Mapping)
            or not cure_data
            or value["owner_role"] not in SELLER_ROLES
            or checkpoint is None
            or value["gate_id"] != checkpoint.get("gate_id")
            or not isinstance(value["gate_id"], str)
            or not value["gate_id"]
        ):
            raise EngineError("remediation plan structure is invalid")
        evidence_ids = tuple(_list(envelope.get("attachments")))
        matching_rules = [
            rule
            for rule in self.action_effect_rules.values()
            if rule.checkpoint_id == checkpoint["checkpoint_id"]
            and rule.fact_type == "authority_decision_observed"
            and rule.remediation_requirements is not None
            and rule.remediation_requirements["owner_role"] == value["owner_role"]
            and dict(rule.remediation_requirements["cure_data"]) == dict(cure_data)
        ]
        contracts = {to_json(rule.remediation_requirements) for rule in matching_rules}
        routes = {
            to_json(
                {
                    "commitment_code": rule.commitment_code,
                    "decision_code": rule.decision_code,
                    "gate_id": rule.gate_id,
                    "purpose_code": rule.purpose_code,
                    "related_record_id": rule.record_id,
                    "requester_role": rule.role,
                    "resolution": rule.resolution,
                }
            )
            for rule in matching_rules
        }
        if len(contracts) != 1 or len(routes) != 1:
            raise EngineError("remediation plan is not grounded in current evidence")
        requirements = json.loads(next(iter(contracts)))
        try:
            basis = self._remediation_evidence_basis(
                str(value["owner_role"]),
                str(value["gate_id"]),
                cure_data,
                evidence_ids,
            )
        except (EngineError, KeyError) as exc:
            raise EngineError(
                "remediation plan is not grounded in current evidence"
            ) from exc
        return (
            {
                "cure_data": dict(cure_data),
                "gate_id": str(value["gate_id"]),
                "owner_role": str(value["owner_role"]),
            },
            {"due_at": requirements["due_at"], **basis},
            json.loads(next(iter(routes))),
        )

    def documents_create(
        self,
        role: str,
        title: str,
        content: str,
        kind: str = "document",
        idempotency_key: str | None = None,
        visibility: Sequence[str] = SELLER_ROLES,
        metadata: Mapping[str, Any] | None = None,
        semantic_envelope: Mapping[str, Any] | None = None,
        remediation: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._authorize(role, "documents", "create")
        envelope = (
            self._semantic_envelope(semantic_envelope)
            if semantic_envelope is not None
            else None
        )
        semantic_summary = self._semantic_summary(envelope) if envelope else None
        remediation_contract = (
            self._structured_remediation(remediation, envelope)
            if kind == "remediation_plan"
            else None
        )
        structured_remediation = (
            remediation_contract[0] if remediation_contract is not None else None
        )
        verification_basis = (
            remediation_contract[1] if remediation_contract is not None else None
        )
        authority_request = (
            remediation_contract[2] if remediation_contract is not None else None
        )
        if kind != "remediation_plan" and remediation is not None:
            raise EngineError("structured remediation requires a remediation plan")
        payload = (
            brokered_document_payload(semantic_summary, structured_remediation)
            if semantic_summary is not None
            else {"title": title, "content": content}
        )

        def create() -> dict[str, Any]:
            document_id = _stable_id(
                "document", self.manifest.world_id, idempotency_key or title
            )
            value = {
                "document_id": document_id,
                "title": payload["title"],
                "content": payload["content"],
                "kind": kind,
                "version": 1,
                "created_at": self.current_time,
                "available_at": self.current_time,
                "visibility": list(visibility),
                "metadata": {
                    **dict(metadata or {}),
                    **({"semantic_envelope": envelope} if envelope is not None else {}),
                    **(
                        {"semantic_summary": semantic_summary}
                        if semantic_summary is not None
                        else {}
                    ),
                    **({"brokered": True} if semantic_summary is not None else {}),
                    **(
                        {"remediation": structured_remediation}
                        if structured_remediation is not None
                        else {}
                    ),
                    **(
                        {"verification_basis": verification_basis}
                        if verification_basis is not None
                        else {}
                    ),
                },
                "author_role": role,
            }
            self.seed_document(document_id, value)
            result = _agent_safe(value)
            if authority_request is not None:
                result["authority_request"] = authority_request
            return result

        return self._idempotent(idempotency_key, "documents.create", create)

    def documents_revise(
        self,
        role: str,
        document_id: str,
        content: str,
        metadata: Mapping[str, Any] | None = None,
        semantic_envelope: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        self._authorize(role, "documents", "revise")
        envelope = self._semantic_envelope(semantic_envelope)
        semantic_summary = self._semantic_summary(envelope)

        def revise() -> dict[str, Any]:
            row = self.connection.execute(
                "SELECT data, version FROM documents WHERE document_id = ?",
                (document_id,),
            ).fetchone()
            if row is None:
                raise EngineError(f"document {document_id!r} not found")
            value = json.loads(str(row[0]))
            version = int(row[1]) + 1
            existing_metadata = value.get("metadata")
            remediation = (
                existing_metadata.get("remediation")
                if value.get("kind") == "remediation_plan"
                and isinstance(existing_metadata, Mapping)
                else None
            )
            payload = brokered_document_payload(semantic_summary, remediation)
            value.update(
                {
                    **payload,
                    "version": version,
                    "revised_at": self.current_time,
                    "revised_by": role,
                }
            )
            if metadata:
                value["metadata"] = {**value.get("metadata", {}), **dict(metadata)}
            value["metadata"] = {
                **value.get("metadata", {}),
                "semantic_envelope": envelope,
                "semantic_summary": semantic_summary,
                "brokered": True,
            }
            self.connection.execute(
                "UPDATE documents SET data = ?, version = ? WHERE document_id = ?",
                (to_json(value), version, document_id),
            )
            self.connection.execute(
                "INSERT INTO document_versions(document_id, version, data) VALUES (?, ?, ?)",
                (document_id, version, to_json(value)),
            )
            return _agent_safe(value)

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
        if not self._related_entity_exists(related_id):
            raise EngineError("related record does not exist")

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
        approver_actor_ids: Sequence[str],
        purpose: str,
        details: Mapping[str, Any],
        idempotency_key: str | None = None,
        semantic_envelope: Mapping[str, Any] | None = None,
        visibility: Sequence[str] = SELLER_ROLES,
    ) -> dict[str, Any]:
        self._authorize(role, "approvals", "request")
        envelope = self._semantic_envelope(semantic_envelope)
        approvers = tuple(str(item) for item in approver_actor_ids)
        if len(approvers) != 1:
            raise EngineError("approval authorities are invalid")
        requester_actor = next(
            (
                actor
                for actor in self._all_actors()
                if actor.get("authority", {}).get("role_id") == f"seller.{role}"
            ),
            None,
        )
        actors = [self._actor_for_recipient(actor_id) for actor_id in approvers]
        gate = details.get("gate")
        if (
            requester_actor is None
            or any(actor is None for actor in actors)
            or any(
                self._organization_scope(actor) != "seller"
                or not isinstance(actor.get("authority"), Mapping)
                or gate not in set(actor["authority"].get("gate_ids", ()))
                or _time_value(str(actor["active_from"]))
                > _time_value(self.current_time)
                or actor.get("active_until") is not None
                and _time_value(str(actor["active_until"]))
                <= _time_value(self.current_time)
                for actor in actors
                if actor is not None
            )
            or requester_actor.get("actor_id") in approvers
        ):
            raise EngineError("approval authorities are invalid")
        seat_roles = {
            str(actor["actor_id"]): str(actor["authority"]["role_id"]).removeprefix(
                "seller."
            )
            for actor in actors
            if actor is not None
            and str(actor["authority"].get("role_id", "")).startswith("seller.")
        }
        if set(seat_roles) != set(approvers):
            raise EngineError("approval authority must be a seller seat")

        def request() -> dict[str, Any]:
            approval_id = _stable_id(
                "approval", self.manifest.world_id, idempotency_key or purpose
            )
            value = {
                "approval_id": approval_id,
                "requester_role": role,
                "approver_actor_ids": list(approvers),
                "purpose": purpose,
                "details": dict(details),
                "semantic_envelope": envelope,
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
            actor = next(
                (
                    actor
                    for actor in self._all_actors()
                    if actor.get("authority", {}).get("role_id") == f"seller.{role}"
                ),
                None,
            )
            if actor is None or actor.get("actor_id") not in set(
                value.get("approver_actor_ids", ())
            ):
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
                    "responded_by_actor_ids": [actor["actor_id"]],
                    "responded_by_role": role,
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
        checkpoint_id: str,
        idempotency_key: str | None = None,
        visibility: Sequence[str] = SELLER_ROLES,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._authorize(role, "team", "send")
        values = _list(recipients)
        if not values or any(item not in SELLER_ROLES for item in values):
            raise AuthorizationError("team recipients must be seller roles")
        checkpoint = self.current_checkpoint()
        if checkpoint is None or checkpoint_id != checkpoint.get("checkpoint_id"):
            raise EngineError("team message checkpoint is not active")

        def send() -> dict[str, Any]:
            message_id = _stable_id(
                "team", self.manifest.run_id, idempotency_key or body
            )
            value = {
                "message_id": message_id,
                "sender_role": role,
                "recipients": values,
                "body": body,
                "checkpoint_id": checkpoint_id,
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
