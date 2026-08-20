from __future__ import annotations

import hashlib
import importlib.resources
import json
import re
import sys
import unittest
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

models = importlib.import_module("edlb.models")
protocol = importlib.import_module("edlb.protocol")
runner = importlib.import_module("edlb.runner")
causal = importlib.import_module("edlb.causal")
Actor = models.Actor
Artifact = models.Artifact
Assertion = models.Assertion
Checkpoint = models.Checkpoint
Event = models.Event
RoleGrant = models.RoleGrant
RunManifest = models.RunManifest
Scorecard = models.Scorecard
TraceEvent = models.TraceEvent
Message = protocol.Message
BundleError = runner.BundleError
normalize_agent_manifest = runner.normalize_agent_manifest
normalize_environment_manifest = runner.normalize_environment_manifest

DATE_TIME = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)
FORMAT_CHECKER = FormatChecker()


@FORMAT_CHECKER.checks("date-time", raises=(TypeError, ValueError))
def _is_rfc3339(value: object) -> bool:
    if not isinstance(value, str):
        return True
    if not DATE_TIME.fullmatch(value):
        raise ValueError("expected an RFC 3339 date-time")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("expected a timezone")
    return True


@FORMAT_CHECKER.checks("uri", raises=(TypeError, ValueError))
def _is_uri(value: object) -> bool:
    if not isinstance(value, str):
        return True
    if not value or any(char.isspace() for char in value):
        raise ValueError("expected a URI")
    if not urlsplit(value).scheme:
        raise ValueError("expected a URI scheme")
    return True


SCHEMA_NAMES = {
    "action-effect-rule",
    "actor",
    "artifact",
    "assertion",
    "branch-definition",
    "branch-resolution",
    "checkpoint",
    "event",
    "forecast-accuracy",
    "milestone-definition",
    "milestone-resolution",
    "protocol-message",
    "role-grant",
    "run-manifest",
    "scenario-manifest",
    "scorecard",
    "trace-event",
}


def _schemas() -> dict[str, dict[str, object]]:
    schema_root = importlib.resources.files("edlb").joinpath("schemas")
    return {
        path.name.removesuffix(".json"): json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(schema_root.iterdir(), key=lambda item: item.name)
        if path.name.endswith(".json")
    }


