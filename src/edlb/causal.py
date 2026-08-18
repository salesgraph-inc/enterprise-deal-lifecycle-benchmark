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


def public_event_effects(event: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    payload = event.get("payload")
    if not isinstance(payload, Mapping):
        return {}
    explicit = payload.get("lane_effects", payload.get("causal_effects"))
    if explicit is not None:
        return _explicit_effects(explicit)
    kind = str(event.get("kind", ""))
    if kind == "stakeholder_departed":
        handoff = payload.get("handoff_actor_ids")
        strong = (
            isinstance(handoff, Sequence)
            and not isinstance(handoff, (str, bytes))
            and bool(handoff)
        )
        return {
            "stakeholder_consensus": _effect(
                -10 if strong else -50,
                "champion departed with a documented handoff"
                if strong
                else "champion departed without a documented handoff",
            )
        }
    if kind == "stakeholder_joined":
        supportive = payload.get("stated_position") == "requested_approval_path"
        return {
            "stakeholder_consensus": _effect(
                15 if supportive else -30, "late stakeholder joined the decision group"
            ),
            "approvals": _effect(
                10 if supportive else -10, "late stakeholder changed the approval path"
            ),
            **(
                {}
                if supportive
                else {
                    "urgency": _effect(
                        -20, "late stakeholder questioned the current priority"
                    )
                }
            ),
        }
    if kind == "budget_changed":
        available = payload.get("budget_status") == "reduced_allocation_available"
        return {
            "commercial_terms": _effect(
                -10 if available else -35, "buyer budget changed"
            ),
            "approvals": _effect(-5 if available else -25, "buyer budget changed"),
            "urgency": _effect(
                -10 if available else -70,
                "budget remains available"
                if available
                else "buyer spending is on hold",
            ),
        }
    if kind == "requirement_changed":
        covered = payload.get("seller_coverage") == "available_in_current_plan"
        return {
            "business_fit": _effect(
                10 if covered else -100,
                "new requirement is covered"
                if covered
                else "new requirement is outside the current seller plan",
            ),
            "validation": _effect(
                10 if covered else -60, "new requirement changed validation scope"
            ),
        }
    if kind == "external_signal_published":
        signal = payload.get("signal")
        if signal in {"incumbent_benchmark_disclosed", "incumbent_offer_referenced"}:
            transparent = signal == "incumbent_benchmark_disclosed"
            return {
                "competition": _effect(
                    -15 if transparent else -40,
                    "buyer disclosed an incumbent benchmark"
                    if transparent
                    else "buyer referenced an incumbent offer",
                )
            }
        if signal in {"temporary_industry_disruption", "buyer_program_paused"}:
            recoverable = payload.get("restart_status") == "workaround_confirmed"
            return {
                "urgency": _effect(
                    -15 if recoverable else -100,
                    "buyer confirmed a workaround"
                    if recoverable
                    else "buyer program is paused without an approved restart date",
                )
            }
    return {}


def action_effects(
    tool_name: str,
    arguments: Mapping[str, Any],
    result: Mapping[str, Any],
    external: bool,
) -> dict[str, dict[str, Any]]:
    envelope = arguments.get("semantic_envelope")
    envelope = envelope if isinstance(envelope, Mapping) else {}
    decisions = envelope.get("requested_decisions")
    commitments = envelope.get("commitments")
    has_decisions = (
        isinstance(decisions, Sequence)
        and not isinstance(decisions, (str, bytes))
        and bool(decisions)
    )
    has_commitments = (
        isinstance(commitments, Sequence)
        and not isinstance(commitments, (str, bytes))
        and bool(commitments)
    )
    if tool_name == "communications.send" and external:
        return {
            "stakeholder_consensus": _effect(
                12, "seller sent an authorized stakeholder communication"
            ),
            **(
                {"urgency": _effect(6, "seller requested a concrete decision")}
                if has_decisions
                else {}
            ),
            **(
                {"commercial_terms": _effect(6, "seller documented a commitment")}
                if has_commitments
                else {}
            ),
        }
    if tool_name in {"calendar.schedule", "calendar.reschedule"} and external:
        return {
            "stakeholder_consensus": _effect(
                8, "seller scheduled an authorized buyer meeting"
            ),
            "validation": _effect(12, "seller scheduled a buyer validation step"),
        }
    if tool_name in {"documents.create", "documents.revise"}:
        return {"validation": _effect(6, "seller produced a versioned deal document")}
    if tool_name == "documents.attach":
        return {"validation": _effect(4, "seller linked evidence to a deal record")}
    if tool_name == "approvals.request":
        return {"approvals": _effect(20, "seller requested an authorized approval")}
    if tool_name == "approvals.approve" and result.get("status") == "approved":
        return {
            "approvals": _effect(
                45, "authorized approver approved the request", "satisfied"
            )
        }
    if tool_name == "approvals.reject" and result.get("status") == "rejected":
        return {
            "approvals": _effect(
                -45, "authorized approver rejected the request", "blocked"
            )
        }
    return {}


def terminal_outcome(
    lanes: Mapping[str, Mapping[str, Any]],
) -> tuple[str, tuple[str, ...]]:
    scores = {lane: int(lanes[lane]["score"]) for lane in LANES}
    if scores["business_fit"] <= -50:
        return "disqualified", ("business_fit",)
    if scores["urgency"] <= -70:
        return "canceled", ("urgency",)
    if scores["competition"] <= -60 or scores["stakeholder_consensus"] <= -60:
        lane = (
            "competition" if scores["competition"] <= -60 else "stakeholder_consensus"
        )
        return "closed_lost", (lane,)
    required = {
        "business_fit": 0,
        "stakeholder_consensus": 40,
        "validation": 40,
        "commercial_terms": 20,
        "approvals": 40,
        "competition": -40,
        "urgency": 0,
    }
    missing = tuple(
        lane for lane, threshold in required.items() if scores[lane] < threshold
    )
    return ("no_decision", missing) if missing else ("closed_won", ())


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
) -> StakeholderAct:
    decisions = envelope.get("requested_decisions")
    commitments = envelope.get("commitments")
    if (
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
    timeout_seconds: float = 30.0,
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
    "LANES",
    "LANE_DEFAULTS",
    "CausalError",
    "StakeholderAct",
    "action_effects",
    "digest",
    "lane_status",
    "normalize_official_seeds",
    "public_event_effects",
    "realization_cache_key",
    "realization_packet",
    "realize",
    "select_stakeholder_act",
    "terminal_outcome",
    "validate_realization",
]
