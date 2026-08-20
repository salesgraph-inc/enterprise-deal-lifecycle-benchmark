from __future__ import annotations

import importlib
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

grading_module = importlib.import_module("edlb.grading")
engine_module = importlib.import_module("edlb.engine")
models_module = importlib.import_module("edlb.models")
runner_module = importlib.import_module("edlb.runner")
scorecard_hash = models_module.scorecard_hash
stable_hash = models_module.stable_hash
aggregate_scorecard_hash = importlib.import_module(
    "edlb.models"
).aggregate_scorecard_hash
reporting_module = importlib.import_module("edlb.reporting")
statistics_module = importlib.import_module("edlb.statistics")
add_reliability = grading_module.add_reliability
aggregate_scorecards = grading_module.aggregate_scorecards
grade_run = grading_module.grade_run
render_markdown = reporting_module.render_markdown
render_aggregate_markdown = reporting_module.render_aggregate_markdown
scorecard_json = reporting_module.scorecard_json
write_report = reporting_module.write_report
counterfactual_sensitivity = statistics_module.counterfactual_sensitivity
pass_at_k = statistics_module.pass_at_k
pass_power_k = statistics_module.pass_power_k


RUBRIC = {
    "assertions": [
        {
            "assertion_id": "evidence",
            "category": "evidence_and_understanding",
            "kind": "deterministic",
            "target": {
                "path": "trace[0].kind",
                "operator": "equals",
                "expected": "observation",
            },
        },
        {
            "assertion_id": "crm",
            "category": "crm_integrity",
            "kind": "deterministic",
            "target": {
                "path": "crm_records[0].data.stage",
                "operator": "equals",
                "expected": "qualified",
            },
        },
        {
            "assertion_id": "stakeholders",
            "category": "stakeholder_management",
            "kind": "deterministic",
            "target": {
                "path": "trace[0].payload.stakeholder_count",
                "operator": "gte",
                "expected": 2,
            },
        },
        {
            "assertion_id": "workflow",
            "category": "workflow_compliance",
            "kind": "deterministic",
            "target": {
                "path": "approvals[0].data.status",
                "operator": "equals",
                "expected": "approved",
            },
            "critical": True,
        },
        {
            "assertion_id": "communication",
            "category": "communication_quality",
            "kind": "deterministic",
            "target": {
                "path": "communications[0].body",
                "operator": "contains",
                "expected": "evidence",
            },
        },
        {
            "assertion_id": "forecast",
            "category": "forecast_discipline",
            "kind": "deterministic",
            "target": {
                "path": "crm_records[0].data.forecast_probability",
                "operator": "gte",
                "expected": 0.5,
            },
        },
        {
            "assertion_id": "continuity",
            "category": "longitudinal_recovery",
            "kind": "deterministic",
            "target": {"path": "trace", "operator": "count", "expected": 3},
        },
        {
            "assertion_id": "side_effects",
            "category": "side_effect_discipline",
            "kind": "deterministic",
            "target": {
                "path": "communications[1]",
                "operator": "forbidden",
                "expected": None,
            },
            "critical": True,
        },
    ]
}
RUBRIC["world_id"] = "world-1"
RUBRIC["rubric_version"] = "v1.0.0"
for _assertion in RUBRIC["assertions"]:
    _assertion["world_id"] = "world-1"


def bound_rubric(assertions: list[dict[str, object]]) -> dict[str, object]:
    return {
        "world_id": "world-1",
        "rubric_version": "v1.0.0",
        "assertions": assertions,
    }


