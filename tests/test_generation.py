from __future__ import annotations

import hashlib
import importlib
import importlib.resources
import json
import re
import sys
import tempfile
import unittest
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

generate_module = importlib.import_module("edlb.generate")
grading_module = importlib.import_module("edlb.grading")
protocol_module = importlib.import_module("edlb.protocol")
runner_module = importlib.import_module("edlb.runner")
ARTIFACT_COUNTS = generate_module.ARTIFACT_COUNTS
CANONICAL_CATEGORIES = generate_module.CANONICAL_CATEGORIES
VERTICALS = generate_module.VERTICALS
generate_dataset = generate_module.generate_dataset
grade_run = grading_module.grade_run
open_world = runner_module.open_world
pair_diff = generate_module.pair_diff
replay_trace = runner_module.replay_trace
validate_synthetic_privacy = generate_module._validate_synthetic_privacy


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
        self.assertEqual(official["artifact_count_per_world"], 72)
        self.assertEqual(official["artifact_total"], 5184)
        self.assertGreaterEqual(official["checkpoint_min"], 8)
        self.assertLessEqual(official["checkpoint_max"], 12)
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
                expected_org = (
                    manifest["buyer_org_id"]
                    if actor["kind"] == "buyer"
                    else manifest["seller_org_id"]
                )
                self.assertEqual(actor["organization_id"], expected_org)
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
                for field in ("available_at", "window_start", "window_end"):
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
        self.assertEqual(
            {channel: len(rows) for channel, rows in by_channel.items()},
            ARTIFACT_COUNTS,
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
        }
        self.assertEqual(crm_fields, {"stage", "close_date", "next_step"})
        calendar_agendas = {
            json.loads(row["content"]["body"])["agenda"].split()[0]
            for row in by_channel["calendar"]
        }
        self.assertEqual(calendar_agendas, {"Review", "Carry", "Confirm"})

    def test_causal_differences_release_after_intervention(self) -> None:
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
                self.assertNotEqual(left["buyer_name"], right["buyer_name"])
                self.assertNotEqual(left["buyer_domain"], right["buyer_domain"])
                self.assertNotEqual(left["buyer_org_id"], right["buyer_org_id"])
                self.assertNotEqual(
                    {actor["actor_id"] for actor in left["actors"]},
                    {actor["actor_id"] for actor in right["actors"]},
                )
                diff = pair_diff(left, right)
                self.assertTrue(diff["base_facts_equal"])
                self.assertTrue(diff["pre_intervention_artifacts_equal"])
                self.assertGreater(diff["post_intervention_artifact_differences"], 0)
                self.assertEqual(
                    diff["allowed_differences"],
                    [
                        "opaque_public_identity_projection",
                        "declared_intervention",
                        "causal_descendants_after_intervention",
                    ],
                )

    def test_material_events_are_gated_and_observable(self) -> None:
        material_keys = {
            "change",
            "budget_status",
            "requirement",
            "stated_position",
            "disclosure_channel",
            "restart_status",
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
            self.assertTrue(event["payload"]["lane_effects"])
            self.assertIn(
                event["kind"],
                {
                    "stakeholder_departed",
                    "stakeholder_joined",
                    "budget_changed",
                    "requirement_changed",
                    "external_signal_published",
                },
            )
            self.assertLess(event["effective_at"], event["recorded_at"])
            self.assertLess(event["recorded_at"], event["available_at"])
            self.assertEqual(
                event["available_at"],
                checkpoints[event["payload"]["checkpoint_id"]]["available_at"],
            )
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
            }
            currencies = {
                json.loads(row["content"]["body"])["currency"]
                for row in artifacts
                if row["kind"] in {"crm_record", "crm_history"}
            }
            self.assertEqual(len(crm_amounts), 1)
            amount = crm_amounts.pop()
            self.assertIsInstance(amount, int)
            self.assertGreaterEqual(amount, 50_000_000)
            self.assertEqual(currencies, {"USD"})
            quotes = [row for row in artifacts if row["kind"] == "quote"]
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
            self.assertEqual(len(completions), 4 * len(checkpoints))
            for checkpoint in checkpoints:
                roles = {
                    row["role"]
                    for row in completions
                    if row["arguments"]["checkpoint_id"] == checkpoint["checkpoint_id"]
                }
                self.assertEqual(roles, set(generate_module.ROLES))
            self.assertEqual(
                sum(
                    row["kind"] == "tool_call"
                    and row["tool_name"] == "communications.send"
                    for row in rows
                ),
                len(checkpoints),
            )
            self.assertEqual(
                sum(
                    row["kind"] == "tool_call" and row["tool_name"] == "crm.update"
                    for row in rows
                ),
                len(checkpoints),
            )
            self.assertEqual(
                sum(
                    row["kind"] == "tool_call"
                    and row["tool_name"] == "documents.create"
                    for row in rows
                ),
                len(checkpoints),
            )
            self.assertEqual(
                sum(
                    row["kind"] == "tool_call"
                    and row["tool_name"] == "documents.attach"
                    for row in rows
                ),
                len(checkpoints),
            )
            self.assertEqual(
                sum(
                    row["kind"] == "tool_call"
                    and row["tool_name"] == "approvals.request"
                    for row in rows
                ),
                1,
            )
            oracle = json.loads((bundle / "oracle.json").read_text())
            approval_count = sum(
                row["kind"] == "tool_call" and row["tool_name"] == "approvals.approve"
                for row in rows
            )
            self.assertEqual(
                approval_count,
                0 if oracle["causal_family"] == "budget_shock" else 1,
            )
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
                self.assertEqual(score["execution_index"], 100.0)
                self.assertTrue(score["strict_cycle_pass"])
                self.assertEqual(
                    engine.run_status("account_executive")["terminal_outcome"],
                    oracle["scenario_manifest"]["terminal_outcome"],
                )
                approvals = engine.approvals_list("sales_manager", limit=10)
                self.assertEqual(len(approvals), 1)
                self.assertEqual(
                    approvals[0]["status"],
                    "pending"
                    if oracle["causal_family"] == "budget_shock"
                    else "approved",
                )
                self.assertEqual(
                    engine.connection.execute(
                        "SELECT COUNT(*) FROM document_links"
                    ).fetchone()[0],
                    len(checkpoints),
                )
                self.assertEqual(len(score["pending_judge_assertions"]), 1)
                self.assertTrue(
                    all(
                        item["status"] == "passed"
                        for item in score["assertions"]
                        if item["required"]
                    )
                )
            finally:
                engine.close()

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
            completions = [
                row
                for row in _rows(reference)
                if row["kind"] == "tool_call"
                and row["tool_name"] == "run.complete_checkpoint"
            ]
            engine = open_world(
                bundle,
                run_id=f"ablation-{winning_worlds}",
                allow_private=private,
            )
            try:
                result = replay_trace(engine, completions)
                self.assertEqual(result.status, "completed")
                self.assertEqual(
                    engine.run_status("account_executive")["terminal_outcome"],
                    "no_decision",
                )
            finally:
                engine.close()
        self.assertEqual(winning_worlds, 24)

    def test_do_nothing_fails_every_required_controllable_assertion(self) -> None:
        bundle = next(
            self.official_root.joinpath("output", "public", "train").glob("world-*")
        )
        engine = open_world(bundle)
        try:
            score = grade_run(
                engine, bundle / "rubric.json", oracle=bundle / "oracle.json"
            )
            required = [item for item in score["assertions"] if item["required"]]
            self.assertEqual(len(required), 8)
            self.assertTrue(all(item["status"] == "failed" for item in required))
            self.assertFalse(score["strict_cycle_pass"])
            self.assertEqual(score["execution_index"], 0.0)
        finally:
            engine.close()

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
