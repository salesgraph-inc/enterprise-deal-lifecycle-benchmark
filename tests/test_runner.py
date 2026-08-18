from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from dataclasses import replace
from importlib import import_module
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

baselines = import_module("edlb.baselines")
grade_run = import_module("edlb.grading").grade_run
runner = import_module("edlb.runner")
Message = import_module("edlb.protocol").Message
ToolCall = import_module("edlb.protocol").ToolCall
ToolDispatcher = import_module("edlb.tools").ToolDispatcher
PodmanConfig = baselines.PodmanConfig
build_podman_command = baselines.build_podman_command
BundleError = runner.BundleError
FixedHarnessScheduler = runner.FixedHarnessScheduler
OpenTeamRunner = runner.OpenTeamRunner
ProtocolViolation = runner.ProtocolViolation
RunLimits = runner.RunLimits
open_world = runner.open_world
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
DEV_WORLD = next(
    (ROOT / "benchmarks/v1/output/public/dev").glob("*/manifest.json")
).parent


class RunnerTest(unittest.TestCase):
    def test_blind_access_uses_manifest_split_for_copies_and_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            blind = root / "blind-source"
            shutil.copytree(WORLD, blind)
            manifest_path = blind / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["split"] = "blind"
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

    def test_fixed_harness_exhausts_budget_on_final_allowed_tool_call(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            open_world(
                WORLD, run_id="fixed-cap-test", db_path=Path(directory) / "run.sqlite"
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
        code = "import json,sys; r=json.loads(sys.stdin.readline()); c=r['checkpoint']; print(json.dumps({'protocol_version':'v1.0.0','run_id':r['run_id'],'sequence':1,'message_id':'wrong-token','occurred_at':r['occurred_at'],'kind':'checkpoint_complete','role':r['role'],'checkpoint_id':c['checkpoint_id'],'summary':'reviewed','observation_token':'x'*32}))"
        with (
            tempfile.TemporaryDirectory() as directory,
            open_world(
                WORLD,
                run_id="fixed-token-test",
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

    def test_model_backed_trace_replay_requires_recorded_realizations(self) -> None:
        model_digest = "sha256:" + "3" * 64
        prompt_hash = "sha256:" + "4" * 64
        command = (
            sys.executable,
            "-c",
            "import json,sys; json.loads(sys.stdin.readline()); print(json.dumps({'text':'Recorded model response.'}))",
        )
        buyer = next(
            json.loads(line)["email"]
            for line in (WORLD / "actors.jsonl").read_text().splitlines()
            if json.loads(line)["kind"] == "buyer"
        )
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
            result = ToolDispatcher(source).dispatch(
                ToolCall(
                    "model-backed-send",
                    "communications.send",
                    "account_executive",
                    {
                        "channel": "email",
                        "recipients": [buyer],
                        "subject": "Decision request",
                        "body": "Please confirm the decision path.",
                        "semantic_envelope": {
                            "purpose": "confirm the decision path",
                            "related_records": ["deal-1"],
                            "requested_decisions": ["confirm owner"],
                            "commitments": [],
                            "attachments": [],
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
            self.assertEqual(len(records), 1)
            self.assertEqual(engine.current_checkpoint_index, -1)

    def test_scripted_oracle_replays_reference_and_strictly_passes(self) -> None:
        with open_world(WORLD, run_id="scripted-oracle-test") as engine:
            result = baselines.ScriptedOracle().run(engine)
            score = grade_run(
                engine,
                WORLD / "rubric.json",
                trace=[item.to_dict() for item in engine.trace_events()],
                oracle=WORLD / "oracle.json",
            )
            self.assertEqual(result.status, "completed")
            self.assertEqual(score["execution_index"], 100.0)
            self.assertTrue(score["strict_cycle_pass"])

    def test_scripted_oracle_explicitly_rejects_dev_without_reference(self) -> None:
        with open_world(DEV_WORLD, run_id="scripted-oracle-dev-test") as engine:
            result = baselines.ScriptedOracle().run(engine)
            self.assertEqual(result.status, "failed")
            self.assertEqual(engine.status, "failed")
            self.assertIn("dev bundles intentionally omit", result.errors[0])

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
        code = "import json,sys; r=json.loads(sys.stdin.readline()); c=r['checkpoint']; print(json.dumps({'protocol_version':'v1.0.0','run_id':r['run_id'],'sequence':len(r.get('messages',[]))+1,'message_id':'m'+str(len(r.get('messages',[]))+1),'occurred_at':r['occurred_at'],'kind':'checkpoint_complete','role':r['role'],'checkpoint_id':c['checkpoint_id'],'summary':'reviewed','observation_token':r['observation_token']}))"
        with (
            tempfile.TemporaryDirectory() as directory,
            open_world(
                WORLD, run_id="fixed-test", db_path=Path(directory) / "run.sqlite"
            ) as engine,
        ):
            result = FixedHarnessScheduler(
                engine,
                (sys.executable, "-c", code),
                RunLimits(timeout_seconds=5),
                Path(directory),
            ).run()
            self.assertEqual(result.status, "completed")
            checkpoint_count = len(
                json.loads((WORLD / "manifest.json").read_text(encoding="utf-8"))[
                    "checkpoint_ids"
                ]
            )
            self.assertEqual(result.turns, checkpoint_count * 4)
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

    def test_open_team_process(self) -> None:
        code = "import json,sys; n=0; roles={};\nfor line in sys.stdin:\n m=json.loads(line);\n if m.get('kind')=='observation':\n  n+=1; p=m['payload']; c=p['checkpoint']; roles.setdefault(c['checkpoint_id'],set()).add(m['role']); print(json.dumps({'protocol_version':'v1.0.0','run_id':m['run_id'],'sequence':n,'message_id':'m'+str(n),'occurred_at':m['occurred_at'],'kind':'checkpoint_complete','role':m['role'],'checkpoint_id':c['checkpoint_id'],'summary':'reviewed','observation_token':m['observation_token']}),flush=True);\n  if c.get('terminal') and len(roles[c['checkpoint_id']])==4: print(json.dumps({'protocol_version':'v1.0.0','run_id':m['run_id'],'sequence':n+1,'message_id':'end','occurred_at':m['occurred_at'],'kind':'run_end','role':'system','status':'completed','observation_token':m['observation_token']}),flush=True)"
        with (
            tempfile.TemporaryDirectory() as directory,
            open_world(
                WORLD, run_id="open-test", db_path=Path(directory) / "run.sqlite"
            ) as engine,
        ):
            result = OpenTeamRunner(
                engine,
                (sys.executable, "-c", code),
                RunLimits(timeout_seconds=5),
                Path(directory),
            ).run()
            self.assertEqual(result.status, "completed")

    def test_default_runner_limits_respect_world_checkpoint_caps(self) -> None:
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
        assert checkpoint is not None and observation.payload is not None
        self.assertEqual(checkpoint["max_tool_calls"], 32)
        self.assertEqual(checkpoint["max_turns"], 64)
        self.assertEqual(
            observation.payload["budget"],
            {"tool_calls_remaining": 32, "turns_remaining": 64},
        )

    def test_both_runners_trace_protocol_team_message_and_yield(self) -> None:
        fixed_code = "import json,sys; r=json.loads(sys.stdin.readline()); c=r['checkpoint']; t=r['observation_token']; n=max([int(item.get('sequence',-1)) for item in r.get('messages',[]) if isinstance(item,dict)]+[-1])+1; out=[];\nif r['role']=='account_executive': out.append({'protocol_version':'v1.0.0','run_id':r['run_id'],'sequence':n,'message_id':'team-'+c['checkpoint_id'],'occurred_at':r['occurred_at'],'kind':'team_message','role':r['role'],'recipient_role':'domain_specialist','payload':{'body':'Review the evidence.'},'observation_token':t}); n+=1\nout.append({'protocol_version':'v1.0.0','run_id':r['run_id'],'sequence':n,'message_id':'yield-'+c['checkpoint_id']+'-'+r['role'],'occurred_at':r['occurred_at'],'kind':'yield','role':r['role'],'reason':'Waiting for evidence.','observation_token':t}); n+=1\nout.append({'protocol_version':'v1.0.0','run_id':r['run_id'],'sequence':n,'message_id':'complete-'+c['checkpoint_id']+'-'+r['role'],'occurred_at':r['occurred_at'],'kind':'checkpoint_complete','role':r['role'],'checkpoint_id':c['checkpoint_id'],'summary':'reviewed','observation_token':t}); [print(json.dumps(item)) for item in out]"
        open_code = "import json,sys;\nfor line in sys.stdin:\n m=json.loads(line)\n if m.get('kind')=='observation':\n  print(json.dumps({'protocol_version':'v1.0.0','run_id':m['run_id'],'sequence':1,'message_id':'team-open','occurred_at':m['occurred_at'],'kind':'team_message','role':'account_executive','recipient_role':'domain_specialist','payload':{'body':'Review the evidence.','usage':{'cost_minor_units':-1,'token_usage':{}}},'observation_token':m['observation_token']}),flush=True); print(json.dumps({'protocol_version':'v1.0.0','run_id':m['run_id'],'sequence':2,'message_id':'yield-open','occurred_at':m['occurred_at'],'kind':'yield','role':'account_executive','reason':'Waiting for evidence.','observation_token':m['observation_token']}),flush=True); break"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with open_world(
                WORLD, run_id="fixed-protocol", db_path=root / "fixed.sqlite"
            ) as engine:
                result = FixedHarnessScheduler(
                    engine,
                    (sys.executable, "-c", fixed_code),
                    RunLimits(timeout_seconds=5),
                    root / "fixed",
                ).run()
                kinds = [event.kind for event in engine.trace_events()]
                self.assertEqual(result.status, "completed")
                self.assertIn("team_message", kinds)
                self.assertIn("yield", kinds)
                self.assertGreaterEqual(len(engine.team_inbox("domain_specialist")), 2)
            with open_world(
                WORLD, run_id="open-protocol", db_path=root / "open.sqlite"
            ) as engine:
                initial_count = len(engine.team_inbox("domain_specialist"))
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
                    len(engine.team_inbox("domain_specialist")) - initial_count, 1
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
                initial_count = len(engine.team_inbox("domain_specialist"))
                replay_trace(engine, protocol_rows)
                first_hash = engine.state_hash()
                first_count = len(engine.team_inbox("domain_specialist"))
                replay_trace(engine, protocol_rows)
                self.assertEqual(first_count - initial_count, 1)
                self.assertEqual(
                    len(engine.team_inbox("domain_specialist")), first_count
                )
                self.assertEqual(engine.state_hash(), first_hash)

    def test_reference_replay_hashes_are_deterministic(self) -> None:
        trace = WORLD / "reference_trace.jsonl"
        rubric = WORLD / "rubric.json"
        oracle = WORLD / "oracle.json"
        run_id = json.loads(trace.read_text(encoding="utf-8").splitlines()[0])["run_id"]
        scores = []
        persisted_scores = []
        states = []
        with tempfile.TemporaryDirectory() as directory:
            for index in (1, 2):
                database = Path(directory) / f"replay-{index}.sqlite"
                with open_world(
                    WORLD,
                    run_id=run_id,
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

    def test_open_team_tool_completion_advances_checkpoints(self) -> None:
        code = "import json,sys; n=0\nfor line in sys.stdin:\n m=json.loads(line)\n if m.get('kind')=='observation':\n  n+=1; p=m['payload']; c=p['checkpoint']; print(json.dumps({'protocol_version':'v1.0.0','run_id':m['run_id'],'sequence':n,'message_id':'m'+str(n),'occurred_at':m['occurred_at'],'kind':'tool_call','role':m['role'],'tool_name':'run.complete_checkpoint','arguments':{'checkpoint_id':c['checkpoint_id'],'summary':'reviewed'},'idempotency_key':'complete-'+c['checkpoint_id']+'-'+m['role'],'observation_token':m['observation_token']}),flush=True)"
        with (
            tempfile.TemporaryDirectory() as directory,
            open_world(
                WORLD, run_id="open-tool-test", db_path=Path(directory) / "run.sqlite"
            ) as engine,
        ):
            result = OpenTeamRunner(
                engine,
                (sys.executable, "-c", code),
                RunLimits(timeout_seconds=5),
                Path(directory),
            ).run()
            self.assertEqual(result.status, "completed")
            checkpoint_count = len(
                json.loads((WORLD / "manifest.json").read_text(encoding="utf-8"))[
                    "checkpoint_ids"
                ]
            )
            self.assertEqual(result.tool_calls, checkpoint_count * 4)

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
            self.assertIn("--network=none", command)
            self.assertIn("fsize=268435456:268435456", command)

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
        code = "import json,sys; r=json.loads(sys.stdin.readline()); c=r['checkpoint']; n=len(r.get('messages',[]))+1; print(json.dumps({'protocol_version':'v1.0.0','run_id':r['run_id'],'sequence':n,'message_id':'m'+str(n),'occurred_at':r['occurred_at'],'kind':'checkpoint_complete','role':r['role'],'checkpoint_id':c['checkpoint_id'],'summary':'reviewed','observation_token':r['observation_token']}))"
        with tempfile.TemporaryDirectory() as directory:
            results = run_replicates(
                WORLD,
                (sys.executable, "-c", code),
                track="fixed_harness",
                trials=3,
                limits=RunLimits(timeout_seconds=5),
                output_dir=directory,
            )
            self.assertEqual(len(results), 3)
            self.assertTrue(all(result.status == "completed" for result in results))
            self.assertEqual(len({result.run_id for result in results}), 3)
            manifests = [
                json.loads(
                    (
                        Path(directory) / f"trial-{trial}" / "run-manifest.json"
                    ).read_text()
                )
                for trial in range(1, 4)
            ]
            official = tuple(manifests[0]["stakeholder_manifest"]["official_seeds"])
            self.assertEqual(len(official), 3)
            self.assertEqual(
                [manifest["seed"] for manifest in manifests], list(official)
            )
            self.assertEqual(
                [manifest["stakeholder_manifest"]["seed"] for manifest in manifests],
                list(official),
            )
            self.assertEqual(
                [
                    tuple(manifest["stakeholder_manifest"]["official_seeds"])
                    for manifest in manifests
                ],
                [official] * 3,
            )


if __name__ == "__main__":
    unittest.main()
