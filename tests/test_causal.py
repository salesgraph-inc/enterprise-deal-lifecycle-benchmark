from __future__ import annotations

import sys
import unittest
from importlib import import_module
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

causal = import_module("edlb.causal")
ScenarioManifest = import_module("edlb.models").ScenarioManifest
Event = import_module("edlb.models").Event
ToolCall = import_module("edlb.protocol").ToolCall
replay_trace = import_module("edlb.runner").replay_trace
open_world = import_module("edlb.runner").open_world
ToolDispatcher = import_module("edlb.tools").ToolDispatcher
engine_tests = import_module("tests.test_engine")
ENVELOPE = engine_tests.ENVELOPE
NEXT = engine_tests.NEXT
ROLES = engine_tests.ROLES
make_engine = engine_tests.make_engine
realization_cache_key = causal.realization_cache_key
realization_packet = causal.realization_packet
select_stakeholder_act = causal.select_stakeholder_act
validate_realization = causal.validate_realization
WORLD = next(
    (ROOT / "benchmarks/v1/output/public/train").glob("*/manifest.json")
).parent


def activate(engine: object) -> None:
    engine.seed_crm_record("deal-1", {"stage": "new"})
    engine.advance_checkpoint(idempotency_key="activate")


def complete_checkpoint(engine: object, checkpoint_id: str) -> None:
    for role in ROLES:
        engine.complete_checkpoint(
            role,
            checkpoint_id,
            idempotency_key=f"complete-{checkpoint_id}-{role}",
        )


