from __future__ import annotations

import json
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
from contextlib import closing
from dataclasses import replace
from importlib import import_module
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

baselines = import_module("edlb.baselines")
grade_run = import_module("edlb.grading").grade_run
configuration_hash = import_module("edlb.grading")._configuration_hash
runner = import_module("edlb.runner")
protocol = import_module("edlb.protocol")
Message = protocol.Message
ToolCall = protocol.ToolCall
ToolResult = protocol.ToolResult
MAX_PROTOCOL_MESSAGE_BYTES = protocol.MAX_PROTOCOL_MESSAGE_BYTES
ToolDispatcher = import_module("edlb.tools").ToolDispatcher
PodmanConfig = baselines.PodmanConfig
build_podman_command = baselines.build_podman_command
BundleError = runner.BundleError
FixedHarnessScheduler = runner.FixedHarnessScheduler
OpenTeamRunner = runner.OpenTeamRunner
ProtocolViolation = runner.ProtocolViolation
RunLimits = runner.RunLimits
raw_open_world = runner.open_world
TEST_AGENT_MANIFEST = runner.deterministic_agent_manifest("test-agent")
TEST_ENVIRONMENT_MANIFEST = {
    "resolved": True,
    "runtime_version": "cpython-test",
    "image_digest": "sha256:" + "6" * 64,
    "git_revision": "7" * 40,
    "executor_policy_digest": "sha256:" + "8" * 64,
}


def open_world(*args, **kwargs):
    kwargs.setdefault("agent_manifest", TEST_AGENT_MANIFEST)
    kwargs.setdefault("environment_manifest", TEST_ENVIRONMENT_MANIFEST)
    return raw_open_world(*args, **kwargs)


load_world_bundle = runner.load_world_bundle
replay_trace = runner.replay_trace
run_replicates = runner.run_replicates
validate_world_bundle = runner.validate_world_bundle


def _manufacturing_world() -> Path:
    for manifest_path in sorted(
        (ROOT / "benchmarks/v1/output/public/train").glob("*/manifest.json")
    ):
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("vertical") == "manufacturing":
            return manifest_path.parent
    raise AssertionError("no manufacturing public train world found")


WORLD = _manufacturing_world()
REFERENCE_ADAPTER = (
    sys.executable,
    str(ROOT / "tests/reference_adapter.py"),
    str(WORLD / "reference_trace.jsonl"),
)
DEV_WORLD = next(
    (ROOT / "benchmarks/v1/output/public/dev").glob("*/manifest.json")
).parent


