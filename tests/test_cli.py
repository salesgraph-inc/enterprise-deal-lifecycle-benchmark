from __future__ import annotations

import io
import json
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from importlib import import_module
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

main = import_module("edlb.cli").main
runner = import_module("edlb.runner")
open_world = runner.open_world
RunLimits = runner.RunLimits


def _manufacturing_world() -> Path:
    for manifest_path in sorted(
        (ROOT / "benchmarks/v1/output/public/train").glob("*/manifest.json")
    ):
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("vertical") == "manufacturing":
            return manifest_path.parent
    raise AssertionError("no manufacturing public train world found")


WORLD = _manufacturing_world()


class CliTest(unittest.TestCase):
    def test_help_and_validate(self) -> None:
        self.assertEqual(main(["validate", str(WORLD)]), 0)

    def test_validate_aggregate_dataset_manifest(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(["validate", str(ROOT / "benchmarks/v1/output")]), 0)
        value = json.loads(output.getvalue())
        self.assertTrue(value["valid"])
        self.assertEqual(value["world_count"], 48)

    def test_validate_rejects_declared_aggregate_world_count(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "arbitrary" / "output"
            root.mkdir(parents=True)
            (root / "public").symlink_to(
                ROOT / "benchmarks/v1/output/public", target_is_directory=True
            )
            manifest = json.loads(
                (ROOT / "benchmarks/v1/output/manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            manifest["world_count"] = 999
            (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(["validate", str(root)]), 1)
            value = json.loads(output.getvalue())
            self.assertFalse(value["valid"])
            self.assertIn(
                "aggregate manifest world_count does not match observed bundles",
                value["errors"],
            )

    def test_validate_rejects_partial_aggregate_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "output"
            root.mkdir()
            (root / "public").symlink_to(
                ROOT / "benchmarks/v1/output/public", target_is_directory=True
            )
            (root / "manifest.json").write_text(
                json.dumps({"splits": {"train": 24}}), encoding="utf-8"
            )
            result = runner.validate_dataset(root)
            self.assertFalse(result["valid"])
            self.assertIn(
                "aggregate manifest dataset_version is missing", result["errors"]
            )
            self.assertIn(
                "aggregate manifest splits does not match observed bundles",
                result["errors"],
            )
            self.assertIn("aggregate validation summary is missing", result["errors"])

    def test_validate_private_summary_covers_all_observed_worlds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "v1"
            (root / "output").mkdir(parents=True)
            (root / "output/public").symlink_to(
                ROOT / "benchmarks/v1/output/public", target_is_directory=True
            )
            shutil.copy2(
                ROOT / "benchmarks/v1/output/manifest.json",
                root / "output/manifest.json",
            )
            (root / "authoring").symlink_to(
                ROOT / "benchmarks/v1/authoring", target_is_directory=True
            )
            blind_root = root / "private/blind"
            blind_root.mkdir(parents=True)
            blind_vertical_counts: dict[str, int] = {}
            for index, source in enumerate(
                sorted((ROOT / "benchmarks/v1/output/public/train").glob("world-*"))
            ):
                world_id = f"world-blind-fixture-{index:02d}"
                target = blind_root / world_id
                shutil.copytree(source, target)
                manifest_path = target / "manifest.json"
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                vertical = str(manifest["vertical"])
                vertical_index = blind_vertical_counts.get(vertical, 0)
                blind_vertical_counts[vertical] = vertical_index + 1
                manifest["split"] = "blind"
                manifest["world_id"] = world_id
                manifest["pair_id"] = (
                    f"pair-blind-fixture-{vertical}-{vertical_index // 2:02d}"
                )
                manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            public = [
                runner.validate_world_bundle(path.parent)
                for path in sorted((root / "output/public").glob("*/*/manifest.json"))
            ]
            private = [
                runner.validate_world_bundle(path.parent, allow_private=True)
                for path in sorted(blind_root.glob("*/manifest.json"))
            ]
            public_ids = {str(row["world_id"]) for row in public}

            def pair_diff(pair_id: str, world_ids: list[str]) -> dict[str, object]:
                return {
                    "pair_id": pair_id,
                    "world_ids": world_ids,
                    "base_facts_equal": True,
                    "pre_intervention_artifacts_equal": True,
                    "post_intervention_artifact_differences": 1,
                    "allowed_differences": [
                        "opaque_public_identity_projection",
                        "declared_intervention",
                        "causal_descendants_after_intervention",
                    ],
                }

            authoring_rows = [
                json.loads(line)
                for line in (root / "authoring/worlds.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
                if line.strip()
            ]
            public_pair_members: dict[str, list[str]] = {}
            for row in authoring_rows:
                world_id = str(row.get("world_id", ""))
                pair_id = row.get("pair_id")
                if world_id in public_ids and pair_id:
                    public_pair_members.setdefault(str(pair_id), []).append(world_id)
            pair_diffs = [
                pair_diff(pair_id, sorted(world_ids))
                for pair_id, world_ids in sorted(public_pair_members.items())
            ]
            paired_public_ids = {
                world_id for item in pair_diffs for world_id in item["world_ids"]
            }
            unpaired_groups: dict[tuple[str, str], list[str]] = {}
            for row in public:
                world_id = str(row["world_id"])
                if world_id not in paired_public_ids:
                    key = (str(row["split"]), str(row["vertical"]))
                    unpaired_groups.setdefault(key, []).append(world_id)
            self.assertEqual(sum(map(len, unpaired_groups.values())), 24)
            for (split, vertical), world_ids in sorted(unpaired_groups.items()):
                self.assertEqual(len(world_ids) % 2, 0)
                pair_diffs.extend(
                    pair_diff(
                        f"pair-{split}-fixture-{vertical}-{index // 2:02d}",
                        sorted(world_ids)[index : index + 2],
                    )
                    for index in range(0, len(world_ids), 2)
                )
            blind_pair_members: dict[str, list[str]] = {}
            for row in private:
                blind_pair_members.setdefault(str(row["pair_id"]), []).append(
                    str(row["world_id"])
                )
            pair_diffs.extend(
                pair_diff(pair_id, sorted(world_ids))
                for pair_id, world_ids in sorted(blind_pair_members.items())
            )
            validation_path = root / "private/validation.json"
            validation_path.write_text(
                json.dumps({"pair_diffs": pair_diffs}), encoding="utf-8"
            )
            observed = runner._observed_dataset(root, public + private, [])
            validation = dict(observed["validation"])
            validation["pair_diffs"] = pair_diffs
            validation_path.write_text(json.dumps(validation), encoding="utf-8")
            valid = runner.validate_dataset(root, allow_private=True)
            self.assertTrue(valid["valid"], valid["errors"])
            self.assertEqual(valid["world_count"], 72)
            mutated = json.loads(json.dumps(validation))
            mutated["pair_diffs"][0]["base_facts_equal"] = False
            validation_path.write_text(json.dumps(mutated), encoding="utf-8")
            invalid_pair = runner.validate_dataset(root, allow_private=True)
            self.assertFalse(invalid_pair["valid"])
            self.assertTrue(
                any(
                    "base facts are not equal" in error
                    for error in invalid_pair["errors"]
                )
            )
            validation_path.write_text(json.dumps(validation), encoding="utf-8")
            blind = min(blind_root.glob("world-*"))
            original_name = blind.name
            duplicate_id = str(public[0]["world_id"])
            duplicate = blind_root / duplicate_id
            manifest_path = blind / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["world_id"] = duplicate_id
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            blind.rename(duplicate)
            duplicate_result = runner.validate_dataset(root, allow_private=True)
            self.assertIn(
                f"duplicate world_id across splits: {duplicate_id}",
                duplicate_result["errors"],
            )
            manifest["world_id"] = original_name
            (duplicate / "manifest.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )
            blind = duplicate.rename(blind_root / original_name)
            held = root / blind.name
            blind.rename(held)
            missing_world = runner.validate_dataset(root, allow_private=True)
            self.assertIn(
                "private blind split does not complete the full dataset",
                missing_world["errors"],
            )
            held.rename(blind_root / original_name)
            tampered = dict(observed["validation"])
            tampered["pair_diffs"] = pair_diffs
            tampered["world_count"] = 999
            validation_path.write_text(json.dumps(tampered), encoding="utf-8")
            invalid = runner.validate_dataset(root, allow_private=True)
            self.assertFalse(invalid["valid"])
            self.assertIn(
                "private validation world_count does not match observed bundles",
                invalid["errors"],
            )
            validation_path.unlink()
            missing = runner.validate_dataset(root, allow_private=True)
            self.assertFalse(missing["valid"])
            self.assertIn("private validation summary is missing", missing["errors"])

    def test_run_rejects_non_empty_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "run"
            output.mkdir()
            (output / "existing").write_text("occupied", encoding="utf-8")
            self.assertEqual(
                main(
                    [
                        "run",
                        str(WORLD),
                        "--baseline",
                        "do_nothing",
                        "--output",
                        str(output),
                    ]
                ),
                2,
            )

    def test_replay_infers_source_run_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            trace = root / "source.jsonl"
            with open_world(
                WORLD,
                run_id="cli-source",
                team_id="team-source",
                seed=7,
                limits=RunLimits(3, 5, 4.0, 0),
                db_path=root / "source.sqlite",
            ) as engine:
                engine.dump_trace(trace)
            output = root / "replay"
            self.assertEqual(
                main(["replay", str(WORLD), str(trace), "--output", str(output)]), 0
            )
            manifest = json.loads(
                (output / "run-manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["run_id"], "cli-source")
            self.assertEqual(manifest["team_id"], "team-source")
            self.assertEqual(manifest["seed"], 7)
            self.assertEqual(manifest["limits"]["tool_calls_per_checkpoint"], 3)
            self.assertEqual(manifest["limits"]["turns_per_checkpoint"], 5)

    def test_replay_scripted_oracle_engine_trace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "oracle-run"
            source_output = io.StringIO()
            with redirect_stdout(source_output):
                self.assertEqual(
                    main(
                        [
                            "run",
                            str(WORLD),
                            "--baseline",
                            "oracle",
                            "--run-id",
                            "cli-oracle",
                            "--output",
                            str(source),
                        ]
                    ),
                    0,
                )
            source_result = json.loads(source_output.getvalue())
            replay_output = io.StringIO()
            replay = root / "replay-run"
            with redirect_stdout(replay_output):
                self.assertEqual(
                    main(
                        [
                            "replay",
                            str(WORLD),
                            str(source / "trace.jsonl"),
                            "--output",
                            str(replay),
                        ]
                    ),
                    0,
                )
            replay_result = json.loads(replay_output.getvalue())
            self.assertEqual(source_result["status"], "completed")
            self.assertEqual(replay_result["status"], "completed")
            self.assertEqual(replay_result["state_hash"], source_result["state_hash"])

    def test_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "scorecard.json"
            source.write_text(
                '{"run_id":"run","world_id":"world","track":"open_team","execution_index":1,"strict_cycle_pass":true,"critical_violation":false,"category_scores":{},"secondary_metrics":{},"reliability":{},"resource_usage":{},"violations":[]}',
                encoding="utf-8",
            )
            target = Path(directory) / "report.json"
            self.assertEqual(main(["report", str(source), "--output", str(target)]), 0)
            self.assertTrue(target.is_file())


if __name__ == "__main__":
    unittest.main()