class CausalEngineTest(unittest.TestCase):
    def test_open_world_injects_pinned_realizer_configuration(self) -> None:
        model_digest = "sha256:" + "1" * 64
        prompt_hash = "sha256:" + "2" * 64
        command = (sys.executable, "-c", "print('{}')")
        with open_world(
            WORLD,
            stakeholder_seeds=(11, 12, 13),
            stakeholder_realizer_command=command,
            stakeholder_model_digest=model_digest,
            stakeholder_prompt_hash=prompt_hash,
            stakeholder_timeout_seconds=7.5,
        ) as engine:
            self.assertEqual(engine.official_stakeholder_seeds, (11, 12, 13))
            self.assertEqual(engine.stakeholder_realizer_command, command)
            self.assertEqual(
                engine.manifest.stakeholder_manifest["model_digest"], model_digest
            )
            self.assertEqual(
                engine.manifest.stakeholder_manifest["timeout_seconds"], 7.5
            )

    def test_public_scenario_omits_private_optional_fields(self) -> None:
        with make_engine() as engine:
            value = engine.scenario.to_dict()
        for key in (
            "pair_id",
            "counterfactual_variant",
            "causal_skeleton",
            "terminal_outcome",
            "seed",
        ):
            value.pop(key)
        parsed = ScenarioManifest.from_dict(value)
        serialized = parsed.to_dict()
        self.assertIsNone(parsed.pair_id)
        self.assertNotIn("pair_id", serialized)
        self.assertNotIn("terminal_outcome", serialized)

    def test_public_events_do_not_grant_milestone_progress(self) -> None:
        with make_engine() as engine:
            self.assertEqual(
                engine.causal_lanes()["stakeholder_consensus"]["score"], 20
            )
            self.assertNotIn("event-later", engine.causal_state()["event_ids"])
            activate(engine)
            complete_checkpoint(engine, "cp-0")
            engine.advance_checkpoint(idempotency_key="advance-next")
            consensus = engine.causal_lanes()["stakeholder_consensus"]
            self.assertEqual(engine.current_time, NEXT)
            self.assertEqual(consensus["score"], 20)
            self.assertIn("event-later", engine.causal_state()["event_ids"])

    def test_seeded_crm_visibility_gates_search_read_and_history(self) -> None:
        with make_engine() as engine:
            engine.seed_crm_record(
                "restricted-deal",
                {
                    "name": "Restricted deal",
                    "visibility": ["account_executive"],
                },
            )
            self.assertEqual(
                [item["record_id"] for item in engine.crm_search("account_executive")],
                ["restricted-deal"],
            )
            self.assertEqual(engine.crm_search("revops"), [])
            for method in (engine.crm_read, engine.crm_history):
                with self.assertRaisesRegex(Exception, "not found"):
                    method("revops", "restricted-deal")

    def test_action_effects_and_stakeholder_selection_are_deterministic(self) -> None:
        states = []
        for suffix in ("a", "b"):
            with make_engine() as engine:
                activate(engine)
                result = ToolDispatcher(engine).dispatch(
                    ToolCall(
                        f"email-{suffix}",
                        "communications.send",
                        "account_executive",
                        {
                            "channel": "email",
                            "recipients": ["buyer@example.test"],
                            "subject": "Next step",
                            "body": "Please confirm.",
                            "semantic_envelope": ENVELOPE,
                        },
                        "same-action",
                    )
                )
                self.assertTrue(result.ok)
                states.append(engine.causal_state())
        self.assertEqual(states[0], states[1])
        self.assertEqual(states[0]["lanes"]["stakeholder_consensus"]["score"], 20)

    def test_generic_actions_never_grant_milestone_progress(self) -> None:
        with make_engine() as engine:
            activate(engine)
            dispatcher = ToolDispatcher(engine)
            for index in range(2):
                result = dispatcher.dispatch(
                    ToolCall(
                        f"email-farm-{index}",
                        "communications.send",
                        "account_executive",
                        {
                            "channel": "email",
                            "recipients": ["buyer@example.test"],
                            "subject": "Next step",
                            "body": "Please confirm.",
                            "semantic_envelope": ENVELOPE,
                        },
                        f"email-farm-key-{index}",
                    )
                )
                self.assertTrue(result.ok)
            self.assertEqual(
                engine.causal_lanes()["stakeholder_consensus"]["score"], 20
            )
            effects = [
                row[0]
                for row in engine.connection.execute(
                    "SELECT effects FROM causal_action_applications ORDER BY rowid"
                )
            ]
            self.assertEqual(len(effects), 2)
            self.assertEqual(effects, ["{}", "{}"])

    def test_agent_event_views_hide_causal_truth(self) -> None:
        with make_engine() as engine:
            engine.append_event(
                Event(
                    "event-causal-truth",
                    "world-1",
                    2,
                    "budget_changed",
                    engine.current_time,
                    engine.current_time,
                    engine.current_time,
                    (),
                    "agent_visible",
                    {
                        "budget_status": "spending_hold",
                        "lane_effects": {"urgency": {"absolute": -100}},
                        "causal_effects": {"approvals": {"absolute": -100}},
                    },
                )
            )
            system_event = next(
                item
                for item in engine.events("system")
                if item["event_id"] == "event-causal-truth"
            )
            agent_event = next(
                item
                for item in engine.events("account_executive")
                if item["event_id"] == "event-causal-truth"
            )
            self.assertIn("lane_effects", system_event["payload"])
            self.assertIn("causal_effects", system_event["payload"])
            self.assertNotIn("lane_effects", agent_event["payload"])
            self.assertNotIn("causal_effects", agent_event["payload"])
            self.assertEqual(
                engine.events("account_executive", query="lane_effects"), []
            )

    def test_realization_cache_uses_world_input_model_prompt_and_seed(self) -> None:
        with make_engine() as engine:
            lanes = engine.causal_lanes()
            act = select_stakeholder_act(
                engine.manifest.world_id,
                "action-1",
                "buyer-contact",
                "email",
                ENVELOPE,
                lanes,
            )
            packet = realization_packet(act, "prompt-a", "model-a", 7)
            base = realization_cache_key(
                "state-a", "input-a", packet, "prompt-a", "model-a", 7
            )
            self.assertNotEqual(
                base,
                realization_cache_key(
                    "state-b", "input-a", packet, "prompt-a", "model-a", 7
                ),
            )
            self.assertNotEqual(
                base,
                realization_cache_key(
                    "state-a", "input-b", packet, "prompt-a", "model-a", 7
                ),
            )
            self.assertNotEqual(
                base,
                realization_cache_key(
                    "state-a", "input-a", packet, "prompt-b", "model-a", 7
                ),
            )
            self.assertNotEqual(
                base,
                realization_cache_key(
                    "state-a", "input-a", packet, "prompt-a", "model-b", 7
                ),
            )
            self.assertNotEqual(
                base,
                realization_cache_key(
                    "state-a", "input-a", packet, "prompt-a", "model-a", 8
                ),
            )
            first = engine._realize_stakeholder_act(act, "input-a")
            second = engine._realize_stakeholder_act(act, "input-a")
            count = engine.connection.execute(
                "SELECT COUNT(*) FROM stakeholder_realizations"
            ).fetchone()[0]
            self.assertFalse(first["cached"])
            self.assertTrue(second["cached"])
            self.assertEqual(count, 1)

    def test_forbidden_claims_are_rejected(self) -> None:
        with self.assertRaisesRegex(Exception, "forbidden claim"):
            validate_realization(
                "The deal is closed won.",
                ("deal is closed won",),
            )

    def test_subprocess_realizer_cannot_mutate_causal_truth(self) -> None:
        code = "import json,sys; json.loads(sys.stdin.readline()); print(json.dumps({'text':'Please send the supporting evidence.'}))"
        with make_engine() as engine:
            engine.stakeholder_realizer_command = (sys.executable, "-c", code)
            lanes = engine.causal_lanes()
            act = select_stakeholder_act(
                engine.manifest.world_id,
                "action-1",
                "buyer-contact",
                "email",
                ENVELOPE,
                lanes,
            )
            before = engine.causal_state_hash()
            engine._realize_stakeholder_act(act, "input-a")
            self.assertEqual(engine.causal_state_hash(), before)

    def test_replay_recreates_causal_state(self) -> None:
        with make_engine() as source:
            activate(source)
            result = ToolDispatcher(source).dispatch(
                ToolCall(
                    "email-replay",
                    "communications.send",
                    "account_executive",
                    {
                        "channel": "email",
                        "recipients": ["buyer@example.test"],
                        "subject": "Next step",
                        "body": "Please confirm.",
                        "semantic_envelope": ENVELOPE,
                    },
                    "email-replay",
                )
            )
            self.assertTrue(result.ok)
            expected = source.causal_state()
            trace = [item.to_dict() for item in source.trace_events()]
        with make_engine() as target:
            target.seed_crm_record("deal-1", {"stage": "new"})
            replay_trace(target, trace)
            self.assertEqual(target.causal_state(), expected)

    def test_terminal_outcome_requires_an_explicit_resolution_mapping(self) -> None:
        with make_engine() as engine:
            activate(engine)
            complete_checkpoint(engine, "cp-0")
            engine.advance_checkpoint(idempotency_key="advance-next")
            complete_checkpoint(engine, "cp-1")
            with self.assertRaisesRegex(Exception, "supported terminal resolution"):
                engine.run_complete(idempotency_key="finish")


if __name__ == "__main__":
    unittest.main()