class RunnerTest(unittest.TestCase):
    def test_in_memory_world_bundle_schema_fails_closed(self) -> None:
        bundle = load_world_bundle(WORLD)
        invalid_type = dict(bundle.manifest)
        invalid_type["title"] = 42
        with self.assertRaisesRegex(BundleError, "scenario-manifest schema"):
            raw_open_world(replace(bundle, manifest=invalid_type))
        unknown_field = dict(bundle.manifest)
        unknown_field["oracle_hint"] = "closed_won"
        with self.assertRaisesRegex(BundleError, "unknown field 'oracle_hint'"):
            raw_open_world(replace(bundle, manifest=unknown_field))

    def test_programmatic_world_is_unresolved_until_execution_manifest_is_supplied(
        self,
    ) -> None:
        with raw_open_world(
            WORLD, run_id="unresolved-agent-test", track="fixed_harness"
        ) as engine:
            self.assertFalse(engine.manifest.agent_manifest["resolved"])
            self.assertFalse(engine.manifest.environment["resolved"])
            with self.assertRaisesRegex(BundleError, "resolved agent manifest"):
                FixedHarnessScheduler(engine, (sys.executable, "-c", "pass"))

    def test_external_runner_requires_resolved_environment(self) -> None:
        with (
            raw_open_world(
                WORLD,
                run_id="unresolved-environment-test",
                agent_manifest=TEST_AGENT_MANIFEST,
            ) as engine,
            self.assertRaisesRegex(BundleError, "resolved environment manifest"),
        ):
            OpenTeamRunner(engine, (sys.executable, "-c", "pass"))

    def test_runner_implementations_require_matching_manifest_tracks(self) -> None:
        with (
            open_world(WORLD, track="fixed_harness") as fixed,
            self.assertRaisesRegex(BundleError, "open_team manifest"),
        ):
            OpenTeamRunner(fixed, (sys.executable, "-c", "pass"))
        with (
            open_world(WORLD, track="open_team") as opened,
            self.assertRaisesRegex(BundleError, "fixed_harness manifest"),
        ):
            FixedHarnessScheduler(opened, (sys.executable, "-c", "pass"))

    def test_replicates_reject_unresolved_manifest_before_output_creation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "replicates"
            with self.assertRaisesRegex(BundleError, "resolved agent manifest"):
                run_replicates(
                    WORLD,
                    (sys.executable, "-c", "pass"),
                    trials=1,
                    output_dir=output,
                )
            self.assertFalse(output.exists())

    def test_runner_rejects_limits_that_differ_from_manifest(self) -> None:
        with (
            open_world(WORLD, run_id="limit-binding-test") as engine,
            self.assertRaisesRegex(BundleError, "do not match"),
        ):
            OpenTeamRunner(
                engine,
                (sys.executable, "-c", "pass"),
                RunLimits(timeout_seconds=1),
            )

    def test_private_access_uses_manifest_visibility_for_copies_and_symlinks(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            blind = root / "blind-source"
            shutil.copytree(WORLD, blind)
            manifest_path = blind / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["split"] = "blind"
            manifest["release_visibility"] = "private"
            manifest.update(
                {
                    "pair_id": "pair-private-test",
                    "counterfactual_variant": "a",
                    "causal_skeleton": "champion_departure",
                    "terminal_outcome": "closed_lost",
                    "seed": 1,
                }
            )
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            copied = root / "copied"
            shutil.copytree(blind, copied)
            with self.assertRaises(BundleError):
                load_world_bundle(copied)
            self.assertEqual(
                load_world_bundle(copied, allow_private=True).split, "blind"
            )
            linked = root / "linked"
            linked.symlink_to(blind, target_is_directory=True)
            with self.assertRaises(BundleError):
                load_world_bundle(linked)
            self.assertEqual(
                load_world_bundle(linked, allow_private=True).split, "blind"
            )

    def test_bundle_loader_rejects_schema_invalid_world_rows(self) -> None:
        mutations = {
            "manifest": ("manifest.json", "world_id", 7),
            "actor": ("actors.jsonl", "actor_id", 7),
            "event": ("events.jsonl", "sequence", "0"),
            "artifact": ("artifacts.jsonl", "version", "1"),
            "checkpoint": ("checkpoints.jsonl", "sequence", "0"),
        }
        with tempfile.TemporaryDirectory() as directory:
            for schema_name, (filename, field, value) in mutations.items():
                with self.subTest(schema_name=schema_name):
                    bundle = Path(directory) / schema_name
                    shutil.copytree(WORLD, bundle)
                    target = bundle / filename
                    if target.suffix == ".jsonl":
                        rows = [
                            json.loads(line)
                            for line in target.read_text(encoding="utf-8").splitlines()
                        ]
                        rows[0][field] = value
                        target.write_text(
                            "\n".join(json.dumps(row) for row in rows) + "\n",
                            encoding="utf-8",
                        )
                    else:
                        manifest = json.loads(target.read_text(encoding="utf-8"))
                        manifest[field] = value
                        target.write_text(json.dumps(manifest), encoding="utf-8")
                    with self.assertRaisesRegex(BundleError, "schema"):
                        load_world_bundle(bundle)

    def test_fixed_harness_exhausts_budget_on_final_allowed_tool_call(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            open_world(
                WORLD,
                run_id="fixed-cap-test",
                track="fixed_harness",
                limits=RunLimits(tool_calls_per_checkpoint=1, turns_per_checkpoint=4),
                db_path=Path(directory) / "run.sqlite",
            ) as engine,
        ):
            scheduler = FixedHarnessScheduler(
                engine,
                (sys.executable, "-c", "pass"),
                RunLimits(tool_calls_per_checkpoint=1, turns_per_checkpoint=4),
            )
            runner._activate_first(engine)
            messages = [
                Message(
                    "v1.0.0",
                    engine.manifest.run_id,
                    sequence,
                    f"cap-{sequence}",
                    engine.current_time,
                    "tool_call",
                    "account_executive",
                    tool_name="run.status",
                    arguments={},
                    observation_token="a" * 32,
                )
                for sequence in (1,)
            ]
            self.assertTrue(scheduler._process("account_executive", messages, set()))
            self.assertEqual(
                sum(event.kind == "tool_call" for event in engine.trace_events()), 1
            )
            self.assertEqual(
                engine.connection.execute(
                    "SELECT attempts FROM checkpoint_tool_usage"
                ).fetchone()[0],
                1,
            )
            runner._advance(engine, True, "fixed-cap-advance")
            self.assertEqual(engine.current_checkpoint_index, 1)

    def test_fixed_harness_rejects_wrong_activation_token(self) -> None:
        code = "import json,sys; r=json.loads(sys.stdin.readline()); c=r['checkpoint']; print(json.dumps({'protocol_version':'v1.0.0','run_id':r['run_id'],'sequence':1,'message_id':'wrong-token','occurred_at':r['occurred_at'],'kind':'checkpoint_complete','role':r['role'],'checkpoint_id':c['checkpoint_id'],'observation_token':'x'*32}))"
        with (
            tempfile.TemporaryDirectory() as directory,
            open_world(
                WORLD,
                run_id="fixed-token-test",
                track="fixed_harness",
                limits=RunLimits(timeout_seconds=5, retries=0),
                db_path=Path(directory) / "run.sqlite",
            ) as engine,
        ):
            scheduler = FixedHarnessScheduler(
                engine,
                (sys.executable, "-c", code),
                RunLimits(timeout_seconds=5, retries=0),
            )
            runner._activate_first(engine)
            with self.assertRaisesRegex(runner.AgentProcessError, "activation token"):
                scheduler._request("account_executive")

    def test_new_engine_trace_binds_safe_run_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with open_world(
                WORLD,
                run_id="manifest-bound-trace",
                team_id="team-bound",
                seed=7,
                limits=RunLimits(tool_calls_per_checkpoint=3, turns_per_checkpoint=5),
                db_path=Path(directory) / "source.sqlite",
            ) as source:
                rows = [event.to_dict() for event in source.trace_events()]
                start = rows[0]
                payload = start["payload"]
                self.assertEqual(
                    payload["manifest_fingerprint"], source.trace_manifest_fingerprint()
                )
                self.assertIn("scenario_hash", payload)
                self.assertNotIn("oracle", payload)
            with open_world(
                WORLD,
                run_id="manifest-bound-trace",
                team_id="team-bound",
                seed=7,
                limits=RunLimits(tool_calls_per_checkpoint=3, turns_per_checkpoint=5),
                db_path=Path(directory) / "replay.sqlite",
            ) as target:
                self.assertEqual(
                    target.manifest.agent_manifest, source.manifest.agent_manifest
                )
                replay_trace(target, rows)
                replay_hash = target.state_hash()
                target.persist_resource_usage({"latency_ms": 999, "turns": 4})
                finalized_hash = target.state_hash()
                self.assertNotEqual(finalized_hash, replay_hash)
                target.persist_resource_usage({"latency_ms": 1, "turns": 4})
                self.assertEqual(target.state_hash(), finalized_hash)
            tampered = [dict(row) for row in rows]
            tampered[0] = dict(tampered[0])
            tampered[0]["payload"] = dict(tampered[0]["payload"])
            tampered[0]["payload"]["seed"] = 8
            tampered[0]["payload_hash"] = runner._sha256(
                runner.to_json(tampered[0]["payload"]).encode("utf-8")
            )
            with (
                open_world(
                    WORLD,
                    run_id="manifest-bound-trace",
                    team_id="team-bound",
                    seed=7,
                    limits=RunLimits(
                        tool_calls_per_checkpoint=3, turns_per_checkpoint=5
                    ),
                    db_path=Path(directory) / "tampered.sqlite",
                ) as target,
                self.assertRaises(ProtocolViolation),
            ):
                replay_trace(target, tampered)

    def test_model_provider_settings_change_configuration_hash(self) -> None:
        first = runner.deterministic_agent_manifest("configuration-test")
        second = json.loads(json.dumps(first))
        second["models"]["deterministic"]["provider_settings"] = {
            "temperature": 0.3,
            "max_output_tokens": 9000,
        }
        with (
            open_world(
                WORLD,
                run_id="configuration-first",
                agent_manifest=first,
            ) as first_engine,
            open_world(
                WORLD,
                run_id="configuration-second",
                agent_manifest=second,
            ) as second_engine,
        ):
            self.assertNotEqual(
                configuration_hash(first_engine.manifest.to_dict()),
                configuration_hash(second_engine.manifest.to_dict()),
            )

    def test_environment_changes_configuration_hash(self) -> None:
        changed = {
            **TEST_ENVIRONMENT_MANIFEST,
            "executor_policy_digest": "sha256:" + "9" * 64,
        }
        with (
            open_world(WORLD, run_id="environment-first") as first,
            open_world(
                WORLD,
                run_id="environment-second",
                environment_manifest=changed,
            ) as second,
        ):
            self.assertNotEqual(
                configuration_hash(first.manifest.to_dict()),
                configuration_hash(second.manifest.to_dict()),
            )

    def test_fixed_harness_requires_one_model_configuration(self) -> None:
        manifest = json.loads(json.dumps(TEST_AGENT_MANIFEST))
        manifest["models"]["model-b"] = {
            **manifest["models"]["deterministic"],
            "model_id": "model-b",
            "model_digest": "sha256:" + "9" * 64,
        }
        manifest["roles"]["revops"] = "model-b"
        with self.assertRaisesRegex(BundleError, "one model configuration"):
            raw_open_world(
                WORLD,
                track="fixed_harness",
                agent_manifest=manifest,
                environment_manifest=TEST_ENVIRONMENT_MANIFEST,
            )
        with raw_open_world(
            WORLD,
            track="open_team",
            agent_manifest=manifest,
            environment_manifest=TEST_ENVIRONMENT_MANIFEST,
        ) as engine:
            self.assertEqual(
                engine.manifest.agent_manifest["roles"]["revops"], "model-b"
            )

    def test_replay_records_source_environment_but_remains_unresolved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with open_world(
                WORLD,
                run_id="cross-environment-replay",
                db_path=Path(directory) / "source.sqlite",
            ) as source:
                runner._activate_first(source)
                rows = [event.to_dict() for event in source.trace_events()]
                source_hash = source.state_hash()
            with raw_open_world(
                WORLD,
                run_id="cross-environment-replay",
                agent_manifest=TEST_AGENT_MANIFEST,
                environment_manifest=TEST_ENVIRONMENT_MANIFEST,
                db_path=Path(directory) / "target.sqlite",
            ) as target:
                result = replay_trace(target, rows)
                source_manifest = json.loads(target._meta("source_manifest") or "{}")
                self.assertEqual(
                    source_manifest["environment"], TEST_ENVIRONMENT_MANIFEST
                )
                self.assertEqual(target.state_hash(), source_hash)
                self.assertEqual(result.state_hash, source_hash)
                self.assertTrue(result.diagnostic_replay)
                scorecard = grade_run(
                    target,
                    WORLD / "rubric.json",
                    oracle=WORLD / "oracle.json",
                )
                self.assertFalse(scorecard["configuration_resolved"])

    def test_model_backed_trace_replay_requires_recorded_realizations(self) -> None:
        model_digest = "sha256:" + "3" * 64
        prompt_hash = "sha256:" + "4" * 64
        command = (
            sys.executable,
            "-c",
            "import json,sys; json.loads(sys.stdin.readline()); print(json.dumps({'text':'Recorded model response.'}))",
        )
        buyer = next(
            json.loads(line)
            for line in (WORLD / "actors.jsonl").read_text().splitlines()
            if json.loads(line)["kind"] == "buyer"
        )
        deal_id = json.loads((WORLD / "oracle.json").read_text())["verification_facts"][
            "deal_id"
        ]
        with (
            tempfile.TemporaryDirectory() as directory,
            open_world(
                WORLD,
                run_id="model-replay-test",
                db_path=Path(directory) / "source.sqlite",
                stakeholder_realizer_command=command,
                stakeholder_model_digest=model_digest,
                stakeholder_prompt_hash=prompt_hash,
            ) as source,
        ):
            runner._activate_first(source)
            checkpoint = source.current_checkpoint()
            if checkpoint is None:
                raise AssertionError("checkpoint did not activate")
            result = ToolDispatcher(source).dispatch(
                ToolCall(
                    "model-backed-send",
                    "communications.send",
                    "account_executive",
                    {
                        "channel": "email",
                        "recipients": [buyer["email"]],
                        "subject": "Decision request",
                        "body": "Please confirm the decision path.",
                        "semantic_envelope": {
                            "target_actor_id": buyer["actor_id"],
                            "purpose": "confirm the decision path",
                            "purpose_code": "advance_gate",
                            "gate_id": "gate-1",
                            "resolution": "accepted",
                            "related_records": [deal_id],
                            "requested_decisions": ["confirm owner"],
                            "decision_codes": ["confirm_gate_authority"],
                            "commitments": ["record before advancing"],
                            "commitment_codes": ["record_before_advancing"],
                            "commitment_owner_role": "account_executive",
                            "decision_due_at": checkpoint["window_end"],
                            "commitment_due_at": checkpoint["window_end"],
                            "attachments": ["artifact-1"],
                            "evidence_claims": [
                                {
                                    "artifact_id": "artifact-1",
                                    "claim_type": "supports_gate_resolution",
                                    "gate_id": "gate-1",
                                    "resolution": "accepted",
                                }
                            ],
                        },
                    },
                    "model-backed-send",
                )
            )
            self.assertTrue(result.ok)
            trace = [event.to_dict() for event in source.trace_events()]
            with (
                open_world(
                    WORLD,
                    run_id="model-replay-test",
                    db_path=Path(directory) / "target.sqlite",
                    stakeholder_model_digest=model_digest,
                    stakeholder_prompt_hash=prompt_hash,
                ) as target,
                self.assertRaisesRegex(
                    ProtocolViolation, "requires recorded stakeholder realizations"
                ),
            ):
                replay_trace(target, trace)

    def test_public_bundle_loads_without_future_crm_rows(self) -> None:
        summary = validate_world_bundle(WORLD)
        self.assertTrue(summary["valid"])
        with open_world(WORLD) as engine:
            records = engine.crm_search("revops", "", 100)
            manifest = json.loads((WORLD / "manifest.json").read_text(encoding="utf-8"))
            expected = {
                row["record_id"]
                for row in (
                    json.loads(line)
                    for line in (WORLD / "artifacts.jsonl")
                    .read_text(encoding="utf-8")
                    .splitlines()
                    if line
                )
                if row["kind"] in {"crm_record", "crm_history"}
                and row["available_at"] <= manifest["start_at"]
            }
            self.assertEqual({record["record_id"] for record in records}, expected)
            self.assertEqual(engine.current_checkpoint_index, -1)

    def test_scripted_oracle_replays_reference_and_strictly_passes(self) -> None:
        start = json.loads(
            (WORLD / "reference_trace.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()[0]
        )["payload"]
        with open_world(
            WORLD,
            run_id="scripted-oracle-test",
            agent_manifest=start["agent_manifest"],
            limits=RunLimits(**start["limits"]),
        ) as engine:
            result = baselines.ScriptedOracle().run(engine)
            score = grade_run(
                engine,
                WORLD / "rubric.json",
                trace=[item.to_dict() for item in engine.trace_events()],
                oracle=WORLD / "oracle.json",
            )
            self.assertEqual(result.status, "completed")
            self.assertGreaterEqual(score["execution_index"], 90.0)
            self.assertTrue(score["strict_cycle_pass"])

    def test_scripted_oracle_replays_public_dev_reference(self) -> None:
        reference = DEV_WORLD / "reference_trace.jsonl"
        start = next(
            row
            for row in (
                json.loads(line)
                for line in reference.read_text(encoding="utf-8").splitlines()
                if line.strip()
            )
            if row["kind"] == "start"
        )
        payload = start["payload"]
        with open_world(
            DEV_WORLD,
            run_id="scripted-oracle-dev-test",
            agent_manifest=payload["agent_manifest"],
            limits=RunLimits(**payload["limits"]),
        ) as engine:
            result = baselines.ScriptedOracle().run(engine)
            self.assertEqual(result.status, "completed")
            self.assertEqual(engine.status, "completed")

    def test_model_metadata_sums_message_metrics(self) -> None:
        result = runner.RunResult("metric-run", "world-1", "fixed_harness", "running")
        for sequence in (1, 2):
            runner._collect_model_metadata(
                result,
                Message(
                    "v1.0.0",
                    "metric-run",
                    sequence,
                    f"metric-{sequence}",
                    "2025-01-01T00:00:00Z",
                    "observation",
                    "account_executive",
                    payload={
                        "usage": {
                            "model_id": "model-1",
                            "model_latency_ms": 7,
                            "token_usage": {"input": 2, "output": 3},
                            "cost_minor_units": 4,
                        }
                    },
                ),
            )
        model = result.model_metadata["models"]["model-1"]
        self.assertEqual(model["model_latency_ms"], 14)
        self.assertEqual(model["token_usage"], {"input": 4, "output": 6})
        self.assertEqual(model["cost_minor_units"], 8)
        self.assertEqual(
            result.model_metadata["token_usage"], {"input": 4, "output": 6}
        )
        self.assertEqual(result.cost_minor_units, 8)
        result.invalid_actions = 2
        result.error_count = 3
        usage = result.to_dict()["resource_usage"]
        self.assertEqual(usage["tokens"], 10)
        self.assertEqual(usage["invalid_actions"], 2)
        self.assertEqual(usage["errors"], 3)
        self.assertEqual(
            usage["metric_availability"],
            {"cost_minor_units": True, "tokens": True},
        )

    def test_fixed_harness_rejects_invalid_or_partial_model_metrics(self) -> None:
        result = runner.RunResult("metric-run", "world-1", "fixed_harness", "running")
        for sequence, token_usage in enumerate(({}, {"input": 4}), 1):
            runner._collect_model_metadata(
                result,
                Message(
                    "v1.0.0",
                    "metric-run",
                    sequence,
                    f"metric-invalid-{sequence}",
                    "2025-01-01T00:00:00Z",
                    "observation",
                    "account_executive",
                    payload={
                        "usage": {
                            "model_latency_ms": -1,
                            "token_usage": token_usage,
                            "cost_minor_units": -2,
                        }
                    },
                ),
            )
        usage = result.to_dict()["resource_usage"]
        self.assertIsNone(usage["cost_minor_units"])
        self.assertIsNone(usage["tokens"])
        self.assertEqual(
            usage["metric_availability"],
            {"cost_minor_units": False, "tokens": False},
        )

    def test_fixed_harness_round_robin(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            open_world(
                WORLD,
                run_id="fixed-test",
                track="fixed_harness",
                limits=RunLimits(timeout_seconds=5),
                db_path=Path(directory) / "run.sqlite",
            ) as engine,
        ):
            result = FixedHarnessScheduler(
                engine,
                REFERENCE_ADAPTER,
                RunLimits(timeout_seconds=5),
                Path(directory),
            ).run()
            self.assertEqual(result.status, "completed", result.errors)
            checkpoint_count = len(
                json.loads((WORLD / "manifest.json").read_text(encoding="utf-8"))[
                    "checkpoint_ids"
                ]
            )
            self.assertGreater(result.turns, checkpoint_count * len(runner.ROLE_ORDER))
            self.assertEqual(engine.branch_resolutions()[0]["option"], "success")
            self.assertEqual(engine.run_status()["terminal_outcome"], "closed_won")
            self.assertTrue(
                any(
                    item["body"] == "Remediation plan ready."
                    for item in engine.team_inbox("account_executive")
                )
            )
            snapshots = [
                json.loads(line)
                for line in (Path(directory) / "snapshots.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
                if line.strip()
            ]
            diffs = [
                json.loads(line)
                for line in (Path(directory) / "state-diffs.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
                if line.strip()
            ]
            self.assertEqual(len(snapshots), len(diffs))
            self.assertIsNone(snapshots[0]["previous_state_hash"])
            for previous, current, diff in zip(snapshots, snapshots[1:], diffs[1:]):
                self.assertEqual(current["previous_state_hash"], previous["state_hash"])
                self.assertEqual(diff["previous_state_hash"], previous["state_hash"])
                self.assertEqual(diff["state_hash"], current["state_hash"])

    def test_fixed_harness_reactivates_yielded_roles_without_limits(self) -> None:
        with open_world(
            WORLD,
            run_id="fixed-yield-reactivation",
            track="fixed_harness",
        ) as engine:
            result = FixedHarnessScheduler(
                engine, (*REFERENCE_ADAPTER, "yield-first"), RunLimits()
            ).run()
        checkpoint_count = len(
            json.loads((WORLD / "manifest.json").read_text(encoding="utf-8"))[
                "checkpoint_ids"
            ]
        )
        self.assertEqual(result.status, "completed")
        self.assertGreater(result.turns, checkpoint_count * len(runner.ROLE_ORDER))

    def test_fixed_harness_empty_activation_consumes_explicit_turn(self) -> None:
        limits = RunLimits(turns_per_checkpoint=1)
        with open_world(
            WORLD,
            run_id="fixed-empty-activation",
            track="fixed_harness",
            limits=limits,
        ) as engine:
            result = FixedHarnessScheduler(
                engine, (sys.executable, "-c", "pass"), limits
            ).run()
        oracle = json.loads((WORLD / "oracle.json").read_text(encoding="utf-8"))
        branch = oracle["verification_facts"]["branches"][0]
        checkpoints = {
            row["checkpoint_id"]: row
            for row in (
                json.loads(line)
                for line in (WORLD / "checkpoints.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
                if line.strip()
            )
        }
        expected_turns = int(
            checkpoints[branch["resolution_checkpoint_id"]]["sequence"]
        )
        self.assertEqual(result.turns, expected_turns)
        self.assertEqual(result.status, "failed")
        self.assertIn(
            "terminal fallback prerequisites are unresolved", result.errors[0]
        )

    def test_open_team_process(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            open_world(
                WORLD,
                run_id="open-test",
                limits=RunLimits(timeout_seconds=5),
                db_path=Path(directory) / "run.sqlite",
            ) as engine,
        ):
            result = OpenTeamRunner(
                engine,
                REFERENCE_ADAPTER,
                RunLimits(timeout_seconds=5),
                Path(directory),
            ).run()
            self.assertEqual(result.status, "completed")

    def test_protocol_decoder_rejects_oversized_message_before_json(self) -> None:
        with self.assertRaisesRegex(protocol.ProtocolError, "transport ceiling"):
            protocol.decode("x" * (MAX_PROTOCOL_MESSAGE_BYTES + 1))

    def test_fixed_harness_rejects_oversized_unterminated_message(self) -> None:
        code = f"import sys; sys.stdout.write('x'*{MAX_PROTOCOL_MESSAGE_BYTES + 1}); sys.stdout.flush()"
        with open_world(
            WORLD,
            run_id="fixed-message-ceiling-test",
            track="fixed_harness",
            limits=RunLimits(timeout_seconds=5),
        ) as engine:
            runner._activate_first(engine)
            scheduler = FixedHarnessScheduler(
                engine,
                (sys.executable, "-c", code),
                RunLimits(timeout_seconds=5),
            )
            with self.assertRaisesRegex(runner.AgentProcessError, "transport ceiling"):
                scheduler._request("account_executive")

    def test_open_team_rejects_oversized_unterminated_message(self) -> None:
        code = f"import sys; [sys.stdin.readline() for _ in range(5)]; sys.stdout.write('x'*{MAX_PROTOCOL_MESSAGE_BYTES + 1}); sys.stdout.flush()"
        with open_world(
            WORLD,
            run_id="open-message-ceiling-test",
            limits=RunLimits(timeout_seconds=5),
        ) as engine:
            result = OpenTeamRunner(
                engine,
                (sys.executable, "-c", code),
                RunLimits(timeout_seconds=5),
            ).run()
            self.assertEqual(result.status, "failed")
            self.assertIn("transport ceiling", result.errors[0])

    def test_default_runner_limits_are_unbounded_and_ignore_world_caps(self) -> None:
        with open_world(WORLD, run_id="checkpoint-cap-test") as engine:
            runner._activate_first(engine)
            checkpoint = engine.current_checkpoint()
            self.assertIsNotNone(checkpoint)
            observation = runner._observation(
                engine,
                "account_executive",
                1,
                RunLimits(),
                "observation-cap-test",
                "a" * 32,
            )
            start_payload = runner._start_payload(engine, RunLimits())
        assert checkpoint is not None and observation.payload is not None
        self.assertNotIn("max_tool_calls", checkpoint)
        self.assertNotIn("max_turns", checkpoint)
        self.assertEqual(
            observation.payload["budget"],
            {"tool_calls_remaining": None, "turns_remaining": None},
        )
        self.assertEqual(
            start_payload["limits"],
            {
                "tool_calls_per_checkpoint": None,
                "turns_per_checkpoint": None,
                "timeout_seconds": None,
                "retries": 0,
            },
        )
        self.assertEqual(
            engine.manifest.limits,
            {
                "tool_calls_per_checkpoint": None,
                "turns_per_checkpoint": None,
                "timeout_seconds": None,
                "retries": 0,
            },
        )

    def test_fixed_harness_retains_full_role_context(self) -> None:
        with open_world(
            WORLD, run_id="full-context-test", track="fixed_harness"
        ) as engine:
            scheduler = FixedHarnessScheduler(engine, (sys.executable, "-c", "pass"))
            for index in range(40):
                scheduler._append(
                    "account_executive", {"kind": "message", "index": index}
                )
            self.assertEqual(len(scheduler.contexts["account_executive"]), 40)

    def test_fixed_harness_retains_complete_activation_context(self) -> None:
        with open_world(
            WORLD, run_id="activation-context-test", track="fixed_harness"
        ) as engine:
            runner._activate_first(engine)
            scheduler = FixedHarnessScheduler(engine, (sys.executable, "-c", "pass"))
            scheduler._request("account_executive")
            observation = scheduler.contexts["account_executive"][-1]
        self.assertEqual(
            set(observation),
            {
                "kind",
                "role",
                "occurred_at",
                "checkpoint",
                "alerts",
                "unread_team_messages",
                "budget",
            },
        )
        self.assertTrue(observation["alerts"])
        self.assertEqual(
            observation["budget"],
            {"tool_calls_per_checkpoint": None, "turns_per_checkpoint": None},
        )

    def test_fixed_harness_retries_explicit_response_timeouts(self) -> None:
        with open_world(
            WORLD,
            run_id="timeout-retry-test",
            track="fixed_harness",
            limits=RunLimits(timeout_seconds=0.01, retries=3),
        ) as engine:
            scheduler = FixedHarnessScheduler(
                engine,
                (sys.executable, "-c", "import time; time.sleep(1)"),
                RunLimits(timeout_seconds=0.01, retries=3),
            )
            runner._activate_first(engine)
            with self.assertRaises(runner.AgentProcessError):
                scheduler._request("account_executive")
            self.assertEqual(scheduler.result.retries, 3)

    def test_fixed_harness_timeout_covers_process_after_stdout_eof(self) -> None:
        code = "import os,time; os.close(1); time.sleep(10)"
        limits = RunLimits(timeout_seconds=0.05)
        with open_world(
            WORLD,
            run_id="fixed-eof-timeout",
            track="fixed_harness",
            limits=limits,
        ) as engine:
            scheduler = FixedHarnessScheduler(
                engine, (sys.executable, "-c", code), limits
            )
            runner._activate_first(engine)
            with self.assertRaisesRegex(runner.AgentProcessError, "timed out"):
                scheduler._request("account_executive")

    def test_open_team_timeout_covers_process_after_stdout_eof(self) -> None:
        code = "import os,sys,time; [sys.stdin.readline() for _ in range(5)]; os.close(1); time.sleep(10)"
        limits = RunLimits(timeout_seconds=0.05)
        with open_world(WORLD, run_id="open-eof-timeout", limits=limits) as engine:
            result = OpenTeamRunner(engine, (sys.executable, "-c", code), limits).run()
        self.assertEqual(result.status, "failed")
        self.assertIn("agent response timed out", result.errors[0])

    def test_open_team_partial_output_does_not_reset_timeout(self) -> None:
        programs = {
            "blank": "import sys,time; [sys.stdin.readline() for _ in range(5)]; [(sys.stdout.write('\\n'),sys.stdout.flush(),time.sleep(.02)) for _ in range(20)]",
            "partial": "import sys,time; [sys.stdin.readline() for _ in range(5)]; [(sys.stdout.write('x'),sys.stdout.flush(),time.sleep(.02)) for _ in range(20)]",
        }
        limits = RunLimits(timeout_seconds=0.05)
        for name, code in programs.items():
            with (
                self.subTest(name=name),
                open_world(WORLD, run_id=f"open-{name}-drip", limits=limits) as engine,
            ):
                result = OpenTeamRunner(
                    engine, (sys.executable, "-c", code), limits
                ).run()
                self.assertEqual(result.status, "failed")
                self.assertIn("agent response timed out", result.errors[0])

    def test_open_team_timeout_excludes_tool_processing(self) -> None:
        code = """import json,sys
messages=[json.loads(sys.stdin.readline()) for _ in range(5)]
observation=next(message for message in messages if message.get('kind')=='observation')
call={'protocol_version':'v1.0.0','run_id':observation['run_id'],'sequence':1,'message_id':'slow-tool','occurred_at':observation['occurred_at'],'kind':'tool_call','role':observation['role'],'tool_name':'run.status','arguments':{},'observation_token':observation['observation_token']}
print(json.dumps(call),flush=True)
json.loads(sys.stdin.readline())
end={'protocol_version':'v1.0.0','run_id':observation['run_id'],'sequence':2,'message_id':'after-slow-tool','occurred_at':observation['occurred_at'],'kind':'run_end','role':'system','status':'failed','observation_token':observation['observation_token']}
print(json.dumps(end),flush=True)
"""

        class SlowDispatcher:
            def __init__(self, engine):
                self.delegate = ToolDispatcher(engine)

            def schemas(self):
                return self.delegate.schemas()

            def dispatch(self, call):
                time.sleep(0.1)
                return self.delegate.dispatch(call)

        limits = RunLimits(timeout_seconds=0.05)
        with open_world(WORLD, run_id="open-slow-tool", limits=limits) as engine:
            result = OpenTeamRunner(
                engine,
                (sys.executable, "-c", code),
                limits,
                dispatcher=SlowDispatcher(engine),
            ).run()
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.errors, [])

    def test_open_team_timeout_bounds_tool_result_delivery(self) -> None:
        code = """import json,sys,time
messages=[json.loads(sys.stdin.readline()) for _ in range(5)]
observation=next(message for message in messages if message.get('kind')=='observation')
call={'protocol_version':'v1.0.0','run_id':observation['run_id'],'sequence':1,'message_id':'large-result','occurred_at':observation['occurred_at'],'kind':'tool_call','role':observation['role'],'tool_name':'run.status','arguments':{},'observation_token':observation['observation_token']}
print(json.dumps(call),flush=True)
time.sleep(10)
"""

        class LargeResultDispatcher:
            def __init__(self, engine):
                self.delegate = ToolDispatcher(engine)

            def schemas(self):
                return self.delegate.schemas()

            def dispatch(self, call):
                return ToolResult(
                    call.call_id, True, {"payload": "x" * (2 * 1024 * 1024)}
                )

        limits = RunLimits(timeout_seconds=0.1)
        with open_world(WORLD, run_id="open-large-result", limits=limits) as engine:
            result = OpenTeamRunner(
                engine,
                (sys.executable, "-c", code),
                limits,
                dispatcher=LargeResultDispatcher(engine),
            ).run()
        self.assertEqual(result.status, "failed")
        self.assertIn("agent response timed out", result.errors[0])

    def test_both_runners_trace_protocol_team_message_and_yield(self) -> None:
        open_code = "import json,sys;\nfor line in sys.stdin:\n m=json.loads(line)\n if m.get('kind')=='observation':\n  print(json.dumps({'protocol_version':'v1.0.0','run_id':m['run_id'],'sequence':1,'message_id':'team-open','occurred_at':m['occurred_at'],'kind':'team_message','role':'account_executive','recipient_role':'domain_specialist','payload':{'body':'Review the evidence.','usage':{'cost_minor_units':-1,'token_usage':{}}},'observation_token':m['observation_token']}),flush=True); print(json.dumps({'protocol_version':'v1.0.0','run_id':m['run_id'],'sequence':2,'message_id':'yield-open','occurred_at':m['occurred_at'],'kind':'yield','role':'account_executive','reason':'Waiting for evidence.','observation_token':m['observation_token']}),flush=True); break"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with open_world(
                WORLD,
                run_id="fixed-protocol",
                track="fixed_harness",
                limits=RunLimits(timeout_seconds=5),
                db_path=root / "fixed.sqlite",
            ) as engine:
                result = FixedHarnessScheduler(
                    engine,
                    (*REFERENCE_ADAPTER, "team-yield"),
                    RunLimits(timeout_seconds=5),
                    root / "fixed",
                ).run()
                kinds = [event.kind for event in engine.trace_events()]
                self.assertEqual(result.status, "completed")
                checkpoint_count = len(
                    json.loads((WORLD / "manifest.json").read_text(encoding="utf-8"))[
                        "checkpoint_ids"
                    ]
                )
                self.assertGreater(
                    result.turns, checkpoint_count * len(runner.ROLE_ORDER)
                )
                self.assertEqual(engine.branch_resolutions()[0]["option"], "success")
                self.assertIn("team_message", kinds)
                self.assertIn("yield", kinds)
                self.assertGreaterEqual(len(engine.team_inbox("domain_specialist")), 2)
            with open_world(
                WORLD,
                run_id="open-protocol",
                limits=RunLimits(timeout_seconds=5, retries=0),
                db_path=root / "open.sqlite",
            ) as engine:
                result = OpenTeamRunner(
                    engine,
                    (sys.executable, "-c", open_code),
                    RunLimits(timeout_seconds=5, retries=0),
                    root / "open",
                ).run()
                kinds = [event.kind for event in engine.trace_events()]
                self.assertEqual(result.status, "failed")
                self.assertIn("team_message", kinds)
                self.assertIn("yield", kinds)
                self.assertEqual(
                    len(
                        [
                            message
                            for message in engine.team_inbox("domain_specialist")
                            if message.get("metadata", {}).get("protocol_message_id")
                            == "team-open"
                        ]
                    ),
                    1,
                )
                usage = result.to_dict()["resource_usage"]
                self.assertIsNone(usage["cost_minor_units"])
                self.assertIsNone(usage["tokens"])

    def test_open_team_discards_old_burst_after_checkpoint_cap(self) -> None:
        checkpoints = [
            json.loads(line)
            for line in (WORLD / "checkpoints.jsonl").read_text().splitlines()
            if line.strip()
        ]
        next_time = checkpoints[1]["available_at"]
        code = f"""import json,sys
sequence=0
sent=False
for line in sys.stdin:
 message=json.loads(line)
 if message.get('kind')!='observation': continue
 if not sent and message['role']=='account_executive':
  for index in range(5):
   sequence+=1
   timestamp=message['occurred_at'] if index<2 else {next_time!r}
   print(json.dumps({{'protocol_version':'v1.0.0','run_id':message['run_id'],'sequence':sequence,'message_id':f'burst-{{index}}','occurred_at':timestamp,'kind':'tool_call','role':'account_executive','tool_name':'run.status','arguments':{{}},'observation_token':message['observation_token']}}),flush=True)
  sent=True
"""
        with (
            tempfile.TemporaryDirectory() as directory,
            open_world(
                WORLD,
                run_id="open-burst-cap-test",
                limits=RunLimits(tool_calls_per_checkpoint=1, turns_per_checkpoint=8),
                db_path=Path(directory) / "run.sqlite",
            ) as engine,
        ):
            result = OpenTeamRunner(
                engine,
                (sys.executable, "-c", code),
                RunLimits(tool_calls_per_checkpoint=1, turns_per_checkpoint=8),
            ).run()
            self.assertEqual(result.status, "failed")
            self.assertEqual(engine.current_checkpoint_index, 1)
            self.assertEqual(
                sum(event.kind == "tool_call" for event in engine.trace_events()), 1
            )
            self.assertEqual(
                [
                    tuple(row)
                    for row in engine.connection.execute(
                        "SELECT attempts FROM checkpoint_tool_usage"
                    )
                ],
                [(1,)],
            )
            self.assertEqual(result.invalid_actions, 1)
            self.assertIn("current observation token", result.errors[0])

    def test_open_team_advances_on_exact_checkpoint_cap(self) -> None:
        code = "import json,sys; sent=False\nfor line in sys.stdin:\n m=json.loads(line)\n if m.get('kind')=='observation' and m['role']=='account_executive' and not sent:\n  sent=True; print(json.dumps({'protocol_version':'v1.0.0','run_id':m['run_id'],'sequence':1,'message_id':'final-allowed','occurred_at':m['occurred_at'],'kind':'tool_call','role':m['role'],'tool_name':'run.status','arguments':{},'observation_token':m['observation_token']}),flush=True)"
        for limits in (
            RunLimits(
                tool_calls_per_checkpoint=1,
                turns_per_checkpoint=8,
                timeout_seconds=0.2,
                retries=0,
            ),
            RunLimits(
                tool_calls_per_checkpoint=8,
                turns_per_checkpoint=1,
                timeout_seconds=0.2,
                retries=0,
            ),
        ):
            with (
                self.subTest(limits=limits),
                tempfile.TemporaryDirectory() as directory,
                open_world(
                    WORLD,
                    run_id=f"open-exact-cap-{limits.tool_calls_per_checkpoint}",
                    limits=limits,
                    db_path=Path(directory) / "run.sqlite",
                ) as engine,
            ):
                result = OpenTeamRunner(
                    engine,
                    (sys.executable, "-c", code),
                    limits,
                ).run()
                self.assertEqual(result.status, "failed")
                self.assertEqual(engine.current_checkpoint_index, 1)
                self.assertEqual(
                    sum(event.kind == "tool_call" for event in engine.trace_events()),
                    1,
                )

    def test_replay_validates_hash_and_replays_protocol_without_duplicate_effects(
        self,
    ) -> None:
        trace_path = WORLD / "reference_trace.jsonl"
        rows = [
            json.loads(line)
            for line in trace_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        tampered = [dict(row) for row in rows]
        tampered[1]["payload_hash"] = "sha256:" + "0" * 64
        with tempfile.TemporaryDirectory() as directory:
            with (
                open_world(
                    WORLD,
                    run_id=rows[0]["run_id"],
                    db_path=Path(directory) / "bad.sqlite",
                ) as engine,
                self.assertRaises(ProtocolViolation),
            ):
                replay_trace(engine, tampered)
            protocol_rows = [
                {
                    "protocol_version": "v1.0.0",
                    "run_id": "protocol-source",
                    "sequence": 0,
                    "message_id": "start-protocol",
                    "occurred_at": "2025-01-01T00:00:00Z",
                    "kind": "start",
                    "role": "system",
                    "payload": {
                        "world_id": json.loads((WORLD / "manifest.json").read_text())[
                            "world_id"
                        ],
                        "track": "open_team",
                        "scenario_hash": runner._digest_bundle(
                            load_world_bundle(WORLD)
                        ),
                    },
                },
                {
                    "protocol_version": "v1.0.0",
                    "run_id": "protocol-source",
                    "sequence": 1,
                    "message_id": "team-protocol",
                    "occurred_at": "2025-01-01T00:00:00Z",
                    "kind": "team_message",
                    "role": "account_executive",
                    "observation_token": "a" * 32,
                    "recipient_role": "domain_specialist",
                    "payload": {"body": "Review the evidence."},
                },
                {
                    "protocol_version": "v1.0.0",
                    "run_id": "protocol-source",
                    "sequence": 2,
                    "message_id": "yield-protocol",
                    "occurred_at": "2025-01-01T00:00:00Z",
                    "kind": "yield",
                    "role": "account_executive",
                    "observation_token": "a" * 32,
                    "reason": "Waiting for evidence.",
                },
            ]
            with open_world(
                WORLD,
                run_id="protocol-target",
                db_path=Path(directory) / "protocol.sqlite",
            ) as engine:
                replay_trace(engine, protocol_rows)
                first_hash = engine.state_hash()
                first_count = len(
                    [
                        message
                        for message in engine.team_inbox("domain_specialist")
                        if message.get("metadata", {}).get("protocol_message_id")
                        == "team-protocol"
                    ]
                )
                replay_trace(engine, protocol_rows)
                self.assertEqual(first_count, 1)
                self.assertEqual(
                    len(
                        [
                            message
                            for message in engine.team_inbox("domain_specialist")
                            if message.get("metadata", {}).get("protocol_message_id")
                            == "team-protocol"
                        ]
                    ),
                    first_count,
                )
                self.assertEqual(engine.state_hash(), first_hash)

    def test_reference_replay_hashes_are_deterministic(self) -> None:
        trace = WORLD / "reference_trace.jsonl"
        rubric = WORLD / "rubric.json"
        oracle = WORLD / "oracle.json"
        start = json.loads(trace.read_text(encoding="utf-8").splitlines()[0])
        run_id = start["run_id"]
        configuration = start["payload"]
        scores = []
        persisted_scores = []
        states = []
        with tempfile.TemporaryDirectory() as directory:
            for index in (1, 2):
                database = Path(directory) / f"replay-{index}.sqlite"
                with open_world(
                    WORLD,
                    run_id=run_id,
                    agent_manifest=configuration["agent_manifest"],
                    limits=RunLimits(**configuration["limits"]),
                    db_path=database,
                ) as engine:
                    replay_trace(engine, trace)
                    states.append(engine.state_hash())
                    scores.append(
                        grade_run(engine, rubric, oracle=oracle)["score_hash"]
                    )
                persisted = grade_run(database, rubric, oracle=oracle)
                self.assertEqual(persisted["state_hash"], states[-1])
                persisted_scores.append(persisted["score_hash"])
        self.assertEqual(states[0], states[1])
        self.assertEqual(scores[0], scores[1])
        self.assertEqual(persisted_scores[0], persisted_scores[1])

    def test_reference_replay_rejects_rebound_configuration(self) -> None:
        start = json.loads(
            (WORLD / "reference_trace.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()[0]
        )
        configuration = start["payload"]
        tampered = json.loads(json.dumps(start))
        tampered_configuration = tampered["payload"]
        model = next(iter(tampered_configuration["agent_manifest"]["models"].values()))
        model["provider_settings"] = {"temperature": 0.5}
        tampered_configuration["configuration_hash"] = runner.stable_hash(
            {
                "agent_manifest": tampered_configuration["agent_manifest"],
                "limits": tampered_configuration["limits"],
            }
        )
        with (
            open_world(
                WORLD,
                run_id=start["run_id"],
                agent_manifest=configuration["agent_manifest"],
                limits=RunLimits(**configuration["limits"]),
            ) as engine,
            self.assertRaisesRegex(ProtocolViolation, "does not match the active run"),
        ):
            replay_trace(engine, [tampered])

    def test_open_team_tool_completion_advances_checkpoints(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            open_world(
                WORLD,
                run_id="open-tool-test",
                limits=RunLimits(timeout_seconds=5),
                db_path=Path(directory) / "run.sqlite",
            ) as engine,
        ):
            result = OpenTeamRunner(
                engine,
                REFERENCE_ADAPTER,
                RunLimits(timeout_seconds=5),
                Path(directory),
            ).run()
            self.assertEqual(result.status, "completed")
            checkpoint_count = len(
                json.loads((WORLD / "manifest.json").read_text(encoding="utf-8"))[
                    "checkpoint_ids"
                ]
            )
            reference = [
                json.loads(line)
                for line in (WORLD / "reference_trace.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertEqual(
                sum(
                    row.get("tool_name") == "run.complete_checkpoint"
                    for row in reference
                ),
                checkpoint_count * 4,
            )
            self.assertEqual(
                result.tool_calls,
                sum(row["kind"] == "tool_call" for row in reference),
            )

    def _bundle_with_artifact_path(self, directory: Path, path: str) -> Path:
        bundle = directory / "world"
        shutil.copytree(WORLD, bundle)
        rows = [
            json.loads(line)
            for line in (bundle / "artifacts.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip()
        ]
        source_uri = (
            f"artifacts/../../{Path(path).name}"
            if Path(path).is_absolute() or path.startswith("..")
            else f"artifacts/../{path}"
        )
        rows[0]["content"]["source_uri"] = source_uri
        (bundle / "artifacts.jsonl").write_text(
            "\n".join(json.dumps(row) for row in rows) + "\n",
            encoding="utf-8",
        )
        return bundle

    def test_world_bundle_rejects_absolute_artifact_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            outside = Path(directory) / "outside.txt"
            outside.write_text("not a bundle artifact", encoding="utf-8")
            bundle = self._bundle_with_artifact_path(
                directory=Path(directory), path=str(outside)
            )
            with self.assertRaisesRegex(BundleError, "escapes bundle root"):
                load_world_bundle(bundle)

    def test_world_bundle_rejects_parent_artifact_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            outside = Path(directory) / "outside.txt"
            outside.write_text("not a bundle artifact", encoding="utf-8")
            bundle = self._bundle_with_artifact_path(
                directory=Path(directory), path="../outside.txt"
            )
            with self.assertRaisesRegex(BundleError, "escapes bundle root"):
                load_world_bundle(bundle)

    def test_world_bundle_rejects_symlinked_artifact_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            outside = base / "outside.txt"
            outside.write_text("not a bundle artifact", encoding="utf-8")
            bundle = base / "world"
            shutil.copytree(WORLD, bundle)
            (bundle / "artifact-escape.txt").symlink_to(outside)
            rows = [
                json.loads(line)
                for line in (bundle / "artifacts.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
                if line.strip()
            ]
            rows[0]["content"]["source_uri"] = "artifacts/../artifact-escape.txt"
            (bundle / "artifacts.jsonl").write_text(
                "\n".join(json.dumps(row) for row in rows) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(BundleError, "escapes bundle root"):
                load_world_bundle(bundle)

    def test_podman_does_not_mount_private_world(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            world = Path(directory) / "private" / "blind" / "world"
            output = Path(directory) / "output"
            world.mkdir(parents=True)
            image = "edlb:test@sha256:" + "a" * 64
            command = build_podman_command(
                PodmanConfig(image, world, output, ("edlb", "run"))
            )
            text = " ".join(command)
            self.assertNotIn(str(world), text)
            self.assertNotIn("/world", text)
            self.assertNotIn(str(output), text)
            self.assertNotIn("/output", text)
            self.assertFalse(output.exists())
            self.assertIn("--network=none", command)
            self.assertIn("--interactive", command)
            self.assertIn("--read-only-tmpfs=false", command)
            self.assertIn("--image-volume=ignore", command)
            self.assertEqual(command.count("--pids-limit=-1"), 1)
            self.assertEqual(command.count("--ulimit=host"), 1)
            self.assertEqual(sum(item.startswith("--ulimit") for item in command), 1)
            self.assertIn("/tmp:rw,noexec,nosuid,nodev,size=64m", command)

    def test_podman_requires_immutable_image(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            world = Path(directory) / "private" / "blind" / "world"
            world.mkdir(parents=True)
            with self.assertRaises(ValueError):
                build_podman_command(
                    PodmanConfig(
                        "edlb:test", world, Path(directory) / "output", ("edlb", "run")
                    )
                )

    def test_only_revops_can_merge(self) -> None:
        grants = {grant.role: grant for grant in runner._role_grants()}
        self.assertNotIn("crm.merge", grants["account_executive"].permissions)
        self.assertIn("crm.merge", grants["revops"].permissions)

    def test_scoped_seeded_records_stay_within_visible_role(self) -> None:
        with open_world(WORLD) as engine:
            bundle = replace(
                engine._edlb_bundle,
                artifacts=(
                    {
                        "artifact_id": "scoped-message",
                        "kind": "email",
                        "title": "Scoped message",
                        "created_at": engine.current_time,
                        "available_at": engine.current_time,
                        "visibility": "role_scoped",
                        "visible_roles": ["account_executive"],
                        "content": {"body": "private message"},
                    },
                    {
                        "artifact_id": "scoped-record",
                        "kind": "crm_record",
                        "created_at": engine.current_time,
                        "available_at": engine.current_time,
                        "visibility": "role_scoped",
                        "visible_roles": ["account_executive"],
                        "content": {
                            "record": {"record_id": "scoped-record", "name": "private"}
                        },
                    },
                ),
            )
            runner._seed_systems(engine, bundle)
            self.assertEqual(
                engine.communications_search("domain_specialist", "scoped-message"), []
            )
            self.assertEqual(
                engine.crm_search("domain_specialist", "scoped-record"), []
            )
            self.assertEqual(
                len(
                    engine.communications_search("account_executive", "scoped-message")
                ),
                1,
            )
            self.assertEqual(
                len(engine.crm_search("account_executive", "scoped-record")), 1
            )

    def test_replicates_use_fresh_runs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            results = run_replicates(
                WORLD,
                REFERENCE_ADAPTER,
                track="fixed_harness",
                trials=4,
                limits=RunLimits(timeout_seconds=5),
                agent_manifest=TEST_AGENT_MANIFEST,
                environment_manifest=TEST_ENVIRONMENT_MANIFEST,
                output_dir=directory,
            )
            self.assertEqual(len(results), 4)
            self.assertTrue(all(result.status == "completed" for result in results))
            self.assertEqual(len({result.run_id for result in results}), 4)
            manifests = [
                json.loads(
                    (
                        Path(directory) / f"trial-{trial}" / "run-manifest.json"
                    ).read_text()
                )
                for trial in range(1, 5)
            ]
            official = tuple(manifests[0]["stakeholder_manifest"]["official_seeds"])
            self.assertEqual(len(official), 3)
            self.assertEqual(
                [manifest["seed"] for manifest in manifests[:3]], list(official)
            )
            self.assertEqual(
                [
                    manifest["stakeholder_manifest"]["seed"]
                    for manifest in manifests[:3]
                ],
                list(official),
            )
            self.assertEqual(manifests[3]["seed"], max(official) + 1)
            self.assertEqual(manifests[3]["stakeholder_manifest"]["seed"], official[0])
            self.assertEqual(
                [
                    tuple(manifest["stakeholder_manifest"]["official_seeds"])
                    for manifest in manifests
                ],
                [official] * 4,
            )
            with closing(
                sqlite3.connect(Path(directory) / "trial-4" / "run.sqlite")
            ) as db:
                realized_seed = db.execute(
                    "SELECT seed FROM stakeholder_realizations"
                ).fetchone()
            self.assertEqual(realized_seed, (official[0],))


if __name__ == "__main__":
    unittest.main()