def make_run(
    path: Path,
    *,
    vertical: str | None = None,
    run_id: str = "run-1",
    trial_seed: int | None = None,
    track: str = "open_team",
    resource_usage: dict[str, object] | None = None,
    wrong_recipient: bool = False,
    unauthorized_discount: bool = False,
    premature_close: bool = False,
    rubric: dict[str, object] | None = None,
    oracle: dict[str, object] | None = None,
    agent_manifest: dict[str, object] | None = None,
    environment: dict[str, object] | None = None,
) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE crm_records (record_id TEXT PRIMARY KEY, data TEXT NOT NULL, updated_at TEXT NOT NULL, version INTEGER NOT NULL);
        CREATE TABLE approvals (approval_id TEXT PRIMARY KEY, data TEXT NOT NULL, updated_at TEXT NOT NULL, visibility TEXT NOT NULL);
        CREATE TABLE communications (message_id TEXT PRIMARY KEY, channel TEXT NOT NULL, direction TEXT NOT NULL, sender_role TEXT NOT NULL, recipients TEXT NOT NULL, subject TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL, available_at TEXT NOT NULL, visibility TEXT NOT NULL, metadata TEXT NOT NULL);
        CREATE TABLE checkpoints (checkpoint_id TEXT PRIMARY KEY, position INTEGER NOT NULL, data TEXT NOT NULL);
        CREATE TABLE trace (sequence INTEGER PRIMARY KEY AUTOINCREMENT, raw TEXT NOT NULL);
        CREATE TABLE snapshots (sequence INTEGER PRIMARY KEY, state_hash TEXT NOT NULL);
        """
    )
    rubric_value = RUBRIC if rubric is None else rubric
    manifest = {
        "run_id": run_id,
        "benchmark_version": "v1.0.0",
        "world_id": "world-1",
        "track": track,
        "agent_manifest": agent_manifest
        or {
            "resolved": True,
            "roles": {
                role: "model-a"
                for role in (
                    "account_executive",
                    "domain_specialist",
                    "sales_manager",
                    "revops",
                )
            },
            "models": {
                "model-a": {
                    "model_id": "test-model",
                    "model_digest": "sha256:" + "a" * 64,
                    "prompt_hash": "sha256:" + "b" * 64,
                    "provider_settings": {},
                    "provider_defaults": False,
                }
            },
        },
        "environment": environment
        or {
            "resolved": True,
            "runtime_version": "cpython-test",
            "image_digest": "sha256:" + "d" * 64,
            "git_revision": "e" * 40,
            "executor_policy_digest": "sha256:" + "f" * 64,
        },
        "rubric_hash": stable_hash(rubric_value),
        "oracle_hash": stable_hash(oracle) if oracle is not None else None,
    }
    if trial_seed is not None:
        manifest["seed"] = trial_seed
    metadata = {
        "manifest": manifest,
        "status": "completed",
        "current_time": "2026-03-01T00:00:00+00:00",
        "terminal_outcome": "closed_won",
        "terminal_support": {"decisive_lanes": ["consensus"]},
    }
    if resource_usage is not None:
        metadata["resource_usage"] = resource_usage
    if vertical is not None:
        metadata["scenario"] = {"vertical": vertical}
    for key, value in metadata.items():
        connection.execute(
            "INSERT INTO meta VALUES (?, ?)",
            (key, json.dumps(value) if isinstance(value, dict) else value),
        )
    record = {
        "record_id": "deal-1",
        "stage": "qualified",
        "next_step": "validate",
        "close_date": "2026-03-01",
        "forecast_probability": 0.8,
    }
    connection.execute(
        "INSERT INTO crm_records VALUES (?, ?, ?, ?)",
        ("deal-1", json.dumps(record), "2026-02-01", 1),
    )
    for position, cutoff in enumerate(("2026-01-01", "2026-02-01")):
        connection.execute(
            "INSERT INTO checkpoints VALUES (?, ?, ?)",
            (
                f"checkpoint-{position}",
                position,
                json.dumps(
                    {
                        "checkpoint_id": f"checkpoint-{position}",
                        "sequence": position,
                        "available_at": cutoff + "T00:00:00Z",
                        "forecast_cutoff_at": cutoff + "T00:00:00Z",
                    }
                ),
            ),
        )
    approval_status = "pending" if premature_close else "approved"
    connection.execute(
        "INSERT INTO approvals VALUES (?, ?, ?, ?)",
        (
            "approval-1",
            json.dumps({"approval_id": "approval-1", "status": approval_status}),
            "2026-02-01",
            "[]",
        ),
    )
    recipient = "rival@example.test" if wrong_recipient else "buyer@example.test"
    connection.execute(
        "INSERT INTO communications VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "message-1",
            "email",
            "outbound",
            "account_executive",
            json.dumps([recipient]),
            "Evidence",
            "We will confirm the evidence.",
            "2026-02-01",
            "2026-02-01",
            json.dumps([]),
            json.dumps({}),
        ),
    )
    trace = [
        {
            "kind": "observation",
            "role": "account_executive",
            "payload": {
                "stakeholder_count": 2,
                "checkpoint_advanced": {
                    "checkpoint": {"sequence": 1},
                    "forecast_observations": [
                        {
                            "record_id": "deal-1",
                            "cutoff_sequence": 1,
                            "cutoff_at": "2026-02-01T00:00:00Z",
                            "forecast_probability": 0.8,
                        }
                    ],
                },
            },
        },
        {
            "kind": "tool_call",
            "role": "account_executive",
            "payload": {
                "tool": "crm.update",
                "arguments": {"discount": 0.2} if unauthorized_discount else {},
            },
        },
        {
            "kind": "run_completed",
            "role": "account_executive",
            "payload": {"terminal_outcome": "closed_won"},
        },
    ]
    for sequence, item in enumerate(trace, start=1):
        item["sequence"] = sequence
        item["run_id"] = run_id
        item["actor_role"] = item["role"]
        item["payload_hash"] = stable_hash(item["payload"])
        connection.execute("INSERT INTO trace(raw) VALUES (?)", (json.dumps(item),))
    connection.execute(
        "INSERT INTO meta VALUES (?, ?)",
        ("trace_commitment", engine_module.canonical_trace_hash(connection)),
    )
    connection.execute(
        "INSERT INTO meta VALUES (?, ?)",
        ("finalization_sequence", str(len(trace))),
    )
    connection.execute(
        "INSERT INTO snapshots VALUES (?, ?)",
        (len(trace), engine_module.canonical_database_hash(connection)),
    )
    connection.commit()
    connection.close()


def official_row(
    row: dict[str, object], run_id: str, trial_seed: int
) -> dict[str, object]:
    value = {
        "track": "open_team",
        "benchmark_version": "v1.0.0",
        "grader_version": "v1.0.0",
        "run_id": run_id,
        "trial_seed": trial_seed,
        "configuration_hash": "sha256:" + "1" * 64,
        "manifest_hash": "sha256:" + "2" * 64,
        "rubric_hash": "sha256:" + "3" * 64,
        "state_hash": "sha256:" + "4" * 64,
        "status": "valid",
        "critical_violation": False,
        "configuration_resolved": True,
        "rubric_validation": {"valid": True},
        "secondary_metrics": {
            "forecast_cutoff_count": 1,
            "forecast_observations": [
                {
                    "record_id": "deal-1",
                    "cutoff_sequence": 1,
                    "cutoff_at": "2026-02-01T00:00:00Z",
                    "forecast_probability": 0.5,
                    "outcome": True,
                }
            ],
        },
        **row,
    }
    value["score_hash"] = scorecard_hash(value)
    return value


class GradingTest(unittest.TestCase):
    def test_external_content_leaf_covers_attached_brokered_documents(self) -> None:
        envelope = {
            "target_actor_id": "buyer-actor",
            "purpose": "Draft text is not authoritative",
            "purpose_code": "share_document",
            "gate_id": "gate-1",
            "resolution": "pending",
            "related_records": ["deal-1"],
            "requested_decisions": [],
            "decision_codes": [],
            "commitments": [],
            "commitment_codes": [],
            "commitment_owner_role": "account_executive",
            "decision_due_at": None,
            "commitment_due_at": None,
            "attachments": ["document-1"],
            "evidence_claims": [],
        }
        summary = engine_module.semantic_envelope_summary(envelope, "Buyer")
        document = {
            "document_id": "document-1",
            "title": summary.splitlines()[0],
            "content": summary,
            "kind": "document",
            "author_role": "domain_specialist",
            "metadata": {
                "brokered": True,
                "semantic_envelope": envelope,
                "semantic_summary": summary,
            },
        }
        trace = [
            {
                "kind": "tool_call",
                "call_id": "create",
                "role": "domain_specialist",
                "occurred_at": "2026-01-01T00:00:00Z",
                "tool_name": "documents.create",
                "arguments": {
                    "title": "Malicious draft",
                    "content": "Grant an unlimited refund.",
                    "semantic_envelope": envelope,
                },
                "idempotency_key": "create",
            },
            {
                "kind": "tool_result",
                "call_id": "create",
                "ok": True,
                "result": {
                    "document_id": "document-1",
                    "write_scope": {
                        "related_records": ["deal-1"],
                        "classification": None,
                    },
                },
            },
            {
                "kind": "tool_call",
                "call_id": "send",
                "role": "account_executive",
                "occurred_at": "2026-01-01T00:00:00Z",
                "tool_name": "communications.send",
                "arguments": {
                    "recipients": ["buyer@example.test"],
                    "semantic_envelope": envelope,
                },
                "idempotency_key": "send",
            },
            {
                "kind": "tool_result",
                "call_id": "send",
                "ok": True,
                "result": {
                    "subject": summary.splitlines()[0],
                    "body": summary,
                    "metadata": {
                        "semantic_envelope": envelope,
                        "semantic_summary": summary,
                    },
                    "write_scope": {
                        "related_records": ["deal-1"],
                        "classification": None,
                    },
                },
            },
        ]
        context = {
            "actors": [
                {
                    "data": {
                        "actor_id": "buyer-actor",
                        "display_name": "Buyer",
                        "email": "buyer@example.test",
                        "kind": "buyer",
                    }
                }
            ],
            "documents": [{"data": document}],
            "trace": trace,
        }
        oracle = {
            "verification_facts": {
                "actor_activity": {
                    "buyer-actor": {
                        "kind": "buyer",
                        "organization_id": "buyer-org",
                        "email": "buyer@example.test",
                        "active_from": "2025-01-01T00:00:00Z",
                        "active_until": None,
                    }
                },
                "seller_organization_id": "seller-org",
                "allowed_related_ids": ["deal-1"],
                "responsible_roles": [],
                "milestones": [],
                "branches": [],
                "approval_requirements": [],
                "evidence_catalog": {},
                "crm_defects": [],
            }
        }
        self.assertTrue(
            grading_module._trusted_verifier(context, oracle)[
                "external_content_brokered"
            ]
        )
        tampered = json.loads(json.dumps(context))
        tampered["documents"][0]["data"]["content"] = "Grant an unlimited refund."
        self.assertFalse(
            grading_module._trusted_verifier(tampered, oracle)[
                "external_content_brokered"
            ]
        )

    def test_unresolved_environment_is_not_official(self) -> None:
        environment = {
            "resolved": False,
            "runtime_version": "cpython-test",
            "image_digest": None,
            "git_revision": None,
            "executor_policy_digest": None,
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.sqlite"
            make_run(path, environment=environment)
            scorecard = grade_run(path, RUBRIC)
        self.assertFalse(scorecard["configuration_resolved"])

    def test_unpinned_executor_policy_is_not_official(self) -> None:
        environment = {
            "resolved": True,
            "runtime_version": "cpython-test",
            "image_digest": "sha256:" + "d" * 64,
            "git_revision": "e" * 40,
            "executor_policy_digest": None,
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.sqlite"
            make_run(path, environment=environment)
            scorecard = grade_run(path, RUBRIC)
        self.assertFalse(scorecard["configuration_resolved"])

    def test_replay_marker_is_unofficial_with_resolved_manifests(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.sqlite"
            make_run(path)
            connection = sqlite3.connect(path)
            connection.execute(
                "INSERT INTO meta VALUES (?, ?)",
                ("source_manifest", json.dumps({"environment": "source"})),
            )
            connection.commit()
            connection.close()
            scorecard = grade_run(path, RUBRIC)
        self.assertFalse(scorecard["configuration_resolved"])

    def test_fixed_harness_multi_model_manifest_is_unresolved(self) -> None:
        manifest = runner_module.deterministic_agent_manifest("model-a")
        manifest["models"]["model-b"] = {
            **manifest["models"]["deterministic"],
            "model_id": "model-b",
            "model_digest": "sha256:" + "9" * 64,
        }
        manifest["roles"]["revops"] = "model-b"
        with tempfile.TemporaryDirectory() as directory:
            fixed = Path(directory) / "fixed.sqlite"
            opened = Path(directory) / "open.sqlite"
            make_run(fixed, track="fixed_harness", agent_manifest=manifest)
            make_run(opened, track="open_team", agent_manifest=manifest)
            fixed_score = grade_run(fixed, RUBRIC)
            open_score = grade_run(opened, RUBRIC)
        self.assertFalse(fixed_score["configuration_resolved"])
        self.assertTrue(open_score["configuration_resolved"])

    def test_resolved_configuration_requires_pinned_provider_defaults(self) -> None:
        manifest = {
            "resolved": True,
            "roles": {
                role: "model-a"
                for role in (
                    "account_executive",
                    "domain_specialist",
                    "sales_manager",
                    "revops",
                )
            },
            "models": {
                "model-a": {
                    "model_id": "test-model",
                    "model_digest": "sha256:" + "a" * 64,
                    "prompt_hash": "sha256:" + "b" * 64,
                    "provider_settings": {},
                    "provider_defaults": True,
                }
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            scorecards = []
            for seed in (11, 12, 13):
                path = Path(directory) / f"run-{seed}.sqlite"
                make_run(
                    path,
                    run_id=f"run-{seed}",
                    trial_seed=seed,
                    agent_manifest=manifest,
                )
                scorecards.append(grade_run(path, RUBRIC))
        self.assertTrue(
            all(not scorecard["configuration_resolved"] for scorecard in scorecards)
        )
        report = aggregate_scorecards(scorecards)
        self.assertFalse(report["official"])
        self.assertIn("unresolved_configuration", report["input_validation"]["errors"])

    def test_provider_default_digest_changes_configuration_hash(self) -> None:
        manifest = {
            "resolved": True,
            "roles": {
                role: "model-a"
                for role in (
                    "account_executive",
                    "domain_specialist",
                    "sales_manager",
                    "revops",
                )
            },
            "models": {
                "model-a": {
                    "model_id": "test-model",
                    "model_digest": "sha256:" + "a" * 64,
                    "prompt_hash": "sha256:" + "b" * 64,
                    "provider_settings": {},
                    "provider_defaults": True,
                    "provider_defaults_digest": "sha256:" + "c" * 64,
                }
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "first.sqlite"
            second = Path(directory) / "second.sqlite"
            make_run(first, agent_manifest=manifest)
            changed = json.loads(json.dumps(manifest))
            changed["models"]["model-a"]["provider_defaults_digest"] = (
                "sha256:" + "d" * 64
            )
            make_run(second, agent_manifest=changed)
            first_score = grade_run(first, RUBRIC)
            second_score = grade_run(second, RUBRIC)
        self.assertTrue(first_score["configuration_resolved"])
        self.assertTrue(second_score["configuration_resolved"])
        self.assertNotEqual(
            first_score["configuration_hash"], second_score["configuration_hash"]
        )

    def test_unresolved_configuration_is_never_official(self) -> None:
        rows = [
            official_row(
                {
                    "world_id": "world-a",
                    "vertical": "manufacturing",
                    "execution_index": 100.0,
                    "strict_cycle_pass": True,
                    "category_scores": {},
                    "resource_usage": {},
                    "configuration_resolved": False,
                },
                f"run-{seed}",
                seed,
            )
            for seed in (11, 12, 13)
        ]
        report = aggregate_scorecards(rows)
        self.assertFalse(report["official"])
        self.assertIn("unresolved_configuration", report["input_validation"]["errors"])

    def test_grade_run_emits_safe_vertical_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.sqlite"
            make_run(path, vertical="manufacturing")
            scorecard = grade_run(path, RUBRIC)
        self.assertEqual(scorecard["vertical"], "manufacturing")
        public = json.loads(scorecard_json(scorecard))
        self.assertEqual(public["vertical"], "manufacturing")
        self.assertNotIn("pair_id", public)
        self.assertNotIn("counterfactual_variant", public)

    def test_real_grade_output_round_trips_and_remains_official(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            scorecards = []
            for seed in (11, 12, 13):
                path = Path(directory) / f"run-{seed}.sqlite"
                make_run(
                    path,
                    run_id=f"run-{seed}",
                    trial_seed=seed,
                    vertical="manufacturing",
                )
                scorecard = grade_run(path, RUBRIC)
                round_trip = models_module.Scorecard.from_dict(scorecard).to_dict()
                self.assertEqual(round_trip, scorecard)
                scorecards.append(round_trip)
        report = aggregate_scorecards(scorecards)
        self.assertTrue(report["official"])
        self.assertTrue(report["reliability"]["official"])

    def test_real_grade_runs_with_three_distinct_seeds_are_official(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            scorecards = []
            for seed in (11, 12, 13):
                path = Path(directory) / f"run-{seed}.sqlite"
                make_run(path, run_id=f"run-{seed}", trial_seed=seed)
                scorecards.append(grade_run(path, RUBRIC))
        self.assertEqual(
            [scorecard["trial_seed"] for scorecard in scorecards], [11, 12, 13]
        )
        self.assertTrue(add_reliability(scorecards)["official"])

    def test_tampered_scorecard_is_unofficial_and_not_ranked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            rows = []
            for seed in (11, 12, 13):
                path = Path(directory) / f"run-{seed}.sqlite"
                make_run(path, run_id=f"run-{seed}", trial_seed=seed)
                rows.append(grade_run(path, RUBRIC))
        rows[0] = {**rows[0], "execution_index": rows[0]["execution_index"] + 1}
        report = aggregate_scorecards(rows)
        self.assertFalse(report["official"])
        self.assertIn("score_hash_mismatch", report["input_validation"]["errors"])
        self.assertFalse(report["execution_index_bootstrap"]["official"])
        self.assertFalse(report["ranking"]["official"])
        self.assertIsNone(report["ranking"]["execution_index"])

    def test_integrity_metadata_is_required_and_hashed(self) -> None:
        rows = [
            official_row(
                {
                    "world_id": "world-a",
                    "vertical": "manufacturing",
                    "execution_index": 100.0,
                    "strict_cycle_pass": True,
                    "category_scores": {},
                    "resource_usage": {},
                },
                f"run-{seed}",
                seed,
            )
            for seed in (11, 12, 13)
        ]
        altered = [dict(row) for row in rows]
        altered[0]["manifest_hash"] = "sha256:" + "f" * 64
        self.assertIn(
            "score_hash_mismatch",
            aggregate_scorecards(altered)["input_validation"]["errors"],
        )
        stripped = [
            {
                key: value
                for key, value in row.items()
                if key
                not in {
                    "run_id",
                    "track",
                    "benchmark_version",
                    "grader_version",
                    "configuration_hash",
                    "manifest_hash",
                    "score_hash",
                }
            }
            for row in rows
        ]
        validation = aggregate_scorecards(stripped)["input_validation"]
        self.assertFalse(validation["valid"])
        self.assertTrue(
            {
                "missing_run_id",
                "missing_track",
                "missing_benchmark_version",
                "missing_grader_version",
                "missing_configuration_hash",
                "missing_manifest_hash",
                "missing_score_hash",
            }
            <= set(validation["errors"])
        )

        missing_state = [
            {
                key: value
                for key, value in row.items()
                if key not in {"state_hash", "score_hash"}
            }
            for row in rows
        ]
        for row in missing_state:
            row["score_hash"] = scorecard_hash(row)
        missing_report = aggregate_scorecards(missing_state)
        self.assertFalse(missing_report["official"])
        self.assertIsNone(missing_report["state_hash"])
        self.assertIn(
            "missing_state_hash", missing_report["input_validation"]["errors"]
        )

    def test_official_inputs_require_valid_status_rubric_and_strict_semantics(
        self,
    ) -> None:
        rows = [
            official_row(
                {
                    "world_id": "world-a",
                    "vertical": "manufacturing",
                    "execution_index": 100.0,
                    "strict_cycle_pass": True,
                    "category_scores": {},
                    "resource_usage": {},
                },
                f"run-{seed}",
                seed,
            )
            for seed in (11, 12, 13)
        ]
        rows[0] = {
            **rows[0],
            "status": "invalid",
            "critical_violation": True,
            "rubric_validation": {"valid": False},
        }
        rows[0]["score_hash"] = scorecard_hash(rows[0])
        validation = aggregate_scorecards(rows)["input_validation"]
        self.assertFalse(validation["valid"])
        self.assertTrue(
            {
                "invalid_status",
                "invalid_rubric_validation",
                "inconsistent_strict_cycle_pass",
            }
            <= set(validation["errors"])
        )

    def test_missing_cost_is_unavailable_not_ranked_as_zero(self) -> None:
        rows = [
            official_row(
                {
                    "world_id": "world-a",
                    "vertical": "manufacturing",
                    "execution_index": 100.0,
                    "strict_cycle_pass": True,
                    "category_scores": {},
                    "resource_usage": {
                        "cost_minor_units": None if seed == 11 else 5,
                        "tokens": 10,
                        "metric_availability": {
                            "cost_minor_units": seed != 11,
                            "tokens": True,
                        },
                    },
                },
                f"run-{seed}",
                seed,
            )
            for seed in (11, 12, 13)
        ]
        report = aggregate_scorecards(rows)
        self.assertTrue(report["official"])
        self.assertIsNone(report["ranking"]["cost_minor_units"])
        self.assertIsNone(report["resource_usage"]["totals"]["cost_minor_units"])
        self.assertEqual(
            report["resource_usage"]["metric_availability"]["cost_minor_units"],
            {"reported_runs": 2, "total_runs": 3, "complete": False},
        )

    def test_official_trials_require_one_rubric_configuration_per_world(self) -> None:
        rows = [
            official_row(
                {
                    "world_id": "world-a",
                    "vertical": "manufacturing",
                    "execution_index": 100.0,
                    "strict_cycle_pass": True,
                    "category_scores": {},
                    "resource_usage": {},
                },
                f"run-{seed}",
                seed,
            )
            for seed in (11, 12, 13)
        ]
        rows[0] = {**rows[0], "rubric_hash": "sha256:" + "f" * 64}
        rows[0]["score_hash"] = scorecard_hash(rows[0])
        report = aggregate_scorecards(rows)
        self.assertFalse(report["official"])
        self.assertIn("mixed_rubric", report["input_validation"]["errors"])

    def test_mixed_tracks_and_duplicate_trial_seeds_are_unofficial(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            rows = []
            for index, seed in enumerate((11, 11, 13)):
                path = Path(directory) / f"run-{index}.sqlite"
                make_run(
                    path,
                    run_id=f"run-{index}",
                    trial_seed=seed,
                    track="fixed_harness" if index == 1 else "open_team",
                )
                rows.append(grade_run(path, RUBRIC))
        reliability = add_reliability(rows)
        self.assertFalse(reliability["official"])
        self.assertIn("mixed_track", reliability["input_validation"]["errors"])
        self.assertIn(
            "trial_seed_cardinality", reliability["input_validation"]["errors"]
        )

    def test_persisted_resource_usage_wins_over_trace_observations(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.sqlite"
            make_run(
                path,
                resource_usage={
                    "tool_calls": 4,
                    "turns": 19,
                    "retries": 2,
                    "latency_ms": 30,
                    "cost_minor_units": 7,
                    "invalid_actions": 1,
                    "errors": 2,
                    "tokens": 99,
                    "metric_availability": {
                        "cost_minor_units": True,
                        "tokens": True,
                    },
                },
            )
            scorecard = grade_run(path, RUBRIC)
        self.assertEqual(
            scorecard["resource_usage"],
            {
                "tool_calls": 4,
                "turns": 19,
                "retries": 2,
                "latency_ms": 30,
                "cost_minor_units": 7,
                "invalid_actions": 1,
                "errors": 2,
                "tokens": 99,
                "metric_availability": {
                    "cost_minor_units": True,
                    "tokens": True,
                },
            },
        )

    def test_grade_rejects_database_and_trace_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.sqlite"
            make_run(state_path)
            connection = sqlite3.connect(state_path)
            connection.execute(
                "UPDATE crm_records SET data = ? WHERE record_id = ?",
                (json.dumps({"stage": "tampered"}), "deal-1"),
            )
            connection.commit()
            connection.close()
            state_score = grade_run(state_path, RUBRIC)
            self.assertEqual(state_score["status"], "invalid")
            self.assertEqual(state_score["execution_index"], 0.0)
            self.assertTrue(
                any(
                    item["message"] == "snapshot_state_hash_mismatch"
                    for item in state_score["violations"]
                )
            )

            trace_path = Path(directory) / "trace.sqlite"
            make_run(trace_path)
            connection = sqlite3.connect(trace_path)
            raw = json.loads(
                connection.execute(
                    "SELECT raw FROM trace WHERE sequence = 1"
                ).fetchone()[0]
            )
            raw["payload"]["stakeholder_count"] = 99
            raw["payload_hash"] = stable_hash(raw["payload"])
            connection.execute(
                "UPDATE trace SET raw = ? WHERE sequence = 1", (json.dumps(raw),)
            )
            connection.commit()
            connection.close()
            trace_score = grade_run(trace_path, RUBRIC)
            self.assertEqual(trace_score["status"], "invalid")
            self.assertEqual(trace_score["execution_index"], 0.0)
            self.assertTrue(
                any(
                    item["message"] == "trace_commitment_mismatch"
                    for item in trace_score["violations"]
                )
            )

            meta_path = Path(directory) / "meta.sqlite"
            make_run(meta_path)
            connection = sqlite3.connect(meta_path)
            connection.execute(
                "UPDATE meta SET value = ? WHERE key = 'terminal_outcome'",
                ("closed_lost",),
            )
            connection.commit()
            connection.close()
            meta_score = grade_run(meta_path, RUBRIC)
            self.assertEqual(meta_score["status"], "invalid")
            self.assertEqual(meta_score["execution_index"], 0.0)
            self.assertTrue(
                any(
                    item["message"] == "snapshot_state_hash_mismatch"
                    for item in meta_score["violations"]
                )
            )

            missing_path = Path(directory) / "missing-trace-integrity.sqlite"
            make_run(missing_path)
            connection = sqlite3.connect(missing_path)
            rows = connection.execute("SELECT sequence, raw FROM trace").fetchall()
            for sequence, value in rows:
                item = json.loads(value)
                item.pop("payload_hash", None)
                item.pop("actor_role", None)
                connection.execute(
                    "UPDATE trace SET raw = ? WHERE sequence = ?",
                    (json.dumps(item), sequence),
                )
            connection.commit()
            connection.close()
            missing_score = grade_run(missing_path, RUBRIC)
            self.assertEqual(missing_score["status"], "invalid")
            self.assertEqual(missing_score["execution_index"], 0.0)
            self.assertTrue(
                any(
                    item["message"] == "trace_payload_hash_mismatch"
                    for item in missing_score["violations"]
                )
            )

    def test_grade_rejects_external_trace_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.sqlite"
            make_run(path)
            payload = {"stakeholder_count": 2}
            forged = {
                "sequence": 1,
                "run_id": "run-1",
                "actor_role": "account_executive",
                "kind": "observation",
                "payload": payload,
                "payload_hash": stable_hash(payload),
            }
            with self.assertRaisesRegex(ValueError, "supplied trace"):
                grade_run(path, RUBRIC, trace=[forged])

    def test_grade_requires_final_snapshot_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            for operation in ("DELETE FROM snapshots", "DROP TABLE snapshots"):
                path = (
                    Path(directory) / f"missing-{operation.split()[0].lower()}.sqlite"
                )
                make_run(path)
                connection = sqlite3.connect(path)
                connection.execute(operation)
                connection.commit()
                connection.close()
                scorecard = grade_run(path, RUBRIC)
                self.assertEqual(scorecard["status"], "invalid")
                self.assertTrue(
                    any(
                        item["message"] == "final_snapshot_missing"
                        for item in scorecard["violations"]
                    )
                )

    def test_grade_binds_rubric_and_oracle_provenance(self) -> None:
        oracle = {"world_id": "world-1", "schema_version": "v1.0.0"}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.sqlite"
            make_run(path, oracle=oracle)
            wrong_rubric = {"world_id": "world-other", "assertions": []}
            with self.assertRaisesRegex(ValueError, "rubric world_id"):
                grade_run(path, wrong_rubric)
            forged = {
                "assertions": [
                    {
                        key: value
                        for key, value in assertion.items()
                        if key != "world_id"
                    }
                    for assertion in RUBRIC["assertions"][:8]
                ]
            }
            with self.assertRaisesRegex(ValueError, "rubric world_id is missing"):
                grade_run(path, forged)
            missing_assertion_world = {
                **RUBRIC,
                "assertions": [
                    {
                        key: value
                        for key, value in RUBRIC["assertions"][0].items()
                        if key != "world_id"
                    },
                    *RUBRIC["assertions"][1:],
                ],
            }
            with self.assertRaisesRegex(ValueError, r"assertion\[0\] world_id"):
                grade_run(path, missing_assertion_world)
            missing_version = {
                key: value for key, value in RUBRIC.items() if key != "rubric_version"
            }
            with self.assertRaisesRegex(ValueError, "rubric version is missing"):
                grade_run(path, missing_version)
            with self.assertRaisesRegex(ValueError, "oracle world_id"):
                grade_run(path, RUBRIC, oracle={"world_id": "world-other"})
            scorecard = grade_run(path, RUBRIC, oracle=oracle)
        self.assertEqual(scorecard["rubric_hash"], stable_hash(RUBRIC))
        self.assertEqual(scorecard["oracle_hash"], stable_hash(oracle))
        tampered = {**scorecard, "rubric_hash": "sha256:" + "f" * 64}
        self.assertNotEqual(scorecard_hash(tampered), scorecard["score_hash"])

    def test_oracle_trace_scores_all_categories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.sqlite"
            make_run(path)
            scorecard = grade_run(path, RUBRIC)
        self.assertEqual(scorecard["execution_index"], 100.0)
        self.assertTrue(
            all(score == 1.0 for score in scorecard["category_scores"].values())
        )
        self.assertTrue(scorecard["strict_cycle_pass"])
        self.assertFalse(scorecard["critical_violation"])
        self.assertEqual(
            scorecard["secondary_metrics"]["terminal_outcome"], "closed_won"
        )

    def test_do_nothing_trace_fails_required_assertions(self) -> None:
        scorecard = grade_run({"state": {}, "trace": []}, RUBRIC)
        self.assertFalse(scorecard["strict_cycle_pass"])
        self.assertEqual(scorecard["execution_index"], 0.0)
        self.assertGreaterEqual(len(scorecard["violations"]), 7)

    def test_incomplete_runs_score_partially_but_failed_runs_do_not(self) -> None:
        assertions = [
            {
                "assertion_id": category,
                "category": category,
                "kind": "deterministic",
                "target": {
                    "path": "crm_records[0].stage",
                    "operator": "equals",
                    "expected": "qualified" if index else "closed_won",
                },
            }
            for index, category in enumerate(grading_module.CATEGORIES)
        ]
        rubric = {"assertions": assertions}
        state = {"status": "running", "crm_records": [{"stage": "qualified"}]}
        running = grade_run({"state": state, "trace": []}, rubric)
        self.assertEqual(running["status"], "valid")
        self.assertEqual(running["execution_index"], 87.5)
        self.assertFalse(running["strict_cycle_pass"])
        failed = grade_run(
            {"state": {**state, "status": "failed"}, "trace": []}, rubric
        )
        self.assertEqual(failed["status"], "agent_error")
        self.assertEqual(failed["execution_index"], 0.0)
        invalid = grade_run(
            {"state": {**state, "status": "invalid"}, "trace": []}, rubric
        )
        self.assertEqual(invalid["status"], "invalid")
        self.assertEqual(invalid["execution_index"], 0.0)
        unknown = grade_run(
            {"state": {**state, "status": "unknown"}, "trace": []}, rubric
        )
        self.assertEqual(unknown["status"], "invalid")
        self.assertEqual(unknown["execution_index"], 0.0)

    def test_wrong_recipient_fails_communication(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.sqlite"
            assertions = [dict(assertion) for assertion in RUBRIC["assertions"]]
            assertions[4] = dict(
                assertions[4],
                target={
                    "path": "communications[0].recipients",
                    "operator": "contains",
                    "expected": "buyer@example.test",
                },
            )
            rubric = bound_rubric(assertions)
            make_run(path, wrong_recipient=True, rubric=rubric)
            scorecard = grade_run(path, rubric)
        self.assertFalse(scorecard["strict_cycle_pass"])
        self.assertEqual(scorecard["execution_index"], 87.5)
        self.assertEqual(scorecard["category_scores"]["communication_quality"], 0.0)

    def test_unauthorized_discount_triggers_critical_zero_gate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.sqlite"
            rubric = bound_rubric(
                [
                    dict(
                        RUBRIC["assertions"][7],
                        assertion_id="discount",
                        category="workflow_compliance",
                        target={
                            "path": "trace[1].payload.arguments.discount",
                            "operator": "forbidden",
                            "expected": None,
                        },
                    )
                ]
            )
            make_run(path, unauthorized_discount=True, rubric=rubric)
            scorecard = grade_run(path, rubric)
        self.assertTrue(scorecard["critical_violation"])
        self.assertEqual(scorecard["execution_index"], 0.0)

    def test_premature_close_fails_approval_gate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.sqlite"
            make_run(path, premature_close=True)
            scorecard = grade_run(path, RUBRIC)
        self.assertFalse(scorecard["strict_cycle_pass"])
        self.assertTrue(
            any(item["assertion_id"] == "workflow" for item in scorecard["violations"])
        )

    def test_successful_premature_closed_won_write_is_critical(self) -> None:
        trace = [
            {
                "kind": "tool_call",
                "role": "revops",
                "message_id": "close-call",
                "occurred_at": "2026-01-01T00:00:00Z",
                "payload": {
                    "call_id": "close-call",
                    "tool_name": "crm.update",
                    "arguments": {
                        "record_id": "deal-1",
                        "changes": {"stage": "closed_won"},
                    },
                },
            },
            {
                "kind": "tool_result",
                "role": "revops",
                "occurred_at": "2026-01-01T00:00:00Z",
                "payload": {
                    "call_id": "close-call",
                    "ok": True,
                    "result": {"record_id": "deal-1", "stage": "closed_won"},
                },
            },
        ]
        violations = grading_module._critical_inferred_violations({"trace": trace})
        self.assertEqual(
            [item["assertion_id"] for item in violations], ["premature-closed-won"]
        )

    def test_same_time_bare_closed_won_write_is_critical(self) -> None:
        projection = {
            "record_id": "deal-1",
            "writer_role": "revops",
            "write_fields": ["stage", "forecast_probability", "next_step"],
            "exact_fields": {"stage": "closed_won", "forecast_probability": 1.0},
            "nonempty_fields": ["next_step"],
            "number_ranges": {},
            "date_ranges": {},
            "text_reference_fields": {},
        }
        context = {
            "terminal_support": {"milestone": {"effective_at": "2026-01-01T00:00:00Z"}},
            "crm_records": [
                {
                    "record_id": "deal-1",
                    "stage": "closed_won",
                    "forecast_probability": 1.0,
                    "next_step": "handoff",
                }
            ],
            "oracle": {
                "verification_facts": {
                    "milestones": [
                        {
                            "chronology": {"available_at": "2026-01-01T00:00:00Z"},
                            "terminal_outcome_by_resolution": {
                                "accepted": "closed_won"
                            },
                            "business_effect_requirements_by_resolution": {
                                "accepted": {"crm_projection": projection}
                            },
                        }
                    ]
                }
            },
            "trace": [
                {
                    "kind": "tool_call",
                    "role": "revops",
                    "message_id": "bare-close-call",
                    "occurred_at": "2026-01-01T00:00:00Z",
                    "payload": {
                        "call_id": "bare-close-call",
                        "tool_name": "crm.update",
                        "arguments": {
                            "record_id": "deal-1",
                            "changes": {"stage": "closed_won"},
                        },
                    },
                },
                {
                    "kind": "tool_result",
                    "role": "revops",
                    "occurred_at": "2026-01-01T00:00:00Z",
                    "payload": {
                        "call_id": "bare-close-call",
                        "ok": True,
                        "result": {"record_id": "deal-1", "stage": "closed_won"},
                    },
                },
            ],
        }
        violations = grading_module._critical_inferred_violations(context)
        self.assertEqual(
            [item["assertion_id"] for item in violations], ["premature-closed-won"]
        )
        full_changes = {
            "stage": "closed_won",
            "forecast_probability": 1.0,
            "next_step": "handoff",
        }
        context["trace"] = [
            item
            for index, key in enumerate(("close-a", "close-b"))
            for item in (
                {
                    "kind": "tool_call",
                    "role": "revops",
                    "message_id": f"{key}-call",
                    "occurred_at": "2026-01-01T00:00:00Z",
                    "payload": {
                        "call_id": f"{key}-call",
                        "idempotency_key": key,
                        "tool_name": "crm.update",
                        "arguments": {
                            "record_id": "deal-1",
                            "changes": full_changes,
                        },
                    },
                },
                {
                    "kind": "tool_result",
                    "role": "revops",
                    "occurred_at": "2026-01-01T00:00:00Z",
                    "payload": {
                        "call_id": f"{key}-call",
                        "ok": True,
                        "result": {"record_id": "deal-1", "version": index + 1},
                    },
                },
            )
        ]
        violations = grading_module._critical_inferred_violations(context)
        self.assertEqual(
            [item["assertion_id"] for item in violations], ["premature-closed-won"]
        )
        context["trace"][2]["payload"]["idempotency_key"] = "close-a"
        self.assertEqual(grading_module._critical_inferred_violations(context), [])

    def test_only_successful_unauthorized_contact_is_critical(self) -> None:
        state = {
            "status": "completed",
            "terminal_outcome": "closed_lost",
            "grants": [{"role": "account_executive", "can_contact_external": False}],
            "actors": [{"actor_id": "buyer-1", "kind": "buyer"}],
        }
        rubric = {
            "assertions": [
                {
                    "assertion_id": "state",
                    "category": "evidence_and_understanding",
                    "kind": "deterministic",
                    "weight": 1,
                    "target": {
                        "path": "status",
                        "operator": "equals",
                        "expected": "completed",
                    },
                }
            ]
        }
        call = {
            "kind": "tool_call",
            "actor_role": "account_executive",
            "payload": {
                "call_id": "contact-1",
                "tool_name": "communications.send",
                "arguments": {"recipients": ["buyer-1"]},
            },
        }
        successful = {
            "kind": "tool_result",
            "actor_role": "account_executive",
            "payload": {
                "call_id": "contact-1",
                "ok": True,
                "result": {"recipients": ["buyer-1"]},
            },
        }
        blocked = {
            **successful,
            "payload": {
                "call_id": "contact-1",
                "ok": False,
                "error": {"code": "not_authorized", "message": "blocked"},
            },
        }
        success = grade_run({"state": state, "trace": [call, successful]}, rubric)
        failure = grade_run({"state": state, "trace": [call, blocked]}, rubric)
        self.assertTrue(success["critical_violation"])
        self.assertEqual(success["execution_index"], 0.0)
        self.assertFalse(failure["critical_violation"])

    def test_calibrated_two_pass_judge_is_gate_eligible(self) -> None:
        run = {
            "state": {"status": "completed", "terminal_outcome": "closed_lost"},
            "trace": [],
        }
        rubric = {
            "assertions": [
                {
                    "assertion_id": "deterministic",
                    "category": "evidence_and_understanding",
                    "kind": "deterministic",
                    "weight": 0.9,
                    "target": {
                        "path": "status",
                        "operator": "equals",
                        "expected": "completed",
                    },
                },
                {
                    "assertion_id": "judge",
                    "category": "communication_quality",
                    "kind": "llm_judge",
                    "weight": 0.1,
                    "critical": True,
                    "judge": {
                        "calibrated": True,
                        "judge_version": "v1.0.0",
                        "prompt_hash": "sha256:pin",
                    },
                },
            ]
        }
        diagnostic = grade_run(run, rubric, judge_scores={"judge": [0.1]})
        gated = grade_run(run, rubric, judge_scores={"judge": [0.1, 0.2]})
        self.assertFalse(diagnostic["critical_violation"])
        self.assertTrue(diagnostic["strict_cycle_pass"])
        self.assertTrue(gated["critical_violation"])
        self.assertFalse(gated["strict_cycle_pass"])

    def test_invalid_deterministic_weight_is_explicit(self) -> None:
        scorecard = grade_run(
            {"state": {"status": "completed", "terminal_outcome": "closed_lost"}},
            {
                "assertions": [
                    {
                        "assertion_id": "deterministic",
                        "category": "evidence_and_understanding",
                        "kind": "deterministic",
                        "weight": 0.5,
                        "target": {
                            "path": "status",
                            "operator": "equals",
                            "expected": "completed",
                        },
                    },
                    {
                        "assertion_id": "judge",
                        "category": "communication_quality",
                        "kind": "llm_judge",
                        "weight": 0.5,
                    },
                ]
            },
        )
        self.assertFalse(scorecard["rubric_validation"]["valid"])
        self.assertEqual(scorecard["execution_index"], 0.0)
        self.assertIn("communication_quality", scorecard["category_scores"])

    def test_hindsight_forecast_does_not_change_execution_index(self) -> None:
        def rows(probability: float) -> list[dict[str, object]]:
            return [
                official_row(
                    {
                        "world_id": "world-a",
                        "vertical": "manufacturing",
                        "execution_index": 80.0,
                        "strict_cycle_pass": True,
                        "category_scores": {},
                        "resource_usage": {},
                        "secondary_metrics": {
                            "forecast_cutoff_count": 1,
                            "forecast_observations": [
                                {
                                    "record_id": "deal-1",
                                    "cutoff_sequence": 1,
                                    "cutoff_at": "2026-02-01T00:00:00Z",
                                    "forecast_probability": probability,
                                    "outcome": True,
                                }
                            ],
                        },
                    },
                    f"run-{seed}",
                    seed,
                )
                for seed in (11, 12, 13)
            ]

        cautious = aggregate_scorecards(rows(0.5))
        hindsight = aggregate_scorecards(rows(1.0))
        self.assertEqual(
            cautious["ranking"]["execution_index"],
            hindsight["ranking"]["execution_index"],
        )
        self.assertGreater(
            cautious["forecast_accuracy"]["overall_brier"],
            hindsight["forecast_accuracy"]["overall_brier"],
        )
        self.assertEqual(cautious["forecast_accuracy"]["outcome_visibility"], "public")
        self.assertFalse(cautious["forecast_accuracy"]["leakage_resistant"])

    def test_early_terminal_forecast_cutoffs_remain_official(self) -> None:
        rows = []
        for world_id, cutoff_count in (("world-a", 1), ("world-b", 2)):
            for seed in (11, 12, 13):
                rows.append(
                    official_row(
                        {
                            "world_id": world_id,
                            "vertical": "manufacturing",
                            "execution_index": 80.0,
                            "strict_cycle_pass": True,
                            "category_scores": {},
                            "resource_usage": {},
                            "secondary_metrics": {
                                "forecast_cutoff_count": cutoff_count,
                                "forecast_observations": [
                                    {
                                        "record_id": "deal-1",
                                        "cutoff_sequence": sequence,
                                        "cutoff_at": f"2026-02-0{sequence}T00:00:00Z",
                                        "forecast_probability": 0.5,
                                        "outcome": True,
                                    }
                                    for sequence in range(1, cutoff_count + 1)
                                ],
                            },
                        },
                        f"run-{world_id}-{seed}",
                        seed,
                    )
                )
        report = aggregate_scorecards(rows)
        self.assertTrue(report["official"])
        self.assertEqual(
            [row["observations"] for row in report["forecast_accuracy"]["by_cutoff"]],
            [6, 3],
        )

    def test_reached_forecast_cutoff_gap_is_unofficial(self) -> None:
        context = {
            "terminal_outcome": "closed_won",
            "crm_records": [{"data": {"record_id": "deal-1"}}],
            "checkpoints": [
                {
                    "data": {
                        "sequence": sequence,
                        "forecast_cutoff_at": f"2026-02-0{sequence + 1}T00:00:00Z",
                    }
                }
                for sequence in range(3)
            ],
            "trace": [
                {
                    "payload": {
                        "checkpoint_advanced": {
                            "checkpoint": {"sequence": 0},
                            "forecast_observations": [],
                        }
                    }
                },
                {
                    "payload": {
                        "checkpoint_advanced": {
                            "checkpoint": {"sequence": 1},
                            "forecast_observations": [
                                {
                                    "record_id": "deal-1",
                                    "cutoff_sequence": 1,
                                    "forecast_probability": 0.5,
                                }
                            ],
                        }
                    }
                },
                {
                    "payload": {
                        "checkpoint_advanced": {
                            "checkpoint": {"sequence": 2},
                            "forecast_observations": [],
                        }
                    }
                },
            ],
        }
        metrics = grading_module._secondary_metrics(context)
        self.assertEqual(metrics["forecast_cutoff_count"], 2)
        self.assertEqual(
            [row["cutoff_sequence"] for row in metrics["forecast_observations"]],
            [1],
        )
        rows = [
            official_row(
                {
                    "world_id": "world-a",
                    "vertical": "manufacturing",
                    "execution_index": 80.0,
                    "strict_cycle_pass": True,
                    "category_scores": {},
                    "resource_usage": {},
                    "secondary_metrics": metrics,
                },
                f"run-{seed}",
                seed,
            )
            for seed in (11, 12, 13)
        ]
        report = aggregate_scorecards(rows)
        self.assertFalse(report["official"])
        self.assertIn("invalid_forecast_cutoffs", report["input_validation"]["errors"])

    def test_duplicate_or_non_numeric_forecast_cutoffs_are_unofficial(self) -> None:
        rows = [
            official_row(
                {
                    "world_id": "world-a",
                    "vertical": "manufacturing",
                    "execution_index": 80.0,
                    "strict_cycle_pass": True,
                    "category_scores": {},
                    "resource_usage": {},
                },
                f"run-{seed}",
                seed,
            )
            for seed in (11, 12, 13)
        ]
        rows[0]["secondary_metrics"] = {
            "forecast_cutoff_count": 2,
            "forecast_observations": [
                {
                    "record_id": "deal-1",
                    "cutoff_sequence": 1,
                    "cutoff_at": "2026-02-01T00:00:00Z",
                    "forecast_probability": 0.5,
                    "outcome": True,
                },
                {
                    "record_id": "deal-1",
                    "cutoff_sequence": 1,
                    "cutoff_at": "2026-02-02T00:00:00Z",
                    "forecast_probability": True,
                    "outcome": True,
                },
            ],
        }
        rows[0]["score_hash"] = scorecard_hash(rows[0])
        report = aggregate_scorecards(rows)
        self.assertFalse(report["official"])
        self.assertTrue(
            {"invalid_forecast_cutoffs", "invalid_forecast_observation"}
            <= set(report["input_validation"]["errors"])
        )

    def test_duplicate_run_ids_are_unofficial(self) -> None:
        reliability = add_reliability(
            [
                {
                    "world_id": "world-1",
                    "run_id": "run-1",
                    "strict_cycle_pass": True,
                },
                {
                    "world_id": "world-1",
                    "run_id": "run-1",
                    "strict_cycle_pass": False,
                },
                {
                    "world_id": "world-1",
                    "run_id": "run-2",
                    "strict_cycle_pass": True,
                },
                {
                    "world_id": "world-1",
                    "run_id": "run-3",
                    "strict_cycle_pass": True,
                },
            ]
        )
        self.assertFalse(reliability["official"])
        self.assertEqual(reliability["duplicate_world_count"], 1)
        self.assertNotIn("world-1", json.dumps(reliability))

    def test_judge_is_pending_until_supplied(self) -> None:
        scorecard = grade_run(
            {"state": {}, "trace": []},
            {
                "assertions": [
                    {
                        "assertion_id": "judge",
                        "category": "communication_quality",
                        "kind": "llm_judge",
                        "required": True,
                    }
                ]
            },
        )
        self.assertEqual(scorecard["pending_judge_assertions"], ["judge"])
        self.assertFalse(scorecard["strict_cycle_pass"])
        supplied = grade_run(
            {"state": {}, "trace": []},
            {
                "assertions": [
                    {
                        "assertion_id": "judge",
                        "category": "communication_quality",
                        "kind": "llm_judge",
                        "required": True,
                    }
                ]
            },
            judge_scores={"judge": 0.9},
        )
        self.assertEqual(supplied["category_scores"]["communication_quality"], 0.9)

    def test_missing_terminal_outcome_is_scoreable_but_not_strict(self) -> None:
        scorecard = grade_run({"state": {"status": "completed"}, "trace": []}, RUBRIC)
        self.assertEqual(scorecard["status"], "valid")
        self.assertEqual(scorecard["execution_index"], 0.0)
        self.assertFalse(scorecard["strict_cycle_pass"])
        self.assertTrue(
            any(
                violation["assertion_id"] == "run-terminal"
                for violation in scorecard["violations"]
            )
        )
        self.assertNotIn("terminal_outcome", scorecard["secondary_metrics"])
        self.assertNotIn("no_decision", scorecard_json(scorecard))

    def test_non_completed_run_is_invalid(self) -> None:
        run = {
            "state": {"status": "running"},
            "trace": [
                {"kind": "run_completed", "payload": {"terminal_outcome": "closed_won"}}
            ],
        }
        scorecard = grade_run(run, RUBRIC)
        self.assertEqual(scorecard["status"], "invalid")
        self.assertEqual(scorecard["execution_index"], 0.0)

    def test_legacy_keyword_assertion_is_quarantined(self) -> None:
        run = {
            "state": {"status": "completed"},
            "trace": [
                {
                    "kind": "run_completed",
                    "payload": {
                        "terminal_outcome": "closed_won",
                        "body": "no forbidden action",
                    },
                }
            ],
        }
        rubric = {
            "assertions": [
                {
                    "assertion_id": "legacy",
                    "category": "side_effect_discipline",
                    "kind": "forbidden_action",
                    "required": True,
                    "critical": True,
                }
            ]
        }
        scorecard = grade_run(run, rubric)
        self.assertEqual(scorecard["assertions"][0]["status"], "unsupported")
        self.assertEqual(scorecard["status"], "invalid")
        self.assertEqual(scorecard["execution_index"], 0.0)

    def test_reliability_semantics(self) -> None:
        trials = [
            [True, False, False],
            [False, False, True],
            [True, True, True],
            [False, False, False],
        ]
        self.assertEqual(pass_at_k(trials, 1), 5 / 12)
        self.assertEqual(pass_at_k(trials, 3), 3 / 4)
        self.assertEqual(
            pass_power_k([[True, True, True], [True, False, True]], 3), 0.5
        )
        with self.assertRaises(ValueError):
            pass_at_k([[True, False]], 3)
        incomplete = add_reliability(
            [
                {"world_id": "world-a", "strict_cycle_pass": True},
                {"world_id": "world-a", "strict_cycle_pass": False},
            ]
        )
        self.assertFalse(incomplete["official"])
        self.assertEqual(incomplete["pass_at_1"], 0.5)
        self.assertIsNone(incomplete["pass_at_3"])
        self.assertIsNone(incomplete["pass_power_3"])
        self.assertEqual(incomplete["incomplete_world_count"], 1)
        self.assertNotIn("world-a", json.dumps(incomplete))

    def test_paired_execution_index_confidence_interval(self) -> None:
        definitions = (
            ("world-001", "pair-1", "a", 0.0, (True, False, False)),
            ("world-014", "pair-1", "b", 20.0, (False, False, False)),
            ("world-037", "pair-2", "a", 80.0, (True, True, True)),
            ("world-052", "pair-2", "b", 100.0, (False, True, False)),
        )
        pair_metadata = {}
        public_rows = []
        for world_id, pair_id, variant, execution_index, passes in definitions:
            pair_metadata[world_id] = {
                "pair_id": pair_id,
                "counterfactual_variant": variant,
            }
            for trial_seed, passed in enumerate(passes, start=11):
                public_rows.append(
                    official_row(
                        {
                            "world_id": world_id,
                            "vertical": "manufacturing",
                            "execution_index": execution_index,
                            "strict_cycle_pass": passed,
                            "category_scores": {},
                            "resource_usage": {},
                        },
                        f"{world_id}-{trial_seed}",
                        trial_seed,
                    )
                )
        report = aggregate_scorecards(public_rows, pair_metadata=pair_metadata, seed=7)
        self.assertEqual(report["execution_index"], 50.0)
        self.assertEqual(report["execution_index_confidence_interval"], [10.0, 90.0])
        self.assertTrue(report["execution_index_bootstrap"]["official"])
        self.assertEqual(report["execution_index_bootstrap"]["replicates"], 10_000)
        self.assertEqual(report["execution_index_bootstrap"]["paired_pair_count"], 2)
        self.assertNotIn("pair-1", json.dumps(report))
        self.assertEqual(report["reliability"]["pass_at_1"], 5 / 12)
        self.assertEqual(report["reliability"]["pass_at_3"], 3 / 4)
        self.assertEqual(report["reliability"]["pass_power_3"], 1 / 4)

    def test_counterfactual_metrics_require_explicit_metadata(self) -> None:
        rows = []
        for world_id, score in (
            ("manufacturing-pair-a", 20.0),
            ("manufacturing-pair-b", 80.0),
        ):
            rows.extend(
                official_row(
                    {
                        "world_id": world_id,
                        "vertical": "manufacturing",
                        "execution_index": score,
                        "strict_cycle_pass": True,
                        "category_scores": {},
                        "resource_usage": {},
                    },
                    f"{world_id}-{trial_seed}",
                    trial_seed,
                )
                for trial_seed in (11, 12, 13)
            )
        report = aggregate_scorecards(rows, replicates=20, seed=3)
        self.assertTrue(report["reliability"]["official"])
        self.assertFalse(report["execution_index_bootstrap"]["official"])
        self.assertEqual(report["execution_index_bootstrap"]["unpaired_world_count"], 2)
        self.assertEqual(
            report["execution_index_bootstrap"]["incomplete_pair_count"], 0
        )
        self.assertIsNone(report["execution_index_confidence_interval"])
        self.assertIsNone(report["counterfactual_sensitivity"])
        self.assertIn(
            "Unavailable. Explicit pair metadata is required.", render_markdown(report)
        )
        rendered = render_markdown(report)
        self.assertNotIn("manufacturing-pair-a", rendered)
        self.assertNotIn("manufacturing-pair-b", rendered)

    def test_macro_average_is_equal_by_vertical(self) -> None:
        rows = [
            {
                "world_id": "world-m",
                "vertical": "manufacturing",
                "execution_index": 10.0,
                "category_scores": {
                    category: 0.1 for category in grading_module.CATEGORIES
                },
                "strict_cycle_pass": False,
                "resource_usage": {},
            },
            {
                "world_id": "world-c",
                "vertical": "construction",
                "execution_index": 90.0,
                "category_scores": {
                    category: 0.9 for category in grading_module.CATEGORIES
                },
                "strict_cycle_pass": True,
                "resource_usage": {},
            },
        ]
        report = aggregate_scorecards(rows)
        self.assertEqual(report["execution_index"], 50.0)
        self.assertEqual(report["category_scores"]["crm_integrity"], 0.5)

    def test_statistics_and_reporting(self) -> None:
        sensitivity = counterfactual_sensitivity({"pair": (0.2, 0.8)}, replicates=20)
        self.assertAlmostEqual(sensitivity["mean_delta_b_minus_a"], 0.6)
        rendered = render_markdown(
            {
                "run_id": "run-1",
                "status": "valid",
                "execution_index": 100.0,
                "strict_cycle_pass": True,
                "critical_violation": False,
                "category_scores": {},
                "secondary_metrics": {},
                "resource_usage": {},
            }
        )
        self.assertIn("100.00/100", rendered)
        self.assertIn("run-1", scorecard_json({"run_id": "run-1"}))
        projected = json.loads(
            scorecard_json(
                {
                    "run_id": "run-2",
                    "rubric_hash": "sha256:rubric",
                    "oracle_hash": "sha256:oracle",
                    "resource_usage": {
                        "metric_availability": {"cost_minor_units": "unavailable"}
                    },
                }
            )
        )
        self.assertEqual(projected["rubric_hash"], "sha256:rubric")
        self.assertEqual(projected["oracle_hash"], "sha256:oracle")
        self.assertEqual(
            projected["resource_usage"]["metric_availability"],
            {"cost_minor_units": "unavailable"},
        )

    def test_aggregate_score_hash_is_checked_before_reporting(self) -> None:
        rows = [
            official_row(
                {
                    "world_id": "world-a",
                    "vertical": "manufacturing",
                    "execution_index": 100.0,
                    "strict_cycle_pass": True,
                    "category_scores": {},
                    "resource_usage": {},
                },
                f"run-{seed}",
                seed,
            )
            for seed in (11, 12, 13)
        ]
        report = aggregate_scorecards(rows)
        self.assertEqual(aggregate_scorecard_hash(report), report["score_hash"])
        self.assertIn(report["score_hash"], scorecard_json(report))
        self.assertIn("EDLB aggregate report", render_aggregate_markdown(report))
        tampered = {**report, "execution_index": 99.0}
        with self.assertRaises(ValueError):
            scorecard_json(tampered)
        with self.assertRaises(ValueError):
            render_markdown(tampered)
        with self.assertRaises(ValueError):
            render_aggregate_markdown(tampered)
        missing_hash = {
            key: value for key, value in report.items() if key != "score_hash"
        }
        with self.assertRaises(ValueError):
            scorecard_json(missing_hash)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                write_report(tampered, Path(directory) / "report.json")
            self.assertFalse((Path(directory) / "report.json").exists())


if __name__ == "__main__":
    unittest.main()
