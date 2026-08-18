from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field, fields, is_dataclass
from typing import Any, Literal, Self

RoleName = Literal[
    "account_executive", "domain_specialist", "sales_manager", "revops", "system"
]


def _json_value(value: Any) -> Any:
    if is_dataclass(value):
        return {
            item.name: _json_value(getattr(value, item.name))
            for item in fields(value)
            if not (
                getattr(value, item.name) is None and item.metadata.get("omit_none")
            )
        }
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_json_value(item) for item in value]
    return value


def to_dict(value: Any) -> dict[str, Any]:
    result = _json_value(value)
    if not isinstance(result, dict):
        raise TypeError("expected a dataclass value")
    return result


def to_json(value: Any) -> str:
    return json.dumps(
        _json_value(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def stable_hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(to_json(value).encode("utf-8")).hexdigest()


def scorecard_hash(value: Mapping[str, Any] | Any) -> str:
    source = to_dict(value) if is_dataclass(value) else dict(value)
    fields_to_hash = (
        "run_id",
        "benchmark_version",
        "world_id",
        "vertical",
        "track",
        "trial_seed",
        "configuration_hash",
        "manifest_hash",
        "rubric_hash",
        "oracle_hash",
        "status",
        "execution_index",
        "strict_cycle_pass",
        "critical_violation",
        "configuration_resolved",
        "category_scores",
        "secondary_metrics",
        "reliability",
        "resource_usage",
        "rubric_validation",
        "violations",
        "pending_judge_assertions",
        "state_hash",
        "grader_version",
    )
    payload = {key: source[key] for key in fields_to_hash if key in source}
    resource_usage = payload.get("resource_usage")
    if isinstance(resource_usage, Mapping):
        resource_usage = dict(resource_usage)
        resource_usage.pop("latency_ms", None)
        payload["resource_usage"] = resource_usage
    return stable_hash(payload)


def aggregate_scorecard_hash(value: Mapping[str, Any] | Any) -> str:
    source = to_dict(value) if is_dataclass(value) else dict(value)
    payload = {key: item for key, item in source.items() if key != "score_hash"}
    resource_usage = payload.get("resource_usage")
    if isinstance(resource_usage, Mapping):
        resource = dict(resource_usage)
        for group in ("totals", "means", "medians"):
            values = resource.get(group)
            if isinstance(values, Mapping):
                normalized = dict(values)
                normalized.pop("latency_ms", None)
                resource[group] = normalized
        payload["resource_usage"] = resource
    return stable_hash(payload)


def _tuple(value: Any) -> tuple[str, ...]:
    return tuple(str(item) for item in (value or ()))


def _boolean(value: Any, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{field_name} must be boolean")
    return value


def _optional_nonnegative_int(value: Any, field_name: str) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise TypeError(f"{field_name} must be a non-negative integer")
    return value


def _resource_scopes(value: Any) -> tuple[str, ...]:
    return tuple(
        "current_world" if str(item) == "world" else str(item) for item in (value or ())
    )


def _counterfactual_variant(value: Any) -> Literal["a", "b"] | None:
    if value is None:
        return None
    if value == "a":
        return "a"
    if value == "b":
        return "b"
    raise TypeError("counterfactual_variant must be 'a', 'b', or null")


def _split(value: Any) -> Literal["train", "dev", "blind"]:
    if value == "train":
        return "train"
    if value == "dev":
        return "dev"
    if value == "blind":
        return "blind"
    raise TypeError("split must be 'train', 'dev', or 'blind'")


def _release_visibility(value: Any) -> Literal["public", "private"]:
    if value == "public":
        return "public"
    if value == "private":
        return "private"
    raise TypeError("release_visibility must be 'public' or 'private'")


def _role(
    value: Any,
) -> Literal["account_executive", "domain_specialist", "sales_manager", "revops"]:
    if value == "account_executive":
        return "account_executive"
    if value == "domain_specialist":
        return "domain_specialist"
    if value == "sales_manager":
        return "sales_manager"
    if value == "revops":
        return "revops"
    raise TypeError("role is invalid")


def _track(value: Any) -> Literal["fixed_harness", "open_team"]:
    if value == "fixed_harness":
        return "fixed_harness"
    if value == "open_team":
        return "open_team"
    raise TypeError("track must be 'fixed_harness' or 'open_team'")


def _scope(value: Any) -> Literal["checkpoint", "world"]:
    if value == "checkpoint":
        return "checkpoint"
    if value == "world":
        return "world"
    raise TypeError("scope must be 'checkpoint' or 'world'")


def _assertion_kind(value: Any) -> Literal["deterministic", "llm_judge", "metric"]:
    if value == "deterministic":
        return "deterministic"
    if value == "llm_judge":
        return "llm_judge"
    if value == "metric":
        return "metric"
    raise TypeError("assertion kind is invalid")


class JsonModel:
    def to_dict(self) -> dict[str, Any]:
        return to_dict(self)

    def to_json(self) -> str:
        return to_json(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Self:
        raise NotImplementedError

    @classmethod
    def from_json(cls, value: str) -> Self:
        parsed = json.loads(value)
        if not isinstance(parsed, Mapping):
            raise TypeError("JSON model value must be an object")
        return cls.from_dict(parsed)


@dataclass(frozen=True, slots=True)
class ScenarioManifest(JsonModel):
    world_id: str
    pair_id: str | None = field(metadata={"omit_none": True})
    counterfactual_variant: Literal["a", "b"] | None = field(
        metadata={"omit_none": True}
    )
    split: Literal["train", "dev", "blind"]
    vertical: str
    causal_skeleton: str | None = field(metadata={"omit_none": True})
    seller_org_id: str
    buyer_org_id: str
    title: str
    description: str
    start_at: str
    end_at: str
    duration_days: int
    checkpoint_ids: tuple[str, ...]
    actor_ids: tuple[str, ...]
    event_ids: tuple[str, ...]
    artifact_ids: tuple[str, ...]
    required_channels: tuple[str, ...]
    terminal_outcome: str | None = field(metadata={"omit_none": True})
    seed: int | None = field(metadata={"omit_none": True})
    license: Mapping[str, str]
    provenance: Mapping[str, Any]
    release_visibility: Literal["public", "private"]
    schema_version: str = "v1.0.0"
    outcome_reason: str | None = None

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ScenarioManifest:
        return cls(
            world_id=str(value["world_id"]),
            pair_id=str(value["pair_id"]) if value.get("pair_id") is not None else None,
            counterfactual_variant=_counterfactual_variant(
                value.get("counterfactual_variant")
            ),
            split=_split(value["split"]),
            vertical=str(value["vertical"]),
            causal_skeleton=str(value["causal_skeleton"])
            if value.get("causal_skeleton") is not None
            else None,
            seller_org_id=str(value["seller_org_id"]),
            buyer_org_id=str(value["buyer_org_id"]),
            title=str(value["title"]),
            description=str(value["description"]),
            start_at=str(value["start_at"]),
            end_at=str(value["end_at"]),
            duration_days=int(value["duration_days"]),
            checkpoint_ids=_tuple(value["checkpoint_ids"]),
            actor_ids=_tuple(value["actor_ids"]),
            event_ids=_tuple(value["event_ids"]),
            artifact_ids=_tuple(value["artifact_ids"]),
            required_channels=_tuple(value["required_channels"]),
            terminal_outcome=str(value["terminal_outcome"])
            if value.get("terminal_outcome") is not None
            else None,
            seed=int(value["seed"]) if value.get("seed") is not None else None,
            license=dict(value["license"]),
            provenance=dict(value["provenance"]),
            schema_version=str(value.get("schema_version", "v1.0.0")),
            outcome_reason=value.get("outcome_reason"),
            release_visibility=_release_visibility(value["release_visibility"]),
        )


@dataclass(frozen=True, slots=True)
class Actor(JsonModel):
    actor_id: str
    kind: str
    display_name: str
    organization_id: str
    role_tags: tuple[str, ...]
    active_from: str
    visibility: str
    active_until: str | None = None
    email: str | None = None
    phone: str | None = None
    reports_to: str | None = None
    attributes: Mapping[str, Any] = field(default_factory=dict)
    visible_roles: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Actor:
        return cls(
            actor_id=str(value["actor_id"]),
            kind=str(value["kind"]),
            display_name=str(value["display_name"]),
            organization_id=str(value["organization_id"]),
            role_tags=_tuple(value["role_tags"]),
            active_from=str(value["active_from"]),
            visibility=str(value["visibility"]),
            active_until=value.get("active_until"),
            email=value.get("email"),
            phone=value.get("phone"),
            reports_to=value.get("reports_to"),
            attributes=dict(value.get("attributes") or {}),
            visible_roles=_tuple(value.get("visible_roles")),
        )


@dataclass(frozen=True, slots=True)
class Event(JsonModel):
    event_id: str
    world_id: str
    sequence: int
    kind: str
    effective_at: str
    recorded_at: str
    available_at: str
    actor_ids: tuple[str, ...]
    visibility: str
    payload: Mapping[str, Any]
    artifact_ids: tuple[str, ...] = ()
    channel: str | None = None
    causal_parent_ids: tuple[str, ...] = ()
    visible_roles: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Event:
        return cls(
            event_id=str(value["event_id"]),
            world_id=str(value["world_id"]),
            sequence=int(value["sequence"]),
            kind=str(value["kind"]),
            effective_at=str(value["effective_at"]),
            recorded_at=str(value["recorded_at"]),
            available_at=str(value["available_at"]),
            actor_ids=_tuple(value["actor_ids"]),
            visibility=str(value["visibility"]),
            payload=dict(value["payload"]),
            artifact_ids=_tuple(value.get("artifact_ids")),
            channel=value.get("channel"),
            causal_parent_ids=_tuple(value.get("causal_parent_ids")),
            visible_roles=_tuple(value.get("visible_roles")),
        )


@dataclass(frozen=True, slots=True)
class Artifact(JsonModel):
    artifact_id: str
    world_id: str
    kind: str
    title: str
    created_at: str
    available_at: str
    visibility: str
    content: Mapping[str, Any]
    checksum: str
    provenance: Mapping[str, Any]
    source_actor_ids: tuple[str, ...] = ()
    recipient_actor_ids: tuple[str, ...] = ()
    thread_id: str | None = None
    record_id: str | None = None
    version: int | None = None
    visible_roles: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Artifact:
        return cls(
            artifact_id=str(value["artifact_id"]),
            world_id=str(value["world_id"]),
            kind=str(value["kind"]),
            title=str(value["title"]),
            created_at=str(value["created_at"]),
            available_at=str(value["available_at"]),
            visibility=str(value["visibility"]),
            content=dict(value["content"]),
            checksum=str(value["checksum"]),
            provenance=dict(value["provenance"]),
            source_actor_ids=_tuple(value.get("source_actor_ids")),
            recipient_actor_ids=_tuple(value.get("recipient_actor_ids")),
            thread_id=value.get("thread_id"),
            record_id=value.get("record_id"),
            version=value.get("version"),
            visible_roles=_tuple(value.get("visible_roles")),
        )


@dataclass(frozen=True, slots=True)
class RoleGrant(JsonModel):
    grant_id: str
    principal_id: str
    role: Literal["account_executive", "domain_specialist", "sales_manager", "revops"]
    permissions: tuple[str, ...]
    resource_scopes: tuple[str, ...]
    can_contact_external: bool
    can_write_crm: bool
    can_approve_commercial: bool
    can_request_approval: bool
    approval_limit_minor_units: int | None = None

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> RoleGrant:
        return cls(
            grant_id=str(value["grant_id"]),
            principal_id=str(value["principal_id"]),
            role=_role(value["role"]),
            permissions=_tuple(value["permissions"]),
            resource_scopes=_resource_scopes(value["resource_scopes"]),
            can_contact_external=_boolean(
                value["can_contact_external"], "can_contact_external"
            ),
            can_write_crm=_boolean(value["can_write_crm"], "can_write_crm"),
            can_approve_commercial=_boolean(
                value["can_approve_commercial"], "can_approve_commercial"
            ),
            can_request_approval=_boolean(
                value["can_request_approval"], "can_request_approval"
            ),
            approval_limit_minor_units=_optional_nonnegative_int(
                value.get("approval_limit_minor_units"), "approval_limit_minor_units"
            ),
        )


@dataclass(frozen=True, slots=True)
class Checkpoint(JsonModel):
    checkpoint_id: str
    world_id: str
    sequence: int
    available_at: str
    window_start: str
    window_end: str
    status: str
    objective_ids: tuple[str, ...]
    visible_artifact_ids: tuple[str, ...]
    required_roles: tuple[str, ...]
    terminal: bool
    released_event_ids: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Checkpoint:
        return cls(
            checkpoint_id=str(value["checkpoint_id"]),
            world_id=str(value["world_id"]),
            sequence=int(value["sequence"]),
            available_at=str(value["available_at"]),
            window_start=str(value["window_start"]),
            window_end=str(value["window_end"]),
            status=str(value["status"]),
            objective_ids=_tuple(value["objective_ids"]),
            visible_artifact_ids=_tuple(value.get("visible_artifact_ids")),
            required_roles=_tuple(value["required_roles"]),
            terminal=bool(value["terminal"]),
            released_event_ids=_tuple(value.get("released_event_ids")),
        )


@dataclass(frozen=True, slots=True)
class RunManifest(JsonModel):
    run_id: str
    benchmark_version: str
    world_id: str
    track: Literal["fixed_harness", "open_team"]
    team_id: str
    protocol_version: str
    tool_schema_version: str
    scenario_hash: str
    rubric_hash: str
    oracle_hash: str | None
    seed: int
    agent_manifest: Mapping[str, Any]
    stakeholder_manifest: Mapping[str, Any]
    limits: Mapping[str, Any]
    environment: Mapping[str, Any]
    started_at: str
    status: str
    ended_at: str | None = None

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> RunManifest:
        return cls(
            run_id=str(value["run_id"]),
            benchmark_version=str(value["benchmark_version"]),
            world_id=str(value["world_id"]),
            track=_track(value["track"]),
            team_id=str(value["team_id"]),
            protocol_version=str(value["protocol_version"]),
            tool_schema_version=str(value["tool_schema_version"]),
            scenario_hash=str(value["scenario_hash"]),
            rubric_hash=str(value["rubric_hash"]),
            oracle_hash=(
                str(value["oracle_hash"])
                if value.get("oracle_hash") is not None
                else None
            ),
            seed=int(value["seed"]),
            agent_manifest=dict(value["agent_manifest"]),
            stakeholder_manifest=dict(value["stakeholder_manifest"]),
            limits=dict(value["limits"]),
            environment=dict(value["environment"]),
            started_at=str(value["started_at"]),
            status=str(value["status"]),
            ended_at=value.get("ended_at"),
        )


@dataclass(frozen=True, slots=True)
class TraceEvent(JsonModel):
    run_id: str
    sequence: int
    message_id: str
    occurred_at: str
    kind: str
    actor_role: str
    payload_hash: str
    payload: Mapping[str, Any]
    idempotency_key: str | None = None
    latency_ms: int | None = None
    token_usage: Mapping[str, int] | None = None
    cost_minor_units: int | None = None

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> TraceEvent:
        return cls(
            run_id=str(value["run_id"]),
            sequence=int(value["sequence"]),
            message_id=str(value["message_id"]),
            occurred_at=str(value["occurred_at"]),
            kind=str(value["kind"]),
            actor_role=str(value["actor_role"]),
            payload_hash=str(value["payload_hash"]),
            payload=dict(value["payload"]),
            idempotency_key=value.get("idempotency_key"),
            latency_ms=value.get("latency_ms"),
            token_usage=dict(value["token_usage"])
            if value.get("token_usage") is not None
            else None,
            cost_minor_units=value.get("cost_minor_units"),
        )


@dataclass(frozen=True, slots=True)
class Assertion(JsonModel):
    assertion_id: str
    world_id: str
    scope: Literal["checkpoint", "world"]
    category: str
    kind: Literal["deterministic", "llm_judge", "metric"]
    target: Mapping[str, Any]
    required: bool
    critical: bool
    controllability: str
    weight: float
    evidence_refs: tuple[str, ...]
    provenance: Mapping[str, Any]
    checkpoint_id: str | None = None
    judge: Mapping[str, Any] | None = None

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Assertion:
        return cls(
            assertion_id=str(value["assertion_id"]),
            world_id=str(value["world_id"]),
            scope=_scope(value["scope"]),
            category=str(value["category"]),
            kind=_assertion_kind(value["kind"]),
            target=dict(value["target"]),
            required=bool(value["required"]),
            critical=bool(value["critical"]),
            controllability=str(value["controllability"]),
            weight=float(value["weight"]),
            evidence_refs=_tuple(value["evidence_refs"]),
            provenance=dict(value["provenance"]),
            checkpoint_id=value.get("checkpoint_id"),
            judge=dict(value["judge"]) if value.get("judge") is not None else None,
        )


@dataclass(frozen=True, slots=True)
class Scorecard(JsonModel):
    run_id: str
    benchmark_version: str
    world_id: str
    track: str
    status: str
    execution_index: float
    strict_cycle_pass: bool
    critical_violation: bool
    configuration_resolved: bool
    category_scores: Mapping[str, float]
    secondary_metrics: Mapping[str, Any]
    reliability: Mapping[str, Any]
    resource_usage: Mapping[str, Any]
    grader_version: str
    generated_at: str
    violations: tuple[Mapping[str, Any], ...] = ()
    state_hash: str | None = field(default=None, metadata={"omit_none": True})
    score_hash: str | None = field(default=None, metadata={"omit_none": True})
    rubric_hash: str | None = field(default=None, metadata={"omit_none": True})
    oracle_hash: str | None = field(default=None, metadata={"omit_none": True})
    vertical: str | None = field(default=None, metadata={"omit_none": True})
    trial_seed: int | None = field(default=None, metadata={"omit_none": True})
    configuration_hash: str | None = field(default=None, metadata={"omit_none": True})
    manifest_hash: str | None = field(default=None, metadata={"omit_none": True})
    rubric_validation: Mapping[str, Any] | None = field(
        default=None, metadata={"omit_none": True}
    )
    pending_judge_assertions: tuple[str, ...] | None = field(
        default=None, metadata={"omit_none": True}
    )
    assertions: tuple[Mapping[str, Any], ...] | None = field(
        default=None, metadata={"omit_none": True}
    )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Scorecard:
        return cls(
            run_id=str(value["run_id"]),
            benchmark_version=str(value["benchmark_version"]),
            world_id=str(value["world_id"]),
            track=str(value["track"]),
            status=str(value["status"]),
            execution_index=float(value["execution_index"]),
            strict_cycle_pass=bool(value["strict_cycle_pass"]),
            critical_violation=bool(value["critical_violation"]),
            configuration_resolved=bool(value["configuration_resolved"]),
            category_scores={
                str(k): float(v) for k, v in value["category_scores"].items()
            },
            secondary_metrics=dict(value["secondary_metrics"]),
            reliability=dict(value["reliability"]),
            resource_usage=dict(value["resource_usage"]),
            grader_version=str(value["grader_version"]),
            generated_at=str(value["generated_at"]),
            violations=tuple(dict(item) for item in value.get("violations", ())),
            state_hash=value.get("state_hash"),
            score_hash=value.get("score_hash"),
            rubric_hash=value.get("rubric_hash"),
            oracle_hash=value.get("oracle_hash"),
            vertical=(
                str(value["vertical"]) if value.get("vertical") is not None else None
            ),
            trial_seed=_optional_nonnegative_int(value.get("trial_seed"), "trial_seed"),
            configuration_hash=(
                str(value["configuration_hash"])
                if value.get("configuration_hash") is not None
                else None
            ),
            manifest_hash=(
                str(value["manifest_hash"])
                if value.get("manifest_hash") is not None
                else None
            ),
            rubric_validation=(
                dict(value["rubric_validation"])
                if value.get("rubric_validation") is not None
                else None
            ),
            pending_judge_assertions=(
                _tuple(value["pending_judge_assertions"])
                if value.get("pending_judge_assertions") is not None
                else None
            ),
            assertions=(
                tuple(dict(item) for item in value["assertions"])
                if value.get("assertions") is not None
                else None
            ),
        )


def from_dict[T](cls: type[T], value: Mapping[str, Any]) -> T:
    parser = getattr(cls, "from_dict", None)
    if parser is None:
        raise TypeError(f"{cls.__name__} does not support from_dict")
    return parser(value)


__all__ = [
    "Actor",
    "Artifact",
    "Assertion",
    "Checkpoint",
    "Event",
    "JsonModel",
    "RoleGrant",
    "RoleName",
    "RunManifest",
    "ScenarioManifest",
    "Scorecard",
    "TraceEvent",
    "aggregate_scorecard_hash",
    "from_dict",
    "scorecard_hash",
    "stable_hash",
    "to_dict",
    "to_json",
]
