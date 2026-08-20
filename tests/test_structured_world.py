from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
DATASET_ROOT = Path(os.environ.get("EDLB_TEST_ROOT", ROOT / "benchmarks/v1"))

generate = __import__("edlb.generate", fromlist=["generate_dataset"])
runner = __import__("edlb.runner", fromlist=["open_world"])


def rows(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def first_world() -> Path:
    return next((DATASET_ROOT / "output/public/train").glob("world-*"))


def resolved_environment() -> dict[str, object]:
    return {
        "resolved": True,
        "runtime_version": "cpython-test",
        "image_digest": "sha256:" + "a" * 64,
        "git_revision": "b" * 40,
        "executor_policy_digest": "sha256:" + "c" * 64,
    }


class StructuredWorldTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.world_root = DATASET_ROOT / "output/public"
        cls.worlds = [
            path
            for split in ("train", "dev", "blind")
            for path in sorted((cls.world_root / split).glob("world-*"))
        ]
        cls.authoring = {
            row["world_id"]: row
            for row in rows(DATASET_ROOT / "authoring/worlds.jsonl")
        }
        vertical_index = {
            vertical["id"]: index for index, vertical in enumerate(generate.VERTICALS)
        }
        family_index = {family: index for index, family in enumerate(generate.FAMILIES)}
        cls.full_worlds = {
            row["world_id"]: generate._build_world(
                vertical_index[row["vertical"]],
                family_index[row["causal_family"]],
                generate.VARIANT_NAMES[row["causal_family"]].index(row["variant"]),
                generate.DATASET_SEED,
            )
            for row in cls.authoring.values()
        }

    def test_blueprints_are_gate_specific_and_source_exact(self) -> None:
        registry = json.loads(
            (ROOT / "src/edlb/resources/source_registry.json").read_text()
        )
        sources = registry["sources"]
        by_vertical: dict[str, dict[str, set[str]]] = {}
        for source in sources:
            gates = by_vertical.setdefault(source["vertical"], {})
            for gate, facts in source["gate_fact_ids"].items():
                gates.setdefault(gate, set()).update(facts)
        for vertical, blueprint in generate.VERTICAL_BLUEPRINTS["verticals"].items():
            kinds = set()
            for gate in blueprint["gates"]:
                expected_facts = by_vertical[vertical][gate["gate_id"]]
                self.assertEqual(set(gate["source_fact_ids"]), expected_facts)
                self.assertGreaterEqual(len(gate["required_artifacts"]), 3)
                self.assertLessEqual(len(gate["required_artifacts"]), 5)
                for spec in gate["required_artifacts"]:
                    kinds.add(spec["kind"])
                    self.assertTrue(spec["author_role_id"])
                    self.assertTrue(spec["authoritative_for"])
                    self.assertTrue(
                        spec["recipient_role_ids"] or spec["kind"] == "crm_record"
                    )
                    self.assertTrue(spec["artifact_subtype"])
                self.assertTrue(gate["decision_route"])
            self.assertGreaterEqual(len(kinds), 3)

    def test_source_provenance_and_buyer_pair_identity(self) -> None:
        registry = json.loads(
            (ROOT / "src/edlb/resources/source_registry.json").read_text()
        )
        source_ids = {source["source_id"] for source in registry["sources"]}
        fact_sources: dict[str, set[str]] = {}
        for source in registry["sources"]:
            self.assertRegex(source["retrieval_sha256"], r"^sha256:[0-9a-f]{64}$")
            self.assertTrue(source["content_type"])
            self.assertTrue(source["version_date"])
            for fact_id in source["fact_ids"]:
                fact_sources.setdefault(fact_id, set()).add(source["source_id"])
        buyers: dict[tuple[str, str], set[str]] = {}
        for world in self.full_worlds.values():
            buyers.setdefault((world["buyer_name"], world["buyer_domain"]), set()).add(
                world["pair_id"]
            )
            for artifact in rows(
                self.world_root.joinpath(
                    world["split"], world["world_id"], "artifacts.jsonl"
                )
            ):
                fact_ids = set(artifact["provenance"]["fact_ids"])
                expected_sources = set().union(
                    *(fact_sources[fact] for fact in fact_ids)
                )
                self.assertEqual(
                    set(artifact["provenance"]["source_ids"]), expected_sources
                )
                self.assertTrue(expected_sources <= source_ids)
        self.assertEqual(len(buyers), 36)
        self.assertTrue(all(len(pair_ids) == 1 for pair_ids in buyers.values()))

    def test_authority_ownership_lineage_and_delay_bounds(self) -> None:
        for bundle in self.worlds:
            manifest = json.loads((bundle / "manifest.json").read_text())
            actors = {row["actor_id"]: row for row in rows(bundle / "actors.jsonl")}
            artifacts = rows(bundle / "artifacts.jsonl")
            by_id = {row["artifact_id"]: row for row in artifacts}
            oracle = json.loads((bundle / "oracle.json").read_text())
            authority_by_gate = {
                milestone["gate_id"]: {
                    requirement["actor_id"]
                    for requirement in milestone["authority_requirements"]
                }
                for milestone in oracle["verification_facts"]["milestones"]
            }
            vertical = generate.VERTICAL_BLUEPRINTS["verticals"][manifest["vertical"]]
            route_gates = {row["gate_id"] for row in rows(bundle / "checkpoints.jsonl")}
            for gate in (
                gate for gate in vertical["gates"] if gate["gate_id"] in route_gates
            ):
                specs = {
                    spec["artifact_key"]: spec for spec in gate["required_artifacts"]
                }
                gate_rows = [
                    row for row in artifacts if row["gate_id"] == gate["gate_id"]
                ]
                structured = [row for row in gate_rows if row["artifact_key"] in specs]
                self.assertEqual(
                    {row["artifact_key"] for row in structured}, set(specs)
                )
                for row in structured:
                    spec = specs[row["artifact_key"]]
                    source = actors[row["source_actor_ids"][0]]
                    if spec["artifact_role"] == "decision":
                        self.assertIn(
                            source["actor_id"], authority_by_gate[gate["gate_id"]]
                        )
                        self.assertEqual(
                            row["structured_payload"]["author_actor_id"],
                            source["actor_id"],
                        )
                    else:
                        self.assertEqual(
                            source["authority"]["role_id"], spec["author_role_id"]
                        )
                    self.assertEqual(row["kind"], spec["kind"])
                    if row["kind"] == "contract":
                        body = json.loads(row["content"]["body"])["structured_payload"]
                        self.assertIn(
                            body["artifact_subtype"],
                            {
                                "carrier_binder",
                                "executed_award_contract",
                                "sow_execution",
                                "purchase_order_execution",
                                "engagement_execution",
                            },
                        )
                    if row["derived_from_artifact_ids"]:
                        for parent_id in row["derived_from_artifact_ids"]:
                            self.assertGreaterEqual(
                                row["created_at"], by_id[parent_id]["available_at"]
                            )
                    if row["supersedes_artifact_id"]:
                        self.assertGreaterEqual(
                            row["created_at"],
                            by_id[row["supersedes_artifact_id"]]["available_at"],
                        )
                    origin = row["projection_origin"]
                    if origin:
                        self.assertIn(origin["source_artifact_id"], by_id)
                        self.assertGreaterEqual(
                            row["created_at"],
                            by_id[origin["source_artifact_id"]]["available_at"],
                        )
                for row in structured:
                    lower = gate["availability_delay_bounds"]["min_minutes"]
                    upper = gate["availability_delay_bounds"]["max_minutes"]
                    created = datetime.fromisoformat(row["created_at"])
                    available = datetime.fromisoformat(row["available_at"])
                    self.assertGreaterEqual(
                        (available - created).total_seconds() / 60, lower
                    )
                    self.assertLessEqual(
                        (available - created).total_seconds() / 60, upper
                    )

    def test_reference_approval_uses_policy_basis_and_gate_keys(self) -> None:
        for bundle in self.worlds:
            manifest = json.loads((bundle / "manifest.json").read_text())
            world = self.full_worlds[manifest["world_id"]]
            trace = rows(bundle / "reference_trace.jsonl")
            requests = [
                row
                for row in trace
                if row["kind"] == "tool_call"
                and row["tool_name"] == "approvals.request"
            ]
            expected = []
            for checkpoint in world["checkpoints"]:
                exception = generate._seller_approval_exception(world, checkpoint)
                if exception["required"] and generate._decision_state(
                    world, checkpoint
                ) in {"accepted", "remedied"}:
                    expected.append((checkpoint, exception))
            self.assertEqual(len(requests), len(expected))
            for request, (checkpoint, exception) in zip(
                requests, expected, strict=True
            ):
                details = request["arguments"]["details"]
                self.assertEqual(details["gate"], checkpoint["gate_id"])
                self.assertEqual(
                    details["amount_minor_units"],
                    exception["basis"]["amount_minor_units"],
                )
                self.assertEqual(details["basis"], exception["basis"])

    def test_terminal_outcome_uses_exact_authored_approval(self) -> None:
        required_exception = None
        for bundle in self.worlds:
            oracle = json.loads((bundle / "oracle.json").read_text())
            if oracle["scenario_manifest"]["terminal_outcome"] != "closed_won":
                continue
            requirements = oracle["verification_facts"]["approval_requirements"]
            if requirements and required_exception is None:
                required_exception = bundle
        self.assertIsNotNone(required_exception)
        assert required_exception is not None
        oracle = json.loads((required_exception / "oracle.json").read_text())
        requirement = oracle["verification_facts"]["approval_requirements"][0]
        milestone = next(
            item
            for item in oracle["verification_facts"]["milestones"]
            if item["checkpoint_id"] == requirement["checkpoint_id"]
        )
        self.assertEqual(milestone["approval_requirement"], requirement)
        required_trace = rows(required_exception / "reference_trace.jsonl")
        request = next(
            row
            for row in required_trace
            if row["kind"] == "tool_call"
            and row["tool_name"] == "approvals.request"
            and row["arguments"]["details"]["checkpoint_id"]
            == requirement["checkpoint_id"]
        )
        decision = next(
            row
            for row in required_trace
            if row["kind"] == "tool_call"
            and row["tool_name"] == "approvals.approve"
            and row["sequence"] > request["sequence"]
        )
        completions = [
            row
            for row in required_trace
            if row["kind"] == "tool_call"
            and row["tool_name"] == "run.complete_checkpoint"
            and row["arguments"]["checkpoint_id"] == requirement["checkpoint_id"]
        ]
        self.assertEqual(
            request["arguments"]["details"]["gate"], requirement["gate_id"]
        )
        self.assertTrue(
            all(row["sequence"] > decision["sequence"] for row in completions)
        )
        with runner.open_world(
            required_exception,
            run_id=str(required_trace[0]["run_id"]),
            agent_manifest=generate.REFERENCE_AGENT_MANIFEST,
        ) as engine:
            result = runner.replay_trace(engine, required_trace)
            self.assertEqual(result.status, "completed")
            self.assertEqual(engine.run_status()["terminal_outcome"], "closed_won")

    def test_crm_projection_preserves_agent_repairs_and_history(self) -> None:
        with runner.open_world(
            first_world(),
            run_id="structured-crm-regression",
            agent_manifest=runner.deterministic_agent_manifest("structured-test"),
            environment_manifest=resolved_environment(),
        ) as engine:
            runner._activate_first(engine)
            initial = engine.crm_search("revops", limit=1)[0]
            record_id = initial["record_id"]
            engine.crm_update(
                "revops",
                record_id,
                {"next_step": "agent repair"},
                "crm-repair-regression",
            )
            engine.seed_crm_projection(
                record_id,
                {"next_step": "system projection one", "stage": "diligence"},
                engine.current_time,
            )
            engine.seed_crm_projection(
                record_id,
                {"next_step": "system projection two", "stage": "underwriting"},
                engine.current_time,
            )
            same_checkpoint = engine.crm_read("revops", record_id)
            self.assertEqual(same_checkpoint["record"]["next_step"], "agent repair")
            self.assertEqual(same_checkpoint["record"]["stage"], "underwriting")
            runner._advance(engine, True, "crm-projection-regression")
            current = engine.crm_read("revops", record_id)
            self.assertEqual(current["record"]["next_step"], "agent repair")
            self.assertGreaterEqual(current["version"], 3)
            history = engine.crm_history("revops", record_id)
            self.assertTrue(any(item["role"] == "revops" for item in history))
            self.assertGreaterEqual(len(history), 3)

    def test_policy_entrypoints_and_agent_redaction(self) -> None:
        with runner.open_world(
            first_world(),
            run_id="structured-policy-regression",
            agent_manifest=runner.deterministic_agent_manifest("structured-test"),
            environment_manifest=resolved_environment(),
        ) as engine:
            runner._activate_first(engine)
            checkpoint = engine.current_checkpoint()
            self.assertIsNotNone(checkpoint)
            docs = engine.documents_search("account_executive", "policy", 100)
            self.assertTrue(docs)
            self.assertTrue(
                {doc["document_id"] for doc in docs}
                >= set(checkpoint["policy_entrypoints"])
            )
            visible = engine.artifacts("account_executive", limit=100)
            text = json.dumps(visible, sort_keys=True)
            for field in (
                "lane_effects",
                "causal_effects",
                "variant",
                "reference_outcome",
                "allowed_state_diff_targets",
                "approval_required",
                "expected_resolution",
                "lane_effects_by_resolution",
                "prerequisite_milestone_ids",
                "source_fact_ids",
                "terminal_outcome_by_resolution",
            ):
                self.assertNotIn(field, text)
            self.assertIn("decision_state", text)

    def test_alerts_are_scoped_and_one_time_in_both_tracks(self) -> None:
        for track in ("open_team", "fixed_harness"):
            with runner.open_world(
                first_world(),
                run_id=f"structured-alert-{track}",
                track=track,
                agent_manifest=runner.deterministic_agent_manifest("structured-test"),
                environment_manifest=resolved_environment(),
            ) as engine:
                runner._activate_first(engine)
                seen: set[str] = set()
                first = runner._released_alerts(engine, "account_executive", seen)
                second = runner._released_alerts(engine, "account_executive", seen)
                self.assertTrue(first)
                self.assertFalse(second)
                runner._advance(engine, True, f"structured-alert-advance-{track}")
                later = runner._released_alerts(engine, "account_executive", seen)
                self.assertTrue(
                    {item["event_id"] for item in first}.isdisjoint(
                        {item["event_id"] for item in later}
                    )
                )

    def test_strict_loaders_reject_missing_v1_fields(self) -> None:
        with tempfile.TemporaryDirectory(dir=DATASET_ROOT) as directory:
            copy = Path(directory) / first_world().name
            shutil.copytree(first_world(), copy)
            actor_path = copy / "actors.jsonl"
            actor_rows = rows(actor_path)
            actor_rows[0].pop("authority", None)
            actor_path.write_text("".join(json.dumps(row) + "\n" for row in actor_rows))
            with self.assertRaises(runner.BundleError):
                runner.open_world(copy)
        with tempfile.TemporaryDirectory(dir=DATASET_ROOT) as directory:
            copy = Path(directory) / first_world().name
            shutil.copytree(first_world(), copy)
            event_path = copy / "events.jsonl"
            event_rows = rows(event_path)
            event_rows[0].pop("available_at")
            event_path.write_text("".join(json.dumps(row) + "\n" for row in event_rows))
            with self.assertRaises(runner.BundleError):
                runner.open_world(copy)


if __name__ == "__main__":
    unittest.main()