def _rows(path: Path) -> list[dict[str, object]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _bundles() -> list[Path]:
    return sorted(
        list((ROOT / "benchmarks/v1/output/public/train").glob("*/"))
        + list((ROOT / "benchmarks/v1/output/public/dev").glob("*/"))
        + list((ROOT / "benchmarks/v1/output/public/blind").glob("*/"))
    )


def _protocol_fixtures() -> dict[str, dict[str, object]]:
    base = {
        "protocol_version": "v1.0.0",
        "run_id": "run-schema",
        "sequence": 0,
        "message_id": "message-schema",
        "occurred_at": "2025-01-01T00:00:00Z",
    }
    return {
        "start": {
            **base,
            "kind": "start",
            "role": "system",
            "payload": {
                "world_id": "world-schema",
                "track": "open_team",
                "scenario_hash": "sha256:" + "0" * 64,
            },
        },
        "observation": {
            **base,
            "kind": "observation",
            "role": "account_executive",
            "observation_token": "a" * 32,
            "payload": {"checkpoint": {"sequence": 0}},
        },
        "tool_call": {
            **base,
            "kind": "tool_call",
            "role": "revops",
            "observation_token": "b" * 32,
            "tool_name": "crm.search",
            "arguments": {"query": "deal"},
            "idempotency_key": "read-schema",
            "model_metadata": {
                "model_id": "model-schema",
                "token_usage": {"input": 1, "output": 1},
            },
        },
        "tool_call_read_without_idempotency": {
            **base,
            "kind": "tool_call",
            "role": "revops",
            "observation_token": "b" * 32,
            "tool_name": "crm.search",
            "arguments": {"query": "deal"},
        },
        "tool_call_write_with_idempotency": {
            **base,
            "kind": "tool_call",
            "role": "account_executive",
            "observation_token": "b" * 32,
            "tool_name": "communications.send",
            "arguments": {"recipients": ["buyer@example"]},
            "idempotency_key": "write-schema",
        },
        "tool_result_ok": {
            **base,
            "kind": "tool_result",
            "role": "revops",
            "call_id": "call-schema",
            "ok": True,
            "result": {"records": []},
        },
        "tool_result_error": {
            **base,
            "kind": "tool_result",
            "role": "revops",
            "call_id": "call-schema",
            "ok": False,
            "error": {"code": "not_found", "message": "No record matched."},
        },
        "team_message": {
            **base,
            "kind": "team_message",
            "role": "sales_manager",
            "observation_token": "b" * 32,
            "recipient_role": "account_executive",
            "payload": {"body": "Review the approval evidence."},
        },
        "yield": {
            **base,
            "kind": "yield",
            "role": "domain_specialist",
            "observation_token": "b" * 32,
            "reason": "Waiting for buyer evidence.",
        },
        "checkpoint_complete": {
            **base,
            "kind": "checkpoint_complete",
            "role": "account_executive",
            "observation_token": "b" * 32,
            "checkpoint_id": "checkpoint-schema",
        },
        "run_end": {
            **base,
            "kind": "run_end",
            "role": "system",
            "observation_token": "b" * 32,
            "status": "completed",
        },
    }


class SchemaTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schemas = _schemas()
        cls.validators = {
            name: Draft202012Validator(schema, format_checker=FORMAT_CHECKER)
            for name, schema in cls.schemas.items()
        }

    def assert_valid(self, schema_name: str, value: object, source: object) -> None:
        errors = sorted(
            self.validators[schema_name].iter_errors(value),
            key=lambda error: list(error.absolute_path),
        )
        self.assertFalse(
            errors,
            f"{source}: " + "; ".join(error.message for error in errors[:3]),
        )

    def test_schema_documents_are_draft_2020_12(self) -> None:
        self.assertEqual(set(self.schemas), SCHEMA_NAMES)
        for name, schema in self.schemas.items():
            self.assertEqual(
                schema["$schema"], "https://json-schema.org/draft/2020-12/schema"
            )
            Draft202012Validator.check_schema(schema)
            self.assertIn("$id", schema, name)

    def test_installed_package_exposes_all_schema_resources(self) -> None:
        self.assertEqual(set(_schemas()), SCHEMA_NAMES)

    def test_rfc3339_format_is_checked(self) -> None:
        schema = {"type": "string", "format": "date-time"}
        validator = Draft202012Validator(schema, format_checker=FORMAT_CHECKER)
        self.assertFalse(list(validator.iter_errors("2025-01-01T00:00:00Z")))
        for value in ("2025-01-01T00:00:00", "2025-13-01T00:00:00Z", "not-a-date"):
            self.assertTrue(list(validator.iter_errors(value)), value)

    def test_forecast_accuracy_declares_public_outcome_leakage(self) -> None:
        value = {
            "official": True,
            "outcome_visibility": "public",
            "leakage_resistant": False,
            "overall_brier": 0.25,
            "by_cutoff": [
                {
                    "cutoff_sequence": 1,
                    "observations": 3,
                    "brier": 0.25,
                    "mean_probability": 0.5,
                    "event_rate": 1.0,
                }
            ],
        }
        self.assert_valid("forecast-accuracy", value, "forecast-accuracy")
        self.assertTrue(
            list(
                self.validators["forecast-accuracy"].iter_errors(
                    {**value, "leakage_resistant": True}
                )
            )
        )

    def test_generated_records(self) -> None:
        files = (
            ("actor", "actors.jsonl"),
            ("artifact", "artifacts.jsonl"),
            ("event", "events.jsonl"),
            ("checkpoint", "checkpoints.jsonl"),
            ("assertion", "assertions.jsonl"),
        )
        public_bundles = sorted(
            list((ROOT / "benchmarks/v1/output/public/train").glob("*/"))
            + list((ROOT / "benchmarks/v1/output/public/dev").glob("*/"))
            + list((ROOT / "benchmarks/v1/output/public/blind").glob("*/"))
        )
        self.assertEqual(len(public_bundles), 72)
        bundles = public_bundles
        for bundle in bundles:
            self.assert_valid(
                "scenario-manifest",
                json.loads((bundle / "manifest.json").read_text(encoding="utf-8")),
                bundle / "manifest.json",
            )
            for schema_name, filename in files:
                path = bundle / filename
                for line_number, row in enumerate(_rows(path), 1):
                    self.assert_valid(schema_name, row, f"{path}:{line_number}")
            rubric = json.loads((bundle / "rubric.json").read_text(encoding="utf-8"))
            for index, assertion in enumerate(rubric["assertions"]):
                self.assert_valid(
                    "assertion",
                    assertion,
                    f"{bundle / 'rubric.json'}:{index + 1}",
                )
            oracle = bundle / "oracle.json"
            if oracle.is_file():
                value = json.loads(oracle.read_text(encoding="utf-8"))
                self.assert_valid(
                    "scenario-manifest", value["scenario_manifest"], oracle
                )
                for index, milestone in enumerate(
                    value["verification_facts"]["milestones"]
                ):
                    self.assert_valid(
                        "milestone-definition",
                        milestone,
                        f"{oracle}:milestone:{index + 1}",
                    )
                for schema_name, key, parser in (
                    (
                        "action-effect-rule",
                        "action_effect_rules",
                        causal.action_effect_rule,
                    ),
                    ("branch-definition", "branches", causal.branch_definition),
                ):
                    for index, record in enumerate(value["verification_facts"][key]):
                        self.assert_valid(
                            schema_name,
                            record,
                            f"{oracle}:{schema_name}:{index + 1}",
                        )
                        parser(record)
            self.assertTrue((bundle / "hidden_events.jsonl").is_file())

    def test_causal_schema_and_parser_reject_unknown_contract_values(self) -> None:
        oracle = json.loads((_bundles()[0] / "oracle.json").read_text())
        facts = oracle["verification_facts"]
        branch = facts["branches"][0]
        resolution = {
            "branch_id": branch["branch_id"],
            "option": "fallback",
            "effect_ids": [],
            "action_keys": [],
            "selected_decision_artifact_ids": branch["fallback_decision_artifact_ids"],
            "resolved_at": facts["intervention_at"],
        }
        self.assert_valid("branch-resolution", resolution, "branch-resolution")
        causal.branch_resolution(resolution)
        cases = (
            (
                "action-effect-rule",
                facts["action_effect_rules"][0],
                "purpose_code",
                "unknown_recovery_purpose",
                causal.action_effect_rule,
            ),
            (
                "branch-definition",
                facts["branches"][0],
                "recoverable",
                "yes",
                causal.branch_definition,
            ),
            (
                "branch-resolution",
                resolution,
                "option",
                "unknown",
                causal.branch_resolution,
            ),
        )
        for schema_name, source, field, value, parser in cases:
            with self.subTest(schema=schema_name, field=field):
                mutated = json.loads(json.dumps(source))
                mutated[field] = value
                self.assertTrue(list(self.validators[schema_name].iter_errors(mutated)))
                with self.assertRaises(causal.CausalError):
                    parser(mutated)

    def test_generated_protocol_messages(self) -> None:
        paths = sorted(
            (ROOT / "benchmarks/v1/output/public").glob("*/*/reference_trace.jsonl")
        )
        self.assertEqual(len(paths), 72)
        for path in paths:
            for line_number, row in enumerate(_rows(path), 1):
                self.assert_valid("protocol-message", row, f"{path}:{line_number}")
        for name, fixture in _protocol_fixtures().items():
            self.assert_valid("protocol-message", fixture, name)
            Message.from_dict(fixture, allow_system=True)
        write_without_idempotency = dict(
            _protocol_fixtures()["tool_call_write_with_idempotency"]
        )
        write_without_idempotency.pop("idempotency_key")
        self.assertTrue(
            list(
                self.validators["protocol-message"].iter_errors(
                    write_without_idempotency
                )
            )
        )

    def test_model_serializations(self) -> None:
        body = "schema fixture"
        checksum = "sha256:" + hashlib.sha256(body.encode()).hexdigest()
        fixtures = {
            "actor": Actor(
                actor_id="actor-schema",
                kind="buyer",
                display_name="Schema Buyer",
                organization_id="org-schema",
                role_tags=("champion",),
                active_from="2025-01-01T00:00:00Z",
                visibility="public",
                synthetic=True,
                authority={
                    "role_id": "champion",
                    "rights": ["confirm_objective"],
                    "gate_ids": ["discovery"],
                },
                email=None,
            ).to_dict(),
            "artifact": Artifact(
                artifact_id="artifact-schema",
                world_id="world-schema",
                kind="email",
                title="Schema artifact",
                created_at="2025-01-01T00:00:00Z",
                available_at="2025-01-01T00:00:00Z",
                visibility="agent_visible",
                content={"mime_type": "text/plain", "body": body, "language": "en"},
                checksum=checksum,
                synthetic=True,
                provenance={
                    "synthetic_only": True,
                    "source_type": "generated_template",
                    "generator": "schema-test",
                    "generator_version": "v1.0.0",
                    "source_ids": ["source-schema-test"],
                    "fact_ids": ["schema-test-fact"],
                    "license": "CC-BY-4.0",
                },
                gate_id="discovery",
                artifact_key="artifact-schema",
                structured_payload={
                    "gate_id": "discovery",
                    "objective": "Confirm objective",
                },
                authoritative_for=("discovery",),
                recipient_role_ids=("account_executive",),
                projection_origin=None,
                logical_document_id="document-schema",
                version=1,
                supersedes_artifact_id=None,
                derived_from_artifact_ids=(),
                source_actor_ids=("actor-schema",),
                recipient_actor_ids=("seller-account-executive",),
                thread_id="thread-schema",
                record_id="record-schema",
            ).to_dict(),
            "assertion": Assertion(
                "assertion-schema",
                "world-schema",
                "world",
                "crm_integrity",
                "deterministic",
                {"path": "state.status", "operator": "equals", "expected": "ok"},
                True,
                False,
                "controllable",
                1.0,
                ("artifact-schema",),
                {"source": "generated", "license": "CC-BY-4.0"},
            ).to_dict(),
            "checkpoint": Checkpoint(
                checkpoint_id="checkpoint-schema",
                world_id="world-schema",
                sequence=0,
                available_at="2025-01-01T00:00:00Z",
                forecast_cutoff_at="2025-01-01T00:00:00Z",
                window_start="2025-01-01T00:00:00Z",
                window_end="2025-01-02T00:00:00Z",
                status="pending",
                synthetic=True,
                objective_ids=("objective-schema",),
                visible_artifact_ids=(),
                released_event_ids=(),
                required_roles=("account_executive",),
                terminal=False,
                visible_gate="discovery",
                label="Discovery",
                business_objective="Confirm the buyer's objective.",
                decision_condition="Advance when the buyer accepts the evidence.",
                role_deliverables={
                    "account_executive": "Confirm the decision owner.",
                    "domain_specialist": "Validate the evidence.",
                    "sales_manager": "Review the forecast.",
                    "revops": "Reconcile the CRM record.",
                },
                completion_conditions=("The buyer decision is recorded.",),
                policy_entrypoints=("policy-schema",),
                gate_id="discovery",
                source_fact_ids=("schema-test-fact",),
                required_artifact_keys=("artifact-schema",),
                required_artifact_roles={"artifact-schema": "evidence"},
                authority_role_ids=("champion",),
                authority_rights=("confirm_objective",),
                required_payload_fields=("objective",),
                decision_route={"accepted": "advance"},
                recovery_decisions=("revalidate",),
                availability_delay_bounds={
                    "min_minutes": 0,
                    "max_minutes": 4320,
                },
            ).to_dict(),
            "event": Event(
                "event-schema",
                "world-schema",
                0,
                "meeting_booked",
                "2025-01-01T00:00:00Z",
                "2025-01-01T00:00:00Z",
                "2025-01-01T00:00:00Z",
                (),
                "agent_visible",
                {"subject": "intro"},
                channel=None,
            ).to_dict(),
            "role-grant": RoleGrant(
                "grant-schema",
                "principal-schema",
                "revops",
                ("crm.read",),
                ("current_world",),
                False,
                True,
                False,
                False,
            ).to_dict(),
            "run-manifest": RunManifest(
                "run-schema",
                "v1.0.0",
                "world-schema",
                "fixed_harness",
                "team-schema",
                "v1.0.0",
                "v1.0.0",
                "sha256:" + "a" * 64,
                "sha256:" + "e" * 64,
                None,
                1,
                {
                    "resolved": True,
                    "roles": {
                        "account_executive": "model-a",
                        "domain_specialist": "model-a",
                        "sales_manager": "model-a",
                        "revops": "model-a",
                    },
                    "models": {
                        "model-a": {
                            "model_id": "model-schema",
                            "model_digest": "sha256:" + "b" * 64,
                            "prompt_hash": "sha256:" + "c" * 64,
                            "provider_settings": {"temperature": 0.2},
                            "provider_defaults": True,
                            "provider_defaults_digest": "sha256:" + "d" * 64,
                        }
                    },
                },
                {
                    "model_id": "model-schema",
                    "model_digest": "sha256:" + "b" * 64,
                    "prompt_hash": "sha256:" + "c" * 64,
                    "seed": 1,
                    "timeout_seconds": None,
                },
                {
                    "tool_calls_per_checkpoint": None,
                    "turns_per_checkpoint": None,
                    "timeout_seconds": None,
                    "retries": 0,
                },
                {
                    "resolved": True,
                    "runtime_version": "v1.0.0",
                    "image_digest": "sha256:" + "d" * 64,
                    "git_revision": "a" * 40,
                    "executor_policy_digest": "sha256:" + "e" * 64,
                },
                "2025-01-01T00:00:00Z",
                "created",
            ).to_dict(),
            "trace-event": TraceEvent(
                "run-schema",
                0,
                "message-schema",
                "2025-01-01T00:00:00Z",
                "observation",
                "account_executive",
                "sha256:" + "e" * 64,
                {"checkpoint": 0},
            ).to_dict(),
            "scorecard": Scorecard(
                "run-schema",
                "v1.0.0",
                "world-schema",
                "fixed_harness",
                "valid",
                1.0,
                True,
                False,
                True,
                {
                    category: 1.0
                    for category in (
                        "evidence_and_understanding",
                        "crm_integrity",
                        "stakeholder_management",
                        "workflow_compliance",
                        "communication_quality",
                        "forecast_discipline",
                        "longitudinal_recovery",
                        "side_effect_discipline",
                    )
                },
                {
                    "terminal_outcome": "closed_won",
                    "forecast_cutoff_count": 1,
                    "forecast_observations": [
                        {
                            "record_id": "deal-schema",
                            "cutoff_sequence": 1,
                            "cutoff_at": "2025-01-01T00:00:00Z",
                            "forecast_probability": 0.5,
                            "outcome": True,
                        }
                    ],
                },
                {},
                {
                    "tool_calls": 0,
                    "turns": 0,
                    "retries": 0,
                    "latency_ms": 0,
                    "cost_minor_units": None,
                    "tokens": None,
                    "metric_availability": {
                        "cost_minor_units": False,
                        "tokens": False,
                    },
                },
                "v1.0.0",
                "2025-01-01T00:00:00Z",
            ).to_dict(),
        }
        fixtures["scorecard"].update(
            {
                "trial_seed": 1,
                "configuration_hash": "sha256:" + "a" * 64,
                "manifest_hash": "sha256:" + "b" * 64,
                "state_hash": "sha256:" + "c" * 64,
                "score_hash": "sha256:" + "d" * 64,
                "rubric_hash": "sha256:" + "e" * 64,
                "oracle_hash": "sha256:" + "f" * 64,
            }
        )
        for schema_name, value in fixtures.items():
            self.assert_valid(schema_name, value, schema_name)
        self.assertEqual(
            Checkpoint.from_dict(fixtures["checkpoint"]).to_dict(),
            fixtures["checkpoint"],
        )
        invalid_scorecard = json.loads(json.dumps(fixtures["scorecard"]))
        invalid_scorecard["status"] = "invalid"
        invalid_scorecard["secondary_metrics"] = {
            "forecast_cutoff_count": 0,
            "forecast_observations": [],
        }
        self.assert_valid("scorecard", invalid_scorecard, "invalid-scorecard")
        non_numeric_forecast = json.loads(json.dumps(fixtures["scorecard"]))
        non_numeric_forecast["secondary_metrics"]["forecast_observations"][0][
            "forecast_probability"
        ] = "not-a-number"
        self.assertTrue(
            list(self.validators["scorecard"].iter_errors(non_numeric_forecast))
        )

        manifest = fixtures["run-manifest"]["agent_manifest"]
        unpinned = json.loads(json.dumps(manifest))
        unpinned["models"]["model-a"]["model_digest"] = None
        self.assertTrue(
            list(
                self.validators["run-manifest"].iter_errors(
                    {
                        **fixtures["run-manifest"],
                        "agent_manifest": unpinned,
                    }
                )
            )
        )
        with self.assertRaises(BundleError):
            normalize_agent_manifest(unpinned, require_resolved=True)
        undeclared_defaults = json.loads(json.dumps(manifest))
        undeclared_defaults["models"]["model-a"].pop("provider_defaults")
        self.assertTrue(
            list(
                self.validators["run-manifest"].iter_errors(
                    {
                        **fixtures["run-manifest"],
                        "agent_manifest": undeclared_defaults,
                    }
                )
            )
        )
        with self.assertRaises(BundleError):
            normalize_agent_manifest(undeclared_defaults, require_resolved=True)
        unpinned_defaults = json.loads(json.dumps(manifest))
        unpinned_defaults["models"]["model-a"]["provider_defaults_digest"] = None
        self.assertTrue(
            list(
                self.validators["run-manifest"].iter_errors(
                    {
                        **fixtures["run-manifest"],
                        "agent_manifest": unpinned_defaults,
                    }
                )
            )
        )
        with self.assertRaises(BundleError):
            normalize_agent_manifest(unpinned_defaults, require_resolved=True)
        unresolved_defaults = json.loads(json.dumps(unpinned_defaults))
        unresolved_defaults["resolved"] = False
        self.assertTrue(
            list(
                self.validators["run-manifest"].iter_errors(
                    {
                        **fixtures["run-manifest"],
                        "agent_manifest": unresolved_defaults,
                    }
                )
            )
        )
        with self.assertRaises(BundleError):
            normalize_agent_manifest(unresolved_defaults)

        explicit_settings = json.loads(json.dumps(manifest))
        explicit_settings["models"]["model-a"]["provider_defaults"] = False
        explicit_settings["models"]["model-a"]["provider_defaults_digest"] = None
        self.assertFalse(
            list(
                self.validators["run-manifest"].iter_errors(
                    {
                        **fixtures["run-manifest"],
                        "agent_manifest": explicit_settings,
                    }
                )
            )
        )
        normalize_agent_manifest(explicit_settings, require_resolved=True)

        unresolved_environment = {
            "resolved": False,
            "runtime_version": "cpython-test",
            "image_digest": None,
            "git_revision": None,
            "executor_policy_digest": None,
        }
        normalize_environment_manifest(unresolved_environment)
        unresolved_run = {
            **fixtures["run-manifest"],
            "environment": unresolved_environment,
        }
        self.assert_valid("run-manifest", unresolved_run, "unresolved environment")
        resolved_environment = {
            **unresolved_environment,
            "resolved": True,
        }
        self.assertTrue(
            list(
                self.validators["run-manifest"].iter_errors(
                    {
                        **fixtures["run-manifest"],
                        "environment": resolved_environment,
                    }
                )
            )
        )
        with self.assertRaises(BundleError):
            normalize_environment_manifest(resolved_environment, require_resolved=True)


if __name__ == "__main__":
    unittest.main()
