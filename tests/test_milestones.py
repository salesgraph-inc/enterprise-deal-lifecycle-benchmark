from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from edlb.engine import AuthorizationError, EngineError
from edlb.generate import REFERENCE_AGENT_MANIFEST
from edlb.grading import _context, _sqlite_state, grade_run
from edlb.models import Artifact
from edlb.protocol import ToolCall
from edlb.runner import (
    ProtocolViolation,
    RunLimits,
    _digest_bundle,
    _release_sources,
    load_world_bundle,
    open_world,
    replay_trace,
)
from edlb.tools import (
    ARGUMENT_SCHEMAS,
    COMMITMENT_CODES,
    DECISION_CODES,
    PURPOSE_CODES,
    REMEDIATION_PLAN,
    RESOLUTION_CODES,
    ToolDispatcher,
)

ROLES = ("account_executive", "domain_specialist", "sales_manager", "revops")
WORLDS = [
    value
    for value in sorted((ROOT / "benchmarks/v1/output/public").glob("*/*"))
    if (value / "manifest.json").is_file() and (value / "oracle.json").is_file()
]
AUTHORING = {
    value["world_id"]: value
    for value in (
        json.loads(line)
        for line in (ROOT / "benchmarks/v1/authoring/worlds.jsonl")
        .read_text()
        .splitlines()
    )
}
WORLD_BY_ID = {value.name: value for value in WORLDS}


def read_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError("fixture must contain an object")
    return value


