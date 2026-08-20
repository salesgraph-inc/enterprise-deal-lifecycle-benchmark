from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any

LANE_DEFAULTS = {
    "business_fit": 50,
    "stakeholder_consensus": 20,
    "validation": 0,
    "commercial_terms": 0,
    "approvals": 0,
    "competition": 0,
    "urgency": 40,
}
LANES = tuple(LANE_DEFAULTS)
LANE_STATUSES = frozenset(
    {"unknown", "at_risk", "progressing", "satisfied", "blocked", "failed"}
)
TERMINAL_OUTCOMES = frozenset(
    {"closed_won", "closed_lost", "no_decision", "disqualified", "canceled"}
)
MILESTONE_RESOLUTIONS = frozenset(
    {"accepted", "rejected", "deferred", "inapplicable", "remedied"}
)
ACTION_FACT_TYPES = frozenset(
    {
        "authority_decision_observed",
        "crm_transition",
        "internal_approval",
    }
)


class CausalError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class StakeholderAct:
    act_id: str
    action_key: str
    actor_id: str
    kind: str
    channel: str
    stance: str
    allowed_facts: tuple[str, ...]
    forbidden_claims: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class MilestoneDefinition:
    milestone_id: str
    checkpoint_id: str
    gate_id: str
    prerequisite_milestone_ids: tuple[str, ...]
    decision_artifact_ids: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    evidence_requirements_by_role: Mapping[str, tuple[str, ...]]
    decision_evidence_role: str
    authority_requirements: tuple[Mapping[str, Any], ...]
    allowed_resolutions: tuple[str, ...]
    approval_requirement: Mapping[str, Any] | None
    business_effect_requirements_by_resolution: Mapping[
        str, Mapping[str, Mapping[str, Any]]
    ]
    chronology: Mapping[str, Any]
    lane_effects_by_resolution: Mapping[str, Mapping[str, Mapping[str, Any]]]
    terminal_outcome_by_resolution: Mapping[str, str]
    remedy_of: str | None = None
    branch_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class MilestoneResolution:
    milestone_id: str
    resolution: str
    decision_artifact_ids: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    authority_resolutions: tuple[Mapping[str, Any], ...]
    business_effects: Mapping[str, str]
    effective_at: str
    remedy_of: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ActionEffectRule:
    effect_id: str
    branch_id: str
    checkpoint_id: str
    fact_type: str
    role: str
    gate_id: str
    record_id: str
    tool_names: tuple[str, ...]
    required_evidence_ids: tuple[str, ...]
    authority_actor_id: str | None = None
    authority_rights: tuple[str, ...] = ()
    next_gate_id: str | None = None
    purpose_code: str | None = None
    decision_code: str | None = None
    commitment_code: str | None = None
    resolution: str | None = None
    document_kind: str | None = None
    next_step_type: str | None = None
    remediation_requirements: Mapping[str, Any] | None = None
    response_resolution: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class BranchDefinition:
    branch_id: str
    action_checkpoint_id: str
    resolution_checkpoint_id: str
    remedy_milestone_id: str
    recoverable: bool
    success_if_any: tuple[tuple[str, ...], ...]
    success_decision_artifact_ids: tuple[str, ...]
    fallback_decision_artifact_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class BranchResolution:
    branch_id: str
    option: str
    effect_ids: tuple[str, ...]
    action_keys: tuple[str, ...]
    selected_decision_artifact_ids: tuple[str, ...]
    resolved_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def normalize_official_seeds(
    values: Sequence[int] | None, fallback: int
) -> tuple[int, int, int]:
    seeds = (
        tuple(values) if values is not None else (fallback, fallback + 1, fallback + 2)
    )
    if (
        len(seeds) != 3
        or len(set(seeds)) != 3
        or any(
            not isinstance(seed, int) or isinstance(seed, bool) or seed < 0
            for seed in seeds
        )
    ):
        raise CausalError(
            "official stakeholder seeds must be three unique non-negative integers"
        )
    return seeds


def lane_status(score: int) -> str:
    if score <= -60:
        return "failed"
    if score <= -20:
        return "blocked"
    if score < 20:
        return "at_risk"
    if score < 60:
        return "progressing"
    return "satisfied"


def _identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise CausalError(f"{field} must be a non-empty string")
    return value


