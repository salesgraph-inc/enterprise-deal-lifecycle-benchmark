from __future__ import annotations

import ast
import hashlib
import importlib
import importlib.resources
import importlib.util
import inspect
import io
import json
import re
import sys
import tempfile
import textwrap
import unittest
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

generate_module = importlib.import_module("edlb.generate")
grading_module = importlib.import_module("edlb.grading")
engine_module = importlib.import_module("edlb.engine")
protocol_module = importlib.import_module("edlb.protocol")
runner_module = importlib.import_module("edlb.runner")
tools_module = importlib.import_module("edlb.tools")
ARTIFACT_COUNTS = generate_module.ARTIFACT_COUNTS
CANONICAL_CATEGORIES = generate_module.CANONICAL_CATEGORIES
VERTICALS = generate_module.VERTICALS
generate_dataset = generate_module.generate_dataset
grade_run = grading_module.grade_run
open_world = runner_module.open_world
pair_diff = generate_module.pair_diff
replay_trace = runner_module.replay_trace
validate_synthetic_privacy = generate_module._validate_synthetic_privacy
source_verifier_spec = importlib.util.spec_from_file_location(
    "verify_source_evidence", ROOT / "scripts/verify_source_evidence.py"
)
if source_verifier_spec is None or source_verifier_spec.loader is None:
    raise RuntimeError("source verifier could not be loaded")
source_verifier = importlib.util.module_from_spec(source_verifier_spec)
source_verifier_spec.loader.exec_module(source_verifier)


CHANNEL_BY_KIND = {
    "call_transcript": "transcript",
    "email": "email",
    "internal_chat": "internal_chat",
    "crm_record": "crm",
    "crm_history": "crm",
    "calendar_event": "calendar",
    "proposal": "document",
    "quote": "document",
    "contract": "document",
    "diligence_document": "document",
    "web_page": "web_news",
    "news_item": "web_news",
}
MANIFEST_TRUTH_FIELDS = {
    "pair_id",
    "counterfactual_variant",
    "causal_skeleton",
    "terminal_outcome",
    "seed",
    "outcome_reason",
}
DEV_AUTHORING_TRUTH_FIELDS = MANIFEST_TRUTH_FIELDS | {
    "causal_family",
    "variant",
    "reference_outcome",
    "intervention_checkpoint_id",
    "intervention_sequence",
    "intervention_gate",
    "resolution_checkpoint_id",
    "resolution_sequence",
    "resolution_gate",
    "causal_action_code",
    "observable_cure",
    "causal_owner_role",
    "causal_authority_role_ids",
    "defects",
    "actors",
    "checkpoints",
}
OPAQUE_PATTERNS = {
    "world_id": re.compile(r"^world-[0-9a-f]{20}$"),
    "pair_id": re.compile(r"^pair-[0-9a-f]{20}$"),
    "seller_org_id": re.compile(r"^org-[0-9a-f]{20}$"),
    "buyer_org_id": re.compile(r"^org-[0-9a-f]{20}$"),
}


def _rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


class GenerationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp_dirs = [tempfile.TemporaryDirectory() for _ in range(3)]
        cls.public_roots = [
            Path(item.name) / "benchmarks" / "v1" for item in cls.temp_dirs[:2]
        ]
        cls.official_root = Path(cls.temp_dirs[2].name) / "benchmarks" / "v1"
        cls.public_summaries = [generate_dataset(root) for root in cls.public_roots]
        cls.official_summary = generate_dataset(cls.official_root)
        cls.schemas = {
            path.name.removesuffix(".json"): json.loads(path.read_text())
            for path in importlib.resources.files("edlb").joinpath("schemas").iterdir()
            if path.name.endswith(".json")
        }

    @classmethod
    def tearDownClass(cls) -> None:
        for temp_dir in cls.temp_dirs:
            temp_dir.cleanup()

    def assert_shape(
        self,
        value: dict[str, Any],
        schema: dict[str, Any],
        allowed_missing: set[str] | None = None,
    ) -> None:
        required = set(schema.get("required", ())) - (allowed_missing or set())
        self.assertTrue(required <= value.keys(), required - value.keys())
        if schema.get("additionalProperties") is False:
            self.assertFalse(
                value.keys() - schema["properties"].keys(),
                value.keys() - schema["properties"].keys(),
            )

    def assert_timestamp(self, value: str) -> None:
        self.assertRegex(value, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        parsed = datetime.fromisoformat(value)
        self.assertIsNotNone(parsed.tzinfo)

    def test_generation_refuses_existing_output_without_force(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "benchmarks" / "v1"
            output = root / "output"
            output.mkdir(parents=True)
            sentinel = output / "owner-data.txt"
            sentinel.write_text("preserve", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "pass force=True"):
                generate_dataset(root)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve")

    def bundles(self) -> list[tuple[Path, bool]]:
        result: list[tuple[Path, bool]] = []
        for split in ("train", "dev", "blind"):
            result.extend(
                (path.parent, False)
                for path in self.official_root.joinpath("output", "public", split).glob(
                    "*/manifest.json"
                )
            )
        return result

    def test_public_counts(self) -> None:
        public = self.public_summaries[0]
        official = self.official_summary
        self.assertTrue(public["valid"])
        self.assertTrue(official["valid"])
        self.assertEqual(public["world_count"], 72)
        self.assertEqual(public["split_counts"], {"train": 24, "dev": 24, "blind": 24})
        self.assertEqual(official["world_count"], 72)
        self.assertEqual(
            official["split_counts"], {"train": 24, "dev": 24, "blind": 24}
        )
        self.assertEqual(official["shared_document_count"], 180)
        self.assertGreaterEqual(official["artifact_count_min"], 60)
        self.assertLessEqual(official["artifact_count_max"], 120)
        self.assertGreater(
            official["artifact_count_max"], official["artifact_count_min"]
        )
        self.assertEqual(official["artifact_total"], 8060)
        self.assertEqual(official["checkpoint_min"], 6)
        self.assertEqual(official["checkpoint_max"], 8)
        self.assertGreaterEqual(official["duration_min"], 180)
        self.assertLessEqual(official["duration_max"], 365)

    def test_balanced_outcomes_and_pair_diffs(self) -> None:
        expected = {
            "closed_won": 4,
            "closed_lost_competitive": 3,
            "closed_lost_fit": 1,
            "no_decision": 2,
            "disqualified_fit": 2,
        }
        for vertical in VERTICALS:
            self.assertEqual(
                self.official_summary["outcome_counts"][vertical["id"]], expected
            )
        self.assertEqual(self.official_summary["pair_count"], 36)
        for diff in self.official_summary["pair_diffs"]:
            self.assertTrue(diff["base_facts_equal"])
            self.assertTrue(diff["pre_intervention_artifacts_equal"])
            self.assertGreater(diff["post_intervention_artifact_differences"], 0)
            self.assertTrue(diff["post_intervention_changes_are_declared_descendants"])
            self.assertTrue(diff["post_intervention_context_isomorphic"])
            self.assertTrue(diff["causal_event_graph_valid"])
            for field in (
                "action_contracts_isomorphic",
                "branch_contracts_isomorphic",
                "milestone_contracts_isomorphic",
                "pre_intervention_events_equal",
                "pre_intervention_hidden_events_equal",
                "reference_trace_causal_material_isomorphic",
                "selected_evidence_contracts_isomorphic",
                "terminal_mappings_isomorphic",
            ):
                self.assertTrue(diff[field], (diff["pair_id"], field))

    def test_public_authoring_contains_full_truth(self) -> None:
        root = self.public_roots[0]
        rows = _rows(root / "authoring" / "worlds.jsonl")
        self.assertEqual(len(rows), 72)
        self.assertEqual({row["split"] for row in rows}, {"train", "dev", "blind"})
        self.assertEqual(len(_rows(root / "authoring" / "shared_documents.jsonl")), 180)
        self.assertFalse((root / "private").exists())
        for row in rows:
            self.assertTrue(
                {
                    "pair_id",
                    "seed",
                    "causal_family",
                    "variant",
                    "reference_outcome",
                    "defects",
                }
                <= row.keys()
            )
        validation = json.loads((root / "authoring" / "validation.json").read_text())
        self.assertEqual(validation["pair_count"], 36)
        self.assertEqual(len(validation["pair_diffs"]), 36)
        self.assertIn("outcome_counts", validation)
        self.assertFalse((root / "authoring" / "schema_projection_gaps.json").exists())
        public_text = "\n".join(
            path.read_text(errors="ignore")
            for base in (
                self.official_root / "authoring",
                self.official_root / "output" / "public",
            )
            for path in base.rglob("*")
            if path.is_file()
        )
        self.assertTrue(all(row["world_id"] in public_text for row in rows))

    def test_opaque_public_ids_and_paths(self) -> None:
        semantic_tokens = {
            "champion_exit",
            "late_stakeholder",
            "budget_shock",
            "requirements_change",
            "competition",
            "external_event",
            "closed_won",
            "closed_lost",
            "no_decision",
            "disqualified",
        }
        for split in ("train", "dev", "blind"):
            for bundle in self.official_root.joinpath(
                "output", "public", split
            ).iterdir():
                if not bundle.is_dir():
                    continue
                self.assertRegex(bundle.name, OPAQUE_PATTERNS["world_id"])
                self.assertFalse(
                    any(token in bundle.as_posix() for token in semantic_tokens)
                )
                manifest = json.loads((bundle / "manifest.json").read_text())
                for field in ("world_id", "seller_org_id", "buyer_org_id"):
                    self.assertRegex(manifest[field], OPAQUE_PATTERNS[field])
                for actor in _rows(bundle / "actors.jsonl"):
                    self.assertRegex(actor["actor_id"], r"^act-[0-9a-f]{20}$")
                for artifact in _rows(bundle / "artifacts.jsonl"):
                    self.assertRegex(
                        artifact["artifact_id"], r"^artifact-[0-9a-f]{20}$"
                    )
                    self.assertRegex(artifact["thread_id"], r"^thread-[0-9a-f]{20}$")
                    self.assertNotIn("pair-", artifact["content"]["source_uri"])
                    for url in re.findall(
                        r"https://[^\s)]+", artifact["content"]["body"]
                    ):
                        if "edlb.example" in url:
                            self.assertRegex(
                                url.rstrip('.,"'),
                                r"https://edlb\.example/(signals|meetings)/artifact-[0-9a-f]{20}$",
                            )
        train_rows = [
            row
            for row in _rows(self.official_root / "authoring" / "worlds.jsonl")
            if row["split"] == "train"
        ]
        for row in train_rows:
            self.assertRegex(row["pair_id"], OPAQUE_PATTERNS["pair_id"])

    def test_manifest_projections_and_truth_files(self) -> None:
        schema = self.schemas["scenario-manifest"]
        for bundle, private in self.bundles():
            manifest = json.loads((bundle / "manifest.json").read_text())
            self.assertFalse(private)
            self.assert_shape(manifest, schema)
            self.assertEqual(manifest["release_visibility"], "public")
            summary = (bundle / "content_summary.md").read_text()
            self.assertNotIn("Causal family", summary)
            self.assertNotIn("Reference outcome", summary)
            oracle = json.loads((bundle / "oracle.json").read_text())
            self.assert_shape(oracle["scenario_manifest"], schema)
            self.assertTrue((bundle / "reference_trace.jsonl").exists())
            self.assertTrue((bundle / "hidden_events.jsonl").exists())

    def test_normative_record_shapes(self) -> None:
        actor_schema = self.schemas["actor"]
        event_schema = self.schemas["event"]
        artifact_schema = self.schemas["artifact"]
        checkpoint_schema = self.schemas["checkpoint"]
        assertion_schema = self.schemas["assertion"]
        for bundle, _ in self.bundles():
            manifest = json.loads((bundle / "manifest.json").read_text())
            actor_ids: set[str] = set()
            for actor in _rows(bundle / "actors.jsonl"):
                self.assert_shape(actor, actor_schema)
                self.assert_timestamp(actor["active_from"])
                self.assertTrue(actor["email"].endswith(".example"))
                scope = actor.get("attributes", {}).get("organization_scope")
                if scope == "buyer" or actor["kind"] == "buyer":
                    self.assertEqual(actor["organization_id"], manifest["buyer_org_id"])
                elif scope == "seller" or actor["kind"] in {"seller", "internal"}:
                    self.assertEqual(
                        actor["organization_id"], manifest["seller_org_id"]
                    )
                else:
                    self.assertNotIn(
                        actor["organization_id"],
                        {manifest["buyer_org_id"], manifest["seller_org_id"]},
                    )
                if actor["visibility"] in {"internal_role_scoped", "restricted"}:
                    self.assertTrue(actor.get("visible_roles"))
                    self.assertTrue(
                        set(actor["visible_roles"]) <= set(generate_module.ROLES)
                    )
                actor_ids.add(actor["actor_id"])
            artifact_ids: set[str] = set()
            for artifact in _rows(bundle / "artifacts.jsonl"):
                self.assert_shape(artifact, artifact_schema)
                self.assert_shape(
                    artifact["content"], artifact_schema["properties"]["content"]
                )
                rendering_schema = artifact_schema["properties"]["content"][
                    "properties"
                ]["renderings"]["items"]
                for rendering in artifact["content"].get("renderings", ()):
                    self.assert_shape(rendering, rendering_schema)
                    self.assert_shape(
                        rendering["renderer"],
                        rendering_schema["properties"]["renderer"],
                    )
                self.assert_shape(
                    artifact["provenance"], artifact_schema["properties"]["provenance"]
                )
                self.assert_timestamp(artifact["created_at"])
                self.assert_timestamp(artifact["available_at"])
                self.assertEqual(
                    artifact["checksum"],
                    "sha256:"
                    + hashlib.sha256(artifact["content"]["body"].encode()).hexdigest(),
                )
                self.assertTrue(
                    set(artifact.get("source_actor_ids", ()))
                    | set(artifact.get("recipient_actor_ids", ()))
                    <= actor_ids
                )
                if artifact["visibility"] == "role_scoped":
                    self.assertTrue(artifact.get("visible_roles"))
                    self.assertTrue(
                        set(artifact["visible_roles"]) <= set(generate_module.ROLES)
                    )
                self.assertEqual(
                    (bundle / artifact["content"]["source_uri"]).read_text().rstrip(),
                    artifact["content"]["body"].rstrip(),
                )
                artifact_ids.add(artifact["artifact_id"])
            event_ids: set[str] = set()
            event_times: list[str] = []
            for event in _rows(bundle / "events.jsonl"):
                self.assert_shape(event, event_schema)
                for field in ("effective_at", "recorded_at", "available_at"):
                    self.assert_timestamp(event[field])
                self.assertTrue(set(event["actor_ids"]) <= actor_ids)
                self.assertTrue(set(event.get("artifact_ids", ())) <= artifact_ids)
                if event["visibility"] == "role_scoped":
                    self.assertTrue(event.get("visible_roles"))
                    self.assertTrue(
                        set(event["visible_roles"]) <= set(generate_module.ROLES)
                    )
                event_ids.add(event["event_id"])
                event_times.append(event["available_at"])
            self.assertEqual(event_times, sorted(event_times))
            for checkpoint in _rows(bundle / "checkpoints.jsonl"):
                self.assert_shape(checkpoint, checkpoint_schema)
                for field in (
                    "available_at",
                    "forecast_cutoff_at",
                    "window_start",
                    "window_end",
                ):
                    self.assert_timestamp(checkpoint[field])
                self.assertTrue(set(checkpoint["visible_artifact_ids"]) <= artifact_ids)
                self.assertTrue(set(checkpoint["released_event_ids"]) <= event_ids)
            categories: set[str] = set()
            deterministic_weight = 0.0
            for assertion in _rows(bundle / "assertions.jsonl"):
                self.assert_shape(assertion, assertion_schema)
                self.assert_shape(
                    assertion["target"], assertion_schema["properties"]["target"]
                )
                self.assert_shape(
                    assertion["provenance"],
                    assertion_schema["properties"]["provenance"],
                )
                self.assertTrue(set(assertion["evidence_refs"]) <= artifact_ids)
                categories.add(assertion["category"])
                if assertion["kind"] in {"deterministic", "metric"}:
                    deterministic_weight += assertion["weight"]
            self.assertEqual(categories, set(CANONICAL_CATEGORIES))
            self.assertGreaterEqual(deterministic_weight, 0.75)

    def test_checkpoints_do_not_declare_execution_caps(self) -> None:
        forbidden = {"max_tool_calls", "max_turns"}
        for world in _rows(self.public_roots[0] / "authoring" / "worlds.jsonl"):
            for checkpoint in world.get("checkpoints", ()):
                self.assertFalse(forbidden & checkpoint.keys())
        for bundle, _ in self.bundles():
            for checkpoint in _rows(bundle / "checkpoints.jsonl"):
                self.assertFalse(forbidden & checkpoint.keys())
            reference_trace = bundle / "reference_trace.jsonl"
            if reference_trace.exists():
                self.assertNotIn("budget_exhausted", reference_trace.read_text())

    def test_reference_fixtures_bind_resolved_configuration(self) -> None:
        for bundle, _ in self.bundles():
            reference_trace = bundle / "reference_trace.jsonl"
            if not reference_trace.exists():
                continue
            start = _rows(reference_trace)[0]
            payload = start["payload"]
            self.assertEqual(
                payload["agent_manifest"], generate_module.REFERENCE_AGENT_MANIFEST
            )
            self.assertEqual(payload["limits"], generate_module.REFERENCE_TRACE_LIMITS)
            self.assertEqual(
                payload["configuration_hash"],
                generate_module._reference_configuration_hash(),
            )

    def test_reference_write_results_are_exhaustively_scoped(self) -> None:
        for bundle, _ in self.bundles():
            rows = _rows(bundle / "reference_trace.jsonl")
            calls = {
                row["message_id"]: row for row in rows if row["kind"] == "tool_call"
            }
            for row in rows:
                if row["kind"] != "tool_result" or not row.get("ok"):
                    continue
                call = calls[row["call_id"]]
                if call["tool_name"] not in generate_module.WRITE_TOOLS:
                    continue
                scope = row["result"]["write_scope"]
                related = scope["related_records"]
                classification = scope["classification"]
                self.assertTrue(
                    related
                    and classification is None
                    or not related
                    and classification in engine_module.WRITE_SCOPE_CLASSIFICATIONS
                )

    def test_selected_rich_renderings(self) -> None:
        pdf_mime = "application/pdf"
        xlsx_mime = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        source_assets = ROOT / "benchmarks/v1/authoring/rendering_assets"
        self.assertEqual(len(list(source_assets.glob("*.pdf"))), 2)
        self.assertEqual(len(list(source_assets.glob("*.xlsx"))), 2)
        rendered_bundles: list[Path] = []
        renderings: list[tuple[Path, dict[str, Any], dict[str, Any]]] = []
        for bundle, _ in self.bundles():
            bundle_renderings: list[tuple[dict[str, Any], dict[str, Any]]] = []
            for artifact in _rows(bundle / "artifacts.jsonl"):
                for rendering in artifact["content"].get("renderings", ()):
                    bundle_renderings.append((artifact, rendering))
                    renderings.append((bundle, artifact, rendering))
            if bundle_renderings:
                rendered_bundles.append(bundle)
                self.assertEqual(
                    {
                        (artifact["kind"], item["mime_type"])
                        for artifact, item in bundle_renderings
                    },
                    {("proposal", pdf_mime), ("quote", xlsx_mime)},
                )
        self.assertEqual(len(rendered_bundles), 2)
        self.assertTrue(
            all(bundle.parent.name == "train" for bundle in rendered_bundles)
        )
        pair_ids = {
            json.loads((bundle / "oracle.json").read_text())["scenario_manifest"][
                "pair_id"
            ]
            for bundle in rendered_bundles
        }
        self.assertEqual(len(pair_ids), 1)
        self.assertEqual(
            sum(item["mime_type"] == pdf_mime for _, _, item in renderings), 2
        )
        self.assertEqual(
            sum(item["mime_type"] == xlsx_mime for _, _, item in renderings), 2
        )
        self.assertEqual(len(list(self.official_root.rglob("*.pdf"))), 2)
        self.assertEqual(len(list(self.official_root.rglob("*.xlsx"))), 2)
        for bundle, artifact, rendering in renderings:
            relative = Path(rendering["path"])
            self.assertFalse(relative.is_absolute())
            self.assertNotIn("..", relative.parts)
            path = bundle / relative
            self.assertTrue(path.is_file())
            self.assertEqual(
                path.read_bytes(), (source_assets / path.name).read_bytes()
            )
            self.assertEqual(
                rendering["checksum"],
                "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
            )
            self.assertEqual(
                rendering["normalized_source_uri"],
                artifact["content"]["source_uri"],
            )
            self.assertRegex(
                rendering["renderer"]["configuration_hash"],
                r"^sha256:[0-9a-f]{64}$",
            )
            self.assertRegex(
                rendering["renderer"]["implementation_hash"],
                r"^sha256:[0-9a-f]{64}$",
            )
            if rendering["mime_type"] == pdf_mime:
                self.assertEqual(path.suffix, ".pdf")
                self.assertTrue(path.read_bytes().startswith(b"%PDF-"))
            else:
                self.assertEqual(path.suffix, ".xlsx")
                with zipfile.ZipFile(path) as archive:
                    self.assertIn("xl/workbook.xml", archive.namelist())
                    formulas = "".join(
                        archive.read(name).decode(errors="ignore")
                        for name in archive.namelist()
                        if name.startswith("xl/worksheets/")
                    )
                    self.assertIn("B9*C9", formulas)
                    self.assertIn("SUM(D9:D10)", formulas)
        worlds = [
            generate_module._build_world(0, 0, variant, generate_module.DATASET_SEED)
            for variant in range(2)
        ]
        records = [
            _rows(
                self.official_root
                / "output"
                / "public"
                / "train"
                / world["world_id"]
                / "artifacts.jsonl"
            )
            for world in worlds
        ]
        replacements = [
            generate_module._alpha_replacements(world, rows)
            for world, rows in zip(worlds, records, strict=True)
        ]
        for kind in ("proposal", "quote"):
            items = [
                next(row for row in rows if row["kind"] == kind) for rows in records
            ]
            self.assertEqual(
                generate_module._normalized_artifact(items[0], replacements[0]),
                generate_module._normalized_artifact(items[1], replacements[1]),
            )

    def test_rich_rendering_resources_are_packaged(self) -> None:
        source_assets = ROOT / "benchmarks/v1/authoring/rendering_assets"
        resource_assets = generate_module._PACKAGE_RESOURCES.joinpath(
            "rendering_assets"
        )
        self.assertEqual(
            sorted(path.name for path in resource_assets.iterdir()),
            sorted(path.name for path in source_assets.iterdir()),
        )
        for source in source_assets.iterdir():
            self.assertEqual(
                resource_assets.joinpath(source.name).read_bytes(), source.read_bytes()
            )
        for resource_name, source_name in (
            ("render_pdf.source", "render_pdf.py"),
            ("render_xlsx.source", "render_xlsx.mjs"),
        ):
            self.assertEqual(
                generate_module._renderer_script(resource_name).read_bytes(),
                (ROOT / "scripts" / source_name).read_bytes(),
            )

    def test_channel_counts_templates_and_vertical_language(self) -> None:
        bundle = next(
            self.official_root.joinpath("output", "public", "train").glob(
                "*/manifest.json"
            )
        ).parent
        manifest = json.loads((bundle / "manifest.json").read_text())
        artifacts = _rows(bundle / "artifacts.jsonl")
        by_channel: dict[str, list[dict[str, Any]]] = {
            channel: [] for channel in ARTIFACT_COUNTS
        }
        for artifact in artifacts:
            by_channel[CHANNEL_BY_KIND[artifact["kind"]]].append(artifact)
        world = next(
            row
            for row in _rows(self.official_root / "authoring" / "worlds.jsonl")
            if row["world_id"] == manifest["world_id"]
        )
        self.assertEqual(
            {channel: len(rows) for channel, rows in by_channel.items()},
            world["artifact_counts"],
        )
        for rows in by_channel.values():
            self.assertGreaterEqual(len({row["content"]["body"] for row in rows}), 3)
        bodies = "\n".join(row["content"]["body"] for row in artifacts)
        vertical = next(
            item for item in VERTICALS if item["id"] == manifest["vertical"]
        )
        self.assertIn(vertical["label"], bodies)
        self.assertIn(vertical["motion"], bodies)
        self.assertTrue(
            all(gate.replace("_", " ") in bodies for gate in vertical["gates"])
        )
        crm_fields = {
            json.loads(row["content"]["body"])["observed_field"]
            for row in by_channel["crm"]
            if "observed_field" in json.loads(row["content"]["body"])
        }
        self.assertEqual(crm_fields, {"stage", "close_date", "next_step"})
        calendar_agendas = {
            json.loads(row["content"]["body"])["agenda"].split()[0]
            for row in by_channel["calendar"]
        }
        self.assertEqual(calendar_agendas, {"Review", "Carry", "Confirm"})

    def test_source_registry_and_world_links_are_complete(self) -> None:
        registry = json.loads(
            (self.official_root / "authoring" / "source_registry.json").read_text()
        )
        sources = {source["source_id"]: source for source in registry["sources"]}
        facts = {
            fact_id for source in sources.values() for fact_id in source["fact_ids"]
        }
        self.assertEqual(len(sources), 11)
        self.assertEqual(
            len(facts),
            sum(len(source["fact_ids"]) for source in sources.values()),
        )
        for source in sources.values():
            self.assertTrue(source["publisher"])
            self.assertTrue(source["url"].startswith("https://"))
            self.assertTrue(source["sections"])
            self.assertEqual(source["retrieved_at"], "2026-08-19")
            self.assertIn(
                source["license_classification"],
                {
                    "copyrighted-facts-only",
                    "us-federal-public-domain",
                    "open-government-licence-3.0",
                },
            )
            self.assertTrue(source["allowed_use"])
        for bundle, _ in self.bundles():
            manifest = json.loads((bundle / "manifest.json").read_text())
            provenance = manifest["provenance"]
            self.assertTrue(set(provenance["source_ids"]) <= sources.keys())
            self.assertTrue(set(provenance["fact_ids"]) <= facts)
            for artifact in _rows(bundle / "artifacts.jsonl"):
                self.assertTrue(
                    set(artifact["provenance"]["source_ids"]) <= sources.keys()
                )
                self.assertTrue(set(artifact["provenance"]["fact_ids"]) <= facts)

    def test_source_contract_mutations_fail_closed(self) -> None:
        registry = generate_module._source_registry()
        self.assertEqual(generate_module._validate_source_contract(registry), [])
        self.assertEqual(generate_module._validate_source_evidence(registry), [])
        self.assertEqual(generate_module._validate_attributions(registry), [])
        required = {
            "fact_id",
            "bounded_claim",
            "location",
            "allowed_gates",
            "interpretation_limit",
            "transformation_code",
            "source_version",
            "source_date",
            "license_class",
            "attribution",
        }
        for source in registry["sources"]:
            for claim in source["claims"]:
                self.assertTrue(required <= claim.keys())

        missing_claim = json.loads(json.dumps(registry))
        missing_claim["sources"][0]["claims"][0].pop("bounded_claim")
        self.assertTrue(
            any(
                error.startswith("source_claim_required=")
                for error in generate_module._validate_source_contract(missing_claim)
            )
        )

        unsupported_gate = json.loads(json.dumps(registry))
        unsupported_gate["sources"][0]["gate_fact_ids"]["unsupported_gate"] = [
            unsupported_gate["sources"][0]["fact_ids"][0]
        ]
        self.assertIn(
            "source_gate_vertical=source-neapco-supplier-requirements:unsupported_gate",
            generate_module._validate_source_contract(unsupported_gate),
        )

        changed_digest = json.loads(json.dumps(registry))
        changed_digest["sources"][-1]["retrieval_sha256"] = "sha256:" + "0" * 64
        self.assertTrue(
            any(
                error.startswith("source_evidence_")
                for error in generate_module._validate_source_evidence(changed_digest)
            )
        )

        attribution = generate_module._attributions()
        attribution["attributions"][0].pop("license_url")
        with patch.object(generate_module, "_attributions", return_value=attribution):
            self.assertIn(
                "attribution_required=source-consultancy-playbook:license_url",
                generate_module._validate_attributions(registry),
            )

    def test_checkpoint_objectives_are_keyed_by_gate(self) -> None:
        for vertical in VERTICALS:
            evidence_by_gate = generate_module.VERTICAL_FACTS[vertical["id"]][
                "evidence_by_gate"
            ]
            self.assertEqual(set(vertical["gates"]), set(evidence_by_gate))
            for sequence, gate_id in enumerate(reversed(vertical["gates"])):
                contract = generate_module._checkpoint_contract(
                    vertical, gate_id, sequence
                )
                self.assertNotIn(
                    evidence_by_gate[gate_id], contract["business_objective"]
                )
                self.assertNotIn(
                    evidence_by_gate[gate_id],
                    contract["role_deliverables"]["domain_specialist"],
                )

    def test_agent_checkpoint_contracts_do_not_expose_exact_evidence_phrases(
        self,
    ) -> None:
        fields = (
            "business_objective",
            "decision_condition",
            "role_deliverables",
            "completion_conditions",
        )
        for vertical_index in range(6):
            for family_index in range(6):
                for variant in range(2):
                    world = generate_module._build_world(
                        vertical_index,
                        family_index,
                        variant,
                        generate_module.DATASET_SEED,
                    )
                    artifacts = generate_module._build_artifacts(world)
                    visible, hidden = generate_module._build_events(world, artifacts)
                    checkpoints = generate_module._checkpoint_records(
                        world, artifacts, [*visible, *hidden]
                    )
                    evidence_phrases = generate_module.VERTICAL_FACTS[
                        world["vertical"]
                    ]["evidence_by_gate"].values()
                    for checkpoint in checkpoints:
                        agent_checkpoint = engine_module._agent_checkpoint(checkpoint)
                        self.assertIsNotNone(agent_checkpoint)
                        contract_text = json.dumps(
                            {field: checkpoint[field] for field in fields}
                        )
                        self.assertTrue(
                            all(
                                phrase not in contract_text
                                and phrase not in json.dumps(agent_checkpoint)
                                for phrase in evidence_phrases
                            )
                        )

    def test_routes_sources_language_and_objectives_are_hard_gated(self) -> None:
        route_counts = {count: 0 for count in (6, 7, 8)}
        prose_samples = []
        structured_profiles = {
            vertical["id"]: set() for vertical in generate_module.VERTICALS
        }
        forbidden = {
            "semantic_envelope",
            "purpose_code",
            "decision_codes",
            "commitment_codes",
            "evidence_claims",
            "run.complete_checkpoint",
            "communications.send",
            "documents.create",
            "crm.update",
        }
        for vertical_index in range(6):
            for family_index in range(6):
                for variant in range(2):
                    world = generate_module._build_world(
                        vertical_index,
                        family_index,
                        variant,
                        generate_module.DATASET_SEED,
                    )
                    route_counts[world["checkpoint_count"]] += 1
                    artifacts = generate_module._build_artifacts(world)
                    prose_samples.append((world, artifacts))
                    timing_errors, timing_profiles = generate_module._artifact_timing(
                        world, artifacts
                    )
                    self.assertEqual(timing_errors, [])
                    structured_profiles[world["vertical"]].update(timing_profiles)
                    visible, hidden = generate_module._build_events(world, artifacts)
                    checkpoints = generate_module._checkpoint_records(
                        world, artifacts, [*visible, *hidden]
                    )
                    intervention = checkpoints[world["intervention_sequence"]]
                    objective_text = json.dumps(
                        {
                            key: intervention[key]
                            for key in (
                                "business_objective",
                                "decision_condition",
                                "role_deliverables",
                                "completion_conditions",
                            )
                        }
                    )
                    self.assertFalse(
                        forbidden & set(re.findall(r"[a-z_.]+", objective_text))
                    )
                    self.assertNotIn(world["observable_cure"], objective_text)
                    self.assertNotIn(
                        world["resolution_gate"].replace("_", " "),
                        intervention["business_objective"],
                    )
                    self.assertTrue(
                        generate_module.MANDATORY_GATES[world["vertical"]]
                        <= set(world["gates"])
                    )
                    for checkpoint in world["checkpoints"]:
                        sources = {
                            field: [
                                artifact
                                for artifact in artifacts
                                if artifact["gate_id"] == checkpoint["gate_id"]
                                and artifact.get("projection_origin") is None
                                and f"crm.{field}" in set(artifact["authoritative_for"])
                            ]
                            for field in ("stage", "close_date", "next_step")
                        }
                        projections = [
                            artifact
                            for artifact in artifacts
                            if artifact["gate_id"] == checkpoint["gate_id"]
                            and (artifact.get("projection_origin") or {}).get(
                                "transformation"
                            )
                            == "structured_authority_projection"
                        ]
                        self.assertTrue(
                            all(len(values) == 1 for values in sources.values())
                        )
                        source_ids = {
                            values[0]["artifact_id"] for values in sources.values()
                        }
                        self.assertEqual(len(source_ids), 3)
                        self.assertEqual(len(projections), 1)
                        self.assertEqual(
                            set(projections[0]["derived_from_artifact_ids"]),
                            source_ids,
                        )
                        self.assertNotIn(
                            "current_state", json.dumps(sources, sort_keys=True)
                        )
                    stale = [
                        artifact
                        for artifact in artifacts
                        if (artifact.get("projection_origin") or {}).get(
                            "transformation"
                        )
                        == "crm_projection_from_stale_state"
                    ]
                    self.assertEqual(len(stale), 3)
                    stale_values = {
                        payload["observed_field"]: payload
                        for artifact in stale
                        if isinstance(
                            payload := json.loads(artifact["content"]["body"]),
                            dict,
                        )
                    }
                    for defect in world["defects"]:
                        self.assertEqual(
                            stale_values[defect["field"]][defect["field"]],
                            defect["observed_value"],
                        )
                    if world["causal_family"] == "competition" and variant == 1:
                        self.assertNotIn(
                            generate_module._causal_cure_data(world)["signal"],
                            json.dumps(artifacts),
                        )
        self.assertEqual(route_counts, {6: 18, 7: 32, 8: 22})
        self.assertTrue(
            all(len(profiles) >= 6 for profiles in structured_profiles.values())
        )
        prose_metrics = generate_module._prose_metrics(prose_samples)
        for channel in (
            "transcript",
            "email",
            "internal_chat",
            "document",
            "web_news",
        ):
            self.assertLessEqual(prose_metrics[channel]["modal_share"], 0.02)
            self.assertLessEqual(prose_metrics[channel]["duplicate_share"], 0.55)
            self.assertLessEqual(
                prose_metrics[f"lines:{channel}"]["duplicate_share"], 0.89
            )
        self.assertTrue(
            all(
                metrics["modal_share"] <= 0.07
                for key, metrics in prose_metrics.items()
                if key.startswith("vertical:")
            )
        )

    def test_projection_timing_rejects_broken_origin_lineage(self) -> None:
        world = generate_module._build_world(0, 0, 0, generate_module.DATASET_SEED)
        artifacts = generate_module._build_artifacts(world)
        projection = next(
            artifact for artifact in artifacts if artifact.get("projection_origin")
        )
        artifact_id = projection["artifact_id"]
        prefix = f"{world['world_id']}:{artifact_id}"

        projection["derived_from_artifact_ids"] = ["missing-parent"]
        errors, _ = generate_module._artifact_timing(world, artifacts)
        self.assertIn(f"artifact_lineage_parent={prefix}", errors)

        artifacts = generate_module._build_artifacts(world)
        projection = next(
            artifact for artifact in artifacts if artifact["artifact_id"] == artifact_id
        )
        projection["projection_origin"] = None
        errors, _ = generate_module._artifact_timing(world, artifacts)
        self.assertIn(f"artifact_projection_origin={prefix}", errors)

        artifacts = generate_module._build_artifacts(world)
        projection = next(
            artifact for artifact in artifacts if artifact["artifact_id"] == artifact_id
        )
        projection["projection_origin"]["source_time"] = world["start_at"]
        errors, _ = generate_module._artifact_timing(world, artifacts)
        self.assertIn(f"artifact_projection_time={prefix}", errors)

    def test_duplicate_field_authority_fails_validation(self) -> None:
        world = generate_module._build_world(0, 0, 0, generate_module.DATASET_SEED)
        artifacts = generate_module._build_artifacts(world)
        self.assertEqual(generate_module._crm_authority_errors(world, artifacts), [])
        checkpoint = world["checkpoints"][0]
        source = next(
            artifact
            for artifact in artifacts
            if artifact["gate_id"] == checkpoint["gate_id"]
            and "crm.stage" in artifact["authoritative_for"]
        )
        duplicate = next(
            artifact
            for artifact in artifacts
            if artifact["gate_id"] == checkpoint["gate_id"]
            and artifact["artifact_id"] != source["artifact_id"]
            and artifact.get("projection_origin") is None
        )
        duplicate["authoritative_for"].append("crm.stage")
        duplicate["structured_payload"]["stage"] = checkpoint["gate_id"]
        self.assertIn(
            f"current_crm_authority={world['world_id']}:{checkpoint['gate_id']}:stage",
            generate_module._crm_authority_errors(world, artifacts),
        )

    def test_recoverable_worlds_offer_two_grounded_evidence_strategies(self) -> None:
        for vertical_index in range(6):
            for family_index in range(6):
                world = generate_module._build_world(
                    vertical_index,
                    family_index,
                    0,
                    generate_module.DATASET_SEED,
                )
                artifacts = generate_module._build_artifacts(world)
                cure = generate_module._causal_cure_data(world)
                intervention = world["checkpoints"][world["intervention_sequence"]]
                source_keys = generate_module._structured_causal_source_keys(
                    world, intervention
                )
                structured = [
                    artifact
                    for artifact in artifacts
                    if artifact["artifact_key"] in source_keys
                ]
                generic = [
                    artifact
                    for artifact in artifacts
                    if artifact["gate_id"] == world["intervention_gate"]
                    and "/structured/" not in artifact["content"]["source_uri"]
                    and all(
                        artifact["structured_payload"].get(key) == value
                        for key, value in cure.items()
                    )
                ]
                self.assertEqual(len(structured), 2)
                self.assertEqual(len(generic), 1)
                self.assertTrue(
                    all(
                        any(
                            artifact["structured_payload"].get(key) == value
                            for artifact in structured
                        )
                        for key, value in cure.items()
                    )
                )
                self.assertTrue(
                    all(
                        artifact["structured_payload"].get(key, value) == value
                        for artifact in structured
                        for key, value in cure.items()
                    )
                )

    def test_delayed_recovery_and_fallback_resolve_at_declared_checkpoint(
        self,
    ) -> None:
        worlds = _rows(self.official_root / "authoring" / "worlds.jsonl")
        recoverable = next(
            world
            for world in worlds
            if world["vertical"] == "legal_services"
            and world["causal_family"] == "external_event"
            and world["variant"] == "recoverable"
            and world["resolution_sequence"] > world["intervention_sequence"] + 1
        )
        nonrecoverable = next(
            world
            for world in worlds
            if world["pair_id"] == recoverable["pair_id"]
            and world["variant"] == "terminal"
        )
        bundles = {path.name: path for path, _ in self.bundles()}

        def replay(world: dict[str, Any], rows: list[dict[str, Any]]) -> Any:
            bundle = bundles[world["world_id"]]
            engine = open_world(
                bundle,
                run_id=rows[0]["run_id"],
                agent_manifest=generate_module.REFERENCE_AGENT_MANIFEST,
            )
            self.addCleanup(engine.close)
            result = replay_trace(engine, rows)
            return bundle, engine, result

        success_rows = _rows(bundles[recoverable["world_id"]] / "reference_trace.jsonl")
        success_bundle, success_engine, success_result = replay(
            recoverable, success_rows
        )
        success_branch = success_engine.branch_resolutions()[0]
        success_contract = json.loads((success_bundle / "oracle.json").read_text())[
            "verification_facts"
        ]["branches"][0]
        resolution = _rows(success_bundle / "checkpoints.jsonl")[
            recoverable["resolution_sequence"]
        ]

        def decision_events(engine: Any, artifact_ids: list[str]) -> set[str]:
            selected = set(artifact_ids)
            return {
                str(event_id)
                for event_id, data in engine.connection.execute(
                    "SELECT event_id, data FROM events"
                )
                if selected & set(json.loads(str(data)).get("artifact_ids", ()))
            }

        def applied_events(engine: Any) -> set[str]:
            return {
                str(row[0])
                for row in engine.connection.execute(
                    "SELECT event_id FROM causal_event_applications"
                )
            }

        def assert_decision_events(
            engine: Any, selected_ids: list[str], unselected_ids: list[str]
        ) -> None:
            selected = decision_events(engine, selected_ids)
            unselected = decision_events(engine, unselected_ids)
            applied = applied_events(engine)
            self.assertTrue(selected)
            self.assertTrue(unselected)
            self.assertTrue(selected <= applied)
            self.assertTrue(unselected.isdisjoint(applied))

        self.assertEqual(success_result.status, "completed")
        self.assertEqual(success_branch["option"], "success")
        self.assertEqual(success_branch["resolved_at"], resolution["available_at"])
        assert_decision_events(
            success_engine,
            success_contract["success_decision_artifact_ids"],
            success_contract["fallback_decision_artifact_ids"],
        )

        def recovery_call(row: dict[str, Any]) -> bool:
            arguments = row.get("arguments", {})
            envelope = arguments.get("semantic_envelope", {})
            changes = arguments.get("changes", {})
            return bool(
                row.get("kind") == "tool_call"
                and (
                    envelope.get("purpose_code") in {"share_document", "recover_gate"}
                    or changes.get("next_step_type") == "remediation_decision"
                )
            )

        fallback_rows = [row for row in success_rows if not recovery_call(row)]
        _, fallback_engine, fallback_result = replay(recoverable, fallback_rows)
        fallback_branch = fallback_engine.branch_resolutions()[0]
        self.assertEqual(fallback_result.status, "completed")
        self.assertEqual(fallback_branch["option"], "fallback")
        self.assertEqual(fallback_branch["resolved_at"], resolution["available_at"])
        assert_decision_events(
            fallback_engine,
            success_contract["fallback_decision_artifact_ids"],
            success_contract["success_decision_artifact_ids"],
        )

        nonrecoverable_rows = _rows(
            bundles[nonrecoverable["world_id"]] / "reference_trace.jsonl"
        )
        nonrecoverable_bundle, nonrecoverable_engine, nonrecoverable_result = replay(
            nonrecoverable, nonrecoverable_rows
        )
        nonrecoverable_oracle = json.loads(
            (nonrecoverable_bundle / "oracle.json").read_text()
        )
        nonrecoverable_branch = nonrecoverable_oracle["verification_facts"]["branches"][
            0
        ]
        self.assertEqual(nonrecoverable_result.status, "completed")
        self.assertEqual(
            nonrecoverable_engine.branch_resolutions()[0]["option"], "fallback"
        )
        self.assertEqual(
            nonrecoverable_engine.run_status()["terminal_outcome"],
            nonrecoverable_oracle["scenario_manifest"]["terminal_outcome"],
        )
        assert_decision_events(
            nonrecoverable_engine,
            nonrecoverable_branch["fallback_decision_artifact_ids"],
            nonrecoverable_branch["success_decision_artifact_ids"],
        )

    def test_projection_only_reads_cannot_resolve_a_runtime_milestone(self) -> None:
        bundle = next(path for path, _ in self.bundles())
        checkpoints = _rows(bundle / "checkpoints.jsonl")
        checkpoint = checkpoints[0]
        artifacts = _rows(bundle / "artifacts.jsonl")
        field_sources = {
            field: [
                artifact
                for artifact in artifacts
                if artifact["gate_id"] == checkpoint["gate_id"]
                and artifact.get("projection_origin") is None
                and f"crm.{field}" in artifact["authoritative_for"]
            ]
            for field in ("stage", "close_date", "next_step")
        }
        self.assertTrue(all(len(values) == 1 for values in field_sources.values()))
        self.assertEqual(
            len({values[0]["artifact_id"] for values in field_sources.values()}),
            3,
        )
        oracle = json.loads((bundle / "oracle.json").read_text())["verification_facts"]
        milestone = next(
            value
            for value in oracle["milestones"]
            if value["checkpoint_id"] == checkpoint["checkpoint_id"]
        )
        decision_ids = {
            artifact_id
            for requirement in milestone["authority_requirements"]
            for artifact_id in requirement["decision_artifact_ids"]
        }
        reference = _rows(bundle / "reference_trace.jsonl")
        completion_indexes = [
            index
            for index, row in enumerate(reference)
            if row.get("kind") == "tool_call"
            and row.get("tool_name") == "run.complete_checkpoint"
            and row.get("arguments", {}).get("checkpoint_id")
            == checkpoint["checkpoint_id"]
        ]
        self.assertEqual(len(completion_indexes), len(generate_module.ROLES))
        reference = reference[: completion_indexes[-1] + 1]
        transformed = json.loads(json.dumps(reference))
        field_source_ids = {
            field: values[0]["artifact_id"] for field, values in field_sources.items()
        }
        self.assertIn(field_source_ids["next_step"], decision_ids)
        self.assertNotIn(field_source_ids["stage"], decision_ids)
        self.assertNotIn(field_source_ids["close_date"], decision_ids)
        decision_reads: list[tuple[str, str]] = []
        replaced_roles: set[str] = set()
        replacement_index = 0
        for row in transformed:
            if row.get("kind") != "tool_call" or row.get("tool_name") not in {
                "communications.read",
                "documents.read",
                "web.open",
                "crm.read",
                "crm.history",
            }:
                continue
            identifier = next(
                (
                    str(row["arguments"][key])
                    for key in (
                        "message_id",
                        "document_id",
                        "record_id",
                        "artifact_id",
                    )
                    if row["arguments"].get(key) is not None
                ),
                "",
            )
            if identifier in decision_ids:
                decision_reads.append((str(row["role"]), identifier))
                continue
            row["tool_name"] = (
                "crm.read" if replacement_index % 2 == 0 else "crm.history"
            )
            row["arguments"] = {"record_id": oracle["deal_id"]}
            replaced_roles.add(str(row["role"]))
            replacement_index += 1
        self.assertTrue(decision_reads)
        self.assertGreaterEqual(replacement_index, 2)
        self.assertEqual(
            [
                row
                for row in transformed
                if row.get("kind") == "tool_call"
                and row.get("tool_name") not in {"crm.read", "crm.history"}
            ],
            [
                row
                for row in reference
                if row.get("kind") == "tool_call"
                and not (
                    row.get("tool_name")
                    in {
                        "communications.read",
                        "documents.read",
                        "web.open",
                        "crm.read",
                        "crm.history",
                    }
                    and not any(
                        row.get("arguments", {}).get(key) in decision_ids
                        for key in (
                            "message_id",
                            "document_id",
                            "record_id",
                            "artifact_id",
                        )
                    )
                )
            ],
        )
        engine = open_world(
            bundle,
            track="open_team",
            agent_manifest=generate_module.REFERENCE_AGENT_MANIFEST,
        )
        try:
            replay_trace(engine, transformed)
            reads = engine._successful_evidence_reads()
            for role, artifact_id in decision_reads:
                self.assertIn(artifact_id, reads[role])
            for artifact_id in (
                field_source_ids["stage"],
                field_source_ids["close_date"],
            ):
                self.assertFalse(
                    any(artifact_id in values for values in reads.values())
                )
            missing = {
                role: set(artifact_ids) - reads[role]
                for role, artifact_ids in milestone[
                    "evidence_requirements_by_role"
                ].items()
                if role in replaced_roles and set(artifact_ids) - reads[role]
            }
            self.assertTrue(missing)
            self.assertEqual(
                engine.connection.execute(
                    "SELECT COUNT(*) FROM milestone_resolutions"
                ).fetchone()[0],
                0,
            )
            self.assertIsNone(engine._supported_terminal_outcome())
        finally:
            engine.close()

    def test_recovery_strategies_match_terminal_score_and_effects(self) -> None:
        representatives: dict[str, Path] = {}
        for bundle, _ in self.bundles():
            manifest = json.loads((bundle / "manifest.json").read_text())
            oracle = json.loads((bundle / "oracle.json").read_text())
            branch = oracle["verification_facts"]["branches"][0]
            if (
                oracle["causal_family"] == "requirements_change"
                and branch["recoverable"]
            ):
                representatives.setdefault(manifest["vertical"], bundle)
        self.assertEqual(len(representatives), 6)

        for vertical, bundle in sorted(representatives.items()):
            with self.subTest(vertical=vertical):
                oracle = json.loads((bundle / "oracle.json").read_text())
                facts = oracle["verification_facts"]
                branch = facts["branches"][0]
                rules = [
                    rule
                    for rule in facts["action_effect_rules"]
                    if rule["branch_id"] == branch["branch_id"]
                    and rule["remediation_requirements"] is not None
                ]
                canonical_ids = set(rules[0]["required_evidence_ids"])
                cure = rules[0]["remediation_requirements"]["cure_data"]
                owner = rules[0]["remediation_requirements"]["owner_role"]
                artifacts = _rows(bundle / "artifacts.jsonl")
                action_gate = next(
                    checkpoint["gate_id"]
                    for checkpoint in _rows(bundle / "checkpoints.jsonl")
                    if checkpoint["checkpoint_id"] == branch["action_checkpoint_id"]
                )
                generic = next(
                    artifact
                    for artifact in artifacts
                    if artifact["gate_id"] == action_gate
                    and "/structured/" not in artifact["content"]["source_uri"]
                    and all(
                        artifact["structured_payload"].get(key) == value
                        for key, value in cure.items()
                    )
                )
                generic_id = generic["artifact_id"]
                strategy_a = _rows(bundle / "reference_trace.jsonl")
                read_tool, identifier_field = {
                    "email": ("communications.read", "message_id"),
                    "call_transcript": ("communications.read", "message_id"),
                    "internal_chat": ("communications.read", "message_id"),
                    "proposal": ("documents.read", "document_id"),
                    "quote": ("documents.read", "document_id"),
                    "contract": ("documents.read", "document_id"),
                    "diligence_document": ("documents.read", "document_id"),
                    "policy_document": ("documents.read", "document_id"),
                    "web_page": ("web.open", "record_id"),
                    "news_item": ("web.open", "record_id"),
                }[generic["kind"]]
                canonical_owner_reads = {
                    next(
                        (
                            row.get("arguments", {}).get(key)
                            for key in ("message_id", "document_id", "record_id")
                            if row.get("arguments", {}).get(key) is not None
                        ),
                        None,
                    )
                    for row in strategy_a
                    if row.get("kind") == "tool_call"
                    and row.get("role") == owner
                    and row.get("tool_name")
                    in {"communications.read", "documents.read", "web.open"}
                }
                self.assertEqual(canonical_ids & canonical_owner_reads, canonical_ids)
                self.assertTrue(
                    all(
                        artifact["gate_id"] == action_gate
                        for artifact in artifacts
                        if artifact["artifact_id"] in canonical_ids | {generic_id}
                    )
                )
                self.assertFalse(canonical_ids <= {generic_id})
                self.assertFalse({generic_id} <= canonical_ids)
                plan_index = next(
                    index
                    for index, row in enumerate(strategy_a)
                    if row.get("kind") == "tool_call"
                    and row.get("tool_name") == "documents.create"
                    and row.get("arguments", {}).get("kind") == "remediation_plan"
                )
                anchor = strategy_a[plan_index]
                call_id = generate_module._opaque_id(
                    "message", oracle["world_id"], "strategy-a-read", owner
                )
                strategy_a[plan_index:plan_index] = [
                    {
                        "protocol_version": "v1.0.0",
                        "run_id": anchor["run_id"],
                        "sequence": 0,
                        "message_id": call_id,
                        "occurred_at": anchor["occurred_at"],
                        "kind": "tool_call",
                        "role": owner,
                        "tool_name": read_tool,
                        "arguments": {identifier_field: generic_id},
                        "observation_token": anchor.get("observation_token"),
                    },
                    {
                        "protocol_version": "v1.0.0",
                        "run_id": anchor["run_id"],
                        "sequence": 0,
                        "message_id": generate_module._opaque_id(
                            "message",
                            oracle["world_id"],
                            "strategy-a-result",
                            owner,
                        ),
                        "occurred_at": anchor["occurred_at"],
                        "kind": "tool_result",
                        "role": owner,
                        "call_id": call_id,
                        "ok": True,
                        "result": {},
                    },
                ]

                for row in strategy_a:
                    if row.get("kind") != "tool_call":
                        continue
                    arguments = row.get("arguments", {})
                    envelope = arguments.get("semantic_envelope")
                    if not isinstance(envelope, dict):
                        continue
                    if (
                        arguments.get("kind") == "remediation_plan"
                        or envelope.get("purpose_code") == "recover_gate"
                    ):
                        attachments = envelope.get("attachments", [])
                        envelope["attachments"] = [
                            value for value in attachments if value not in canonical_ids
                        ] + [generic_id]
                        claims = envelope.get("evidence_claims", [])
                        removed = [
                            claim
                            for claim in claims
                            if claim["artifact_id"] in canonical_ids
                        ]
                        envelope["evidence_claims"] = [
                            claim
                            for claim in claims
                            if claim["artifact_id"] not in canonical_ids
                        ] + [{**removed[0], "artifact_id": generic_id}]
                selected_basis = {
                    claim["artifact_id"]
                    for row in strategy_a
                    if row.get("kind") == "tool_call"
                    and row.get("arguments", {}).get("kind") == "remediation_plan"
                    for claim in row["arguments"]["semantic_envelope"][
                        "evidence_claims"
                    ]
                }
                self.assertEqual(selected_basis, {generic_id})
                for sequence, row in enumerate(strategy_a):
                    row["sequence"] = sequence

                outcomes = []
                scores = []
                effect_sets = []
                for trace in (
                    bundle / "reference_trace.jsonl",
                    strategy_a,
                ):
                    engine = open_world(
                        bundle,
                        agent_manifest=generate_module.REFERENCE_AGENT_MANIFEST,
                    )
                    try:
                        result = replay_trace(engine, trace)
                        score = grade_run(
                            engine,
                            bundle / "rubric.json",
                            oracle=bundle / "oracle.json",
                        )
                        effects = set()
                        for row in engine.connection.execute(
                            "SELECT effects FROM causal_action_applications"
                        ):
                            effects.update(json.loads(str(row[0])))
                        self.assertEqual(result.status, "completed")
                        self.assertTrue(score["strict_cycle_pass"])
                        outcomes.append(engine.run_status()["terminal_outcome"])
                        scores.append(score["execution_index"])
                        effect_sets.append(effects)
                    finally:
                        engine.close()
                self.assertEqual(
                    outcomes, [oracle["scenario_manifest"]["terminal_outcome"]] * 2
                )
                self.assertEqual(scores[0], scores[1])
                self.assertEqual(effect_sets[0], effect_sets[1])

    def test_hidden_influence_symptoms_do_not_form_a_cure_basis(self) -> None:
        bundle = next(
            bundle
            for bundle, _ in self.bundles()
            if json.loads((bundle / "oracle.json").read_text())["variant"]
            == "hidden_influence"
        )
        oracle = json.loads((bundle / "oracle.json").read_text())
        facts = oracle["verification_facts"]
        rule = next(
            rule
            for rule in facts["action_effect_rules"]
            if rule["remediation_requirements"] is not None
        )
        remediation = rule["remediation_requirements"]
        branch = facts["branches"][0]
        action_checkpoint = next(
            checkpoint
            for checkpoint in _rows(bundle / "checkpoints.jsonl")
            if checkpoint["checkpoint_id"] == branch["action_checkpoint_id"]
        )
        action_gate = action_checkpoint["gate_id"]
        symptom = next(
            artifact
            for artifact in _rows(bundle / "artifacts.jsonl")
            if artifact["gate_id"] == action_gate
            and artifact["structured_payload"].get("evaluation_status")
            == "ranking_changed"
            and artifact["kind"] != "internal_chat"
        )
        read_tool, identifier_field = {
            "email": ("communications.read", "message_id"),
            "call_transcript": ("communications.read", "message_id"),
            "proposal": ("documents.read", "document_id"),
            "quote": ("documents.read", "document_id"),
            "contract": ("documents.read", "document_id"),
            "diligence_document": ("documents.read", "document_id"),
            "policy_document": ("documents.read", "document_id"),
            "web_page": ("web.open", "record_id"),
            "news_item": ("web.open", "record_id"),
        }[symptom["kind"]]
        engine = open_world(
            bundle,
            agent_manifest=generate_module.REFERENCE_AGENT_MANIFEST,
        )
        try:
            runner_module._activate_first(engine)
            while engine.current_checkpoint_index < action_checkpoint["sequence"]:
                runner_module._advance(
                    engine,
                    True,
                    f"hidden-advance-{engine.current_checkpoint_index}",
                )
            result = tools_module.ToolDispatcher(engine).dispatch(
                protocol_module.ToolCall(
                    "hidden-symptom-read",
                    read_tool,
                    remediation["owner_role"],
                    {identifier_field: symptom["artifact_id"]},
                )
            )
            self.assertTrue(result.ok, result.error)
            with self.assertRaises(engine_module.EngineError):
                engine._remediation_evidence_basis(
                    remediation["owner_role"],
                    rule["gate_id"],
                    remediation["cure_data"],
                    (symptom["artifact_id"],),
                )
        finally:
            engine.close()

    def test_source_locations_and_bounds_are_exact(self) -> None:
        registry = generate_module._source_registry()
        claims = {
            claim["fact_id"]: claim
            for source in registry["sources"]
            for claim in source["claims"]
        }
        expected = {
            "manufacturing-apqp-ppap": ([6, 7, 8], [6, 7, 8]),
            "manufacturing-capacity-validation": ([8, 9, 10], [8, 9, 10]),
            "construction-cmgc-value-engineering": ("unpaginated", 5),
            "construction-solicitation-addenda": ("15.2-5", 439),
            "construction-best-value-award": (
                ["15.3-1", "15.3-5"],
                [443, 447],
            ),
            "construction-performance-payment-bonds": (
                ["28.1-3", "28.1-4"],
                [877, 878],
            ),
            "construction-site-visit": ("36.5-4", 1110),
            "consulting-evaluation-pricing": ([23, 29], [25, 31]),
            "consulting-knowledge-transfer": ([36, 41], [38, 43]),
            "legal-mini-rfi": ("unpaginated", 3),
            "legal-conflicts": ([10, 11], [20, 21]),
            "legal-confidentiality": ([7, 8], [17, 18]),
            "banking-lending-authority": (
                [16, 17, 72, 76],
                [19, 20, 75, 79],
            ),
            "banking-underwriting-repayment": (
                [21, 32, 33, 79],
                [24, 35, 36, 82],
            ),
            "banking-credit-separation": ([20, 21, 72], [23, 24, 75]),
            "banking-exception-approval": ([26, 27], [29, 30]),
            "banking-preclosing-documentation": (26, 29),
        }
        for fact_id, (printed_page, physical_page) in expected.items():
            self.assertEqual(claims[fact_id]["location"]["printed_page"], printed_page)
            self.assertEqual(
                claims[fact_id]["location"]["physical_page"], physical_page
            )
        blueprints = json.dumps(generate_module.VERTICAL_BLUEPRINTS, sort_keys=True)
        for unsupported in (
            "bonding_and_safety",
            "safety_status",
            "legal-confidentiality-info-security",
            "security_clearance",
            "privilege_review",
            "consulting-staffing-data",
            "named_roles",
            "data_access",
            "committee_status",
            "committee_memo",
            "committee_minutes",
            "funding_confirmation",
            "disbursement_status",
        ):
            self.assertNotIn(unsupported, blueprints)
        construction = next(
            vertical for vertical in VERTICALS if vertical["id"] == "construction"
        )
        self.assertEqual(construction["procurement_scope"], "federal")
        self.assertEqual(construction["project_sector"], "public_transportation")
        self.assertIn("federal", construction["jurisdiction"].lower())
        consulting_policy = json.dumps(
            generate_module.POLICY_CONTROLS["consulting"], sort_keys=True
        ).lower()
        self.assertNotIn("named staffing", consulting_policy)
        self.assertNotIn("statement of work", consulting_policy)
        banking_policy = json.dumps(
            generate_module.POLICY_CONTROLS["corporate_banking"], sort_keys=True
        ).lower()
        self.assertNotIn("committee", banking_policy)

    def test_dated_sources_predate_their_worlds(self) -> None:
        earliest: dict[str, datetime] = {}
        for bundle, _ in self.bundles():
            manifest = json.loads((bundle / "manifest.json").read_text())
            first = min(
                _rows(bundle / "checkpoints.jsonl"), key=lambda row: row["sequence"]
            )
            timestamp = datetime.fromisoformat(first["available_at"])
            current = earliest.get(manifest["vertical"])
            if current is None or timestamp < current:
                earliest[manifest["vertical"]] = timestamp
        for source in generate_module._source_registry()["sources"]:
            version = source["version_date"]
            if not re.fullmatch(r"\d{4}-\d{2}(?:-\d{2})?", version):
                continue
            published = datetime.fromisoformat(
                version if len(version) == 10 else f"{version}-01"
            )
            self.assertLessEqual(
                published.date(),
                earliest[source["vertical"]].date(),
                source["source_id"],
            )

    def test_hash_only_evidence_fails_closed(self) -> None:
        registry = generate_module._source_registry()
        hash_only = {
            source["source_id"]: source
            for source in registry["sources"]
            if source["retrieval_method"] == "verified_official_hash_only"
        }
        self.assertEqual(
            set(hash_only), {source["source_id"] for source in registry["sources"]}
        )
        for source in hash_only.values():
            self.assertNotIn("evidence_path", source)
            self.assertGreater(source["retrieval_bytes"], 0)
        changed = json.loads(json.dumps(registry))
        source = next(
            item
            for item in changed["sources"]
            if item["source_id"] == "source-calbar-rules-2018"
        )
        source["evidence_path"] = "source_evidence/calbar-rules.pdf"
        errors = generate_module._validate_source_evidence(changed)
        self.assertIn(
            "source_evidence_copyrighted_bytes=source-calbar-rules-2018", errors
        )
        self.assertIn("source_evidence_hash_only_path=source-calbar-rules-2018", errors)

        manifest = generate_module._source_evidence_manifest()
        self.assertEqual(
            {row["source_id"] for row in manifest["evidence"]}, set(hash_only)
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            evidence = root / "source_evidence"
            evidence.mkdir()
            changed_manifest = json.loads(json.dumps(manifest))
            changed_manifest["evidence"].pop()
            (evidence / "manifest.json").write_text(json.dumps(changed_manifest))
            self.assertTrue(
                any(
                    error.startswith("source_evidence_manifest_entry=")
                    for error in generate_module._validate_source_evidence(
                        registry, root
                    )
                )
            )

    def test_source_verifier_streams_and_rejects_manifest_drift(self) -> None:
        registry_path = ROOT / "src/edlb/resources/source_registry.json"
        manifest_path = ROOT / "src/edlb/resources/source_evidence/manifest.json"
        records = list(source_verifier.source_records(registry_path, manifest_path))
        self.assertEqual(len(records), 11)
        payload = b"source bytes"
        calls = []

        def opener(request: object, **options: object) -> io.BytesIO:
            calls.append((request, options))
            return io.BytesIO(payload)

        size, digest = source_verifier.fetch_digest(
            "https://example.invalid/source", None, opener
        )
        self.assertEqual(size, len(payload))
        self.assertEqual(digest, hashlib.sha256(payload).hexdigest())
        self.assertEqual(calls[0][1], {})
        source_verifier.fetch_digest("https://example.invalid/source", 3.0, opener)
        self.assertEqual(calls[1][1], {"timeout": 3.0})
        with tempfile.TemporaryDirectory() as temp_dir:
            changed = json.loads(manifest_path.read_text())
            changed["evidence"][0]["bytes"] += 1
            changed_path = Path(temp_dir) / "manifest.json"
            changed_path.write_text(json.dumps(changed))
            with self.assertRaisesRegex(ValueError, "metadata mismatch"):
                list(source_verifier.source_records(registry_path, changed_path))

    def test_every_vertical_declares_narrow_modeled_applicability(self) -> None:
        expected = {
            "manufacturing": "Synthetic supplier lifecycle modeled on Neapco's company-specific process, not an industry requirement",
            "construction": "Synthetic composite of a direct United States federal FAR construction acquisition and nonbinding public-transportation CM/GC practice, not an FTA grant-recipient regime or unified legal framework",
            "commercial_insurance": "Synthetic London Market placement workflow modeled on Lloyd's January 2023 digital placement journey, not universal insurance law",
            "consulting": "Synthetic United Kingdom central-government consultancy procurement workflow under Playbook guidance",
            "legal_services": "Synthetic California engagement workflow combining California professional-conduct rules with GSK-specific sourcing practice",
            "corporate_banking": "Synthetic OCC-supervised United States national-bank lending workflow using contemporaneous OCC and BSA/AML guidance",
        }
        self.assertEqual(
            {vertical["id"]: vertical["jurisdiction"] for vertical in VERTICALS},
            expected,
        )
        for bundle, _ in self.bundles():
            manifest = json.loads((bundle / "manifest.json").read_text())
            self.assertEqual(manifest["jurisdiction"], expected[manifest["vertical"]])

    def test_generated_manifest_and_docs_counts_match(self) -> None:
        summary = self.official_summary
        expected_range = (
            f"{summary['artifact_count_min']} to {summary['artifact_count_max']}"
        )
        expected_total = f"{summary['artifact_total']:,}"
        for document in (
            ROOT / "README.md",
            ROOT / "DATA_CARD.md",
            ROOT / "BENCHMARK_CARD.md",
        ):
            text = " ".join(document.read_text(encoding="utf-8").split())
            self.assertIn(expected_range, text)
            self.assertIn(expected_total, text)
        manifest = json.loads(
            (self.official_root / "output" / "manifest.json").read_text()
        )
        validation = json.loads(
            (self.official_root / "authoring" / "validation.json").read_text()
        )
        self.assertEqual(manifest["world_count"], summary["world_count"])
        self.assertEqual(manifest["artifact_total"], summary["artifact_total"])
        self.assertEqual(validation["artifact_total"], summary["artifact_total"])

    def test_realism_invariants(self) -> None:
        worlds = _rows(self.official_root / "authoring" / "worlds.jsonl")
        signatures = {
            tuple(sorted(world["artifact_counts"].items())) for world in worlds
        }
        self.assertGreaterEqual(len(signatures), 12)
        owners: dict[str, set[str]] = {}
        for world in worlds:
            for actor in world["actors"]:
                owners.setdefault(actor["display_name"], set()).add(world["pair_id"])
        self.assertTrue(all(len(pair_ids) == 1 for pair_ids in owners.values()))
        for world in worlds:
            self.assertTrue(
                all(
                    datetime.fromisoformat(checkpoint["date"]).weekday() < 5
                    for checkpoint in world["checkpoints"]
                )
            )
            bundle = (
                self.official_root
                / "output"
                / "public"
                / world["split"]
                / world["world_id"]
            )
            artifacts = _rows(bundle / "artifacts.jsonl")
            actors_by_id = {actor["actor_id"]: actor for actor in world["actors"]}
            for artifact in artifacts:
                for actor_id in [
                    *artifact.get("source_actor_ids", ()),
                    *artifact.get("recipient_actor_ids", ()),
                ]:
                    actor = actors_by_id[actor_id]
                    self.assertLessEqual(actor["active_from"], artifact["available_at"])
                    if actor.get("active_until"):
                        self.assertLess(artifact["available_at"], actor["active_until"])
            times = {artifact["available_at"][11:16] for artifact in artifacts}
            self.assertGreaterEqual(len(times), 12)
            checkpoints = _rows(bundle / "checkpoints.jsonl")
            for checkpoint in checkpoints:
                self.assertEqual(
                    set(checkpoint["role_deliverables"]),
                    set(generate_module.ROLES),
                )
                for field in (
                    "visible_gate",
                    "business_objective",
                    "decision_condition",
                    "completion_conditions",
                    "policy_entrypoints",
                ):
                    self.assertTrue(checkpoint[field])
            departed = next(
                (
                    actor
                    for actor in world["actors"]
                    if actor["role_tags"] == ["champion"] and actor.get("active_until")
                ),
                None,
            )
            if departed:
                for artifact in artifacts:
                    if artifact["available_at"] > departed["active_until"]:
                        participants = set(artifact.get("source_actor_ids", ())) | set(
                            artifact.get("recipient_actor_ids", ())
                        )
                        self.assertNotIn(departed["actor_id"], participants)

    def test_causal_windows_and_family_contracts_are_distinct(self) -> None:
        expected_actions = {
            "champion_exit": "successor_handoff",
            "late_stakeholder": "late_authority_decision",
            "budget_shock": "authorized_budget_path",
            "requirements_change": "versioned_requirement_revalidation",
            "competition": "buyer_owned_competitive_evaluation",
            "external_event": "authorized_restart_or_workaround",
        }
        external_fact_signatures: set[tuple[tuple[str, object], ...]] = set()
        for vertical_index, vertical in enumerate(generate_module.VERTICALS):
            signatures = set()
            for family_index, family in enumerate(generate_module.FAMILIES):
                pair = [
                    generate_module._build_world(
                        vertical_index,
                        family_index,
                        variant,
                        generate_module.DATASET_SEED,
                    )
                    for variant in range(2)
                ]
                left, right = pair
                signature = (
                    left["intervention_gate"],
                    left["resolution_gate"],
                    left["causal_action_code"],
                )
                signatures.add(signature)
                self.assertEqual(
                    signature,
                    (
                        right["intervention_gate"],
                        right["resolution_gate"],
                        right["causal_action_code"],
                    ),
                )
                self.assertEqual(left["causal_action_code"], expected_actions[family])
                self.assertLess(
                    left["intervention_sequence"], left["resolution_sequence"]
                )
                self.assertIn(left["checkpoint_count"], (6, 7, 8))
                self.assertEqual(left["checkpoint_count"], len(left["gates"]))
                self.assertGreaterEqual(left["duration_days"], 180)
                self.assertLessEqual(left["duration_days"], 365)
                self.assertEqual(left["release_visibility"], "public")
                artifacts = generate_module._build_artifacts(left)
                facts = generate_module._verification_facts(left, artifacts)
                rules = [
                    rule
                    for rule in facts["action_effect_rules"]
                    if rule["fact_type"] == "authority_decision_observed"
                ]
                self.assertTrue(rules)
                cure_data = generate_module._causal_cure_data(left)
                self.assertTrue(
                    all(
                        rule["remediation_requirements"]["action_code"]
                        == expected_actions[family]
                        and rule["remediation_requirements"]["cure_data"] == cure_data
                        and rule["remediation_requirements"]["owner_role"]
                        == left["causal_owner_role"]
                        for rule in rules
                    )
                )
                evidence_ids = set(rules[0]["required_evidence_ids"])
                evidence = [
                    artifact
                    for artifact in artifacts
                    if artifact["artifact_id"] in evidence_ids
                ]
                self.assertTrue(evidence)
                self.assertTrue(
                    all(
                        any(
                            artifact["structured_payload"].get(key) == value
                            for artifact in evidence
                        )
                        for key, value in cure_data.items()
                    )
                )
                self.assertTrue(
                    all(
                        artifact["structured_payload"].get(key, value) == value
                        for artifact in evidence
                        for key, value in cure_data.items()
                    )
                )
                if family == "external_event":
                    for candidate in pair:
                        candidate_artifacts = generate_module._build_artifacts(
                            candidate
                        )
                        visible, hidden = generate_module._build_events(
                            candidate, candidate_artifacts
                        )
                        helper = generate_module._vertical_causal_facts(
                            candidate["vertical"],
                            family,
                            candidate["variant"],
                            include_source=True,
                        )
                        cure = generate_module._causal_cure_data(candidate)
                        self.assertEqual(
                            cure,
                            {
                                key: value
                                for key, value in helper.items()
                                if key != "source"
                            },
                        )
                        observable = next(
                            event
                            for event in visible
                            if event["kind"] in {"message_sent", "document_revised"}
                            and all(
                                event["payload"].get(key) == value
                                for key, value in helper.items()
                            )
                        )
                        self.assertTrue(
                            all(
                                observable["payload"].get(key) == value
                                for key, value in helper.items()
                            )
                        )
                        self.assertTrue(
                            generate_module._event_contract_valid(
                                candidate, visible, hidden
                            )
                        )
                        external_fact_signatures.add(tuple(sorted(cure.items())))
                if family == "champion_exit":
                    champion = next(
                        actor
                        for actor in left["actors"]
                        if actor["role_tags"] == ["champion"]
                    )
                    self.assertLess(
                        champion["active_until"],
                        left["checkpoints"][left["intervention_sequence"]][
                            "available_at"
                        ],
                    )
                if family == "late_stakeholder":
                    late = next(
                        actor
                        for actor in left["actors"]
                        if actor["authority"]["role_id"] == "buyer.executive_sponsor"
                    )
                    self.assertEqual(late["active_from"], left["late_activation_at"])
                    self.assertIn(
                        late["actor_id"], {rule["authority_actor_id"] for rule in rules}
                    )
            self.assertEqual(len(signatures), 6)
        self.assertEqual(len(external_fact_signatures), 12)
        insurance = generate_module._build_world(2, 4, 0, generate_module.DATASET_SEED)
        insurance_checkpoint = next(
            value for value in insurance["checkpoints"] if value["gate_id"] == "binding"
        )
        insurance_roles = set(
            generate_module._checkpoint_authority_role_ids(
                insurance, insurance_checkpoint
            )
        )
        self.assertTrue(
            {
                "insurance.binding_authority",
                "insurance.client_authority",
                "insurance.broker_authority",
            }
            <= insurance_roles
        )
        banking = generate_module._build_world(5, 4, 0, generate_module.DATASET_SEED)
        banking_checkpoint = next(
            value
            for value in banking["checkpoints"]
            if value["gate_id"] == "credit_approval"
        )
        banking_roles = set(
            generate_module._checkpoint_authority_role_ids(banking, banking_checkpoint)
        )
        self.assertIn("bank.credit_authority", banking_roles)

    def test_checkpoint_contract_reaches_both_tracks(self) -> None:
        bundle = next(
            self.official_root.joinpath("output", "public", "train").glob("world-*")
        )
        fields = {
            "visible_gate",
            "business_objective",
            "decision_condition",
            "role_deliverables",
            "completion_conditions",
            "policy_entrypoints",
        }
        for track, manifest in (
            ("open_team", None),
            ("fixed_harness", generate_module.REFERENCE_AGENT_MANIFEST),
        ):
            engine = open_world(bundle, track=track, agent_manifest=manifest)
            try:
                runner_module._activate_first(engine)
                payload = runner_module._start_payload(
                    engine, runner_module.RunLimits()
                )
                self.assertTrue(
                    (fields - {"decision_condition"}) <= payload["checkpoint"].keys()
                )
            finally:
                engine.close()

    def test_counterfactual_pair_diff_is_declared_descendant_only(self) -> None:
        for vertical_index in range(6):
            for family_index in range(6):
                seed = generate_module.DATASET_SEED
                left = generate_module._build_world(
                    vertical_index, family_index, 0, seed
                )
                right = generate_module._build_world(
                    vertical_index, family_index, 1, seed
                )
                self.assertNotEqual(left["world_id"], right["world_id"])
                self.assertEqual(left["buyer_name"], right["buyer_name"])
                self.assertEqual(left["buyer_domain"], right["buyer_domain"])
                self.assertEqual(left["buyer_org_id"], right["buyer_org_id"])
                self.assertEqual(
                    {actor["actor_id"] for actor in left["actors"]},
                    {actor["actor_id"] for actor in right["actors"]},
                )
                diff = pair_diff(left, right)
                self.assertTrue(diff["base_facts_equal"])
                self.assertTrue(diff["pre_intervention_artifacts_equal"])
                self.assertGreater(diff["post_intervention_artifact_differences"], 0)
                self.assertTrue(
                    diff["post_intervention_changes_are_declared_descendants"]
                )
                self.assertTrue(diff["causal_event_graph_valid"])
                for field in (
                    "action_contracts_isomorphic",
                    "branch_contracts_isomorphic",
                    "milestone_contracts_isomorphic",
                    "pre_intervention_events_equal",
                    "pre_intervention_hidden_events_equal",
                    "reference_trace_causal_material_isomorphic",
                    "selected_evidence_contracts_isomorphic",
                    "terminal_mappings_isomorphic",
                ):
                    self.assertTrue(diff[field], (diff["pair_id"], field))
                self.assertEqual(
                    diff["allowed_differences"],
                    [
                        "opaque_public_identity_projection",
                        "declared_intervention",
                        "causal_descendants_after_intervention",
                    ],
                )

        left = generate_module._build_world(0, 0, 0, generate_module.DATASET_SEED)
        right = generate_module._build_world(0, 0, 1, generate_module.DATASET_SEED)
        original = generate_module._build_events

        def remove_descendant_edges(
            world: dict[str, Any], artifacts: list[dict[str, Any]]
        ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
            visible, hidden = original(world, artifacts)
            for event in visible:
                if event["artifact_ids"] and event["causal_parent_ids"]:
                    event["causal_parent_ids"] = []
            return visible, hidden

        with patch.object(
            generate_module, "_build_events", side_effect=remove_descendant_edges
        ):
            invalid = pair_diff(left, right)
        self.assertFalse(invalid["post_intervention_changes_are_declared_descendants"])

    def test_counterfactual_pair_verifier_rejects_oracle_and_trace_mutations(
        self,
    ) -> None:
        left = generate_module._build_world(0, 0, 0, generate_module.DATASET_SEED)
        right = generate_module._build_world(0, 0, 1, generate_module.DATASET_SEED)
        original_events = generate_module._build_events

        def mutate_hidden_event(
            world: dict[str, Any], artifacts: list[dict[str, Any]]
        ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
            visible, hidden = original_events(world, artifacts)
            if world["variant_index"] == 1:
                hidden[-2]["payload"]["description"] = "unattributed change"
            return visible, hidden

        with patch.object(
            generate_module, "_build_events", side_effect=mutate_hidden_event
        ):
            invalid = pair_diff(left, right)
        self.assertFalse(invalid["post_intervention_changes_are_declared_descendants"])

        def mutate_visible_event(
            world: dict[str, Any], artifacts: list[dict[str, Any]]
        ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
            visible, hidden = original_events(world, artifacts)
            if world["variant_index"] == 1:
                event_id = generate_module._opaque_id(
                    "event", world["world_id"], "observable-intervention"
                )
                event = next(
                    value for value in visible if value["event_id"] == event_id
                )
                event["payload"]["source"] = "unattributed change"
            return visible, hidden

        with patch.object(
            generate_module, "_build_events", side_effect=mutate_visible_event
        ):
            invalid = pair_diff(left, right)
        self.assertFalse(invalid["post_intervention_changes_are_declared_descendants"])

        original_facts = generate_module._verification_facts

        def mutate_terminal_mapping(
            world: dict[str, Any], artifacts: list[dict[str, Any]]
        ) -> dict[str, Any]:
            facts = json.loads(json.dumps(original_facts(world, artifacts)))
            if world["variant_index"] == 1:
                remedy_id = facts["branches"][0]["remedy_milestone_id"]
                remedy = next(
                    value
                    for value in facts["milestones"]
                    if value["milestone_id"] == remedy_id
                )
                remedy["terminal_outcome_by_resolution"] = {}
            return facts

        with patch.object(
            generate_module, "_verification_facts", side_effect=mutate_terminal_mapping
        ):
            invalid = pair_diff(left, right)
        self.assertFalse(invalid["terminal_mappings_isomorphic"])

        def mutate_selected_evidence(
            world: dict[str, Any], artifacts: list[dict[str, Any]]
        ) -> dict[str, Any]:
            facts = json.loads(json.dumps(original_facts(world, artifacts)))
            if world["variant_index"] == 1:
                branch = facts["branches"][0]
                branch["fallback_decision_artifact_ids"] = list(
                    branch["success_decision_artifact_ids"]
                )
            return facts

        with patch.object(
            generate_module,
            "_verification_facts",
            side_effect=mutate_selected_evidence,
        ):
            invalid = pair_diff(left, right)
        self.assertFalse(invalid["selected_evidence_contracts_isomorphic"])

        def mutate_milestone_evidence(
            world: dict[str, Any], artifacts: list[dict[str, Any]]
        ) -> dict[str, Any]:
            facts = json.loads(json.dumps(original_facts(world, artifacts)))
            if world["variant_index"] == 1:
                branch = facts["branches"][0]
                remedy = next(
                    value
                    for value in facts["milestones"]
                    if value["milestone_id"] == branch["remedy_milestone_id"]
                )
                extra = next(
                    artifact["artifact_id"]
                    for artifact in artifacts
                    if artifact["artifact_id"] not in remedy["evidence_ids"]
                )
                remedy["evidence_requirements_by_role"]["domain_specialist"].append(
                    extra
                )
            return facts

        with patch.object(
            generate_module,
            "_verification_facts",
            side_effect=mutate_milestone_evidence,
        ):
            invalid = pair_diff(left, right)
        self.assertFalse(invalid["selected_evidence_contracts_isomorphic"])

        def mutate_action_evidence(
            world: dict[str, Any], artifacts: list[dict[str, Any]]
        ) -> dict[str, Any]:
            facts = json.loads(json.dumps(original_facts(world, artifacts)))
            if world["variant_index"] == 1:
                rule = next(
                    value
                    for value in facts["action_effect_rules"]
                    if value["fact_type"] == "authority_decision_observed"
                )
                rule["required_evidence_ids"][-1] = next(
                    artifact["artifact_id"]
                    for artifact in artifacts
                    if artifact["artifact_id"] not in rule["required_evidence_ids"]
                )
            return facts

        with patch.object(
            generate_module,
            "_verification_facts",
            side_effect=mutate_action_evidence,
        ):
            invalid = pair_diff(left, right)
        self.assertFalse(invalid["branch_contracts_isomorphic"])

        def mutate_success_option(
            world: dict[str, Any], artifacts: list[dict[str, Any]]
        ) -> dict[str, Any]:
            facts = json.loads(json.dumps(original_facts(world, artifacts)))
            if world["variant_index"] == 1:
                facts["branches"][0]["success_if_any"][0].pop()
            return facts

        with patch.object(
            generate_module,
            "_verification_facts",
            side_effect=mutate_success_option,
        ):
            invalid = pair_diff(left, right)
        self.assertFalse(invalid["branch_contracts_isomorphic"])

        original_trace = generate_module._build_reference_trace

        def mutate_reference_trace(
            world: dict[str, Any],
            artifacts: list[dict[str, Any]],
            scenario_hash: str,
        ) -> list[dict[str, Any]]:
            trace = original_trace(world, artifacts, scenario_hash)
            if world["variant_index"] == 1:
                call = next(
                    row
                    for row in trace
                    if row.get("kind") == "tool_call"
                    and row.get("tool_name") == "communications.send"
                    and row.get("arguments", {})
                    .get("semantic_envelope", {})
                    .get("purpose_code")
                    == "recover_gate"
                )
                call["arguments"]["semantic_envelope"]["gate_id"] = "wrong_gate"
            return trace

        with patch.object(
            generate_module,
            "_build_reference_trace",
            side_effect=mutate_reference_trace,
        ):
            invalid = pair_diff(left, right)
        self.assertFalse(invalid["reference_trace_causal_material_isomorphic"])

    def test_pair_verifier_rejects_unrelated_post_intervention_changes(self) -> None:
        left = generate_module._build_world(0, 0, 0, generate_module.DATASET_SEED)
        right = generate_module._build_world(0, 0, 1, generate_module.DATASET_SEED)
        original_artifacts = generate_module._build_artifacts

        def target(
            world: dict[str, Any], artifacts: list[dict[str, Any]], structured: bool
        ) -> dict[str, Any]:
            return next(
                artifact
                for artifact in artifacts
                if generate_module._artifact_checkpoint(world, artifact)["sequence"]
                > world["intervention_sequence"]
                and bool("/structured/" in artifact["content"]["source_uri"])
                == structured
                and not artifact.get("branch_option")
            )

        def mutated_artifacts(field: str):
            def build(world: dict[str, Any]) -> list[dict[str, Any]]:
                artifacts = original_artifacts(world)
                if world["variant_index"] != 1:
                    return artifacts
                artifact = target(world, artifacts, field == "structured_payload")
                if field == "actor":
                    artifact["source_actor_ids"] = [
                        next(
                            actor["actor_id"]
                            for actor in world["actors"]
                            if actor["actor_id"] not in artifact["source_actor_ids"]
                        )
                    ]
                elif field == "channel":
                    artifact["structured_payload"]["channel"] = "email"
                elif field == "time":
                    artifact["available_at"] = (
                        artifact["available_at"][:11] + "16:59:00Z"
                    )
                elif field == "prose":
                    artifact["content"]["body"] += "\nUnrelated post-intervention note."
                    artifact["checksum"] = generate_module._checksum(
                        artifact["content"]["body"]
                    )
                elif field == "source":
                    artifact["provenance"]["source_type"] = "unrelated_source"
                else:
                    artifact["structured_payload"]["unrelated_field"] = True
                return artifacts

            return build

        for field in (
            "actor",
            "channel",
            "time",
            "prose",
            "source",
            "structured_payload",
        ):
            with (
                self.subTest(field=field),
                patch.object(
                    generate_module,
                    "_build_artifacts",
                    side_effect=mutated_artifacts(field),
                ),
            ):
                invalid = pair_diff(left, right)
                self.assertFalse(invalid["post_intervention_context_isomorphic"])

        changed_route = json.loads(json.dumps(right))
        changed_route["gates"][-1] = changed_route["gates"][-2]
        self.assertFalse(pair_diff(left, changed_route)["base_facts_equal"])

        original_events = generate_module._build_events

        def mutate_event(
            world: dict[str, Any], artifacts: list[dict[str, Any]]
        ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
            visible, hidden = original_events(world, artifacts)
            if world["variant_index"] == 1:
                event = next(
                    item
                    for item in visible
                    if item["available_at"]
                    > world["checkpoints"][world["intervention_sequence"]][
                        "available_at"
                    ]
                    and item["artifact_ids"]
                    and not any(
                        artifact.get("branch_option")
                        for artifact in artifacts
                        if artifact["artifact_id"] in item["artifact_ids"]
                    )
                )
                event["recorded_at"] = event["effective_at"]
                event["payload"]["unrelated_field"] = True
            return visible, hidden

        with patch.object(generate_module, "_build_events", side_effect=mutate_event):
            invalid = pair_diff(left, right)
        self.assertFalse(invalid["post_intervention_context_isomorphic"])

    def test_material_events_are_gated_and_observable(self) -> None:
        material_keys = {
            "change",
            "budget_status",
            "requirement",
            "stated_position",
            "disclosure_channel",
            "capacity_status",
            "purchase_order_restart_status",
            "solicitation_status",
            "award_schedule_status",
            "contract_data_status",
            "tax_data_status",
            "delivery_baseline_status",
            "knowledge_transfer_status",
            "conflicts_status",
            "confidentiality_status",
            "closing_exception_status",
            "closing_schedule_status",
        }
        truth_keys = {"family", "variant", "outcome", "reference_outcome", "pair_id"}
        for bundle, _ in self.bundles():
            checkpoints = {
                row["checkpoint_id"]: row for row in _rows(bundle / "checkpoints.jsonl")
            }
            candidates = [
                event
                for event in _rows(bundle / "events.jsonl")
                if material_keys & event["payload"].keys()
            ]
            self.assertEqual(len(candidates), 1)
            event = candidates[0]
            self.assertFalse(truth_keys & event["payload"].keys())
            self.assertNotIn("lane_effects", event["payload"])
            hidden = [
                row
                for row in _rows(bundle / "hidden_events.jsonl")
                if row.get("payload", {}).get("trigger_event_id") == event["event_id"]
            ]
            self.assertEqual(len(hidden), 1)
            self.assertNotIn("lane_effects", hidden[0]["payload"])
            self.assertIn(
                event["kind"],
                {
                    "stakeholder_departed",
                    "stakeholder_joined",
                    "budget_changed",
                    "requirement_changed",
                    "external_signal_published",
                    "message_sent",
                    "document_revised",
                },
            )
            self.assertLess(event["effective_at"], event["recorded_at"])
            self.assertLess(event["recorded_at"], event["available_at"])
            checkpoint = checkpoints[event["payload"]["checkpoint_id"]]
            self.assertLessEqual(event["available_at"], checkpoint["available_at"])
            if checkpoint["sequence"]:
                previous = next(
                    row
                    for row in checkpoints.values()
                    if row["sequence"] == checkpoint["sequence"] - 1
                )
                self.assertGreater(event["available_at"], previous["available_at"])
            self.assertIn(
                event["event_id"],
                checkpoints[event["payload"]["checkpoint_id"]]["released_event_ids"],
            )

    def test_public_boundary_scans_safe_projections(self) -> None:
        prohibited_keys = {
            "pair_id",
            "causal_family",
            "variant",
            "reference_outcome",
            "intervention_checkpoint_id",
            "intervention_sequence",
            "intervention_gate",
            "resolution_checkpoint_id",
            "resolution_sequence",
            "resolution_gate",
            "causal_action_code",
            "observable_cure",
            "causal_owner_role",
            "causal_authority_role_ids",
            "seed",
            "defects",
        }
        prohibited_tokens = {
            "champion_exit",
            "late_stakeholder",
            "budget_shock",
            "requirements_change",
            "external_event",
            "strong_handoff",
            "weak_handoff",
            "within_fit",
            "out_of_fit",
            "hidden_influence",
            "closed_won",
            "closed_lost_competitive",
            "closed_lost_fit",
            "no_decision",
            "disqualified_fit",
        }
        coordinate = re.compile(r"(?:buyer|world)-\d{2}-\d{2}", re.IGNORECASE)
        pair_id = re.compile(r"pair-[0-9a-f]{20}")
        for split in ("train", "dev", "blind"):
            for bundle in self.official_root.joinpath("output", "public", split).glob(
                "world-*"
            ):
                for path in bundle.rglob("*"):
                    if not path.is_file() or path.name in {
                        "manifest.json",
                        "oracle.json",
                        "reference_trace.jsonl",
                        "hidden_events.jsonl",
                        "rubric.json",
                        "assertions.jsonl",
                    }:
                        continue
                    text = path.read_text(errors="ignore")
                    self.assertIsNone(coordinate.search(text), path)
                    self.assertIsNone(pair_id.search(text), path)
                    self.assertFalse(
                        any(f'"{key}"' in text for key in prohibited_keys), path
                    )
                    self.assertFalse(
                        any(token in text for token in prohibited_tokens), path
                    )
                manifest = json.loads((bundle / "manifest.json").read_text())
                self.assertNotIn(manifest["vertical"], manifest["title"].casefold())
        for row in _rows(self.official_root / "authoring" / "worlds.jsonl"):
            self.assertTrue(
                {
                    "pair_id",
                    "seed",
                    "causal_family",
                    "variant",
                    "reference_outcome",
                    "defects",
                }
                <= row.keys()
            )

    def test_no_scenario_note_or_real_contact_data(self) -> None:
        forbidden_email = re.compile(r"@[a-z0-9.-]+\.(com|org|net)\b", re.IGNORECASE)
        for path in self.official_root.rglob("*"):
            if path.is_file():
                text = path.read_text(errors="ignore")
                self.assertNotIn("Scenario note:", text)
                self.assertIsNone(forbidden_email.search(text))

    def test_privacy_validator_rejects_live_domains_and_allows_source_urls(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "manifest.json"
            source.write_text(
                json.dumps(
                    {
                        "provenance": {
                            "source_urls": ["https://github.com/example/source"]
                        },
                        "body": "https://buyer.example https://host.invalid https://localhost https://service.test",
                    }
                )
            )
            self.assertEqual(validate_synthetic_privacy(root, []), [])
            source.write_text("Contact https://real-company.com today")
            errors = validate_synthetic_privacy(root, [])
            self.assertTrue(
                any("privacy_non_reserved_domain" in error for error in errors)
            )

    def test_privacy_validator_rejects_realistic_phone_numbers(self) -> None:
        world = {
            "world_id": "fixture",
            "actors": [{"actor_id": "actor", "kind": "buyer", "phone": "415-867-5309"}],
        }
        errors = validate_synthetic_privacy(Path("/nonexistent"), [world])
        self.assertTrue(any("privacy_non_reserved_phone" in error for error in errors))
        world["actors"][0]["phone"] = "202-555-5309"
        errors = validate_synthetic_privacy(Path("/nonexistent"), [world])
        self.assertTrue(any("privacy_non_reserved_phone" in error for error in errors))
        world["actors"][0]["phone"] = "+1-202-555-0100"
        self.assertEqual(validate_synthetic_privacy(Path("/nonexistent"), [world]), [])

    def test_privacy_validator_rejects_copied_source_phrases(self) -> None:
        world = {"body": "Copied\nsource phrase with punctuation."}
        errors = validate_synthetic_privacy(
            Path("/nonexistent"),
            [world],
            forbidden_phrases=("copied source phrase",),
        )
        self.assertTrue(any("privacy_forbidden_phrase" in error for error in errors))

    def test_privacy_validator_rejects_configured_entity_collisions(self) -> None:
        world = {
            "actors": [
                {"actor_id": "actor", "kind": "buyer", "display_name": "Real Person"}
            ]
        }
        errors = validate_synthetic_privacy(
            Path("/nonexistent"),
            [world],
            forbidden_entities=(" real-person ",),
        )
        self.assertTrue(any("privacy_entity_collision" in error for error in errors))

    def test_privacy_validator_rejects_duplicate_person_identities(self) -> None:
        world = {
            "world_id": "fixture",
            "actors": [
                {"actor_id": "buyer-a", "kind": "buyer", "display_name": "Same Name"},
                {"actor_id": "buyer-b", "kind": "buyer", "display_name": "same-name"},
            ],
        }
        errors = validate_synthetic_privacy(Path("/nonexistent"), [world])
        self.assertTrue(any("privacy_duplicate_person" in error for error in errors))
        world["actors"] = [
            {
                "actor_id": "shared-seller",
                "kind": "seller",
                "display_name": "Same Name",
            },
            {
                "actor_id": "shared-seller",
                "kind": "seller",
                "display_name": "same-name",
            },
        ]
        self.assertEqual(
            validate_synthetic_privacy(
                Path("/nonexistent"),
                [world],
                shared_seller_actor_ids=("shared-seller",),
            ),
            [],
        )

    def test_shared_documents(self) -> None:
        rows = _rows(self.official_root / "authoring" / "shared_documents.jsonl")
        self.assertEqual(len(rows), 180)
        for vertical_index, vertical in enumerate(VERTICALS):
            seller_id = f"org-{hashlib.sha256(f'{20260817}|seller|{vertical_index}'.encode()).hexdigest()[:20]}"
            base = self.official_root / "output" / "public" / "shared" / seller_id
            self.assertEqual(len(list(base.joinpath("documents").glob("*.md"))), 30)
            index = _rows(base / "documents.jsonl")
            self.assertEqual(len(index), 30)
            for row in index:
                text = (self.official_root / row["path"]).read_text()
                for label in (
                    "Rule:",
                    "Owner:",
                    "Required evidence:",
                    "Approval threshold:",
                    "Escalation trigger:",
                    "Effective date:",
                    "Provenance:",
                ):
                    self.assertIn(label, text)
                self.assertIn(vertical["motion"], text)
                self.assertTrue(
                    any(gate.replace("_", " ") in text for gate in vertical["gates"])
                )
                self.assertGreater(len(text), 500)
                self.assertIsInstance(row["approval_threshold_minor_units"], int)
                self.assertGreater(row["approval_threshold_minor_units"], 0)
                self.assertEqual(row["currency"], "USD")
                self.assertEqual(
                    row["checksum"],
                    "sha256:" + hashlib.sha256(text.encode()).hexdigest(),
                )
                self.assertTrue(row["provenance"]["synthetic_only"])

    def test_money_is_minor_units_and_quotes_total_exactly(self) -> None:
        amount_pattern = re.compile(r"\((\d+) minor units\)")
        for bundle, _ in self.bundles():
            artifacts = _rows(bundle / "artifacts.jsonl")
            crm_amounts = {
                json.loads(row["content"]["body"])["amount_minor_units"]
                for row in artifacts
                if row["kind"] in {"crm_record", "crm_history"}
                and "amount_minor_units" in json.loads(row["content"]["body"])
            }
            currencies = {
                json.loads(row["content"]["body"])["currency"]
                for row in artifacts
                if row["kind"] in {"crm_record", "crm_history"}
                and "currency" in json.loads(row["content"]["body"])
            }
            self.assertEqual(len(crm_amounts), 1)
            amount = crm_amounts.pop()
            self.assertIsInstance(amount, int)
            self.assertGreaterEqual(amount, 50_000_000)
            self.assertEqual(currencies, {"USD"})
            quotes = [
                row
                for row in artifacts
                if row["kind"] == "quote"
                and amount_pattern.search(row["content"]["body"])
            ]
            self.assertTrue(quotes)
            for quote in quotes:
                values = [
                    int(value)
                    for value in amount_pattern.findall(quote["content"]["body"])
                ]
                self.assertEqual(len(values), 3)
                self.assertEqual(values[0] + values[1], values[2])
                self.assertEqual(values[2], amount)

    def test_reference_protocol_replays_and_grades(self) -> None:
        reference_bundles = [
            (bundle, private)
            for bundle, private in self.bundles()
            if (bundle / "reference_trace.jsonl").exists()
        ]
        self.assertEqual(len(reference_bundles), 72)
        for bundle, private in reference_bundles:
            rows = _rows(bundle / "reference_trace.jsonl")
            for row in rows:
                protocol_module.Message.from_dict(row, allow_system=True)
            checkpoints = _rows(bundle / "checkpoints.jsonl")
            completions = [
                row
                for row in rows
                if row["kind"] == "tool_call"
                and row["tool_name"] == "run.complete_checkpoint"
            ]
            completed_checkpoint_ids = {
                str(row["arguments"]["checkpoint_id"]) for row in completions
            }
            self.assertTrue(completed_checkpoint_ids)
            self.assertEqual(len(completions), 4 * len(completed_checkpoint_ids))
            completion_sequences = [
                index
                for index, checkpoint in enumerate(checkpoints)
                if checkpoint["checkpoint_id"] in completed_checkpoint_ids
            ]
            self.assertEqual(
                completion_sequences, list(range(len(completion_sequences)))
            )
            oracle = json.loads((bundle / "oracle.json").read_text())
            milestone_by_checkpoint = {
                milestone["checkpoint_id"]: milestone
                for milestone in oracle["verification_facts"]["milestones"]
            }
            for checkpoint in checkpoints:
                if checkpoint["checkpoint_id"] not in completed_checkpoint_ids:
                    continue
                rows_by_role = {
                    row["role"]: row
                    for row in completions
                    if row["arguments"]["checkpoint_id"] == checkpoint["checkpoint_id"]
                }
                self.assertEqual(set(rows_by_role), set(generate_module.ROLES))
                for completion in rows_by_role.values():
                    self.assertEqual(
                        completion["arguments"],
                        {"checkpoint_id": checkpoint["checkpoint_id"]},
                    )
                milestone = milestone_by_checkpoint[checkpoint["checkpoint_id"]]
                decision_reads = {
                    str(value)
                    for row in rows[: rows.index(rows_by_role["sales_manager"])]
                    if row.get("kind") == "tool_call"
                    and row.get("role") == milestone["decision_evidence_role"]
                    and isinstance(row.get("arguments"), dict)
                    for value in row["arguments"].values()
                }
                self.assertTrue(
                    decision_reads & set(milestone["decision_artifact_ids"])
                )
            approval_requests = sum(
                row["kind"] == "tool_call" and row["tool_name"] == "approvals.request"
                for row in rows
            )
            approval_count = sum(
                row["kind"] == "tool_call" and row["tool_name"] == "approvals.approve"
                for row in rows
            )
            expected_approvals = len(
                oracle["verification_facts"]["approval_requirements"]
            )
            self.assertEqual(approval_requests, expected_approvals)
            self.assertLessEqual(approval_count, approval_requests)
            engine = open_world(
                bundle,
                run_id=rows[0]["run_id"],
                agent_manifest=generate_module.REFERENCE_AGENT_MANIFEST,
                allow_private=private,
            )
            try:
                result = replay_trace(engine, bundle / "reference_trace.jsonl")
                score = grade_run(
                    engine,
                    bundle / "rubric.json",
                    oracle=bundle / "oracle.json",
                )
                self.assertEqual(result.status, "completed")
                self.assertEqual(score["status"], "valid")
                self.assertEqual(score["execution_index"], 100.0, bundle.name)
                self.assertTrue(score["strict_cycle_pass"], bundle.name)
                self.assertEqual(
                    engine.run_status()["terminal_outcome"],
                    oracle["scenario_manifest"]["terminal_outcome"],
                )
                approvals = engine.approvals_list("sales_manager", limit=10)
                self.assertEqual(len(approvals), expected_approvals)
                self.assertEqual(
                    sum(item["status"] == "approved" for item in approvals),
                    approval_count,
                )
                self.assertEqual(len(score["pending_judge_assertions"]), 1)
                self.assertTrue(
                    all(
                        item["status"] == "passed"
                        for item in score["assertions"]
                        if item["required"]
                    )
                )
                required_metrics = [
                    item
                    for item in score["assertions"]
                    if item["required"] and item["kind"] == "metric"
                ]
                self.assertEqual(len(required_metrics), 8)
                self.assertTrue(all(item["score"] == 1.0 for item in required_metrics))
            finally:
                engine.close()

    def test_authority_audience_uses_normative_followups_and_recovery(self) -> None:
        passive_bundle = (
            self.official_root / "output/public/blind/world-07342a1c843b11a893e1"
        )
        passive_oracle = json.loads((passive_bundle / "oracle.json").read_text())
        passive_rows = _rows(passive_bundle / "reference_trace.jsonl")
        with open_world(
            passive_bundle,
            run_id=passive_rows[0]["run_id"],
            agent_manifest=generate_module.REFERENCE_AGENT_MANIFEST,
        ) as engine:
            replay_trace(engine, passive_rows)
            state, trace = grading_module._sqlite_state(engine.connection)
            verifier = grading_module._trusted_verifier(
                grading_module._context(state, trace, passive_oracle), passive_oracle
            )
            resolutions = {
                row["milestone_id"]: row["resolution"]
                for row in engine.milestone_resolutions()
            }
            contacted = {
                row["arguments"]["semantic_envelope"]["target_actor_id"]
                for row in passive_rows
                if row.get("tool_name") == "communications.send"
            }
            passive = {
                requirement["actor_id"]
                for milestone in passive_oracle["verification_facts"]["milestones"]
                if (resolution := resolutions.get(milestone["milestone_id"]))
                in milestone["business_effect_requirements_by_resolution"]
                for requirement in milestone["authority_requirements"]
                if requirement["actor_id"]
                != milestone["business_effect_requirements_by_resolution"][resolution][
                    "decision_followup"
                ]["recipient_actor_id"]
            }
            self.assertTrue(passive - contacted)
            self.assertEqual(verifier["authority_audience_coverage_score"], 1.0)

        recovery_bundle = (
            self.official_root / "output/public/blind/world-0735648420249715dbeb"
        )
        recovery_oracle = json.loads((recovery_bundle / "oracle.json").read_text())
        recovery_rows = _rows(recovery_bundle / "reference_trace.jsonl")
        with open_world(
            recovery_bundle,
            run_id=recovery_rows[0]["run_id"],
            agent_manifest=generate_module.REFERENCE_AGENT_MANIFEST,
        ) as engine:
            replay_trace(engine, recovery_rows)
            state, trace = grading_module._sqlite_state(engine.connection)
            facts = recovery_oracle["verification_facts"]
            rules = {rule["effect_id"]: rule for rule in facts["action_effect_rules"]}
            applied = {
                effect_id
                for row in state["causal_action_applications"]
                for effect_id in row["effects"]
            }
            applied_external_authorities = {
                authority_actor_id
                for effect_id in applied
                if (authority_actor_id := rules[effect_id].get("authority_actor_id"))
                is not None
                and facts["actor_activity"][authority_actor_id]["organization_id"]
                != facts["seller_organization_id"]
            }
            actions = grading_module._successful_actions(trace)
            recovery_targets = {
                grading_module._action_arguments(action)
                .get("semantic_envelope", {})
                .get("target_actor_id")
                for action in actions
                if action["tool_name"] == "communications.send"
            }
            self.assertGreater(len(applied_external_authorities), 1)
            self.assertLessEqual(applied_external_authorities, recovery_targets)
            verifier = grading_module._trusted_verifier(
                grading_module._context(state, trace, recovery_oracle), recovery_oracle
            )
            self.assertEqual(verifier["authority_audience_coverage_score"], 1.0)

    def test_reference_actions_change_every_winning_outcome(self) -> None:
        winning_worlds = 0
        for bundle, private in self.bundles():
            reference = bundle / "reference_trace.jsonl"
            oracle_path = bundle / "oracle.json"
            if not reference.exists() or not oracle_path.exists():
                continue
            oracle = json.loads(oracle_path.read_text())
            if oracle["scenario_manifest"]["terminal_outcome"] != "closed_won":
                continue
            winning_worlds += 1
            source_rows = _rows(reference)
            completions = [
                source_rows[0],
                *[
                    row
                    for row in source_rows
                    if row["kind"] == "tool_call"
                    and row["tool_name"] == "run.complete_checkpoint"
                ],
            ]
            engine = open_world(
                bundle,
                run_id=f"ablation-{winning_worlds}",
                agent_manifest=generate_module.REFERENCE_AGENT_MANIFEST,
                limits=runner_module.RunLimits(None, None, None, 0),
                allow_private=private,
            )
            try:
                result = replay_trace(engine, completions)
                self.assertEqual(result.status, "running")
                self.assertEqual(engine.milestone_resolutions(), [])
                self.assertIsNone(engine.run_status()["terminal_outcome"])
                score = grade_run(
                    engine, bundle / "rubric.json", oracle=bundle / "oracle.json"
                )
                self.assertEqual(score["status"], "valid")
                self.assertFalse(score["strict_cycle_pass"])
                self.assertLess(score["execution_index"], 25.0)
                self.assertEqual(
                    score["category_scores"]["side_effect_discipline"], 0.0
                )
            finally:
                engine.close()
        self.assertEqual(winning_worlds, 24)

    def test_do_nothing_scores_below_fifteen(self) -> None:
        bundle = next(
            self.official_root.joinpath("output", "public", "train").glob("world-*")
        )
        engine = open_world(bundle)
        try:
            score = grade_run(
                engine, bundle / "rubric.json", oracle=bundle / "oracle.json"
            )
            required = [item for item in score["assertions"] if item["required"]]
            self.assertEqual(len(required), 40)
            self.assertFalse(score["strict_cycle_pass"])
            self.assertLess(score["execution_index"], 15.0)
        finally:
            engine.close()

    def test_rubric_uses_independent_trusted_verifier_leaves(self) -> None:
        verifier_dicts = []
        for verifier in (
            grading_module._trusted_verifier,
            grading_module._forecast_verifier,
        ):
            tree = ast.parse(textwrap.dedent(inspect.getsource(verifier)))
            verifier_dicts.extend(
                node.value
                for node in ast.walk(tree)
                if isinstance(node, ast.Assign)
                and any(
                    isinstance(target, ast.Name) and target.id == "verifier"
                    for target in node.targets
                )
                and isinstance(node.value, ast.Dict)
            )
            verifier_dicts.extend(
                node.value
                for node in ast.walk(tree)
                if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict)
            )
        normalized = {
            str(key.value): ast.dump(value, include_attributes=False)
            for verifier_dict in verifier_dicts
            for key, value in zip(verifier_dict.keys, verifier_dict.values, strict=True)
            if isinstance(key, ast.Constant) and isinstance(key.value, str)
        }
        bundles = list(
            self.official_root.joinpath("output", "public").glob("*/world-*")
        )
        self.assertEqual(len(bundles), 72)
        for bundle in bundles:
            rubric = json.loads((bundle / "rubric.json").read_text())
            oracle = json.loads((bundle / "oracle.json").read_text())
            required = [
                assertion for assertion in rubric["assertions"] if assertion["required"]
            ]
            self.assertEqual(len(required), 40)
            for category in CANONICAL_CATEGORIES:
                self.assertEqual(
                    sum(assertion["category"] == category for assertion in required),
                    5,
                )
            self.assertTrue(
                all(
                    assertion["target"]["path"].startswith("verifier.")
                    for assertion in required
                )
            )
            self.assertTrue(
                grading_module._validate_rubric(rubric, {"oracle": oracle})["valid"]
            )
            facts = oracle["verification_facts"]
            authored_refs = {
                artifact_id
                for source_map in facts["checkpoint_sources"].values()
                for artifact_id in source_map.values()
            } | {
                artifact_id
                for source in (*facts["milestones"], *facts["action_effect_rules"])
                for artifact_id in source.get(
                    "evidence_ids", source.get("required_evidence_ids", [])
                )
            }
            self.assertEqual(set(facts["evidence_catalog"]), authored_refs)
            self.assertLess(
                len(facts["evidence_catalog"]), len(_rows(bundle / "artifacts.jsonl"))
            )
            predicates = [
                normalized[assertion["target"]["path"].removeprefix("verifier.")]
                for assertion in required
            ]
            self.assertEqual(len(predicates), len(set(predicates)))
        rubric = json.loads((bundles[0] / "rubric.json").read_text())
        oracle = json.loads((bundles[0] / "oracle.json").read_text())
        indexed = json.loads(json.dumps(rubric))
        indexed["assertions"][0]["target"]["path"] = "verifier.items[0].passed"
        self.assertFalse(
            grading_module._validate_rubric(indexed, {"oracle": oracle})["valid"]
        )
        magic = json.loads(json.dumps(rubric))
        magic["assertions"][0]["target"]["path"] = "verifier.record_integrity_status"
        self.assertFalse(
            grading_module._validate_rubric(magic, {"oracle": oracle})["valid"]
        )
        duplicate = json.loads(json.dumps(rubric))
        duplicate["assertions"][1]["semantic_target"] = duplicate["assertions"][0][
            "semantic_target"
        ]
        self.assertFalse(
            grading_module._validate_rubric(duplicate, {"oracle": oracle})["valid"]
        )

    def test_verification_facts_define_authored_milestones(self) -> None:
        bundle = next(
            self.official_root.joinpath("output", "public", "train").glob("world-*")
        )
        oracle = json.loads((bundle / "oracle.json").read_text())
        facts = oracle["verification_facts"]
        milestones = facts["milestones"]
        branch = facts["branches"][0]
        remedy = next(
            milestone
            for milestone in milestones
            if milestone["milestone_id"] == branch["remedy_milestone_id"]
        )
        intervention_index = next(
            index
            for index, milestone in enumerate(milestones)
            if milestone["milestone_id"] == remedy["remedy_of"]
        )
        resolution_index = int(remedy["chronology"]["sequence"])
        self.assertEqual(len(milestones), len(facts["checkpoint_ids"]))
        for index, milestone in enumerate(milestones):
            self.assertTrue(milestone["milestone_id"])
            self.assertTrue(milestone["evidence_ids"])
            self.assertTrue(milestone["gate_id"])
            self.assertTrue(milestone["authority_requirements"])
            self.assertEqual(
                set(milestone["evidence_requirements_by_role"]),
                set(generate_module.ROLES),
            )
            self.assertEqual(milestone["chronology"]["sequence"], index)
            expected_prerequisites = (
                [str(remedy["remedy_of"])]
                if index == resolution_index
                else (
                    [milestones[intervention_index - 1]["milestone_id"]]
                    if intervention_index > 0
                    else []
                )
                if intervention_index < index < resolution_index
                else [milestones[index - 1]["milestone_id"]]
                if index
                else []
            )
            self.assertEqual(
                milestone["prerequisite_milestone_ids"], expected_prerequisites
            )
        terminal = [
            milestone
            for milestone in milestones
            if milestone["terminal_outcome_by_resolution"]
        ]
        self.assertTrue(terminal)
        self.assertEqual(remedy["branch_id"], branch["branch_id"])
        self.assertIsNotNone(remedy["remedy_of"])

    def test_magic_crm_fields_cannot_satisfy_verifier(self) -> None:
        bundle = next(
            self.official_root.joinpath("output", "public", "train").glob("world-*")
        )
        oracle = json.loads((bundle / "oracle.json").read_text())
        context = {
            "status": "completed",
            "terminal_outcome": "closed_lost",
            "terminal_support": {},
            "crm_records": [
                {
                    "data": {
                        "record_integrity_status": "reconciled",
                        "side_effect_review": "completed_without_unrelated_changes",
                        "post_intervention_evidence_ref": "fabricated",
                    }
                }
            ],
            "trace": [],
        }
        verifier = grading_module._trusted_verifier(context, oracle)
        self.assertFalse(verifier["crm_terminal_state_consistent"])
        self.assertEqual(verifier["write_scope_coverage_score"], 0.0)
        self.assertFalse(verifier["terminal_rationale_supported"])

    def test_semantically_equivalent_crm_repairs_receive_equal_credit(self) -> None:
        bundle = next(
            self.official_root.joinpath("output", "public", "train").glob("world-*")
        )
        oracle = json.loads((bundle / "oracle.json").read_text())
        defects = oracle["verification_facts"]["crm_defects"]
        changes = {defect["field"]: defect["truth_value"] for defect in defects}
        first = {
            "crm_history": [{"changed_at": "2099-01-01T00:00:00Z", "changes": changes}],
            "trace": [],
        }
        second = {
            "crm_history": [
                {
                    "changed_at": "2099-01-01T00:00:00Z",
                    "changes": dict(reversed(list(changes.items()))),
                }
            ],
            "trace": [],
        }
        left = grading_module._trusted_verifier(first, oracle)
        right = grading_module._trusted_verifier(second, oracle)
        for name in (
            "stage_defect_repaired",
            "close_date_defect_repaired",
            "next_step_defect_repaired",
        ):
            self.assertEqual(left[name], right[name])
            self.assertTrue(left[name])

    def test_metric_assertions_preserve_partial_credit(self) -> None:
        assertion = {
            "assertion_id": "partial",
            "category": "evidence_and_understanding",
            "kind": "metric",
            "required": True,
            "weight": 1.0,
            "target": {
                "path": "verifier.evidence_coverage_score",
                "operator": "gte",
                "expected": 0,
                "minimum_score": 1.0,
            },
        }
        result = grading_module.evaluate_assertion(
            assertion, {"verifier": {"evidence_coverage_score": 0.37}}
        )
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["score"], 0.37)
        zero = grading_module.evaluate_assertion(
            assertion, {"verifier": {"other_score": 0.0}}
        )
        self.assertEqual(zero["status"], "failed")
        self.assertEqual(zero["score"], 0.0)

    def test_each_leaf_isolated_to_its_semantic_target(self) -> None:
        bundle = next(
            self.official_root.joinpath("output", "public", "train").glob("world-*")
        )
        rubric = json.loads((bundle / "rubric.json").read_text())
        leaves = [
            assertion
            for assertion in rubric["assertions"]
            if assertion["kind"] in {"deterministic", "metric"}
        ]
        baseline = {
            assertion["target"]["path"].removeprefix("verifier."): 1.0
            if assertion["kind"] == "metric"
            else True
            for assertion in leaves
        }
        for target in leaves:
            mutated = dict(baseline)
            name = target["target"]["path"].removeprefix("verifier.")
            mutated[name] = 0.0 if target["kind"] == "metric" else False
            failed = [
                assertion["semantic_target"]
                for assertion in leaves
                if grading_module.evaluate_assertion(assertion, {"verifier": mutated})[
                    "status"
                ]
                == "failed"
            ]
            self.assertEqual(failed, [target["semantic_target"]])

    def test_attachment_grounding_is_bound_to_exact_send(self) -> None:
        bundle = next(
            self.official_root.joinpath("output", "public", "train").glob("world-*")
        )
        oracle = json.loads((bundle / "oracle.json").read_text())
        facts = oracle["verification_facts"]
        evidence_id = next(iter(facts["evidence_catalog"]))
        buyer = next(
            actor
            for actor in facts["actor_activity"].values()
            if actor["kind"] == "buyer"
        )
        occurred_at = max(
            buyer["active_from"], facts["milestones"][0]["chronology"]["available_at"]
        )
        milestone = facts["milestones"][0]
        resolution = next(iter(milestone["business_effect_requirements_by_resolution"]))
        semantic = milestone["business_effect_requirements_by_resolution"][resolution][
            "decision_followup"
        ]["semantic_requirements"]
        envelope = {
            "target_actor_id": semantic["authority_actor_id"],
            "purpose": "confirm evidence",
            "purpose_code": semantic["purpose_code"],
            "gate_id": semantic["gate_id"],
            "resolution": semantic["resolution"],
            "related_records": [facts["deal_id"]],
            "requested_decisions": ["confirm owner"],
            "decision_codes": [semantic["decision_code"]],
            "commitments": ["respond by next review"],
            "commitment_codes": [semantic["commitment_code"]],
            "commitment_owner_role": semantic["commitment_owner_role"],
            "decision_due_at": occurred_at,
            "commitment_due_at": occurred_at,
            "attachments": [evidence_id],
            "evidence_claims": [
                {
                    "artifact_id": evidence_id,
                    "claim_type": "supports_gate_resolution",
                    "gate_id": semantic["gate_id"],
                    "resolution": semantic["resolution"],
                }
            ],
        }
        later_envelope = {**envelope, "attachments": []}
        trace = [
            {
                "kind": "tool_call",
                "call_id": "send-before-read",
                "role": "account_executive",
                "occurred_at": occurred_at,
                "tool_name": "communications.send",
                "arguments": {
                    "recipients": [buyer["email"]],
                    "subject": "first",
                    "body": "first",
                    "semantic_envelope": envelope,
                },
                "idempotency_key": "send-before-read",
            },
            {
                "kind": "tool_result",
                "call_id": "send-before-read",
                "ok": True,
                "result": {},
            },
            {
                "kind": "tool_call",
                "call_id": "read-late",
                "role": "account_executive",
                "occurred_at": occurred_at,
                "tool_name": "communications.read",
                "arguments": {"message_id": evidence_id},
            },
            {
                "kind": "tool_result",
                "call_id": "read-late",
                "ok": True,
                "result": {},
            },
            {
                "kind": "tool_call",
                "call_id": "unrelated-later-send",
                "role": "account_executive",
                "occurred_at": occurred_at,
                "tool_name": "communications.send",
                "arguments": {
                    "recipients": [buyer["email"]],
                    "subject": "second",
                    "body": "second",
                    "semantic_envelope": later_envelope,
                },
                "idempotency_key": "unrelated-later-send",
            },
            {
                "kind": "tool_result",
                "call_id": "unrelated-later-send",
                "ok": True,
                "result": {},
            },
        ]
        context = {
            "trace": trace,
            "grants": [
                {
                    "data": {
                        "role": "account_executive",
                        "can_contact_external": True,
                    }
                }
            ],
        }
        verifier = grading_module._trusted_verifier(context, oracle)
        self.assertFalse(verifier["grounded_attachment_sent"])

    def test_deterministic_public_output(self) -> None:
        def digest(root: Path) -> str:
            hasher = hashlib.sha256()
            for path in sorted(root.rglob("*")):
                if path.is_file():
                    hasher.update(path.relative_to(root).as_posix().encode())
                    hasher.update(path.read_bytes())
            return hasher.hexdigest()

        self.assertEqual(digest(self.public_roots[0]), digest(self.public_roots[1]))


if __name__ == "__main__":
    unittest.main()