def trace_rows(world: Path) -> list[dict[str, object]]:
    return [
        json.loads(line)
        for line in (world / "reference_trace.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]


def configured_engine(world: Path):
    return open_world(
        world,
        agent_manifest=REFERENCE_AGENT_MANIFEST,
        limits=RunLimits(None, None, None, 0),
    )


def checkpoint_prefix(
    rows: list[dict[str, object]], checkpoint_id: str
) -> list[dict[str, object]]:
    indexes = [
        index
        for index, row in enumerate(rows)
        if row.get("kind") == "tool_call"
        and row.get("tool_name") == "run.complete_checkpoint"
        and isinstance(row.get("arguments"), dict)
        and row["arguments"].get("checkpoint_id") == checkpoint_id
    ]
    if len(indexes) != len(ROLES):
        raise AssertionError("reference checkpoint completions are incomplete")
    return rows[: indexes[-1] + 1]


def retarget_effect(effect: dict[str, object], resolution: str) -> dict[str, object]:
    value = json.loads(json.dumps(effect))
    followup = value["decision_followup"]
    deliverable = value["deliverable"]
    crm = value["crm_projection"]
    previous = followup["semantic_requirements"]["resolution"]
    for requirement in (followup, deliverable):
        semantic = requirement["semantic_requirements"]
        semantic["resolution"] = resolution
        for claim in semantic["evidence_claims"]:
            claim["resolution"] = resolution
    followup["required_message_facts"] = [
        resolution if fact == previous else fact
        for fact in followup["required_message_facts"]
    ]
    deliverable["required_content_terms"] = [
        resolution if term == previous else term
        for term in deliverable["required_content_terms"]
    ]
    crm["exact_fields"]["disposition_code"] = resolution
    return value


def retarget_checkpoint_rows(
    rows: list[dict[str, object]], checkpoint_id: str, resolution: str
) -> None:
    completions = [
        index
        for index, row in enumerate(rows)
        if row.get("kind") == "tool_call"
        and row.get("tool_name") == "run.complete_checkpoint"
        and isinstance(row.get("arguments"), dict)
        and row["arguments"].get("checkpoint_id") == checkpoint_id
    ]
    if len(completions) != len(ROLES):
        raise AssertionError("reference checkpoint completions are incomplete")
    earlier = [
        index
        for index, row in enumerate(rows[: completions[0]])
        if row.get("kind") == "tool_call"
        and row.get("tool_name") == "run.complete_checkpoint"
    ]
    start = earlier[-1] + 1 if earlier else 0
    for row in rows[start : completions[-1] + 1]:
        if row.get("kind") != "tool_call" or not isinstance(row.get("arguments"), dict):
            continue
        arguments = row["arguments"]
        envelope = arguments.get("semantic_envelope")
        if isinstance(envelope, dict):
            previous = envelope["resolution"]
            envelope["resolution"] = resolution
            for claim in envelope["evidence_claims"]:
                claim["resolution"] = resolution
            envelope["purpose"] = envelope["purpose"].replace(str(previous), resolution)
            for field in ("requested_decisions", "commitments"):
                envelope[field] = [
                    value.replace(str(previous), resolution)
                    for value in envelope[field]
                ]
            for field in ("body", "content"):
                if isinstance(arguments.get(field), str):
                    arguments[field] = arguments[field].replace(
                        str(previous), resolution
                    )
        changes = arguments.get("changes")
        if isinstance(changes, dict) and "disposition_code" in changes:
            changes["disposition_code"] = resolution


def milestone_facts(world: Path) -> list[dict[str, object]]:
    oracle = read_json(world / "oracle.json")
    facts = oracle["verification_facts"]
    if not isinstance(facts, dict) or not isinstance(facts.get("milestones"), list):
        raise TypeError("oracle milestones are invalid")
    return facts["milestones"]


def reference_resolution(world: Path, milestone: dict[str, object]) -> str:
    available_at = str(milestone["chronology"]["available_at"])
    values = [
        str(row["arguments"]["changes"]["disposition_code"])
        for row in trace_rows(world)
        if row.get("kind") == "tool_call"
        and row.get("tool_name") == "crm.update"
        and row.get("occurred_at") == available_at
        and isinstance(row.get("arguments"), dict)
        and isinstance(row["arguments"].get("changes"), dict)
        and row["arguments"]["changes"].get("disposition_code") is not None
    ]
    if values:
        return values[-1]
    terminal = milestone["terminal_outcome_by_resolution"]
    if milestone.get("branch_id") is not None and len(terminal) == 1:
        return str(next(iter(terminal)))
    return "inapplicable"


def accepted_world() -> Path:
    return next(
        world
        for world in WORLDS
        if "accepted"
        in milestone_facts(world)[0]["business_effect_requirements_by_resolution"]
    )


def branch_contract(
    world: Path,
) -> tuple[dict[str, object], dict[str, object], list[dict[str, object]]]:
    facts = read_json(world / "oracle.json")["verification_facts"]
    if not isinstance(facts, dict):
        raise TypeError("oracle verification facts are invalid")
    branch = facts["branches"][0]
    milestone = next(
        value
        for value in facts["milestones"]
        if value["milestone_id"] == branch["remedy_milestone_id"]
    )
    return branch, milestone, facts["action_effect_rules"]


def world_with_branch(recoverable: bool, min_authorities: int = 1) -> Path:
    for world in WORLDS:
        branch, milestone, _ = branch_contract(world)
        if (
            branch["recoverable"] is recoverable
            and len(milestone["authority_requirements"]) >= min_authorities
        ):
            return world
    raise AssertionError("matching branch world is unavailable")


def is_recovery_call(row: dict[str, object]) -> bool:
    if row.get("kind") != "tool_call" or not isinstance(row.get("arguments"), dict):
        return False
    arguments = row["arguments"]
    envelope = arguments.get("semantic_envelope")
    return bool(
        isinstance(envelope, dict)
        and envelope.get("purpose_code") == "recover_gate"
        or row.get("tool_name") == "documents.create"
        and arguments.get("kind") == "remediation_plan"
        or row.get("tool_name") == "crm.update"
        and isinstance(arguments.get("changes"), dict)
        and arguments["changes"].get("next_step_type") == "remediation_decision"
    )


def without_recovery_actions(
    rows: list[dict[str, object]],
) -> list[dict[str, object]]:
    recovery_document_ids = {
        str(row["result"]["document_id"])
        for row in rows
        if row.get("kind") == "tool_result"
        and isinstance(row.get("result"), dict)
        and row["result"].get("document_id") is not None
        and any(
            candidate.get("message_id") == row.get("call_id")
            and is_recovery_call(candidate)
            for candidate in rows
        )
    }
    removed = {
        str(row["message_id"])
        for row in rows
        if is_recovery_call(row)
        or row.get("kind") == "tool_call"
        and row.get("tool_name") == "documents.attach"
        and isinstance(row.get("arguments"), dict)
        and str(row["arguments"].get("document_id")) in recovery_document_ids
    }
    return [
        row
        for row in rows
        if str(row.get("message_id")) not in removed
        and str(row.get("call_id")) not in removed
    ]


def as_tool_call(row: dict[str, object], suffix: str = "") -> ToolCall:
    return ToolCall(
        f"{row['message_id']}{suffix}",
        str(row["tool_name"]),
        str(row["role"]),
        row["arguments"],
        str(row["idempotency_key"]),
    )


def replay_before_checkpoint(engine: object, world: Path, checkpoint_id: str) -> None:
    checkpoint = next(
        row
        for row in (
            json.loads(line)
            for line in (world / "checkpoints.jsonl").read_text().splitlines()
        )
        if row["checkpoint_id"] == checkpoint_id
    )
    replay_trace(
        engine,
        [
            row
            for row in trace_rows(world)
            if row.get("kind") == "start"
            or str(row.get("occurred_at", "")) < checkpoint["available_at"]
        ],
    )


def visible_structured_payload(value: dict[str, object]) -> dict[str, object]:
    payload = value.get("structured_payload")
    if isinstance(payload, dict):
        return payload
    for field in ("body", "content"):
        text = value.get(field)
        if not isinstance(text, str):
            continue
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and isinstance(
            parsed.get("structured_payload"), dict
        ):
            return parsed["structured_payload"]
    return {}


def visible_recovery_plan(
    engine: object, vertical: str
) -> tuple[dict[str, object], tuple[str, ...], dict[str, object]]:
    dispatcher = ToolDispatcher(engine)

    def dispatch(
        call_id: str,
        tool_name: str,
        role: str,
        arguments: dict[str, object],
        idempotency_key: str | None = None,
    ) -> dict[str, object]:
        result = dispatcher.dispatch(
            ToolCall(call_id, tool_name, role, arguments, idempotency_key)
        )
        if not result.ok or not isinstance(result.result, dict):
            raise AssertionError(result.error)
        return result.result

    status = dispatch(
        f"visible-status-{vertical}", "run.status", "account_executive", {}
    )
    checkpoint = status["checkpoint"]
    if not isinstance(checkpoint, dict):
        raise TypeError("active checkpoint is unavailable")
    deliverables = checkpoint["role_deliverables"]
    if not isinstance(deliverables, dict):
        raise TypeError("checkpoint role deliverables are unavailable")
    owners = [
        role
        for role, text in deliverables.items()
        if "Define a practical response" in str(text)
    ]
    if len(owners) != 1:
        raise AssertionError("remediation owner is not discoverable")
    owner = owners[0]
    label = str(checkpoint["label"])
    read_records: dict[str, dict[str, object]] = {}
    actors: list[str] = []
    resources = (
        ("communications.search", "communications.read", "message_id"),
        ("documents.search", "documents.read", "document_id"),
        ("web.search", "web.open", "record_id"),
    )
    for index, (search_tool, read_tool, identifier_field) in enumerate(resources):
        search = dispatch(
            f"visible-search-{vertical}-{index}",
            search_tool,
            owner,
            {"query": label, "limit": 100},
        )
        items = search.get("items", ())
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict) or identifier_field not in item:
                continue
            metadata = item.get("metadata")
            if isinstance(metadata, dict):
                for field in ("source_actors", "recipient_actors"):
                    values = metadata.get(field, ())
                    if not isinstance(values, list):
                        continue
                    actors.extend(
                        str(actor["actor_id"])
                        for actor in values
                        if isinstance(actor, dict)
                        and actor.get("kind") != "seller"
                        and actor.get("actor_id")
                    )
            identifier = str(item[identifier_field])
            if identifier in read_records:
                continue
            read_records[identifier] = dispatch(
                f"visible-read-{vertical}-{len(read_records)}",
                read_tool,
                owner,
                {identifier_field: identifier},
            )
    cure_fields = ("requirement", "requirement_version", "seller_coverage")
    values = {field: set() for field in cure_fields}
    payloads: dict[str, dict[str, object]] = {}
    for identifier, record in read_records.items():
        payload = visible_structured_payload(record)
        payloads[identifier] = payload
        for field in cure_fields:
            if field in payload:
                values[field].add(json.dumps(payload[field], sort_keys=True))
    if any(len(values[field]) != 1 for field in cure_fields):
        raise AssertionError("visible cure facts are incomplete or conflicting")
    cure_data = {field: json.loads(next(iter(values[field]))) for field in cure_fields}
    gates = {
        str(read_records[identifier].get("gate_id") or payload.get("gate_id"))
        for identifier, payload in payloads.items()
        if set(cure_fields) & payload.keys()
    }
    if len(gates) != 1:
        raise AssertionError("visible cure gate is ambiguous")
    gate_id = next(iter(gates))
    candidates = [
        identifier
        for identifier, payload in payloads.items()
        if str(read_records[identifier].get("gate_id") or payload.get("gate_id"))
        == gate_id
        and all(
            payload.get(field, value) == value for field, value in cure_data.items()
        )
    ]
    selected: list[str] = []
    uncovered = set(cure_fields)
    for identifier in sorted(
        candidates,
        key=lambda item: (
            -len(uncovered & payloads[item].keys()),
            item,
        ),
    ):
        covered = uncovered & payloads[identifier].keys()
        if covered:
            selected.append(identifier)
            uncovered -= covered
    if uncovered:
        raise AssertionError("visible cure facts lack supporting artifacts")
    supporting = next(
        (identifier for identifier in sorted(candidates) if identifier not in selected),
        None,
    )
    if supporting is None:
        raise AssertionError("benign supporting evidence is unavailable")
    evidence_ids = tuple(sorted([*selected, supporting]))
    if not actors:
        raise AssertionError("visible source actor is unavailable")
    crm = dispatch(
        f"visible-crm-{vertical}",
        "crm.search",
        owner,
        {"query": "", "limit": 100},
    )
    crm_items = crm.get("items", ())
    if not isinstance(crm_items, list) or not crm_items:
        raise AssertionError("visible deal record is unavailable")
    record_id = str(crm_items[0]["record_id"])
    envelope = {
        "target_actor_id": actors[0],
        "purpose": f"document the evidence-backed remediation for {label}",
        "purpose_code": "share_document",
        "gate_id": gate_id,
        "resolution": "pending",
        "related_records": [record_id],
        "requested_decisions": [],
        "decision_codes": [],
        "commitments": [f"record the documented {label} remediation plan"],
        "commitment_codes": ["complete_remediation"],
        "commitment_owner_role": owner,
        "decision_due_at": None,
        "commitment_due_at": checkpoint["window_end"],
        "attachments": list(evidence_ids),
        "evidence_claims": [
            {
                "artifact_id": artifact_id,
                "claim_type": "supports_gate_basis",
                "gate_id": gate_id,
                "resolution": "pending",
            }
            for artifact_id in evidence_ids
        ],
    }
    remediation = {
        "cure_data": cure_data,
        "gate_id": gate_id,
        "owner_role": owner,
    }
    plan = dispatch(
        f"visible-plan-{vertical}",
        "documents.create",
        owner,
        {
            "title": "draft supplied title",
            "content": "draft supplied content",
            "kind": "remediation_plan",
            "semantic_envelope": envelope,
            "remediation": remediation,
        },
        f"visible-plan-{vertical}",
    )
    return plan, evidence_ids, remediation


class MilestoneRuntimeTest(unittest.TestCase):
    def test_generic_actions_and_self_attestation_do_not_resolve(self) -> None:
        world = WORLDS[0]
        facts = read_json(world / "oracle.json")["verification_facts"]
        with configured_engine(world) as engine:
            engine.advance_checkpoint(idempotency_key="activate-generic-test")
            _release_sources(engine)
            checkpoint = engine.current_checkpoint()
            if checkpoint is None:
                raise AssertionError("checkpoint did not activate")
            buyer = next(
                json.loads(str(row[0]))
                for row in engine.connection.execute("SELECT data FROM actors")
                if json.loads(str(row[0]))["kind"] == "buyer"
            )
            sales_manager = next(
                json.loads(str(row[0]))
                for row in engine.connection.execute("SELECT data FROM actors")
                if json.loads(str(row[0]))["authority"]["role_id"]
                == "seller.sales_manager"
            )
            envelope = {
                "target_actor_id": buyer["actor_id"],
                "purpose": "claim completion",
                "purpose_code": "close_won",
                "gate_id": "gate-unrelated",
                "resolution": "accepted",
                "related_records": [facts["deal_id"]],
                "requested_decisions": ["approve everything"],
                "decision_codes": ["confirm_closing_authority"],
                "commitments": ["claim all gates are complete"],
                "commitment_codes": ["handoff_delivery"],
                "commitment_owner_role": "account_executive",
                "decision_due_at": checkpoint["window_end"],
                "commitment_due_at": checkpoint["window_end"],
                "attachments": ["artifact-unrelated"],
                "evidence_claims": [
                    {
                        "artifact_id": "artifact-unrelated",
                        "claim_type": "supports_gate_resolution",
                        "gate_id": "gate-unrelated",
                        "resolution": "accepted",
                    }
                ],
            }
            dispatcher = ToolDispatcher(engine)
            calls = [
                ToolCall(
                    "generic-email",
                    "communications.send",
                    "account_executive",
                    {
                        "channel": "email",
                        "recipients": [buyer["email"]],
                        "subject": "Complete",
                        "body": "Everything is complete.",
                        "semantic_envelope": envelope,
                    },
                    "generic-email",
                ),
                ToolCall(
                    "generic-meeting",
                    "calendar.schedule",
                    "account_executive",
                    {
                        "subject": "Completion meeting",
                        "start_at": checkpoint["window_start"],
                        "end_at": checkpoint["window_end"],
                        "participants": [buyer["email"]],
                        "description": "Everything is complete.",
                        "semantic_envelope": envelope,
                    },
                    "generic-meeting",
                ),
                ToolCall(
                    "generic-document",
                    "documents.create",
                    "domain_specialist",
                    {
                        "title": "Completion claim",
                        "content": "Everything is complete.",
                        "semantic_envelope": envelope,
                    },
                    "generic-document",
                ),
                ToolCall(
                    "generic-crm",
                    "crm.update",
                    "revops",
                    {
                        "record_id": facts["deal_id"],
                        "changes": {"stage": "closed_won"},
                    },
                    "generic-crm",
                ),
                ToolCall(
                    "generic-approval",
                    "approvals.request",
                    "account_executive",
                    {
                        "approver_actor_ids": [sales_manager["actor_id"]],
                        "purpose": "claim completion",
                        "details": {
                            "amount_minor_units": 0,
                            "gate": checkpoint["gate_id"],
                        },
                        "semantic_envelope": envelope,
                    },
                    "generic-approval",
                ),
            ]
            results = [dispatcher.dispatch(call) for call in calls]
            self.assertEqual(
                [result.ok for result in results], [True, True, True, False, True]
            )
            self.assertEqual(results[3].error["code"], "tool_error")
            self.assertNotEqual(
                engine.crm_read("revops", facts["deal_id"])["record"]["stage"],
                "closed_won",
            )
            approval_id = results[-1].result["approval_id"]
            self.assertTrue(
                dispatcher.dispatch(
                    ToolCall(
                        "generic-approval-decision",
                        "approvals.approve",
                        "sales_manager",
                        {"approval_id": approval_id, "note": "Approved."},
                        "generic-approval-decision",
                    )
                ).ok
            )
            completions = [
                dispatcher.dispatch(
                    ToolCall(
                        f"generic-complete-{role}",
                        "run.complete_checkpoint",
                        role,
                        {"checkpoint_id": checkpoint["checkpoint_id"]},
                        f"generic-complete-{role}",
                    )
                )
                for role in ROLES
            ]
            self.assertEqual(
                [result.ok for result in completions], [True, True, True, False]
            )
            self.assertEqual(engine.milestone_resolutions(), [])
            self.assertEqual(
                {lane: value["score"] for lane, value in engine.causal_lanes().items()},
                {
                    "approvals": 0,
                    "business_fit": 50,
                    "commercial_terms": 0,
                    "competition": 0,
                    "stakeholder_consensus": 20,
                    "urgency": 40,
                    "validation": 0,
                },
            )

    def test_reference_trace_resolves_every_milestone_without_oracle_leakage(
        self,
    ) -> None:
        world = next(
            value
            for value in WORLDS
            if read_json(value / "oracle.json")["expected_lanes"]["terminal_state"]
            == "closed_won"
        )
        rows = trace_rows(world)
        self.assertNotIn("milestone_id", json.dumps(rows, sort_keys=True))
        with configured_engine(world) as engine:
            result = replay_trace(engine, rows)
            resolutions = engine.milestone_resolutions()
            self.assertEqual(result.status, "completed")
            self.assertEqual(len(resolutions), len(engine.milestone_definitions))
            self.assertEqual(
                engine.finalize_terminal_outcome()["terminal_outcome"], "closed_won"
            )
            agent_views = []
            for role in ROLES:
                agent_views.extend(engine.events(role))
                agent_views.extend(engine.artifacts(role))
                agent_views.extend(engine.communications_search(role))
                agent_views.extend(engine.documents_search(role))
                agent_views.extend(engine.web_search(role))
                agent_views.append(engine.run_status(role))
            projection = json.dumps(agent_views, sort_keys=True)
            for forbidden in (
                "business_effect_requirements_by_resolution",
                "branch alternative",
                "branch_id",
                "branch_option",
                "effect_id",
                "expected_resolution",
                "fallback_decision_artifact_ids",
                "lane_effects_by_resolution",
                "prerequisite_milestone_ids",
                "remedy_of",
                "success_if_any",
                "terminal_outcome_by_resolution",
            ):
                self.assertNotIn(forbidden, projection)

    def test_recovery_actions_select_success_branch(self) -> None:
        world = world_with_branch(True)
        branch, _, _ = branch_contract(world)
        with configured_engine(world) as engine:
            result = replay_trace(engine, trace_rows(world))
            resolution = engine.branch_resolutions()[0]
            milestone = next(
                value
                for value in engine.milestone_resolutions()
                if value["milestone_id"] == branch["remedy_milestone_id"]
            )
            self.assertEqual(result.status, "completed")
            self.assertEqual(resolution["option"], "success")
            self.assertTrue(resolution["effect_ids"])
            self.assertEqual(milestone["resolution"], "remedied")
            self.assertIsNotNone(milestone["remedy_of"])

    def test_recovery_plan_is_solvable_from_agent_visible_evidence(self) -> None:
        self.assertNotIn("action_code", REMEDIATION_PLAN["properties"])
        self.assertNotIn("due_at", REMEDIATION_PLAN["properties"])
        self.assertNotIn("evidence_ids", REMEDIATION_PLAN["properties"])
        self.assertNotIn("evidence_checksums", REMEDIATION_PLAN["properties"])
        verticals = sorted(
            {read_json(world / "manifest.json")["vertical"] for world in WORLDS}
        )
        alternative_verticals = set()
        for vertical in verticals:
            with self.subTest(vertical=vertical):
                world = next(
                    value
                    for value in WORLDS
                    if read_json(value / "manifest.json")["vertical"] == vertical
                    and read_json(value / "oracle.json")["causal_family"]
                    == "requirements_change"
                    and branch_contract(value)[0]["recoverable"]
                )
                branch, _, rules = branch_contract(world)
                with configured_engine(world) as engine:
                    replay_before_checkpoint(
                        engine, world, str(branch["action_checkpoint_id"])
                    )
                    result, evidence_ids, remediation = visible_recovery_plan(
                        engine, str(vertical)
                    )
                    stored = result["metadata"]["remediation"]
                    self.assertEqual(stored, remediation)
                    self.assertEqual(
                        set(stored), {"cure_data", "gate_id", "owner_role"}
                    )
                    self.assertNotIn("verification_basis", result["metadata"])
                    self.assertNotIn("Due:", result["content"])
                    self.assertTrue(
                        all(
                            artifact_id not in result["content"]
                            for artifact_id in evidence_ids
                        )
                    )
                    document = json.loads(
                        str(
                            engine.connection.execute(
                                "SELECT data FROM documents WHERE document_id = ?",
                                (result["document_id"],),
                            ).fetchone()[0]
                        )
                    )
                    basis = document["metadata"]["verification_basis"]
                    self.assertEqual(basis["evidence_ids"], sorted(evidence_ids))
                    self.assertEqual(
                        set(basis["evidence_checksums"]), set(evidence_ids)
                    )
                    canonical = {
                        artifact_id
                        for rule in rules
                        if rule["fact_type"] == "authority_decision_observed"
                        for artifact_id in rule["required_evidence_ids"]
                    }
                    if set(evidence_ids) != canonical:
                        alternative_verticals.add(vertical)
        self.assertTrue(alternative_verticals)

    def test_recovery_authorities_are_active_status_contacts_before_action(
        self,
    ) -> None:
        recoverable = 0
        contact_fields = {
            "actor_id",
            "display_name",
            "email",
            "kind",
            "organization_id",
            "authority",
            "job_title",
        }
        for world in WORLDS:
            branch, milestone, _ = branch_contract(world)
            if not branch["recoverable"]:
                continue
            recoverable += 1
            required = {
                str(requirement["actor_id"])
                for requirement in milestone["authority_requirements"]
            }
            with self.subTest(world=world.name), configured_engine(world) as engine:
                replay_before_checkpoint(
                    engine, world, str(branch["action_checkpoint_id"])
                )
                active = {
                    str(actor["actor_id"])
                    for actor in AUTHORING[world.name]["actors"]
                    if str(actor["active_from"]) <= engine.current_time
                    and (
                        actor.get("active_until") is None
                        or engine.current_time < str(actor["active_until"])
                    )
                }
                dispatcher = ToolDispatcher(engine)
                for role in ROLES:
                    result = dispatcher.dispatch(
                        ToolCall(
                            f"contacts-{world.name}-{role}", "run.status", role, {}
                        )
                    )
                    self.assertTrue(result.ok, result.error)
                    contacts = result.result["active_contacts"]
                    contact_ids = [str(contact["actor_id"]) for contact in contacts]
                    self.assertEqual(contact_ids, sorted(active))
                    self.assertLessEqual(required, set(contact_ids))
                    for contact in contacts:
                        self.assertEqual(set(contact), contact_fields)
                        self.assertEqual(set(contact["authority"]), {"role_id"})
                self.assertNotIn("active_contacts", engine.run_status("system"))
        self.assertEqual(recoverable, 36)

    def test_family_specific_shortcuts_do_not_satisfy_recovery(self) -> None:
        mutations = {
            "budget_shock": ("budget_status", "seller_discount_only"),
            "requirements_change": ("requirement_version", "revision_1"),
            "competition": ("evaluation_record_owner", "seller"),
            "external_event": (None, "bulletin_only"),
        }
        for family, (field, invalid) in mutations.items():
            with self.subTest(family=family):
                authoring = next(
                    value
                    for value in AUTHORING.values()
                    if value["causal_family"] == family
                    and branch_contract(WORLD_BY_ID[value["world_id"]])[0][
                        "recoverable"
                    ]
                )
                world = WORLD_BY_ID[authoring["world_id"]]
                rows = trace_rows(world)
                plan = next(
                    row
                    for row in rows
                    if row.get("tool_name") == "documents.create"
                    and row.get("arguments", {}).get("kind") == "remediation_plan"
                )
                index = rows.index(plan)
                changed = json.loads(json.dumps(plan))
                if field is None:
                    field = next(
                        key
                        for key in changed["arguments"]["remediation"]["cure_data"]
                        if key.endswith("status")
                    )
                changed["arguments"]["remediation"]["cure_data"][field] = invalid
                with configured_engine(world) as engine:
                    replay_trace(engine, rows[:index])
                    result = ToolDispatcher(engine).dispatch(
                        as_tool_call(changed, f".{family}")
                    )
                    self.assertFalse(result.ok)

    def test_visible_recovery_path_binds_persisted_basis(self) -> None:
        world = next(
            value
            for value in WORLDS
            if read_json(value / "manifest.json")["vertical"] == "manufacturing"
            and read_json(value / "oracle.json")["causal_family"]
            == "requirements_change"
            and branch_contract(value)[0]["recoverable"]
        )
        branch, _, _ = branch_contract(world)
        action_checkpoint = next(
            row
            for row in (
                json.loads(line)
                for line in (world / "checkpoints.jsonl").read_text().splitlines()
            )
            if any(
                "Define a practical response" in str(deliverable)
                for deliverable in row["role_deliverables"].values()
            )
        )
        with configured_engine(world) as engine:
            replay_before_checkpoint(
                engine, world, str(action_checkpoint["checkpoint_id"])
            )
            plan, evidence_ids, remediation = visible_recovery_plan(
                engine, "manufacturing-authority"
            )
            dispatcher = ToolDispatcher(engine)

            def dispatch(
                call_id: str,
                tool_name: str,
                role: str,
                arguments: dict[str, object],
                idempotency_key: str | None = None,
            ) -> dict[str, object]:
                result = dispatcher.dispatch(
                    ToolCall(
                        call_id,
                        tool_name,
                        role,
                        arguments,
                        idempotency_key,
                    )
                )
                self.assertTrue(result.ok, result.error)
                if not isinstance(result.result, dict):
                    raise TypeError("tool result is unavailable")
                return result.result

            route = plan.get("authority_request")
            if not isinstance(route, dict):
                raise TypeError("authority request route is unavailable")
            self.assertEqual(
                set(route),
                {
                    "commitment_code",
                    "decision_code",
                    "gate_id",
                    "purpose_code",
                    "related_record_id",
                    "requester_role",
                    "resolution",
                },
            )
            self.assertNotIn("target_actor_id", route)
            self.assertNotIn("due_at", route)
            self.assertNotIn("evidence_ids", route)
            self.assertIn(route["purpose_code"], PURPOSE_CODES)
            self.assertIn(route["decision_code"], DECISION_CODES)
            self.assertIn(route["commitment_code"], COMMITMENT_CODES)
            self.assertIn(route["resolution"], RESOLUTION_CODES)
            attached = dispatcher.dispatch(
                ToolCall(
                    "alternative-plan-attach",
                    "documents.attach",
                    str(remediation["owner_role"]),
                    {
                        "document_id": plan["document_id"],
                        "related_type": "opportunity",
                        "related_id": route["related_record_id"],
                    },
                    "alternative-plan-attach",
                )
            )
            self.assertTrue(attached.ok, attached.error)

            status = dispatch(
                "visible-authority-status",
                "run.status",
                str(route["requester_role"]),
                {},
            )
            checkpoint = status.get("checkpoint")
            if not isinstance(checkpoint, dict):
                raise TypeError("active checkpoint is unavailable")
            contacts: dict[str, dict[str, object]] = {}
            resources = (
                ("communications.search", "message_id"),
                ("documents.search", "document_id"),
                ("web.search", "record_id"),
            )
            for query in (str(checkpoint["label"]), "engineering"):
                for index, (search_tool, identifier_field) in enumerate(resources):
                    search = dispatch(
                        f"visible-authority-search-{query}-{index}",
                        search_tool,
                        str(route["requester_role"]),
                        {"query": query, "limit": 100},
                    )
                    items = search.get("items", ())
                    if not isinstance(items, list):
                        continue
                    for item in items:
                        if not isinstance(item, dict):
                            continue
                        if (
                            query == checkpoint["label"]
                            and item.get(identifier_field) not in evidence_ids
                        ):
                            continue
                        metadata = item.get("metadata")
                        if not isinstance(metadata, dict):
                            continue
                        for field in ("source_actors", "recipient_actors"):
                            actors = metadata.get(field, ())
                            if not isinstance(actors, list):
                                continue
                            for actor in actors:
                                if (
                                    isinstance(actor, dict)
                                    and actor.get("kind") == "buyer"
                                    and str(actor.get("role", "")).endswith(
                                        "_authority"
                                    )
                                ):
                                    contacts[str(actor["actor_id"])] = actor
            self.assertEqual(
                {contact["role"] for contact in contacts.values()},
                {
                    "manufacturing.engineering_authority",
                    "manufacturing.plant_authority",
                },
            )
            due_at = str(checkpoint["window_end"])

            def authority_envelope(
                actor: dict[str, object], extra_attachments: tuple[str, ...] = ()
            ) -> dict[str, object]:
                return {
                    "target_actor_id": actor["actor_id"],
                    "purpose": "request the evidence-backed remediation decision",
                    "purpose_code": route["purpose_code"],
                    "gate_id": route["gate_id"],
                    "resolution": route["resolution"],
                    "related_records": [route["related_record_id"]],
                    "requested_decisions": ["confirm the remediation decision"],
                    "decision_codes": [route["decision_code"]],
                    "commitments": ["complete the documented remediation"],
                    "commitment_codes": [route["commitment_code"]],
                    "commitment_owner_role": route["requester_role"],
                    "decision_due_at": due_at,
                    "commitment_due_at": due_at,
                    "attachments": [
                        *evidence_ids,
                        plan["document_id"],
                        *extra_attachments,
                    ],
                    "evidence_claims": [
                        {
                            "artifact_id": artifact_id,
                            "claim_type": "supports_gate_basis",
                            "gate_id": route["gate_id"],
                            "resolution": route["resolution"],
                        }
                        for artifact_id in evidence_ids
                    ],
                }

            first_contact = next(iter(contacts.values()))
            dispatch(
                "visible-authority-mismatch",
                "communications.send",
                str(route["requester_role"]),
                {
                    "channel": "email",
                    "recipients": [first_contact["email"]],
                    "subject": "Remediation decision",
                    "body": "Review the attached remediation basis.",
                    "semantic_envelope": authority_envelope(
                        first_contact, (str(route["related_record_id"]),)
                    ),
                },
                "visible-authority-mismatch",
            )
            for index, actor in enumerate(contacts.values()):
                dispatch(
                    f"visible-authority-{index}",
                    "communications.send",
                    str(route["requester_role"]),
                    {
                        "channel": "email",
                        "recipients": [actor["email"]],
                        "subject": "Remediation decision",
                        "body": "Review the attached remediation basis.",
                        "semantic_envelope": authority_envelope(actor),
                    },
                    f"visible-authority-{index}",
                )
            for index, (search_tool, read_tool, identifier_field) in enumerate(
                (
                    ("communications.search", "communications.read", "message_id"),
                    ("documents.search", "documents.read", "document_id"),
                    ("web.search", "web.open", "record_id"),
                )
            ):
                search = dispatch(
                    f"visible-revops-search-{index}",
                    search_tool,
                    "revops",
                    {"query": checkpoint["label"], "limit": 100},
                )
                items = search.get("items", ())
                if not isinstance(items, list):
                    continue
                for item_index, item in enumerate(items):
                    if not isinstance(item, dict) or identifier_field not in item:
                        continue
                    dispatch(
                        f"visible-revops-read-{index}-{item_index}",
                        read_tool,
                        "revops",
                        {identifier_field: item[identifier_field]},
                    )
            update = dispatch(
                "visible-recovery-crm",
                "crm.update",
                "revops",
                {
                    "record_id": route["related_record_id"],
                    "changes": {
                        "next_step_gate_id": route["gate_id"],
                        "next_step_type": "remediation_decision",
                    },
                },
                "visible-recovery-crm",
            )
            record = update.get("record")
            if not isinstance(record, dict):
                raise TypeError("updated CRM record is unavailable")
            self.assertEqual(record["next_step_gate_id"], route["gate_id"])
            self.assertEqual(record["next_step_type"], "remediation_decision")
            engine._resolve_branches_from_checkpoint(
                str(branch["resolution_checkpoint_id"])
            )
            resolution = engine.branch_resolutions()[0]
            self.assertEqual(resolution["option"], "success")
            self.assertEqual(len(resolution["effect_ids"]), len(contacts) + 1)
            self.assertNotIn("visible-authority-mismatch", resolution["action_keys"])

    def test_remediation_evidence_rejects_invalid_provenance(self) -> None:
        world = next(
            value
            for value in WORLDS
            if read_json(value / "manifest.json")["vertical"] == "manufacturing"
            and read_json(value / "oracle.json")["causal_family"]
            == "requirements_change"
            and branch_contract(value)[0]["recoverable"]
        )
        branch, _, _ = branch_contract(world)
        with configured_engine(world) as engine:
            replay_before_checkpoint(engine, world, str(branch["action_checkpoint_id"]))
            _, evidence_ids, remediation = visible_recovery_plan(
                engine, "manufacturing-invalid"
            )
            owner = str(remediation["owner_role"])
            gate_id = str(remediation["gate_id"])
            cure_data = remediation["cure_data"]
            if not isinstance(cure_data, dict):
                raise TypeError("visible cure data is unavailable")
            base = engine._artifact_row(evidence_ids[0])

            def clone(identifier: str, **changes: object) -> str:
                value = json.loads(json.dumps(base))
                value.update(
                    {
                        "artifact_id": identifier,
                        "artifact_key": identifier,
                        "logical_document_id": None,
                        "supersedes_artifact_id": None,
                        "version": None,
                        **changes,
                    }
                )
                engine.append_artifact(Artifact.from_dict(value))
                return identifier

            def mark_read(identifier: str) -> None:
                call_id = f"read-{identifier}"
                engine._trace(
                    "tool_call",
                    owner,
                    {
                        "call_id": call_id,
                        "tool_name": "documents.read",
                        "arguments": {"document_id": identifier},
                    },
                )
                engine._trace(
                    "tool_result",
                    owner,
                    {"call_id": call_id, "ok": True, "result": {}},
                )

            supporting_payload = {
                key: value
                for key, value in base["structured_payload"].items()
                if key not in cure_data
            }
            supporting = clone(
                "artifact-supporting-remediation",
                structured_payload=supporting_payload,
            )
            mark_read(supporting)
            incomplete = (supporting,)
            unread = (clone("artifact-unread-remediation"),)
            wrong_gate_id = clone(
                "artifact-wrong-gate-remediation", gate_id="wrong_gate"
            )
            mark_read(wrong_gate_id)
            conflicting_payload = dict(base["structured_payload"])
            conflicting_payload[next(iter(cure_data))] = "conflicting"
            conflicting_id = clone(
                "artifact-conflicting-remediation",
                structured_payload=conflicting_payload,
            )
            mark_read(conflicting_id)
            hidden_id = clone(
                "artifact-hidden-remediation",
                visibility="role_scoped",
                visible_roles=[next(role for role in ROLES if role != owner)],
            )
            mark_read(hidden_id)
            stale_id = clone("artifact-stale-remediation")
            mark_read(stale_id)
            clone(
                "artifact-stale-successor-remediation",
                supersedes_artifact_id=stale_id,
            )
            future = next(
                str(row[0])
                for row in engine.connection.execute(
                    "SELECT artifact_id FROM artifacts WHERE available_at > ? ORDER BY available_at, artifact_id",
                    (engine.current_time,),
                )
            )
            mark_read(future)
            cases = {
                "incomplete": incomplete,
                "unread": unread,
                "wrong_gate": (wrong_gate_id,),
                "conflicting": (conflicting_id,),
                "wrong_role": (hidden_id,),
                "stale": (stale_id,),
                "unavailable": (future,),
            }
            for name, selected in cases.items():
                with self.subTest(name=name), self.assertRaises(EngineError):
                    engine._remediation_evidence_basis(
                        owner, gate_id, cure_data, selected
                    )

    def test_departed_and_late_actors_respect_activity_windows(self) -> None:
        champion_authoring = next(
            value
            for value in AUTHORING.values()
            if value["causal_family"] == "champion_exit"
            and branch_contract(WORLD_BY_ID[value["world_id"]])[0]["recoverable"]
        )
        champion_world = WORLD_BY_ID[champion_authoring["world_id"]]
        champion = next(
            actor
            for actor in champion_authoring["actors"]
            if actor["role_tags"] == ["champion"]
        )
        champion_rows = trace_rows(champion_world)
        champion_plan = next(
            row
            for row in champion_rows
            if row.get("tool_name") == "documents.create"
            and row.get("arguments", {}).get("kind") == "remediation_plan"
        )
        with configured_engine(champion_world) as engine:
            replay_trace(engine, champion_rows[: champion_rows.index(champion_plan)])
            with self.assertRaises(AuthorizationError):
                engine._recipient_actors("account_executive", [champion["email"]])
        late_authoring = next(
            value
            for value in AUTHORING.values()
            if value["causal_family"] == "late_stakeholder"
            and branch_contract(WORLD_BY_ID[value["world_id"]])[0]["recoverable"]
        )
        late_world = WORLD_BY_ID[late_authoring["world_id"]]
        late_actor = next(
            actor
            for actor in late_authoring["actors"]
            if actor["authority"]["role_id"] == "buyer.executive_sponsor"
        )
        with configured_engine(late_world) as engine:
            engine.advance_checkpoint(idempotency_key="activate-late-actor-test")
            with self.assertRaises(AuthorizationError):
                engine._recipient_actors("account_executive", [late_actor["email"]])

    def test_generated_activity_intervals_cover_every_participant(self) -> None:
        for world in WORLDS:
            authoring = AUTHORING[world.name]
            actors = {actor["actor_id"]: actor for actor in authoring["actors"]}
            for artifact in (
                json.loads(line)
                for line in (world / "artifacts.jsonl").read_text().splitlines()
            ):
                self.assertLessEqual(artifact["created_at"], artifact["available_at"])
                for actor_id in {
                    *artifact.get("source_actor_ids", ()),
                    *artifact.get("recipient_actor_ids", ()),
                }:
                    actor = actors[actor_id]
                    self.assertLessEqual(actor["active_from"], artifact["created_at"])
                    self.assertLessEqual(actor["active_from"], artifact["available_at"])
                    if actor.get("active_until"):
                        self.assertLess(artifact["created_at"], actor["active_until"])
                        self.assertLess(artifact["available_at"], actor["active_until"])
            for event in (
                json.loads(line)
                for line in (world / "events.jsonl").read_text().splitlines()
            ):
                self.assertLessEqual(event["effective_at"], event["recorded_at"])
                self.assertLessEqual(event["recorded_at"], event["available_at"])
                for actor_id in event["actor_ids"]:
                    actor = actors[actor_id]
                    self.assertLessEqual(actor["active_from"], event["effective_at"])
                    if actor.get("active_until"):
                        if event["kind"] == "stakeholder_departed":
                            self.assertEqual(
                                event["effective_at"], actor["active_until"]
                            )
                        else:
                            self.assertLess(
                                event["available_at"], actor["active_until"]
                            )

    def test_external_bulletin_without_authority_decision_falls_back(self) -> None:
        authoring = next(
            value
            for value in AUTHORING.values()
            if value["causal_family"] == "external_event"
            and branch_contract(WORLD_BY_ID[value["world_id"]])[0]["recoverable"]
        )
        world = WORLD_BY_ID[authoring["world_id"]]
        branch, _, _ = branch_contract(world)
        rows = checkpoint_prefix(trace_rows(world), branch["resolution_checkpoint_id"])
        removed = {
            str(row["message_id"])
            for row in rows
            if row.get("tool_name") == "communications.send"
            and row.get("arguments", {})
            .get("semantic_envelope", {})
            .get("purpose_code")
            == "recover_gate"
        }
        rows = [
            row
            for row in rows
            if str(row.get("message_id")) not in removed
            and str(row.get("call_id")) not in removed
        ]
        with configured_engine(world) as engine:
            replay_trace(engine, rows)
            self.assertEqual(engine.branch_resolutions()[0]["option"], "fallback")

    def test_missing_recovery_action_selects_fallback_branch(self) -> None:
        world = world_with_branch(True)
        branch, milestone, _ = branch_contract(world)
        prefix = checkpoint_prefix(
            trace_rows(world), str(branch["resolution_checkpoint_id"])
        )
        with configured_engine(world) as engine:
            result = replay_trace(engine, without_recovery_actions(prefix))
            self.assertEqual(result.status, "completed")
            self.assertEqual(engine.branch_resolutions()[0]["option"], "fallback")
            self.assertEqual(engine.branch_resolutions()[0]["effect_ids"], ())
            terminal = engine.finalize_terminal_outcome()["terminal_outcome"]
            self.assertIsNotNone(terminal)
            resolution = next(
                value
                for value in engine.milestone_resolutions()
                if value["milestone_id"] == branch["remedy_milestone_id"]
            )
            self.assertEqual(
                milestone["terminal_outcome_by_resolution"][resolution["resolution"]],
                terminal,
            )
            terminal_sequence = int(milestone["chronology"]["sequence"])
            self.assertEqual(
                engine.connection.execute(
                    "SELECT COUNT(*) FROM checkpoint_completions WHERE checkpoint_id = ?",
                    (branch["resolution_checkpoint_id"],),
                ).fetchone()[0],
                0,
            )
            self.assertTrue(
                all(
                    checkpoint["status"] == "skipped"
                    for checkpoint in engine.checkpoints()
                    if int(checkpoint["sequence"]) > terminal_sequence
                )
            )

    def test_fallback_scoring_distinguishes_unavoidable_and_missed_recovery(
        self,
    ) -> None:
        nonrecoverable = world_with_branch(False)
        with configured_engine(nonrecoverable) as engine:
            replay_trace(engine, trace_rows(nonrecoverable))
            score = grade_run(
                engine,
                nonrecoverable / "rubric.json",
                oracle=nonrecoverable / "oracle.json",
            )
            self.assertTrue(score["strict_cycle_pass"])
            self.assertGreaterEqual(score["execution_index"], 90.0)
        recoverable = world_with_branch(True)
        branch, _, _ = branch_contract(recoverable)
        rows = checkpoint_prefix(
            trace_rows(recoverable), str(branch["resolution_checkpoint_id"])
        )
        with configured_engine(recoverable) as engine:
            replay_trace(engine, without_recovery_actions(rows))
            score = grade_run(
                engine,
                recoverable / "rubric.json",
                oracle=recoverable / "oracle.json",
            )
            self.assertFalse(score["strict_cycle_pass"])
            self.assertLess(score["category_scores"]["longitudinal_recovery"], 1.0)
            failed_ids = {
                item["assertion_id"]
                for item in score["assertions"]
                if item["status"] == "failed"
            }
            rubric = read_json(recoverable / "rubric.json")
            recovery_ids = {
                assertion["target"]["path"]: assertion["assertion_id"]
                for assertion in rubric["assertions"]
            }
            self.assertIn(
                recovery_ids["verifier.post_intervention_crm_update"], failed_ids
            )
            self.assertIn(
                recovery_ids["verifier.post_intervention_stakeholder_action"],
                failed_ids,
            )

    def test_bare_closed_won_write_is_rejected_at_terminal_checkpoint(self) -> None:
        world = next(
            value
            for value in WORLDS
            if read_json(value / "oracle.json")["verification_facts"][
                "expected_terminal_outcome"
            ]
            == "closed_won"
        )
        rows = trace_rows(world)
        close_index = next(
            index
            for index, row in enumerate(rows)
            if row.get("tool_name") == "crm.update"
            and isinstance(row.get("arguments"), dict)
            and row["arguments"].get("changes", {}).get("stage") == "closed_won"
        )
        call = rows[close_index]
        with configured_engine(world) as engine:
            replay_trace(engine, rows[:close_index])
            result = ToolDispatcher(engine).dispatch(
                ToolCall(
                    "injected-bare-close",
                    "crm.update",
                    str(call["role"]),
                    {
                        "record_id": call["arguments"]["record_id"],
                        "changes": {"stage": "closed_won"},
                    },
                    "injected-bare-close",
                )
            )
            self.assertFalse(result.ok)
            self.assertEqual(result.error["code"], "tool_error")
            changes = call["arguments"]["changes"]
            first = engine.crm_update(
                str(call["role"]),
                str(call["arguments"]["record_id"]),
                changes,
                "grounded-close",
            )
            cached = engine.crm_update(
                str(call["role"]),
                str(call["arguments"]["record_id"]),
                changes,
                "grounded-close",
            )
            self.assertEqual(cached, first)
            with self.assertRaises(EngineError):
                engine.crm_update(
                    str(call["role"]),
                    str(call["arguments"]["record_id"]),
                    changes,
                    "duplicate-grounded-close",
                )

    def test_unselected_branch_sources_never_enter_agent_views(self) -> None:
        world = world_with_branch(True)
        branch, _, _ = branch_contract(world)
        with configured_engine(world) as engine:
            replay_trace(engine, trace_rows(world))
            unselected = set(branch["fallback_decision_artifact_ids"])
            views = []
            for role in ROLES:
                views.extend(engine.events(role))
                views.extend(engine.artifacts(role))
                views.extend(engine.communications_search(role))
                views.extend(engine.documents_search(role))
                views.extend(engine.web_search(role))
                for token in (
                    "branch_option",
                    "branch alternative",
                    "success_if_any",
                    "fallback_decision_artifact_ids",
                ):
                    self.assertEqual(engine.artifacts(role, token), [])
                    self.assertEqual(engine.communications_search(role, token), [])
                    self.assertEqual(engine.documents_search(role, token), [])
                    self.assertEqual(engine.web_search(role, token), [])
            projection = json.dumps(views, sort_keys=True)
            self.assertTrue(all(value not in projection for value in unselected))
            self.assertNotIn("branch_option", projection)
            self.assertNotIn("branch alternative", projection)
            artifacts = {
                row["artifact_id"]: row
                for row in (
                    json.loads(line)
                    for line in (world / "artifacts.jsonl").read_text().splitlines()
                )
            }

            def read_artifact(artifact_id: str) -> dict[str, object]:
                kind = artifacts[artifact_id]["kind"]
                if kind in {"email", "internal_chat", "call_transcript"}:
                    return engine.communications_read("sales_manager", artifact_id)
                if kind in {"web_page", "news_item"}:
                    return engine.web_open("sales_manager", artifact_id)
                return engine.documents_read("sales_manager", artifact_id)

            selected = set(branch["success_decision_artifact_ids"])
            for artifact_id in selected:
                value = json.dumps(read_artifact(str(artifact_id)), sort_keys=True)
                self.assertNotIn("branch_option", value)
                self.assertNotIn("branch alternative", value)
            for artifact_id in unselected:
                with self.assertRaises(EngineError):
                    read_artifact(str(artifact_id))

    def test_multi_authority_resolution_requires_independent_artifacts(self) -> None:
        world = world_with_branch(True, 2)
        _, milestone, _ = branch_contract(world)
        artifacts = {
            row["artifact_id"]: row
            for row in (
                json.loads(line)
                for line in (world / "artifacts.jsonl").read_text().splitlines()
            )
        }
        candidate_owners: dict[str, str] = {}
        for requirement in milestone["authority_requirements"]:
            actor_id = str(requirement["actor_id"])
            for artifact_id in requirement["decision_artifact_ids"]:
                artifact = artifacts[str(artifact_id)]
                decisions = artifact["structured_payload"]["authority_decisions"]
                self.assertEqual(artifact["source_actor_ids"], [actor_id])
                self.assertEqual(artifact["authority_decision_actor_id"], actor_id)
                self.assertEqual(len(decisions), 1)
                self.assertEqual(decisions[0]["actor_id"], actor_id)
                self.assertEqual(
                    set(decisions[0]["rights"]), set(requirement["rights"])
                )
                candidate_owners[str(artifact_id)] = actor_id
        self.assertEqual(
            len(set(candidate_owners.values())),
            len(milestone["authority_requirements"]),
        )

    def test_recovery_engages_every_required_authority_or_delegation(self) -> None:
        for vertical in ("commercial_insurance", "corporate_banking"):
            world = next(
                value
                for value in WORLDS
                if read_json(value / "manifest.json")["vertical"] == vertical
                and branch_contract(value)[0]["recoverable"]
            )
            branch, milestone, rules = branch_contract(world)
            rules_by_id = {str(rule["effect_id"]): rule for rule in rules}
            required = {
                str(requirement["actor_id"])
                for requirement in milestone["authority_requirements"]
            }
            self.assertGreaterEqual(len(required), 2)
            for option in branch["success_if_any"]:
                targeted = {
                    str(rules_by_id[str(effect_id)]["authority_actor_id"])
                    for effect_id in option
                    if rules_by_id[str(effect_id)]["fact_type"]
                    == "authority_decision_observed"
                }
                self.assertEqual(targeted, required)

    def test_action_write_and_effect_record_are_atomic(self) -> None:
        world = world_with_branch(True)
        rows = trace_rows(world)
        index = next(index for index, row in enumerate(rows) if is_recovery_call(row))
        call = rows[index]
        with configured_engine(world) as engine:
            replay_trace(engine, rows[:index])
            before = {
                table: engine.connection.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0]
                for table in (
                    "communications",
                    "causal_action_applications",
                    "documents",
                    "idempotency",
                )
            }
            with patch.object(
                engine,
                "apply_agent_action",
                side_effect=EngineError("injected effect failure"),
            ):
                result = ToolDispatcher(engine).dispatch(as_tool_call(call, ".atomic"))
            self.assertFalse(result.ok)
            self.assertEqual(
                before,
                {
                    table: engine.connection.execute(
                        f"SELECT COUNT(*) FROM {table}"
                    ).fetchone()[0]
                    for table in before
                },
            )

    def test_recovery_effects_are_revalidated_before_branch_freeze(self) -> None:
        world = world_with_branch(True)
        branch, _, _ = branch_contract(world)
        rows = trace_rows(world)
        action_checkpoint = str(branch["action_checkpoint_id"])
        first_completion = next(
            index
            for index, row in enumerate(rows)
            if row.get("kind") == "tool_call"
            and row.get("tool_name") == "run.complete_checkpoint"
            and row.get("arguments", {}).get("checkpoint_id") == action_checkpoint
        )
        with configured_engine(world) as engine:
            replay_trace(engine, rows[:first_completion])
            engine.crm_update(
                "revops",
                str(read_json(world / "oracle.json")["verification_facts"]["deal_id"]),
                {
                    "next_step_gate_id": "unrelated_gate",
                    "next_step_type": "coordinate_meeting",
                },
                "invalidate-recovery-crm",
            )
            engine._resolve_branches_from_checkpoint(
                str(branch["resolution_checkpoint_id"])
            )
            self.assertEqual(engine.branch_resolutions()[0]["option"], "fallback")

    def test_delayed_crm_effect_rejects_forged_stale_history(self) -> None:
        world = world_with_branch(True)
        branch, _, _ = branch_contract(world)
        with configured_engine(world) as engine:
            replay_trace(
                engine,
                checkpoint_prefix(
                    trace_rows(world), str(branch["action_checkpoint_id"])
                ),
            )
            application = next(
                json.loads(str(row[0]))
                for row in engine.connection.execute(
                    "SELECT effects FROM causal_action_applications"
                )
                if any(
                    engine.action_effect_rules[str(effect_id)].fact_type
                    == "crm_transition"
                    for effect_id in json.loads(str(row[0]))
                )
            )
            effect_id, support = next(
                (str(effect_id), dict(value))
                for effect_id, value in application.items()
                if engine.action_effect_rules[str(effect_id)].fact_type
                == "crm_transition"
            )
            history = engine.connection.execute(
                "SELECT record_id, role, changes, snapshot FROM crm_history WHERE history_id = ?",
                (support["support_id"],),
            ).fetchone()
            self.assertIsNotNone(history)
            cursor = engine.connection.execute(
                "INSERT INTO crm_history(record_id, changed_at, role, changes, snapshot) VALUES (?, ?, ?, ?, ?)",
                (history[0], "2000-01-01T00:00:00Z", *history[1:]),
            )
            support["support_id"] = str(cursor.lastrowid)
            resolution_position = engine.connection.execute(
                "SELECT position FROM checkpoints WHERE checkpoint_id = ?",
                (branch["resolution_checkpoint_id"],),
            ).fetchone()
            self.assertIsNotNone(resolution_position)
            engine._set_meta("current_checkpoint", str(resolution_position[0]))
            self.assertFalse(
                engine._effect_still_valid(
                    engine.action_effect_rules[effect_id], support
                )
            )

    def test_idempotent_recovery_retry_is_one_logical_action(self) -> None:
        world = world_with_branch(True)
        rows = trace_rows(world)
        index = next(index for index, row in enumerate(rows) if is_recovery_call(row))
        call = rows[index]
        with configured_engine(world) as engine:
            replay_trace(engine, rows[:index])
            dispatcher = ToolDispatcher(engine)
            first = dispatcher.dispatch(as_tool_call(call, ".first"))
            second = dispatcher.dispatch(as_tool_call(call, ".second"))
            self.assertTrue(first.ok)
            self.assertTrue(second.ok)
            key = str(call["idempotency_key"])
            self.assertEqual(
                engine.connection.execute(
                    "SELECT COUNT(*) FROM causal_action_applications WHERE action_key = ?",
                    (key,),
                ).fetchone()[0],
                1,
            )
            self.assertEqual(first.result, second.result)

    def test_branch_resolution_is_immutable_after_late_actions(self) -> None:
        world = world_with_branch(True)
        branch, _, _ = branch_contract(world)
        rows = trace_rows(world)
        resolution_prefix = checkpoint_prefix(
            rows, str(branch["resolution_checkpoint_id"])
        )
        late_call = next(row for row in rows if is_recovery_call(row))
        with configured_engine(world) as engine:
            replay_trace(engine, without_recovery_actions(resolution_prefix))
            before = engine.branch_resolutions()[0]
            result = ToolDispatcher(engine).dispatch(as_tool_call(late_call, ".late"))
            self.assertFalse(result.ok)
            self.assertEqual(engine.branch_resolutions()[0], before)

    def test_internal_attachment_does_not_count_as_shared_deliverable(self) -> None:
        world = world_with_branch(True)
        branch, _, _ = branch_contract(world)
        prefix = checkpoint_prefix(
            trace_rows(world), str(branch["resolution_checkpoint_id"])
        )
        removed = {
            str(row["message_id"])
            for row in prefix
            if row.get("kind") == "tool_call"
            and row.get("tool_name") == "communications.send"
            and isinstance(row.get("arguments"), dict)
            and row["arguments"].get("semantic_envelope", {}).get("purpose_code")
            == "recover_gate"
            and any(
                str(value).startswith("document-")
                for value in row["arguments"]["semantic_envelope"].get(
                    "attachments", ()
                )
            )
        }
        rows = [
            row
            for row in prefix
            if str(row.get("message_id")) not in removed
            and str(row.get("call_id")) not in removed
        ]
        with configured_engine(world) as engine:
            replay_trace(engine, rows)
            self.assertEqual(engine.branch_resolutions()[0]["option"], "fallback")

    def test_all_generated_branches_execute_success_and_fallback(self) -> None:
        self.assertIn("confirm_remedied_disposition", DECISION_CODES)
        self.assertIn(
            "confirm_remedied_disposition",
            json.dumps(ARGUMENT_SCHEMAS["communications.send"]),
        )
        observed: set[tuple[bool, str]] = set()
        for world in WORLDS:
            branch, _, _ = branch_contract(world)
            replays = []
            for _ in range(2):
                with configured_engine(world) as engine:
                    result = replay_trace(engine, trace_rows(world))
                    option = str(engine.branch_resolutions()[0]["option"])
                    oracle = read_json(world / "oracle.json")
                    terminal = engine.finalize_terminal_outcome()
                    score = grade_run(
                        engine,
                        world / "rubric.json",
                        oracle=world / "oracle.json",
                    )
                    self.assertEqual(result.status, "completed")
                    self.assertEqual(result.invalid_actions, 0)
                    self.assertEqual(result.error_count, 0)
                    self.assertEqual(
                        terminal["terminal_outcome"],
                        oracle["scenario_manifest"]["terminal_outcome"],
                    )
                    self.assertEqual(set(terminal["support"]), {"milestone"})
                    self.assertTrue(
                        score["strict_cycle_pass"],
                        (
                            world.name,
                            [
                                item["assertion_id"]
                                for item in score["assertions"]
                                if item["status"] == "failed"
                            ],
                            score["violations"],
                        ),
                    )
                    self.assertEqual(
                        option,
                        "success" if branch["recoverable"] else "fallback",
                    )
                    replays.append(
                        (
                            result.state_hash,
                            score["execution_index"],
                            score["strict_cycle_pass"],
                            terminal["terminal_outcome"],
                            option,
                        )
                    )
                    observed.add((bool(branch["recoverable"]), option))
            self.assertEqual(replays[0], replays[1], world.name)
        self.assertEqual(observed, {(True, "success"), (False, "fallback")})

    def test_delayed_fallback_forecast_survives_intermediate_checkpoints(self) -> None:
        authoring = next(
            value
            for value in AUTHORING.values()
            if not branch_contract(WORLD_BY_ID[value["world_id"]])[0]["recoverable"]
            and value["resolution_sequence"] - value["intervention_sequence"] >= 3
        )
        world = WORLD_BY_ID[authoring["world_id"]]
        checkpoints = [
            json.loads(line)
            for line in (world / "checkpoints.jsonl").read_text().splitlines()
        ]
        expected = checkpoints[authoring["resolution_sequence"]]["available_at"][:10]
        deal_id = read_json(world / "oracle.json")["verification_facts"]["deal_id"]
        for sequence in range(
            authoring["intervention_sequence"] + 1,
            authoring["resolution_sequence"],
        ):
            with self.subTest(sequence=sequence), configured_engine(world) as engine:
                replay_trace(
                    engine,
                    checkpoint_prefix(
                        trace_rows(world), checkpoints[sequence]["checkpoint_id"]
                    ),
                )
                record = engine.crm_read("revops", deal_id)["record"]
                self.assertEqual(record["close_date"], expected)

    def test_each_business_effect_is_required_and_read_only_farming_fails(
        self,
    ) -> None:
        world = accepted_world()
        milestone = milestone_facts(world)[0]
        checkpoint_id = str(milestone["checkpoint_id"])
        base = checkpoint_prefix(trace_rows(world), checkpoint_id)

        def without_calls(
            rows: list[dict[str, object]], names: set[str]
        ) -> list[dict[str, object]]:
            removed = {
                str(row["message_id"])
                for row in rows
                if row.get("kind") == "tool_call" and row.get("tool_name") in names
            }
            return [
                row
                for row in rows
                if str(row.get("message_id")) not in removed
                and str(row.get("call_id")) not in removed
            ]

        cases: dict[str, list[dict[str, object]]] = {
            "decision_followup": without_calls(base, {"communications.send"}),
            "crm_projection": without_calls(base, {"crm.update"}),
            "deliverable_create": without_calls(base, {"documents.create"}),
            "deliverable_link": without_calls(base, {"documents.attach"}),
            "read_only_farming": without_calls(
                base,
                {
                    "communications.send",
                    "crm.update",
                    "documents.create",
                    "documents.attach",
                },
            ),
            "one_effect": without_calls(
                base,
                {"communications.send", "documents.create", "documents.attach"},
            ),
        }
        scores: dict[str, float] = {}
        for case, rows in cases.items():
            with self.subTest(case=case), configured_engine(world) as engine:
                result = replay_trace(engine, rows)
                self.assertEqual(result.status, "running")
                self.assertEqual(engine.milestone_resolutions(), [])
                score = grade_run(
                    engine, world / "rubric.json", oracle=world / "oracle.json"
                )
                self.assertEqual(score["status"], "valid")
                self.assertFalse(score["strict_cycle_pass"])
                scores[case] = float(score["execution_index"])
        self.assertGreater(scores["read_only_farming"], 0.0)
        self.assertGreater(scores["one_effect"], scores["read_only_farming"])

        for case in ("envelope", "role", "forecast"):
            with self.subTest(case=case):
                rows = json.loads(json.dumps(base))
                if case in {"envelope", "role"}:
                    call = next(
                        row
                        for row in rows
                        if row.get("tool_name") == "documents.create"
                    )
                    if case == "envelope":
                        call["arguments"]["semantic_envelope"]["attachments"] = []
                    else:
                        call["role"] = "account_executive"
                else:
                    call = next(
                        row for row in rows if row.get("tool_name") == "crm.update"
                    )
                    call["arguments"]["changes"]["forecast_probability"] = 2.0
                with configured_engine(world) as engine:
                    result = replay_trace(engine, rows)
                    self.assertEqual(result.status, "running")
                    self.assertEqual(engine.milestone_resolutions(), [])

    def test_semantic_fields_and_state_are_independently_required(self) -> None:
        world = accepted_world()
        milestone = milestone_facts(world)[0]
        base = checkpoint_prefix(trace_rows(world), str(milestone["checkpoint_id"]))

        def mutate(rows: list[dict[str, object]], case: str) -> None:
            calls = [
                row
                for row in rows
                if row.get("kind") == "tool_call"
                and isinstance(row.get("arguments"), dict)
            ]
            envelopes = [
                row["arguments"]["semantic_envelope"]
                for row in calls
                if isinstance(row["arguments"].get("semantic_envelope"), dict)
            ]
            if case == "purpose_code":
                for envelope in envelopes:
                    envelope["purpose_code"] = "request_information"
            elif case == "decision_code":
                for envelope in envelopes:
                    envelope["decision_codes"] = ["request_information"]
            elif case == "commitment_code":
                for envelope in envelopes:
                    envelope["commitment_codes"] = ["follow_up"]
            elif case == "target_actor":
                for envelope in envelopes:
                    envelope["target_actor_id"] = "actor-unrelated"
            elif case == "gate":
                for envelope in envelopes:
                    envelope["gate_id"] = "gate-unrelated"
            elif case == "resolution":
                for envelope in envelopes:
                    envelope["resolution"] = "pending"
            elif case == "claim_type":
                for envelope in envelopes:
                    envelope["evidence_claims"][0]["claim_type"] = "context_only"
            elif case == "missing_claim":
                for envelope in envelopes:
                    envelope["evidence_claims"].pop()
            elif case == "extra_claim":
                for envelope in envelopes:
                    claim = dict(envelope["evidence_claims"][0])
                    claim["claim_type"] = "context_only"
                    envelope["evidence_claims"].append(claim)
            elif case == "extra_attachment":
                for envelope in envelopes:
                    envelope["attachments"].append("artifact-unrelated")
            elif case == "extra_related_record":
                for envelope in envelopes:
                    envelope["related_records"].append("deal-unrelated")
            elif case == "message_body":
                for row in calls:
                    if row.get("tool_name") == "communications.send":
                        row["arguments"]["subject"] = "x"
                        row["arguments"]["body"] = "x"
            else:
                update = next(
                    row for row in calls if row.get("tool_name") == "crm.update"
                )
                changes = update["arguments"]["changes"]
                if case == "next_step":
                    changes["next_step"] = "Review the current record."
                elif case == "next_step_decision":
                    changes["next_step_decision"] = "Review the current record."
                elif case == "next_step_type":
                    changes["next_step_type"] = "monitor_reentry"
                elif case == "next_step_gate":
                    changes["next_step_gate_id"] = "gate-unrelated"
                else:
                    changes["disposition_code"] = "pending"

        cases = (
            "purpose_code",
            "decision_code",
            "commitment_code",
            "target_actor",
            "gate",
            "resolution",
            "claim_type",
            "missing_claim",
            "extra_claim",
            "extra_attachment",
            "extra_related_record",
            "next_step",
            "next_step_decision",
            "next_step_type",
            "next_step_gate",
            "disposition_code",
        )
        for case in cases:
            with self.subTest(case=case):
                rows = json.loads(json.dumps(base))
                mutate(rows, case)
                with configured_engine(world) as engine:
                    result = replay_trace(engine, rows)
                    self.assertEqual(result.status, "running")
                    self.assertEqual(engine.milestone_resolutions(), [])

    def test_combined_semantic_farming_fails_for_every_terminal_outcome(
        self,
    ) -> None:
        worlds: dict[str, Path] = {}
        for world in WORLDS:
            state = str(
                read_json(world / "oracle.json")["expected_lanes"]["terminal_state"]
            )
            outcome = (
                "closed_lost"
                if state.startswith("closed_lost")
                else "disqualified"
                if state.startswith("disqualified")
                else state
            )
            worlds.setdefault(outcome, world)
        for outcome in ("closed_won", "closed_lost", "no_decision", "disqualified"):
            with self.subTest(outcome=outcome):
                world = worlds[outcome]
                milestone = milestone_facts(world)[0]
                rows = checkpoint_prefix(
                    trace_rows(world), str(milestone["checkpoint_id"])
                )
                for row in rows:
                    if row.get("kind") != "tool_call" or not isinstance(
                        row.get("arguments"), dict
                    ):
                        continue
                    arguments = row["arguments"]
                    envelope = arguments.get("semantic_envelope")
                    if isinstance(envelope, dict):
                        envelope.update(
                            {
                                "purpose": "x",
                                "purpose_code": "request_information",
                                "resolution": "pending",
                                "requested_decisions": [],
                                "decision_codes": [],
                                "commitments": [],
                                "commitment_codes": [],
                                "attachments": [],
                                "evidence_claims": [],
                            }
                        )
                    if row.get("tool_name") == "communications.send":
                        arguments["subject"] = "x"
                        arguments["body"] = "x"
                    elif row.get("tool_name") == "documents.create":
                        arguments["content"] = "x"
                    elif row.get("tool_name") == "crm.update":
                        changes = arguments["changes"]
                        if "next_step" in changes:
                            changes["next_step"] = "Review the current record."
                        if "next_step_decision" in changes:
                            changes["next_step_decision"] = "Review the current record."
                with configured_engine(world) as engine:
                    result = replay_trace(engine, rows)
                    self.assertNotEqual(result.status, "completed")
                    self.assertLess(
                        len(engine.milestone_resolutions()),
                        len(engine.milestone_definitions),
                    )

    def test_terminal_resolution_semantics_cannot_be_swapped(self) -> None:
        worlds: dict[str, Path] = {}
        for world in WORLDS:
            state = str(
                read_json(world / "oracle.json")["expected_lanes"]["terminal_state"]
            )
            outcome = (
                "closed_lost"
                if state.startswith("closed_lost")
                else "disqualified"
                if state.startswith("disqualified")
                else state
            )
            terminals = [
                milestone
                for milestone in milestone_facts(world)
                if milestone["terminal_outcome_by_resolution"].get(
                    reference_resolution(world, milestone)
                )
                == outcome
            ]
            if not terminals:
                continue
            terminal_id = terminals[0]["checkpoint_id"]
            has_actions = any(
                row.get("kind") == "tool_call"
                and row.get("tool_name") == "run.complete_checkpoint"
                and row.get("arguments", {}).get("checkpoint_id") == terminal_id
                for row in trace_rows(world)
            )
            if has_actions:
                worlds[outcome] = world
        self.assertEqual(set(worlds), {"closed_won", "closed_lost", "no_decision"})
        for outcome, world in worlds.items():
            with self.subTest(outcome=outcome):
                terminal = next(
                    milestone
                    for milestone in milestone_facts(world)
                    if milestone["terminal_outcome_by_resolution"].get(
                        reference_resolution(world, milestone)
                    )
                    == outcome
                )
                rows = checkpoint_prefix(
                    trace_rows(world), str(terminal["checkpoint_id"])
                )
                completions = [
                    index
                    for index, row in enumerate(rows)
                    if row.get("kind") == "tool_call"
                    and row.get("tool_name") == "run.complete_checkpoint"
                    and isinstance(row.get("arguments"), dict)
                    and row["arguments"].get("checkpoint_id")
                    == terminal["checkpoint_id"]
                ]
                prior = [
                    index
                    for index, row in enumerate(rows[: completions[0]])
                    if row.get("kind") == "tool_call"
                    and row.get("tool_name") == "run.complete_checkpoint"
                ]
                start = prior[-1] + 1 if prior else 0
                resolution = reference_resolution(world, terminal)
                expected_purpose = terminal[
                    "business_effect_requirements_by_resolution"
                ][resolution]["decision_followup"]["semantic_requirements"][
                    "purpose_code"
                ]
                wrong_purpose = next(
                    value for value in PURPOSE_CODES if value != expected_purpose
                )
                for row in rows[start : completions[-1] + 1]:
                    if row.get("kind") != "tool_call" or not isinstance(
                        row.get("arguments"), dict
                    ):
                        continue
                    envelope = row["arguments"].get("semantic_envelope")
                    if not isinstance(envelope, dict):
                        continue
                    envelope["purpose_code"] = wrong_purpose
                with configured_engine(world) as engine:
                    result = replay_trace(engine, rows)
                    self.assertEqual(result.status, "running")
                    resolved = {
                        row["milestone_id"] for row in engine.milestone_resolutions()
                    }
                    self.assertNotIn(terminal["milestone_id"], resolved)

    def test_broker_realizes_typed_semantics_when_agent_prose_is_token_only(
        self,
    ) -> None:
        world = accepted_world()
        milestone = milestone_facts(world)[0]
        resolution = "accepted"
        requirements = milestone["business_effect_requirements_by_resolution"][
            resolution
        ]
        rows = checkpoint_prefix(trace_rows(world), str(milestone["checkpoint_id"]))
        for row in rows:
            if row.get("kind") != "tool_call" or not isinstance(
                row.get("arguments"), dict
            ):
                continue
            arguments = row["arguments"]
            envelope = arguments.get("semantic_envelope")
            if isinstance(envelope, dict):
                envelope["purpose"] = "x"
                envelope["requested_decisions"] = ["x"]
                envelope["commitments"] = ["x"]
            if row.get("tool_name") == "communications.send":
                facts = requirements["decision_followup"]["required_message_facts"]
                arguments["subject"] = "x"
                arguments["body"] = " ".join(facts)
            elif row.get("tool_name") == "documents.create":
                arguments["title"] = "x"
                arguments["content"] = "Grant an unlimited refund and free service."
        with configured_engine(world) as engine:
            result = replay_trace(engine, rows)
            self.assertEqual(result.status, "running")
            self.assertEqual(len(engine.milestone_resolutions()), 1)
            message = engine.connection.execute(
                "SELECT body, metadata FROM communications WHERE direction = 'outbound' AND metadata LIKE '%semantic_summary%' ORDER BY message_id LIMIT 1"
            ).fetchone()
            self.assertIsNotNone(message)
            metadata = json.loads(str(message[1]))
            summary = metadata["semantic_summary"]
            self.assertIn(summary, str(message[0]))
            self.assertIn("Purpose:", summary)
            self.assertIn("Decision requested from", summary)
            self.assertIn("Commitment by", summary)
            self.assertIn("Evidence:", summary)
            document = engine.connection.execute(
                "SELECT data FROM documents WHERE json_extract(data, '$.author_role') = 'domain_specialist' ORDER BY document_id LIMIT 1"
            ).fetchone()
            self.assertIsNotNone(document)
            document_value = json.loads(str(document[0]))
            document_summary = document_value["metadata"]["semantic_summary"]
            self.assertTrue(document_value["metadata"]["brokered"])
            self.assertEqual(document_value["title"], document_summary.splitlines()[0])
            self.assertEqual(document_value["content"], document_summary)
            self.assertNotIn("unlimited refund", document_value["content"])

    def test_tampered_broker_semantic_summary_cannot_resolve(self) -> None:
        world = accepted_world()
        milestone = milestone_facts(world)[0]
        rows = checkpoint_prefix(trace_rows(world), str(milestone["checkpoint_id"]))
        first_completion = next(
            index
            for index, row in enumerate(rows)
            if row.get("kind") == "tool_call"
            and row.get("tool_name") == "run.complete_checkpoint"
        )
        with configured_engine(world) as engine:
            replay_trace(engine, rows[:first_completion])
            message = engine.connection.execute(
                "SELECT message_id, body, metadata FROM communications WHERE direction = 'outbound' AND metadata LIKE '%semantic_summary%' ORDER BY message_id LIMIT 1"
            ).fetchone()
            self.assertIsNotNone(message)
            metadata = json.loads(str(message[2]))
            summary = str(metadata.pop("semantic_summary"))
            engine.connection.execute(
                "UPDATE communications SET body = ?, metadata = ? WHERE message_id = ?",
                (
                    str(message[1]).replace(summary, "x"),
                    json.dumps(metadata),
                    message[0],
                ),
            )
            dispatcher = ToolDispatcher(engine)
            results = []
            for row in rows[first_completion:]:
                if (
                    row.get("kind") != "tool_call"
                    or row.get("tool_name") != "run.complete_checkpoint"
                ):
                    continue
                call = ToolCall(
                    str(row["message_id"]),
                    "run.complete_checkpoint",
                    str(row["role"]),
                    row["arguments"],
                    str(row["idempotency_key"]),
                )
                result = dispatcher.dispatch(call)
                results.append(result)
            self.assertTrue(any(not result.ok for result in results))
            self.assertEqual(engine.milestone_resolutions(), [])

    def test_alternate_grounded_language_resolves_without_reference_wording(
        self,
    ) -> None:
        world = accepted_world()
        milestone = milestone_facts(world)[0]
        resolution = "accepted"
        requirements = milestone["business_effect_requirements_by_resolution"][
            resolution
        ]
        rows = checkpoint_prefix(trace_rows(world), str(milestone["checkpoint_id"]))
        for row in rows:
            if row.get("kind") != "tool_call" or not isinstance(
                row.get("arguments"), dict
            ):
                continue
            arguments = row["arguments"]
            if row.get("tool_name") == "communications.send":
                facts = requirements["decision_followup"]["required_message_facts"]
                arguments["subject"] = "Buyer decision record"
                envelope = arguments["semantic_envelope"]
                envelope["commitments"] = [
                    f"We will preserve the {facts[1]} {facts[0]} evidence before the next action."
                ]
                envelope["purpose"] = (
                    f"Record the supported {facts[1]} {facts[0]} buyer disposition."
                )
                envelope["requested_decisions"] = [
                    f"Please verify the accountable owner for {facts[1]} {facts[0]}."
                ]
                arguments["body"] = "\n".join(
                    [
                        f"Our review of {facts[0]} records the {facts[1]} decision.",
                        envelope["purpose"],
                        *envelope["requested_decisions"],
                        *envelope["commitments"],
                    ]
                )
            elif row.get("tool_name") == "documents.create":
                terms = requirements["deliverable"]["required_content_terms"]
                envelope = arguments["semantic_envelope"]
                arguments["content"] = "\n".join(
                    [
                        "Reviewed evidence and decision facts: "
                        + ", ".join(terms)
                        + ".",
                        envelope["purpose"],
                        *envelope["requested_decisions"],
                        *envelope["commitments"],
                    ]
                )
            elif row.get("tool_name") == "crm.update":
                changes = arguments["changes"]
                gate = changes["next_step_gate_id"]
                changes["next_step"] = f"The assigned owner will review {gate}."
                if "next_step_decision" in changes:
                    changes["next_step_decision"] = (
                        f"Obtain the dated authority decision for {gate}."
                    )
        with configured_engine(world) as engine:
            result = replay_trace(engine, rows)
            self.assertEqual(result.status, "running")
            self.assertEqual(len(engine.milestone_resolutions()), 1)

    def test_neutral_external_work_uses_pending_semantics_without_progress(
        self,
    ) -> None:
        world = WORLDS[0]
        facts = read_json(world / "oracle.json")["verification_facts"]
        with configured_engine(world) as engine:
            engine.advance_checkpoint(idempotency_key="activate-neutral-test")
            _release_sources(engine)
            checkpoint = engine.current_checkpoint()
            if checkpoint is None:
                raise AssertionError("checkpoint did not activate")
            buyer = next(
                json.loads(str(row[0]))
                for row in engine.connection.execute("SELECT data FROM actors")
                if json.loads(str(row[0]))["kind"] == "buyer"
            )
            result = ToolDispatcher(engine).dispatch(
                ToolCall(
                    "neutral-email",
                    "communications.send",
                    "account_executive",
                    {
                        "channel": "email",
                        "recipients": [buyer["email"]],
                        "subject": "Information request",
                        "body": "Please send the missing exposure schedule.",
                        "semantic_envelope": {
                            "target_actor_id": buyer["actor_id"],
                            "purpose": "Request missing evidence.",
                            "purpose_code": "request_information",
                            "gate_id": checkpoint["gate_id"],
                            "resolution": "pending",
                            "related_records": [facts["deal_id"]],
                            "requested_decisions": [
                                "Confirm when the evidence will be available."
                            ],
                            "decision_codes": ["request_information"],
                            "commitments": [],
                            "commitment_codes": [],
                            "commitment_owner_role": "account_executive",
                            "decision_due_at": checkpoint["window_end"],
                            "commitment_due_at": None,
                            "attachments": [],
                            "evidence_claims": [],
                        },
                    },
                    "neutral-email",
                )
            )
            self.assertTrue(result.ok, result.error)
            self.assertEqual(engine.milestone_resolutions(), [])

    def test_checkpoint_views_do_not_select_semantic_codes_or_resolution(
        self,
    ) -> None:
        selected = set(PURPOSE_CODES) | set(DECISION_CODES) | set(COMMITMENT_CODES)
        selected |= set(RESOLUTION_CODES)
        for world in WORLDS:
            authority_ids = {
                str(requirement["actor_id"])
                for milestone in milestone_facts(world)
                for requirement in milestone["authority_requirements"]
            }
            for line in (world / "checkpoints.jsonl").read_text().splitlines():
                checkpoint = json.loads(line)
                deliverables = json.dumps(
                    checkpoint["role_deliverables"], sort_keys=True
                )
                self.assertTrue(all(code not in deliverables for code in selected))
                self.assertTrue(
                    all(actor_id not in deliverables for actor_id in authority_ids)
                )
                self.assertNotIn("supports_gate_resolution", deliverables)
                self.assertNotIn("next_step_type", deliverables)

    def test_semantically_valid_crm_projection_need_not_match_reference_values(
        self,
    ) -> None:
        world = accepted_world()
        milestone = milestone_facts(world)[0]
        rows = checkpoint_prefix(trace_rows(world), str(milestone["checkpoint_id"]))
        update = next(row for row in rows if row.get("tool_name") == "crm.update")
        changes = update["arguments"]["changes"]
        changes["forecast_probability"] = 0.41
        changes["next_step_owner"] = "sales_manager"
        changes["next_step_decision"] = (
            f"Validate {changes['next_step_gate_id']} with the dated buyer decision."
        )
        with configured_engine(world) as engine:
            replay_trace(engine, rows)
            self.assertEqual(len(engine.milestone_resolutions()), 1)

    def test_decision_evidence_must_be_read_by_the_authority_role(self) -> None:
        world = WORLDS[0]
        milestone = milestone_facts(world)[0]
        checkpoint_id = str(milestone["checkpoint_id"])
        evidence_id = str(milestone["decision_artifact_ids"][0])
        decision_role = str(milestone["decision_evidence_role"])
        for case in ("unread", "failed", "other_role"):
            with self.subTest(case=case):
                rows = checkpoint_prefix(trace_rows(world), checkpoint_id)
                read = next(
                    row
                    for row in rows
                    if row.get("kind") == "tool_call"
                    and row.get("role") == decision_role
                    and isinstance(row.get("arguments"), dict)
                    and evidence_id in row["arguments"].values()
                )
                if case == "unread":
                    rows.remove(read)
                elif case == "failed":
                    arguments = read["arguments"]
                    key = next(
                        key for key, value in arguments.items() if value == evidence_id
                    )
                    arguments[key] = "artifact-missing-evidence"
                else:
                    read["role"] = "account_executive"
                with configured_engine(world) as engine:
                    replay_trace(engine, rows)
                    self.assertEqual(engine.milestone_resolutions(), [])
                    self.assertEqual(
                        engine.connection.execute(
                            "SELECT COUNT(*) FROM checkpoint_completions"
                        ).fetchone()[0],
                        3,
                    )

    def test_decision_read_alone_cannot_complete_other_roles(self) -> None:
        world = WORLDS[0]
        milestone = milestone_facts(world)[0]
        rows = checkpoint_prefix(trace_rows(world), str(milestone["checkpoint_id"]))
        decision_ids = set(milestone["decision_artifact_ids"])
        decision_role = str(milestone["decision_evidence_role"])
        removed = {
            str(row["message_id"])
            for row in rows
            if row.get("kind") == "tool_call"
            and row.get("tool_name")
            in {
                "communications.read",
                "crm.read",
                "crm.history",
                "documents.read",
                "web.open",
            }
            and (
                row.get("role") != decision_role
                or not decision_ids & set(row.get("arguments", {}).values())
            )
        }
        rows = [
            row
            for row in rows
            if str(row.get("message_id")) not in removed
            and str(row.get("call_id")) not in removed
        ]
        with configured_engine(world) as engine:
            replay_trace(engine, rows)
            self.assertEqual(engine.milestone_resolutions(), [])
            self.assertEqual(
                engine.connection.execute(
                    "SELECT COUNT(*) FROM checkpoint_completions"
                ).fetchone()[0],
                3,
            )

    def test_extra_current_gate_evidence_is_accepted(self) -> None:
        selected = None
        for world in WORLDS:
            artifacts = [
                json.loads(line)
                for line in (world / "artifacts.jsonl").read_text().splitlines()
            ]
            checkpoints = {
                row["checkpoint_id"]: row
                for row in (
                    json.loads(line)
                    for line in (world / "checkpoints.jsonl").read_text().splitlines()
                )
            }
            for milestone in milestone_facts(world):
                visible = set(
                    checkpoints[milestone["checkpoint_id"]]["visible_artifact_ids"]
                )
                extra = next(
                    (
                        artifact
                        for artifact in artifacts
                        if artifact.get("gate_id") == milestone["gate_id"]
                        and artifact["artifact_id"] not in milestone["evidence_ids"]
                        and artifact["artifact_id"] in visible
                        and "/structured/" not in artifact["content"]["source_uri"]
                        and artifact["kind"]
                        in {
                            "document",
                            "proposal",
                            "quote",
                            "contract",
                            "diligence_document",
                            "policy_document",
                        }
                    ),
                    None,
                )
                if extra is not None:
                    selected = world, milestone, extra
                    break
            if selected is not None:
                break
        self.assertIsNotNone(selected)
        world, milestone, extra = selected
        checkpoint_id = str(milestone["checkpoint_id"])
        rows = checkpoint_prefix(trace_rows(world), checkpoint_id)
        first_completion = next(
            index
            for index, row in enumerate(rows)
            if row.get("tool_name") == "run.complete_checkpoint"
            and row.get("arguments", {}).get("checkpoint_id") == checkpoint_id
        )
        with configured_engine(world) as engine:
            replay_trace(engine, rows[:first_completion])
            self.assertTrue(engine._artifact_row(str(extra["artifact_id"])))
            dispatcher = ToolDispatcher(engine)
            read = dispatcher.dispatch(
                ToolCall(
                    "extra-current-gate-read",
                    "documents.read",
                    "account_executive",
                    {"document_id": extra["artifact_id"]},
                )
            )
            self.assertTrue(read.ok, read.error)
            for row in rows[first_completion:]:
                if row.get("tool_name") != "run.complete_checkpoint":
                    continue
                result = dispatcher.dispatch(
                    ToolCall(
                        str(row["message_id"]),
                        "run.complete_checkpoint",
                        str(row["role"]),
                        row["arguments"],
                        str(row["idempotency_key"]),
                    )
                )
                self.assertTrue(result.ok, result.error)
            resolution = next(
                value
                for value in engine.milestone_resolutions()
                if value["milestone_id"] == milestone["milestone_id"]
            )
            self.assertIn(extra["artifact_id"], resolution["evidence_ids"])

    def test_future_superseded_and_wrong_authority_evidence_cannot_resolve(
        self,
    ) -> None:
        world, milestone = next(
            (value, candidate)
            for value in WORLDS
            for candidate in milestone_facts(value)
            if len(candidate["authority_requirements"]) >= 2
            and candidate["branch_id"] is None
            and reference_resolution(value, candidate) != "inapplicable"
        )
        checkpoint_id = str(milestone["checkpoint_id"])
        decision_id = str(milestone["decision_artifact_ids"][0])
        rows = checkpoint_prefix(trace_rows(world), checkpoint_id)
        authority_actor_id = str(
            next(
                requirement["actor_id"]
                for requirement in milestone["authority_requirements"]
                if decision_id in requirement["decision_artifact_ids"]
            )
        )
        for case in (
            "future",
            "superseded",
            "authority",
            "cross_authority",
            "inactive",
            "gate",
            "right",
            "signed_right",
            "state",
        ):
            with self.subTest(case=case), configured_engine(world) as engine:
                row = engine.connection.execute(
                    "SELECT data FROM artifacts WHERE artifact_id = ?", (decision_id,)
                ).fetchone()
                artifact = json.loads(str(row[0]))
                if case == "future":
                    artifact["available_at"] = engine.scenario.end_at
                    engine.connection.execute(
                        "UPDATE artifacts SET data = ? WHERE artifact_id = ?",
                        (json.dumps(artifact, sort_keys=True), decision_id),
                    )
                elif case in {"authority", "cross_authority"}:
                    actor_row = engine.connection.execute(
                        "SELECT data FROM actors WHERE actor_id = ?",
                        (authority_actor_id,),
                    ).fetchone()
                    actor = json.loads(str(actor_row[0]))
                    if case == "authority":
                        actor["actor_id"] = "actor-same-authority-wrong-identity"
                        engine.connection.execute(
                            "INSERT INTO actors(actor_id, data) VALUES (?, ?)",
                            (actor["actor_id"], json.dumps(actor, sort_keys=True)),
                        )
                    else:
                        actor["actor_id"] = str(
                            next(
                                requirement["actor_id"]
                                for requirement in milestone["authority_requirements"]
                                if requirement["actor_id"] != authority_actor_id
                            )
                        )
                    artifact["structured_payload"]["author_actor_id"] = actor[
                        "actor_id"
                    ]
                    artifact["source_actor_ids"] = [actor["actor_id"]]
                    engine.connection.execute(
                        "UPDATE artifacts SET data = ? WHERE artifact_id = ?",
                        (json.dumps(artifact, sort_keys=True), decision_id),
                    )
                elif case in {"inactive", "gate", "right"}:
                    actor_row = engine.connection.execute(
                        "SELECT data FROM actors WHERE actor_id = ?",
                        (authority_actor_id,),
                    ).fetchone()
                    actor = json.loads(str(actor_row[0]))
                    if case == "inactive":
                        actor["active_until"] = "1970-01-01T00:00:00Z"
                    elif case == "gate":
                        actor["authority"]["gate_ids"] = []
                    else:
                        actor["authority"]["rights"] = []
                    engine.connection.execute(
                        "UPDATE actors SET data = ? WHERE actor_id = ?",
                        (json.dumps(actor, sort_keys=True), authority_actor_id),
                    )
                elif case == "signed_right":
                    artifact["structured_payload"]["authority_decisions"][0][
                        "rights"
                    ] = []
                    engine.connection.execute(
                        "UPDATE artifacts SET data = ? WHERE artifact_id = ?",
                        (json.dumps(artifact, sort_keys=True), decision_id),
                    )
                elif case == "state":
                    artifact["structured_payload"]["decision_state"] = "deferred"
                    engine.connection.execute(
                        "UPDATE artifacts SET data = ? WHERE artifact_id = ?",
                        (json.dumps(artifact, sort_keys=True), decision_id),
                    )
                else:
                    source = Artifact.from_dict(artifact)
                    engine.append_artifact(
                        replace(
                            source,
                            artifact_id="artifact-current-superseder",
                            created_at=engine.current_time,
                            available_at=engine.current_time,
                            supersedes_artifact_id=decision_id,
                        )
                    )
                replay_trace(engine, rows)
                self.assertNotIn(
                    milestone["milestone_id"],
                    {value["milestone_id"] for value in engine.milestone_resolutions()},
                )

    def test_exact_approval_requirement_cannot_be_substituted(self) -> None:
        world = next(
            value
            for value in WORLDS
            if any(
                milestone.get("approval_requirement") is not None
                and "accepted"
                in milestone["business_effect_requirements_by_resolution"]
                for milestone in milestone_facts(value)
            )
        )
        milestone = next(
            value
            for value in milestone_facts(world)
            if value.get("approval_requirement") is not None
            and "accepted" in value["business_effect_requirements_by_resolution"]
        )
        checkpoint_id = str(milestone["checkpoint_id"])
        for case in (
            "amount",
            "basis",
            "checkpoint",
            "gate",
            "approver",
            "policy_evidence",
            "policy_limit",
            "policy_owner",
            "trigger",
        ):
            with self.subTest(case=case):
                rows = checkpoint_prefix(trace_rows(world), checkpoint_id)
                request = next(
                    row
                    for row in rows
                    if row.get("tool_name") == "approvals.request"
                    and isinstance(row.get("arguments"), dict)
                    and isinstance(row["arguments"].get("details"), dict)
                    and row["arguments"]["details"].get("checkpoint_id")
                    == checkpoint_id
                )
                arguments = request["arguments"]
                details = arguments["details"]
                if case == "approver":
                    arguments["approver_actor_ids"] = ["actor-unrelated"]
                elif case == "basis":
                    details["basis"] = {**details["basis"], "value": "wrong"}
                elif case == "amount":
                    details["amount_minor_units"] += 1
                elif case == "policy_limit":
                    details["policy_limit_minor_units"] += 1
                elif case == "checkpoint":
                    details["checkpoint_id"] = "checkpoint-wrong"
                elif case == "gate":
                    details["gate"] = "gate-wrong"
                else:
                    details[case] = f"wrong-{case}"
                with configured_engine(world) as engine:
                    replay_trace(engine, rows)
                    self.assertNotIn(
                        str(milestone["milestone_id"]),
                        {
                            value["milestone_id"]
                            for value in engine.milestone_resolutions()
                        },
                    )

    def test_approval_chronology_and_unrelated_approval_are_exact(self) -> None:
        world = next(
            value
            for value in WORLDS
            if any(
                milestone.get("approval_requirement") is not None
                and "accepted"
                in milestone["business_effect_requirements_by_resolution"]
                for milestone in milestone_facts(value)
            )
        )
        milestone = next(
            value
            for value in milestone_facts(world)
            if value.get("approval_requirement") is not None
            and "accepted" in value["business_effect_requirements_by_resolution"]
        )
        checkpoint_id = str(milestone["checkpoint_id"])
        rows = checkpoint_prefix(trace_rows(world), checkpoint_id)
        request_index = next(
            index
            for index, row in enumerate(rows)
            if row.get("tool_name") == "approvals.request"
            and isinstance(row.get("arguments"), dict)
            and isinstance(row["arguments"].get("details"), dict)
            and row["arguments"]["details"].get("checkpoint_id") == checkpoint_id
        )
        decision_index = next(
            index
            for index, row in enumerate(rows[request_index + 1 :], request_index + 1)
            if row.get("tool_name") == "approvals.approve"
        )
        with configured_engine(world) as engine:
            replay_trace(engine, rows[: decision_index + 1])
            requirement = milestone["approval_requirement"]
            available_at = str(milestone["chronology"]["available_at"])
            self.assertTrue(engine._approval_satisfies(requirement, available_at))
            approval_row = engine.connection.execute(
                "SELECT approval_id, data FROM approvals WHERE json_extract(data, '$.details.checkpoint_id') = ?",
                (checkpoint_id,),
            ).fetchone()
            approval = json.loads(str(approval_row[1]))
            approval["created_at"] = "1970-01-01T00:00:00Z"
            engine.connection.execute(
                "UPDATE approvals SET data = ? WHERE approval_id = ?",
                (json.dumps(approval, sort_keys=True), approval_row[0]),
            )
            self.assertFalse(engine._approval_satisfies(requirement, available_at))
            approval["created_at"] = available_at
            approval["responded_at"] = engine.scenario.end_at
            engine.connection.execute(
                "UPDATE approvals SET data = ? WHERE approval_id = ?",
                (json.dumps(approval, sort_keys=True), approval_row[0]),
            )
            self.assertFalse(engine._approval_satisfies(requirement, available_at))

        with configured_engine(world) as engine:
            replay_trace(engine, checkpoint_prefix(trace_rows(world), checkpoint_id))
            resolution = next(
                value
                for value in engine.milestone_resolutions()
                if value["milestone_id"] == milestone["milestone_id"]
            )
            approval = next(
                json.loads(str(row[0]))
                for row in engine.connection.execute("SELECT data FROM approvals")
                if json.loads(str(row[0])).get("details", {}).get("checkpoint_id")
                == checkpoint_id
            )
            self.assertGreaterEqual(
                resolution["effective_at"], approval["responded_at"]
            )
            self.assertGreaterEqual(
                resolution["effective_at"],
                max(
                    value["available_at"]
                    for value in milestone["chronology"]["decision_times"].values()
                ),
            )

        exact_request_id = str(rows[request_index]["message_id"])
        exact_decision_id = str(rows[decision_index]["message_id"])
        without_exact = [
            row
            for row in rows
            if row.get("message_id") not in {exact_request_id, exact_decision_id}
            and row.get("call_id") not in {exact_request_id, exact_decision_id}
        ]
        with configured_engine(world) as engine:
            unrelated_envelope = {
                **rows[request_index]["arguments"]["semantic_envelope"],
                "related_records": [engine.scenario.seller_org_id],
            }
            unrelated = engine.approvals_request(
                "account_executive",
                milestone["approval_requirement"]["approver_actor_ids"],
                "unrelated zero-dollar request",
                {
                    "amount_minor_units": 0,
                    "gate": milestone["gate_id"],
                },
                "unrelated-zero-request",
                unrelated_envelope,
            )
            engine.approvals_approve(
                "sales_manager",
                unrelated["approval_id"],
                "Unrelated.",
                "unrelated-zero-decision",
            )
            replay_trace(engine, without_exact)
            self.assertNotIn(
                str(milestone["milestone_id"]),
                {value["milestone_id"] for value in engine.milestone_resolutions()},
            )

    def test_approval_idempotency_keys_are_semantically_opaque(self) -> None:
        world = next(
            value
            for value in WORLDS
            if any(
                milestone.get("approval_requirement") is not None
                for milestone in milestone_facts(value)
            )
        )
        rows = trace_rows(world)
        world_id = str(read_json(world / "manifest.json")["world_id"])
        requests = [
            row
            for row in rows
            if row.get("kind") == "tool_call"
            and row.get("tool_name") == "approvals.request"
        ]
        for index, request in enumerate(requests):
            result = next(
                row
                for row in rows
                if row.get("kind") == "tool_result"
                and row.get("call_id") == request["message_id"]
            )
            old_id = str(result["result"]["approval_id"])
            key = f"uuid-{index:04d}-opaque-idempotency-key"
            new_id = (
                "approval-"
                + hashlib.sha256(f"{world_id}:{key}".encode()).hexdigest()[:20]
            )
            request["idempotency_key"] = key
            for row in rows:
                if (
                    row.get("kind") == "tool_call"
                    and row.get("tool_name")
                    in {"approvals.approve", "approvals.reject"}
                    and row.get("arguments", {}).get("approval_id") == old_id
                ):
                    row["arguments"]["approval_id"] = new_id
                    row["idempotency_key"] = f"uuid-{index:04d}-opaque-decision-key"
        with configured_engine(world) as engine:
            result = replay_trace(engine, rows)
            state, trace = _sqlite_state(engine.connection)
            oracle = read_json(world / "oracle.json")
            verifier = _context(state, trace, oracle)["verifier"]
            self.assertEqual(result.status, "completed")
            self.assertTrue(verifier["approval_path_handled"])

    def test_prerequisites_are_acyclic_and_chronological(self) -> None:
        world = WORLDS[0]
        with configured_engine(world) as engine:
            definitions = sorted(
                engine.milestone_definitions.values(),
                key=lambda value: int(value.chronology["sequence"]),
            )
            engine.milestone_definitions[definitions[0].milestone_id] = replace(
                definitions[0],
                prerequisite_milestone_ids=(definitions[1].milestone_id,),
            )
            with self.assertRaisesRegex(EngineError, "prerequisite chronology"):
                engine._validate_milestone_contract()

    def test_non_remedy_prerequisites_become_inapplicable_after_fallback(self) -> None:
        world = world_with_branch(False)
        branch, _, _ = branch_contract(world)
        milestones = milestone_facts(world)
        fallback_at = next(
            index
            for index, milestone in enumerate(milestones)
            if milestone["milestone_id"] == branch["remedy_milestone_id"]
        )
        with configured_engine(world) as engine:
            result = replay_trace(engine, trace_rows(world))
            resolutions = {
                value["milestone_id"]: value["resolution"]
                for value in engine.milestone_resolutions()
            }
            self.assertEqual(result.status, "completed")
            self.assertIn(
                resolutions[str(milestones[fallback_at]["milestone_id"])],
                {"rejected", "deferred"},
            )
            self.assertTrue(
                all(
                    resolutions[str(milestone["milestone_id"])] == "inapplicable"
                    for milestone in milestones[fallback_at + 1 :]
                )
            )

    def test_terminal_mapping_must_be_unique(self) -> None:
        world = next(
            value
            for value in WORLDS
            if read_json(value / "oracle.json")["expected_lanes"]["terminal_state"]
            == "closed_won"
        )
        rows = trace_rows(world)
        for case in ("missing", "conflicting"):
            with self.subTest(case=case), configured_engine(world) as engine:
                replay_trace(engine, rows)
                definitions = sorted(
                    engine.milestone_definitions.values(),
                    key=lambda value: int(value.chronology["sequence"]),
                )
                resolutions = {
                    value["milestone_id"]: value["resolution"]
                    for value in engine.milestone_resolutions()
                }
                if case == "missing":
                    terminal = next(
                        value
                        for value in definitions
                        if resolutions[value.milestone_id]
                        in value.terminal_outcome_by_resolution
                    )
                    engine.milestone_definitions[terminal.milestone_id] = replace(
                        terminal, terminal_outcome_by_resolution={}
                    )
                else:
                    extra = next(
                        value
                        for value in definitions
                        if not value.terminal_outcome_by_resolution
                        and resolutions[value.milestone_id] != "inapplicable"
                    )
                    engine.milestone_definitions[extra.milestone_id] = replace(
                        extra,
                        terminal_outcome_by_resolution={
                            resolutions[extra.milestone_id]: "closed_won"
                        },
                    )
                engine.connection.execute(
                    "DELETE FROM meta WHERE key IN ('terminal_outcome', 'terminal_support')"
                )
                with self.assertRaisesRegex(
                    EngineError, "exactly one supported terminal resolution"
                ):
                    engine.finalize_terminal_outcome()

    def test_inapplicable_resolution_cannot_support_a_terminal_outcome(self) -> None:
        world = WORLDS[0]
        with configured_engine(world) as engine:
            definition = next(iter(engine.milestone_definitions.values()))
            engine.milestone_definitions[definition.milestone_id] = replace(
                definition,
                terminal_outcome_by_resolution={"inapplicable": "closed_won"},
            )
            with self.assertRaisesRegex(
                EngineError, "inapplicable milestone cannot be terminal"
            ):
                engine._validate_milestone_contract()

    def test_identical_trace_reproduces_resolution_and_state_hashes(self) -> None:
        world = WORLDS[0]
        rows = trace_rows(world)
        observed = []
        for _ in range(2):
            with configured_engine(world) as engine:
                result = replay_trace(engine, rows)
                observed.append(
                    (
                        result.status,
                        engine.milestone_resolutions(),
                        engine.finalize_terminal_outcome(),
                        engine.state_hash(),
                    )
                )
        self.assertEqual(observed[0], observed[1])

    def test_all_public_outcomes_have_explicit_final_mappings(self) -> None:
        observed: dict[str, set[str]] = {}
        for world in WORLDS:
            oracle = read_json(world / "oracle.json")
            outcome = str(oracle["verification_facts"]["expected_terminal_outcome"])
            milestones = milestone_facts(world)
            ids = [str(value["milestone_id"]) for value in milestones]
            self.assertEqual(len(ids), len(set(ids)))
            positions = {milestone_id: index for index, milestone_id in enumerate(ids)}
            for index, milestone in enumerate(milestones):
                prerequisites = milestone["prerequisite_milestone_ids"]
                self.assertTrue(
                    all(positions[str(value)] < index for value in prerequisites)
                )
                role_evidence = milestone["evidence_requirements_by_role"]
                self.assertEqual(set(role_evidence), set(ROLES))
                self.assertEqual(
                    {
                        str(evidence_id)
                        for evidence_ids in role_evidence.values()
                        for evidence_id in evidence_ids
                    }
                    | set(milestone["decision_artifact_ids"]),
                    set(milestone["evidence_ids"]),
                )
            mapped = [
                milestone
                for milestone in milestones
                if milestone["terminal_outcome_by_resolution"].get(
                    reference_resolution(world, milestone)
                )
            ]
            self.assertEqual(len(mapped), 1)
            resolution = reference_resolution(world, mapped[0])
            self.assertEqual(
                mapped[0]["terminal_outcome_by_resolution"][resolution],
                outcome,
            )
            observed.setdefault(outcome, set()).update(
                reference_resolution(world, value) for value in milestones
            )
        self.assertIn("accepted", observed["closed_won"])
        self.assertTrue(observed["closed_lost"] & {"rejected", "deferred"})
        self.assertIn("deferred", observed["no_decision"])
        self.assertIn("rejected", observed["disqualified"])

    def test_terminal_reference_actions_record_rational_dispositions(self) -> None:
        observed: set[str] = set()
        for world in WORLDS:
            oracle = read_json(world / "oracle.json")
            outcome = str(oracle["verification_facts"]["expected_terminal_outcome"])
            if outcome in observed:
                continue
            observed.add(outcome)
            terminal = next(
                milestone
                for milestone in milestone_facts(world)
                if milestone["terminal_outcome_by_resolution"].get(
                    reference_resolution(world, milestone)
                )
                == outcome
            )
            available_at = str(terminal["chronology"]["available_at"])
            rows = trace_rows(world)
            crm_updates = [
                row
                for row in rows
                if row.get("kind") == "tool_call"
                and row.get("tool_name") == "crm.update"
                and row.get("occurred_at") == available_at
                and isinstance(row.get("arguments"), dict)
                and isinstance(row["arguments"].get("changes"), dict)
                and "stage" in row["arguments"]["changes"]
            ]
            if crm_updates:
                changes = crm_updates[-1]["arguments"]["changes"]
                self.assertNotIn("terminal_outcome", changes)
                self.assertEqual(changes["stage"], outcome)
                if outcome == "closed_won":
                    self.assertEqual(changes["forecast_probability"], 1.0)
                else:
                    self.assertEqual(changes["forecast_probability"], 0.0)
                    self.assertTrue(changes["loss_reason"])
                    envelope = next(
                        row["arguments"]["semantic_envelope"]
                        for row in rows
                        if row.get("kind") == "tool_call"
                        and row.get("tool_name") == "communications.send"
                        and row.get("occurred_at") == available_at
                    )
                    self.assertNotIn("advanc", json.dumps(envelope).casefold())
            else:
                self.assertIsNotNone(terminal["branch_id"])
                self.assertIn(
                    reference_resolution(world, terminal), {"rejected", "deferred"}
                )
                self.assertFalse(
                    any(
                        row.get("kind") == "tool_call"
                        and row.get("occurred_at") == available_at
                        for row in rows
                    )
                )
            later_writes = [
                row
                for row in rows
                if row.get("kind") == "tool_call"
                and str(row.get("occurred_at")) > available_at
                and row.get("tool_name") != "run.complete_checkpoint"
            ]
            self.assertEqual(later_writes, [])
            checkpoints = [
                json.loads(line)
                for line in (world / "checkpoints.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
                if line.strip()
            ]
            terminal_sequence = int(terminal["chronology"]["sequence"])
            for checkpoint in checkpoints[terminal_sequence + 1 :]:
                self.assertEqual(
                    checkpoint["visible_gate"], "post-disposition closeout"
                )
                self.assertIn("stop buyer outreach", checkpoint["business_objective"])
                self.assertIn(
                    "No buyer outreach", checkpoint["completion_conditions"][0]
                )
        self.assertEqual(
            observed, {"closed_won", "closed_lost", "no_decision", "disqualified"}
        )

    def test_oracle_milestone_changes_change_the_scenario_hash(self) -> None:
        world = WORLDS[0]
        original = _digest_bundle(load_world_bundle(world))
        rows = trace_rows(world)
        self.assertEqual(rows[0]["payload"]["scenario_hash"], original)
        missing_hash = json.loads(json.dumps(rows))
        del missing_hash[0]["payload"]["scenario_hash"]
        with (
            configured_engine(world) as engine,
            self.assertRaisesRegex(ProtocolViolation, "scenario binding"),
        ):
            replay_trace(engine, missing_hash)
        with (
            configured_engine(world) as engine,
            self.assertRaisesRegex(ProtocolViolation, "first row"),
        ):
            replay_trace(engine, rows[1:])
        with tempfile.TemporaryDirectory() as directory:
            copied = Path(directory) / world.name
            shutil.copytree(world, copied)
            oracle_path = copied / "oracle.json"
            oracle = read_json(oracle_path)
            milestones = oracle["verification_facts"]["milestones"]
            resolution = reference_resolution(world, milestones[0])
            effects = milestones[0]["lane_effects_by_resolution"][resolution]
            effect = next(iter(effects.values()))
            effect["delta"] = int(effect.get("delta", 0)) + 1
            oracle_path.write_text(
                json.dumps(oracle, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )
            changed = _digest_bundle(load_world_bundle(copied))
            with (
                configured_engine(copied) as engine,
                self.assertRaisesRegex(ProtocolViolation, "scenario hash"),
            ):
                replay_trace(engine, rows)
        self.assertNotEqual(original, changed)


if __name__ == "__main__":
    unittest.main()