def _approval_requirement(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    fields = {
        "amount_minor_units",
        "approver_actor_ids",
        "basis",
        "checkpoint_id",
        "gate_id",
        "policy_evidence",
        "policy_limit_minor_units",
        "policy_owner",
        "trigger",
    }
    if not isinstance(value, Mapping) or set(value) != fields:
        raise CausalError("milestone approval requirement is invalid")
    amount = value["amount_minor_units"]
    limit = value["policy_limit_minor_units"]
    approvers = value["approver_actor_ids"]
    basis = value["basis"]
    if (
        not isinstance(amount, int)
        or isinstance(amount, bool)
        or amount < 0
        or not isinstance(limit, int)
        or isinstance(limit, bool)
        or limit < 0
        or not isinstance(approvers, Sequence)
        or isinstance(approvers, (str, bytes))
        or len(approvers) != 1
        or any(not isinstance(actor_id, str) or not actor_id for actor_id in approvers)
        or len(set(approvers)) != len(approvers)
        or not isinstance(basis, Mapping)
        or set(basis) != {"amount_minor_units", "field", "value"}
        or basis["amount_minor_units"] != amount
        or not isinstance(basis["value"], int)
        or isinstance(basis["value"], bool)
    ):
        raise CausalError("milestone approval requirement is invalid")
    normalized = dict(value)
    normalized["approver_actor_ids"] = [str(actor_id) for actor_id in approvers]
    normalized["basis"] = {
        "amount_minor_units": amount,
        "field": _identifier(basis["field"], "approval basis field"),
        "value": basis["value"],
    }
    for field in (
        "checkpoint_id",
        "gate_id",
        "policy_evidence",
        "policy_owner",
        "trigger",
    ):
        normalized[field] = _identifier(value[field], f"approval {field}")
    return normalized


def _semantic_requirements(value: Any) -> dict[str, Any]:
    required = {
        "authority_actor_id",
        "commitment_code",
        "commitment_owner_role",
        "decision_code",
        "evidence_claims",
        "gate_id",
        "purpose_code",
        "resolution",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise CausalError("milestone semantic requirements are invalid")
    claims = value["evidence_claims"]
    if (
        not isinstance(claims, Sequence)
        or isinstance(claims, (str, bytes))
        or not claims
        or any(
            not isinstance(claim, Mapping)
            or set(claim) != {"artifact_id", "claim_type", "gate_id", "resolution"}
            or claim.get("claim_type")
            not in {"supports_gate_basis", "supports_gate_resolution"}
            for claim in claims
        )
    ):
        raise CausalError("milestone semantic evidence claims are invalid")
    normalized_claims = [
        {
            "artifact_id": _identifier(
                claim["artifact_id"], "semantic evidence artifact"
            ),
            "claim_type": str(claim["claim_type"]),
            "gate_id": _identifier(claim["gate_id"], "semantic evidence gate"),
            "resolution": _identifier(
                claim["resolution"], "semantic evidence resolution"
            ),
        }
        for claim in claims
    ]
    if len({json.dumps(claim, sort_keys=True) for claim in normalized_claims}) != len(
        normalized_claims
    ):
        raise CausalError("milestone semantic evidence claims are not unique")
    return {
        "authority_actor_id": _identifier(
            value["authority_actor_id"], "semantic authority actor"
        ),
        "commitment_code": _identifier(
            value["commitment_code"], "semantic commitment code"
        ),
        "commitment_owner_role": _identifier(
            value["commitment_owner_role"], "semantic commitment owner"
        ),
        "decision_code": _identifier(value["decision_code"], "semantic decision code"),
        "evidence_claims": normalized_claims,
        "gate_id": _identifier(value["gate_id"], "semantic gate"),
        "purpose_code": _identifier(value["purpose_code"], "semantic purpose code"),
        "resolution": _identifier(value["resolution"], "semantic resolution"),
    }


def _business_effect_requirements(value: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(value, Mapping) or set(value) != {
        "decision_followup",
        "crm_projection",
        "deliverable",
    }:
        raise CausalError("milestone business effect requirements are invalid")
    followup = value["decision_followup"]
    crm = value["crm_projection"]
    deliverable = value["deliverable"]
    if (
        not isinstance(followup, Mapping)
        or set(followup)
        != {
            "allowed_channels",
            "recipient_actor_id",
            "related_record_id",
            "required_evidence_ids",
            "required_message_facts",
            "semantic_requirements",
            "sender_role",
        }
        or not isinstance(crm, Mapping)
        or set(crm)
        != {
            "date_ranges",
            "exact_fields",
            "nonempty_fields",
            "number_ranges",
            "record_id",
            "text_reference_fields",
            "write_fields",
            "writer_role",
        }
        or not isinstance(deliverable, Mapping)
        or set(deliverable)
        != {
            "author_role",
            "kind",
            "minimum_version",
            "related_id",
            "related_type",
            "required_content_terms",
            "required_evidence_ids",
            "semantic_requirements",
        }
    ):
        raise CausalError("milestone business effect requirements are invalid")
    channels = followup["allowed_channels"]
    followup_evidence = followup["required_evidence_ids"]
    exact_fields = crm["exact_fields"]
    nonempty_fields = crm["nonempty_fields"]
    number_ranges = crm["number_ranges"]
    date_ranges = crm["date_ranges"]
    text_reference_fields = crm["text_reference_fields"]
    write_fields = crm["write_fields"]
    deliverable_evidence = deliverable["required_evidence_ids"]
    content_terms = deliverable["required_content_terms"]
    minimum_version = deliverable["minimum_version"]
    message_facts = followup["required_message_facts"]
    if (
        not isinstance(channels, Sequence)
        or isinstance(channels, (str, bytes))
        or not channels
        or len(set(channels)) != len(channels)
        or not set(channels) <= {"email", "calendar"}
        or not isinstance(followup_evidence, Sequence)
        or isinstance(followup_evidence, (str, bytes))
        or not followup_evidence
        or len(set(followup_evidence)) != len(followup_evidence)
        or not isinstance(exact_fields, Mapping)
        or not isinstance(nonempty_fields, Sequence)
        or isinstance(nonempty_fields, (str, bytes))
        or len(set(nonempty_fields)) != len(nonempty_fields)
        or any(not isinstance(field, str) or not field for field in nonempty_fields)
        or not isinstance(number_ranges, Mapping)
        or any(
            not isinstance(bounds, Mapping)
            or set(bounds) != {"maximum", "minimum"}
            or not isinstance(bounds["minimum"], (int, float))
            or isinstance(bounds["minimum"], bool)
            or not isinstance(bounds["maximum"], (int, float))
            or isinstance(bounds["maximum"], bool)
            or bounds["minimum"] > bounds["maximum"]
            for bounds in number_ranges.values()
        )
        or not isinstance(date_ranges, Mapping)
        or any(
            not isinstance(bounds, Mapping)
            or set(bounds) != {"not_after", "not_before"}
            or not isinstance(bounds["not_before"], str)
            or not bounds["not_before"]
            or not isinstance(bounds["not_after"], str)
            or not bounds["not_after"]
            for bounds in date_ranges.values()
        )
        or not isinstance(text_reference_fields, Mapping)
        or not text_reference_fields
        or any(
            not isinstance(field, str)
            or not field
            or field not in nonempty_fields
            or not isinstance(references, Sequence)
            or isinstance(references, (str, bytes))
            or not references
            or any(
                not isinstance(reference, str) or reference not in exact_fields
                for reference in references
            )
            for field, references in text_reference_fields.items()
        )
        or not (exact_fields or nonempty_fields or number_ranges or date_ranges)
        or not isinstance(write_fields, Sequence)
        or isinstance(write_fields, (str, bytes))
        or not write_fields
        or len(set(write_fields)) != len(write_fields)
        or any(not isinstance(field, str) or not field for field in write_fields)
        or not set(write_fields)
        <= (
            set(exact_fields)
            | set(nonempty_fields)
            | set(number_ranges)
            | set(date_ranges)
        )
        or not isinstance(deliverable_evidence, Sequence)
        or isinstance(deliverable_evidence, (str, bytes))
        or not deliverable_evidence
        or len(set(deliverable_evidence)) != len(deliverable_evidence)
        or not isinstance(content_terms, Sequence)
        or isinstance(content_terms, (str, bytes))
        or not content_terms
        or any(not isinstance(term, str) or not term for term in content_terms)
        or not isinstance(minimum_version, int)
        or isinstance(minimum_version, bool)
        or minimum_version < 1
        or not isinstance(message_facts, Sequence)
        or isinstance(message_facts, (str, bytes))
        or not message_facts
        or any(not isinstance(fact, str) or not fact for fact in message_facts)
    ):
        raise CausalError("milestone business effect requirements are invalid")
    return {
        "decision_followup": {
            "allowed_channels": [str(item) for item in channels],
            "recipient_actor_id": _identifier(
                followup["recipient_actor_id"], "decision followup recipient"
            ),
            "related_record_id": _identifier(
                followup["related_record_id"], "decision followup related record"
            ),
            "required_evidence_ids": [str(item) for item in followup_evidence],
            "required_message_facts": [str(item) for item in message_facts],
            "semantic_requirements": _semantic_requirements(
                followup["semantic_requirements"]
            ),
            "sender_role": _identifier(
                followup["sender_role"], "decision followup sender role"
            ),
        },
        "crm_projection": {
            "date_ranges": {
                str(field): dict(bounds) for field, bounds in date_ranges.items()
            },
            "exact_fields": dict(exact_fields),
            "nonempty_fields": [str(item) for item in nonempty_fields],
            "number_ranges": {
                str(field): dict(bounds) for field, bounds in number_ranges.items()
            },
            "record_id": _identifier(crm["record_id"], "CRM projection record"),
            "text_reference_fields": {
                str(field): [str(reference) for reference in references]
                for field, references in text_reference_fields.items()
            },
            "write_fields": [str(item) for item in write_fields],
            "writer_role": _identifier(
                crm["writer_role"], "CRM projection writer role"
            ),
        },
        "deliverable": {
            "author_role": _identifier(
                deliverable["author_role"], "deliverable author role"
            ),
            "kind": _identifier(deliverable["kind"], "deliverable kind"),
            "minimum_version": minimum_version,
            "related_id": _identifier(
                deliverable["related_id"], "deliverable related id"
            ),
            "related_type": _identifier(
                deliverable["related_type"], "deliverable related type"
            ),
            "required_content_terms": [str(item) for item in content_terms],
            "required_evidence_ids": [str(item) for item in deliverable_evidence],
            "semantic_requirements": _semantic_requirements(
                deliverable["semantic_requirements"]
            ),
        },
    }


def milestone_definition(value: Mapping[str, Any]) -> MilestoneDefinition:
    if not isinstance(value, Mapping):
        raise CausalError("milestone definition must be an object")
    allowed = tuple(str(item) for item in value.get("allowed_resolutions", ()))
    if (
        not allowed
        or len(set(allowed)) != len(allowed)
        or not set(allowed) <= MILESTONE_RESOLUTIONS
    ):
        raise CausalError("milestone allowed resolutions are invalid")
    terminal = value.get("terminal_outcome_by_resolution", {})
    if not isinstance(terminal, Mapping) or any(
        key not in allowed or key == "inapplicable" or outcome not in TERMINAL_OUTCOMES
        for key, outcome in terminal.items()
    ):
        raise CausalError("milestone terminal outcome mapping is invalid")
    lane_effects = value.get("lane_effects_by_resolution", {})
    if not isinstance(lane_effects, Mapping):
        raise CausalError("milestone lane effects must be an object")
    normalized_effects: dict[str, dict[str, dict[str, Any]]] = {}
    for resolution, effects in lane_effects.items():
        if resolution not in allowed:
            raise CausalError("milestone lane effect resolution is invalid")
        normalized_effects[str(resolution)] = _explicit_effects(effects)
    approval = _approval_requirement(value.get("approval_requirement"))
    raw_business_effects = value.get("business_effect_requirements_by_resolution")
    required_effect_resolutions = set(allowed) - {"inapplicable"}
    if (
        not isinstance(raw_business_effects, Mapping)
        or set(raw_business_effects) != required_effect_resolutions
    ):
        raise CausalError("milestone business effect resolutions are invalid")
    business_effects = {
        str(resolution): _business_effect_requirements(requirements)
        for resolution, requirements in raw_business_effects.items()
    }
    chronology = value.get("chronology")
    if not isinstance(chronology, Mapping) or set(chronology) != {
        "available_at",
        "decision_times",
        "sequence",
    }:
        raise CausalError("milestone chronology must be an object")
    evidence = tuple(str(item) for item in value.get("evidence_ids", ()))
    if not evidence or len(set(evidence)) != len(evidence):
        raise CausalError("milestone evidence ids are invalid")
    decisions = tuple(str(item) for item in value.get("decision_artifact_ids", ()))
    if (
        not decisions
        or len(set(decisions)) != len(decisions)
        or not set(decisions) <= set(evidence)
    ):
        raise CausalError("milestone decision artifact ids are invalid")
    decision_times = chronology["decision_times"]
    if (
        not isinstance(decision_times, Mapping)
        or set(decision_times) != set(decisions)
        or any(
            not isinstance(times, Mapping)
            or set(times) != {"available_at", "created_at"}
            or not all(isinstance(item, str) and item for item in times.values())
            for times in decision_times.values()
        )
    ):
        raise CausalError("milestone decision chronology is invalid")
    prerequisites = tuple(
        str(item) for item in value.get("prerequisite_milestone_ids", ())
    )
    if len(set(prerequisites)) != len(prerequisites):
        raise CausalError("milestone prerequisites are invalid")
    raw_authorities = value.get("authority_requirements")
    if (
        not isinstance(raw_authorities, Sequence)
        or isinstance(raw_authorities, (str, bytes))
        or not raw_authorities
    ):
        raise CausalError("milestone authority requirements are required")
    authorities: list[dict[str, Any]] = []
    for raw in raw_authorities:
        if not isinstance(raw, Mapping) or set(raw) != {
            "actor_id",
            "decision_artifact_ids",
            "organization_scope",
            "rights",
        }:
            raise CausalError("milestone authority requirement is invalid")
        authority_decisions = tuple(str(item) for item in raw["decision_artifact_ids"])
        rights = tuple(str(item) for item in raw["rights"])
        if (
            not authority_decisions
            or len(set(authority_decisions)) != len(authority_decisions)
            or not set(authority_decisions) <= set(decisions)
            or not rights
            or len(set(rights)) != len(rights)
            or raw["organization_scope"] not in {"buyer", "seller", "third_party"}
        ):
            raise CausalError("milestone authority requirement is invalid")
        authorities.append(
            {
                "actor_id": _identifier(raw["actor_id"], "authority actor_id"),
                "decision_artifact_ids": authority_decisions,
                "organization_scope": str(raw["organization_scope"]),
                "rights": rights,
            }
        )
    if len({item["actor_id"] for item in authorities}) != len(authorities):
        raise CausalError("milestone authority actors are not unique")
    remedy_of = value.get("remedy_of")
    if remedy_of is not None:
        remedy_of = _identifier(remedy_of, "remedy_of")
    role_requirements = value.get("evidence_requirements_by_role")
    if not isinstance(role_requirements, Mapping) or not role_requirements:
        raise CausalError("milestone role evidence requirements are required")
    normalized_role_requirements = {
        str(role): tuple(str(item) for item in items)
        for role, items in role_requirements.items()
        if isinstance(items, Sequence) and not isinstance(items, (str, bytes))
    }
    if set(normalized_role_requirements) != set(role_requirements) or any(
        not set(items) <= set(evidence)
        for items in normalized_role_requirements.values()
    ):
        raise CausalError("milestone role evidence requirements are invalid")
    decision_evidence_role = _identifier(
        value.get("decision_evidence_role"), "decision_evidence_role"
    )
    if decision_evidence_role not in normalized_role_requirements:
        raise CausalError("milestone decision evidence role is invalid")
    branch_id = value.get("branch_id")
    if branch_id is not None:
        branch_id = _identifier(branch_id, "branch_id")
    return MilestoneDefinition(
        _identifier(value.get("milestone_id"), "milestone_id"),
        _identifier(value.get("checkpoint_id"), "checkpoint_id"),
        _identifier(value.get("gate_id"), "gate_id"),
        prerequisites,
        decisions,
        evidence,
        normalized_role_requirements,
        decision_evidence_role,
        tuple(authorities),
        allowed,
        approval,
        business_effects,
        dict(chronology),
        normalized_effects,
        {str(key): str(outcome) for key, outcome in terminal.items()},
        remedy_of,
        branch_id,
    )


def milestone_resolution(value: Mapping[str, Any]) -> MilestoneResolution:
    if not isinstance(value, Mapping):
        raise CausalError("milestone resolution must be an object")
    resolution = _identifier(value.get("resolution"), "resolution")
    if resolution not in MILESTONE_RESOLUTIONS:
        raise CausalError("milestone resolution is invalid")
    evidence = tuple(str(item) for item in value.get("evidence_ids", ()))
    if len(set(evidence)) != len(evidence):
        raise CausalError("milestone resolution evidence ids are invalid")
    decisions = tuple(str(item) for item in value.get("decision_artifact_ids", ()))
    raw_authorities = value.get("authority_resolutions", ())
    if not isinstance(raw_authorities, Sequence) or isinstance(
        raw_authorities, (str, bytes)
    ):
        raise CausalError("milestone authority resolutions are invalid")
    authorities: list[dict[str, Any]] = []
    for raw in raw_authorities:
        if not isinstance(raw, Mapping) or set(raw) != {
            "actor_id",
            "decision_artifact_id",
            "organization_scope",
            "resolution",
            "rights",
        }:
            raise CausalError("milestone authority resolution is invalid")
        rights = tuple(str(item) for item in raw["rights"])
        if (
            not rights
            or len(set(rights)) != len(rights)
            or raw["organization_scope"] not in {"buyer", "seller", "third_party"}
            or raw["resolution"] not in MILESTONE_RESOLUTIONS - {"inapplicable"}
        ):
            raise CausalError("milestone authority resolution is invalid")
        authorities.append(
            {
                "actor_id": _identifier(raw["actor_id"], "authority actor_id"),
                "decision_artifact_id": _identifier(
                    raw["decision_artifact_id"], "authority decision_artifact_id"
                ),
                "organization_scope": str(raw["organization_scope"]),
                "resolution": str(raw["resolution"]),
                "rights": rights,
            }
        )
    if len({item["actor_id"] for item in authorities}) != len(authorities):
        raise CausalError("milestone authority resolutions are not unique")
    remedy_of = value.get("remedy_of")
    business_effects = value.get("business_effects")
    if not isinstance(business_effects, Mapping) or any(
        not isinstance(key, str) or not key or not isinstance(item, str) or not item
        for key, item in business_effects.items()
    ):
        raise CausalError("milestone resolution business effects are invalid")
    if resolution == "inapplicable":
        if (
            decisions
            or authorities
            or evidence
            or business_effects
            or remedy_of is not None
        ):
            raise CausalError(
                "inapplicable milestone resolution must not claim evidence"
            )
    else:
        if not decisions or len(set(decisions)) != len(decisions) or not authorities:
            raise CausalError("milestone resolution decision evidence is required")
        if not set(decisions) <= set(evidence):
            raise CausalError("milestone resolution decision evidence is invalid")
        if not evidence:
            raise CausalError("milestone resolution evidence is required")
        if resolution == "remedied":
            remedy_of = _identifier(remedy_of, "remedy_of")
        elif remedy_of is not None:
            raise CausalError("only remedied resolutions may set remedy_of")
    return MilestoneResolution(
        _identifier(value.get("milestone_id"), "milestone_id"),
        resolution,
        decisions,
        evidence,
        tuple(authorities),
        {str(key): str(item) for key, item in business_effects.items()},
        _identifier(value.get("effective_at"), "effective_at"),
        remedy_of,
    )


def action_effect_rule(value: Mapping[str, Any]) -> ActionEffectRule:
    if not isinstance(value, Mapping):
        raise CausalError("action effect rule must be an object")
    required_evidence_ids = tuple(
        str(item) for item in value.get("required_evidence_ids", ())
    )
    if len(set(required_evidence_ids)) != len(required_evidence_ids):
        raise CausalError("action effect evidence ids are invalid")
    tool_names = tuple(str(item) for item in value.get("tool_names", ()))
    if not tool_names or len(set(tool_names)) != len(tool_names):
        raise CausalError("action effect tool names are invalid")
    fact_type = _identifier(value.get("fact_type"), "action fact_type")
    if fact_type not in ACTION_FACT_TYPES:
        raise CausalError("action effect fact type is invalid")
    authority_actor_id = value.get("authority_actor_id")
    if authority_actor_id is not None:
        authority_actor_id = _identifier(
            authority_actor_id, "action authority_actor_id"
        )
    next_gate_id = value.get("next_gate_id")
    if next_gate_id is not None:
        next_gate_id = _identifier(next_gate_id, "action next_gate_id")
    authority_rights = tuple(str(item) for item in value.get("authority_rights", ()))
    if len(set(authority_rights)) != len(authority_rights):
        raise CausalError("action authority rights are invalid")
    if fact_type == "authority_decision_observed" and (
        authority_actor_id is None or not authority_rights
    ):
        raise CausalError("grounded action requires an authority actor")
    if fact_type == "crm_transition" and next_gate_id is None:
        raise CausalError("CRM transition action requires a next gate")
    semantic_fields = {
        key: value.get(key)
        for key in (
            "purpose_code",
            "decision_code",
            "commitment_code",
            "resolution",
        )
    }
    if fact_type == "authority_decision_observed" and any(
        not isinstance(item, str) or not item for item in semantic_fields.values()
    ):
        raise CausalError("grounded action semantic codes are required")
    document_kind = value.get("document_kind")
    if fact_type == "authority_decision_observed" and (
        not isinstance(document_kind, str) or not document_kind
    ):
        raise CausalError("grounded deliverable kind is required")
    next_step_type = value.get("next_step_type")
    if fact_type == "crm_transition" and (
        not isinstance(next_step_type, str) or not next_step_type
    ):
        raise CausalError("CRM transition next step type is required")
    if fact_type == "authority_decision_observed" and semantic_fields != {
        "purpose_code": "recover_gate",
        "decision_code": "request_remediation_decision",
        "commitment_code": "complete_remediation",
        "resolution": "pending",
    }:
        raise CausalError("grounded action semantic codes are invalid")
    remediation_requirements = value.get("remediation_requirements")
    response_resolution = value.get("response_resolution")
    if fact_type == "authority_decision_observed" and (
        tool_names != ("communications.send",)
        or document_kind != "remediation_plan"
        or response_resolution not in {"accepted", "rejected", "deferred"}
        or not isinstance(remediation_requirements, Mapping)
        or set(remediation_requirements)
        != {
            "action_code",
            "cure_data",
            "due_at",
            "evidence_checksums",
            "evidence_ids",
            "owner_role",
        }
    ):
        raise CausalError("grounded authority decision contract is invalid")
    if fact_type == "authority_decision_observed":
        if not isinstance(remediation_requirements, Mapping):
            raise CausalError("remediation requirements are invalid")
        requirements = dict(remediation_requirements)
        evidence_checksums = requirements["evidence_checksums"]
        remediation_evidence = requirements["evidence_ids"]
        cure_data = requirements["cure_data"]
        if (
            not isinstance(evidence_checksums, Mapping)
            or set(evidence_checksums) != set(required_evidence_ids)
            or any(
                not isinstance(checksum, str) or not checksum.startswith("sha256:")
                for checksum in evidence_checksums.values()
            )
            or not isinstance(remediation_evidence, Sequence)
            or isinstance(remediation_evidence, (str, bytes))
            or tuple(remediation_evidence) != required_evidence_ids
            or not isinstance(cure_data, Mapping)
            or not cure_data
            or any(
                not isinstance(requirements[field], str) or not requirements[field]
                for field in ("action_code", "due_at", "owner_role")
            )
        ):
            raise CausalError("remediation requirements are invalid")
        remediation_requirements = {
            "action_code": str(requirements["action_code"]),
            "cure_data": dict(cure_data),
            "due_at": str(requirements["due_at"]),
            "evidence_checksums": {
                str(key): str(item) for key, item in evidence_checksums.items()
            },
            "evidence_ids": tuple(str(item) for item in remediation_evidence),
            "owner_role": str(requirements["owner_role"]),
        }
    elif remediation_requirements is not None or response_resolution is not None:
        raise CausalError("non-authority action cannot define a remediation response")
    if fact_type == "crm_transition" and (
        tool_names != ("crm.update",)
        or next_step_type != "remediation_decision"
        or any(item is not None for item in semantic_fields.values())
    ):
        raise CausalError("CRM transition contract is invalid")
    return ActionEffectRule(
        _identifier(value.get("effect_id"), "effect_id"),
        _identifier(value.get("branch_id"), "branch_id"),
        _identifier(value.get("checkpoint_id"), "checkpoint_id"),
        fact_type,
        _identifier(value.get("role"), "action role"),
        _identifier(value.get("gate_id"), "action gate_id"),
        _identifier(value.get("record_id"), "action record_id"),
        tool_names,
        required_evidence_ids,
        authority_actor_id,
        authority_rights,
        next_gate_id,
        semantic_fields["purpose_code"],
        semantic_fields["decision_code"],
        semantic_fields["commitment_code"],
        semantic_fields["resolution"],
        document_kind,
        next_step_type,
        remediation_requirements,
        str(response_resolution) if response_resolution is not None else None,
    )


def branch_definition(value: Mapping[str, Any]) -> BranchDefinition:
    if not isinstance(value, Mapping):
        raise CausalError("branch definition must be an object")
    raw_options = value.get("success_if_any")
    if (
        not isinstance(raw_options, Sequence)
        or isinstance(raw_options, (str, bytes))
        or not raw_options
    ):
        raise CausalError("branch success alternatives are required")
    options: list[tuple[str, ...]] = []
    for raw in raw_options:
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)) or not raw:
            raise CausalError("branch success alternative is invalid")
        option = tuple(str(item) for item in raw)
        if len(set(option)) != len(option):
            raise CausalError("branch success alternative is invalid")
        options.append(option)
    if len(set(options)) != len(options):
        raise CausalError("branch success alternatives are not unique")
    success = tuple(
        str(item) for item in value.get("success_decision_artifact_ids", ())
    )
    fallback = tuple(
        str(item) for item in value.get("fallback_decision_artifact_ids", ())
    )
    if (
        not success
        or not fallback
        or len(set(success)) != len(success)
        or len(set(fallback)) != len(fallback)
        or set(success) & set(fallback)
    ):
        raise CausalError("branch decision artifacts are invalid")
    recoverable = value.get("recoverable")
    if not isinstance(recoverable, bool):
        raise CausalError("branch recoverable flag must be boolean")
    return BranchDefinition(
        _identifier(value.get("branch_id"), "branch_id"),
        _identifier(value.get("action_checkpoint_id"), "action_checkpoint_id"),
        _identifier(value.get("resolution_checkpoint_id"), "resolution_checkpoint_id"),
        _identifier(value.get("remedy_milestone_id"), "remedy_milestone_id"),
        recoverable,
        tuple(options),
        success,
        fallback,
    )


def branch_resolution(value: Mapping[str, Any]) -> BranchResolution:
    if not isinstance(value, Mapping):
        raise CausalError("branch resolution must be an object")
    option = value.get("option")
    if option not in {"success", "fallback"}:
        raise CausalError("branch resolution option is invalid")
    effect_ids = tuple(str(item) for item in value.get("effect_ids", ()))
    action_keys = tuple(str(item) for item in value.get("action_keys", ()))
    selected = tuple(
        str(item) for item in value.get("selected_decision_artifact_ids", ())
    )
    if (
        len(set(effect_ids)) != len(effect_ids)
        or len(set(action_keys)) != len(action_keys)
        or not selected
        or len(set(selected)) != len(selected)
    ):
        raise CausalError("branch resolution support is invalid")
    if option == "fallback" and (effect_ids or action_keys):
        raise CausalError("fallback branch cannot claim successful effects")
    return BranchResolution(
        _identifier(value.get("branch_id"), "branch_id"),
        str(option),
        effect_ids,
        action_keys,
        selected,
        _identifier(value.get("resolved_at"), "resolved_at"),
    )


def _effect(
    delta: int,
    fact: str,
    status: str | None = None,
    absolute: int | None = None,
    sticky: bool = False,
) -> dict[str, Any]:
    return {
        "delta": delta,
        "fact": fact,
        **({"status": status} if status else {}),
        **({"absolute": absolute} if absolute is not None else {}),
        **({"sticky": True} if sticky else {}),
    }


def _explicit_effects(value: Any) -> dict[str, dict[str, Any]]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise CausalError("lane effects must be an object")
    result: dict[str, dict[str, Any]] = {}
    for lane, raw in value.items():
        if lane not in LANES:
            raise CausalError(f"unknown causal lane: {lane}")
        if isinstance(raw, int) and not isinstance(raw, bool):
            result[str(lane)] = _effect(raw, "structured public event")
            continue
        if not isinstance(raw, Mapping):
            raise CausalError(
                f"causal lane effect for {lane} must be an integer or object"
            )
        delta = raw.get("delta", 0)
        absolute = raw.get("absolute")
        sticky = raw.get("sticky", False)
        status = raw.get("status")
        fact = raw.get("fact", "structured public event")
        if (
            not isinstance(delta, int)
            or isinstance(delta, bool)
            or not -100 <= delta <= 100
        ):
            raise CausalError(f"causal lane delta for {lane} is invalid")
        if absolute is not None and (
            not isinstance(absolute, int)
            or isinstance(absolute, bool)
            or not -100 <= absolute <= 100
        ):
            raise CausalError(f"causal lane absolute score for {lane} is invalid")
        if not isinstance(sticky, bool):
            raise CausalError(f"causal lane sticky flag for {lane} is invalid")
        if status is not None and status not in LANE_STATUSES:
            raise CausalError(f"causal lane status for {lane} is invalid")
        if not isinstance(fact, str) or not fact:
            raise CausalError(f"causal lane fact for {lane} is invalid")
        result[str(lane)] = _effect(delta, fact, status, absolute, sticky)
    return result


def _facts(envelope: Mapping[str, Any]) -> tuple[str, ...]:
    values: list[str] = []
    purpose = envelope.get("purpose")
    if isinstance(purpose, str) and purpose:
        values.append(purpose)
    for key in ("related_records", "requested_decisions", "commitments"):
        items = envelope.get(key)
        if isinstance(items, Sequence) and not isinstance(items, (str, bytes)):
            values.extend(str(item) for item in items if isinstance(item, str) and item)
    return tuple(dict.fromkeys(values))


def forbidden_claims(lanes: Mapping[str, Mapping[str, Any]]) -> tuple[str, ...]:
    claims = ["deal is closed won", "purchase is complete"]
    if int(lanes["approvals"]["score"]) < 40:
        claims.extend(("final approval granted", "approved by procurement"))
    if int(lanes["commercial_terms"]["score"]) < 20:
        claims.append("contract is signed")
    return tuple(claims)


def select_stakeholder_act(
    world_id: str,
    action_key: str,
    actor_id: str,
    channel: str,
    envelope: Mapping[str, Any],
    lanes: Mapping[str, Mapping[str, Any]],
    decision_resolution: str | None = None,
) -> StakeholderAct:
    decisions = envelope.get("requested_decisions")
    commitments = envelope.get("commitments")
    if decision_resolution in {"accepted", "rejected", "deferred"}:
        kind, stance = {
            "accepted": ("accept_remediation", "decisive"),
            "rejected": ("reject_remediation", "decisive"),
            "deferred": ("defer_remediation", "cautious"),
        }[decision_resolution]
    elif (
        int(lanes["approvals"]["score"]) < 20
        and isinstance(decisions, Sequence)
        and not isinstance(decisions, (str, bytes))
        and decisions
    ):
        kind, stance = "request_approval_path", "cautious"
    elif int(lanes["validation"]["score"]) < 20:
        kind, stance = "request_evidence", "analytical"
    elif (
        int(lanes["commercial_terms"]["score"]) < 20
        and isinstance(commitments, Sequence)
        and not isinstance(commitments, (str, bytes))
        and commitments
    ):
        kind, stance = "clarify_commitment", "careful"
    else:
        kind, stance = "acknowledge_next_step", "neutral"
    identifier = digest(
        {
            "world_id": world_id,
            "action_key": action_key,
            "actor_id": actor_id,
            "kind": kind,
        }
    )[7:27]
    return StakeholderAct(
        f"stakeholder-act-{identifier}",
        action_key,
        actor_id,
        kind,
        "email" if channel not in {"email", "internal_chat"} else channel,
        stance,
        _facts(envelope),
        forbidden_claims(lanes),
    )


def realization_packet(
    act: StakeholderAct, prompt_hash: str, model_digest: str, seed: int
) -> dict[str, Any]:
    return {
        "act": act.to_dict(),
        "allowed_facts": list(act.allowed_facts),
        "stance": act.stance,
        "channel": act.channel,
        "forbidden_claims": list(act.forbidden_claims),
        "prompt_hash": prompt_hash,
        "model_digest": model_digest,
        "seed": seed,
    }


def realization_cache_key(
    world_state_hash: str,
    input_hash: str,
    packet: Mapping[str, Any],
    prompt_hash: str,
    model_digest: str,
    seed: int,
) -> str:
    return digest(
        {
            "world_state_hash": world_state_hash,
            "input_hash": input_hash,
            "packet": packet,
            "prompt_hash": prompt_hash,
            "model_digest": model_digest,
            "seed": seed,
        }
    )


def validate_realization(text: str, forbidden: Sequence[str]) -> str:
    if not isinstance(text, str) or not text.strip():
        raise CausalError("stakeholder realization must contain text")
    lowered = text.casefold()
    matched = next((claim for claim in forbidden if claim.casefold() in lowered), None)
    if matched is not None:
        raise CausalError(
            f"stakeholder realization contains forbidden claim: {matched}"
        )
    return text.strip()


def template_realization(packet: Mapping[str, Any]) -> str:
    act = packet.get("act")
    kind = str(act.get("kind", "")) if isinstance(act, Mapping) else ""
    templates = {
        "accept_remediation": "The documented remediation is accepted. Proceed to the stated next decision.",
        "reject_remediation": "The documented remediation is not accepted. Stop advancement and record the disposition.",
        "defer_remediation": "The remediation decision is deferred. Preserve the record and do not advance.",
        "request_approval_path": "Thanks for the update. Please confirm the remaining approval steps and owners.",
        "request_evidence": "Thanks for the update. Please send the supporting evidence before we confirm the next step.",
        "clarify_commitment": "Thanks for the update. Please clarify the commitment, owner, and due date.",
        "acknowledge_next_step": "Thanks for the update. We will review it and respond on the proposed next step.",
    }
    return validate_realization(
        templates.get(kind, templates["acknowledge_next_step"]),
        packet.get("forbidden_claims", ()),
    )


def realize(
    packet: Mapping[str, Any],
    command: Sequence[str] | None = None,
    timeout_seconds: float | None = None,
) -> str:
    if command is None:
        return template_realization(packet)
    if not command or any(not isinstance(item, str) or not item for item in command):
        raise CausalError("stakeholder realizer command is invalid")
    try:
        completed = subprocess.run(
            tuple(command),
            input=canonical_json(packet) + "\n",
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CausalError(f"stakeholder realizer failed: {exc}") from exc
    if completed.returncode != 0:
        raise CausalError(
            f"stakeholder realizer exited with status {completed.returncode}"
        )
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise CausalError("stakeholder realizer returned invalid JSON") from exc
    if not isinstance(value, Mapping):
        raise CausalError("stakeholder realizer response must be an object")
    text = value.get("text")
    if not isinstance(text, str):
        raise CausalError("stakeholder realizer response must contain text")
    return validate_realization(text, packet.get("forbidden_claims", ()))


__all__ = [
    "ACTION_FACT_TYPES",
    "LANES",
    "LANE_DEFAULTS",
    "MILESTONE_RESOLUTIONS",
    "ActionEffectRule",
    "BranchDefinition",
    "BranchResolution",
    "CausalError",
    "MilestoneDefinition",
    "MilestoneResolution",
    "StakeholderAct",
    "action_effect_rule",
    "branch_definition",
    "branch_resolution",
    "digest",
    "lane_status",
    "milestone_definition",
    "milestone_resolution",
    "normalize_official_seeds",
    "realization_cache_key",
    "realization_packet",
    "realize",
    "select_stakeholder_act",
    "validate_realization",
]
