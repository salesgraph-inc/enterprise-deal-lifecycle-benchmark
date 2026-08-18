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

from edlb.models import (
    Actor,
    Artifact,
    Assertion,
    Checkpoint,
    Event,
    RoleGrant,
    RunManifest,
    Scorecard,
    TraceEvent,
)
from edlb.protocol import Message

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
    "actor",
    "artifact",
    "assertion",
    "checkpoint",
    "event",
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
        + list((ROOT / "benchmarks/v1/private/blind").glob("*/"))
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
            "payload": {"world_id": "world-schema", "track": "open_team"},
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
            "summary": "Reviewed the available evidence.",
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
        )
        private_bundles = sorted((ROOT / "benchmarks/v1/private/blind").glob("*/"))
        self.assertEqual(len(public_bundles), 48)
        if private_bundles:
            self.assertEqual(len(private_bundles), 24)
        bundles = public_bundles + private_bundles
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

    def test_generated_protocol_messages(self) -> None:
        paths = sorted(
            (ROOT / "benchmarks/v1/output/public").glob(
                "*/world-*/reference_trace.jsonl"
            )
        )
        self.assertGreater(len(paths), 0)
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
                "actor-schema",
                "buyer",
                "Schema Buyer",
                "org-schema",
                ("champion",),
                "2025-01-01T00:00:00Z",
                "public",
                email=None,
            ).to_dict(),
            "artifact": Artifact(
                "artifact-schema",
                "world-schema",
                "email",
                "Schema artifact",
                "2025-01-01T00:00:00Z",
                "2025-01-01T00:00:00Z",
                "agent_visible",
                {"mime_type": "text/plain", "body": body, "language": "en"},
                checksum,
                {
                    "synthetic_only": True,
                    "source_type": "generated_template",
                    "generator": "schema-test",
                    "generator_version": "v1.0.0",
                    "license": "CC-BY-4.0",
                },
                thread_id="thread-schema",
                record_id="record-schema",
                version=1,
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
                "checkpoint-schema",
                "world-schema",
                0,
                "2025-01-01T00:00:00Z",
                "2025-01-01T00:00:00Z",
                "2025-01-02T00:00:00Z",
                "pending",
                ("objective-schema",),
                (),
                ("account_executive",),
                1,
                1,
                False,
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
                    "roles": {
                        "account_executive": "agent-a",
                        "domain_specialist": "agent-b",
                        "sales_manager": "agent-c",
                        "revops": "agent-d",
                    }
                },
                {
                    "model_id": "model-schema",
                    "model_digest": "sha256:" + "b" * 64,
                    "prompt_hash": "sha256:" + "c" * 64,
                    "seed": 1,
                },
                {
                    "tool_calls_per_checkpoint": 1,
                    "turns_per_checkpoint": 1,
                    "retries": 0,
                },
                {
                    "runtime_version": "v1.0.0",
                    "image_digest": "sha256:" + "d" * 64,
                    "git_revision": "a" * 7,
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
                {
                    category: 1.0
                    for category in (
                        "evidence_and_understanding",
                        "crm_integrity",
                        "stakeholder_management",
                        "workflow_compliance",
                        "communication_quality",
                        "forecast_calibration",
                        "longitudinal_recovery",
                        "side_effect_discipline",
                    )
                },
                {"terminal_outcome": "closed_won"},
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


if __name__ == "__main__":
    unittest.main()
