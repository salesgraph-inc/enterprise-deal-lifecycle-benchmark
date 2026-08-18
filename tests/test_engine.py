from __future__ import annotations

import importlib
import importlib.resources
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

engine_module = importlib.import_module("edlb.engine")
models_module = importlib.import_module("edlb.models")
protocol_module = importlib.import_module("edlb.protocol")
tools_module = importlib.import_module("edlb.tools")
AuthorizationError = engine_module.AuthorizationError
EngineError = engine_module.EngineError
ImmutableError = engine_module.ImmutableError
RunEngine = engine_module.RunEngine
Actor = models_module.Actor
Artifact = models_module.Artifact
Checkpoint = models_module.Checkpoint
Event = models_module.Event
RoleGrant = models_module.RoleGrant
RunManifest = models_module.RunManifest
ScenarioManifest = models_module.ScenarioManifest
from_dict = models_module.from_dict
to_dict = models_module.to_dict
Message = protocol_module.Message
ProtocolError = protocol_module.ProtocolError
ToolCall = protocol_module.ToolCall
decode = protocol_module.decode
encode = protocol_module.encode
ToolDispatcher = tools_module.ToolDispatcher


START = "2026-01-01T00:00:00+00:00"
NEXT = "2026-02-01T00:00:00+00:00"
TOKEN = "a" * 32
DIGEST = "sha256:" + "0" * 64
ROLES = ("account_executive", "domain_specialist", "sales_manager", "revops")
ENVELOPE = {
    "purpose": "Confirm the next step",
    "related_records": ["deal-1"],
    "requested_decisions": ["Confirm attendance"],
    "commitments": [],
    "attachments": [],
}


def grants() -> tuple[RoleGrant, ...]:
    permissions = (
        "run.read",
        "run.complete_checkpoint",
        "crm.read",
        "crm.write",
        "crm.merge",
        "communications.read",
        "communications.send_external",
        "communications.send_internal",
        "calendar.read",
        "calendar.write",
        "documents.read",
        "documents.write",
        "approvals.read",
        "approvals.request",
        "approvals.decide",
        "web.read",
        "team.read",
        "team.send",
    )
    return tuple(
        RoleGrant(
            f"grant-{role}",
            role,
            role,
            permissions,
            ("current_world",),
            role == "account_executive",
            role in {"account_executive", "revops"},
            role == "sales_manager",
            role in {"account_executive", "sales_manager"},
            1000000 if role == "sales_manager" else None,
        )
        for role in ROLES
    )


def make_engine(trace_path: Path | None = None, max_tool_calls: int = 64) -> RunEngine:
    manifest = RunManifest(
        "run-1",
        "v1.0.0",
        "world-1",
        "open_team",
        "team-1",
        "v1.0.0",
        "v1.0.0",
        DIGEST,
        DIGEST,
        None,
        7,
        {"roles": {role: "reference" for role in ROLES}},
        {
            "model_id": "deterministic",
            "model_digest": DIGEST,
            "prompt_hash": DIGEST,
            "seed": 7,
        },
        {"tool_calls_per_checkpoint": 64, "turns_per_checkpoint": 128, "retries": 2},
        {
            "runtime_version": "python-3.14",
            "image_digest": DIGEST,
            "git_revision": "0000000",
        },
        START,
        "created",
    )
    scenario = ScenarioManifest(
        "world-1",
        "pair-1",
        "a",
        "dev",
        "manufacturing",
        "champion_departure",
        "seller-1",
        "buyer-1",
        "Manufacturing deal",
        "Synthetic manufacturing deal",
        START,
        NEXT,
        181,
        ("cp-0", "cp-1"),
        (),
        ("event-now", "event-later"),
        ("artifact-now",),
        (
            "call_transcript",
            "email",
            "internal_chat",
            "crm",
            "calendar",
            "document",
            "web_signal",
        ),
        "closed_won",
        7,
        {"code": "MIT", "data": "CC-BY-4.0"},
        {
            "synthetic_only": True,
            "generator": "edlb",
            "generator_version": "v1.0.0",
            "created_at": START,
            "source_policy_ids": (),
        },
    )
    checkpoints = (
        Checkpoint(
            "cp-0",
            "world-1",
            0,
            START,
            START,
            START,
            "pending",
            ("objective-0",),
            ("artifact-now",),
            ROLES,
            max_tool_calls,
            128,
            False,
        ),
        Checkpoint(
            "cp-1",
            "world-1",
            1,
            NEXT,
            NEXT,
            NEXT,
            "pending",
            ("objective-1",),
            (),
            ROLES,
            max_tool_calls,
            128,
            True,
        ),
    )
    events = (
        Event(
            "event-now",
            "world-1",
            0,
            "meeting_booked",
            START,
            START,
            START,
            (),
            "agent_visible",
            {"meeting": "first"},
            channel="call_transcript",
        ),
        Event(
            "event-later",
            "world-1",
            1,
            "stakeholder_departed",
            NEXT,
            START,
            NEXT,
            ("champion-1",),
            "agent_visible",
            {"actor_id": "champion-1"},
        ),
    )
    artifacts = (
        Artifact(
            "artifact-now",
            "world-1",
            "call_transcript",
            "Intro",
            START,
            START,
            "agent_visible",
            {"mime_type": "text/plain", "body": "Hello", "language": "en"},
            DIGEST,
            {
                "synthetic_only": True,
                "source_type": "generated_template",
                "generator": "edlb",
                "generator_version": "v1.0.0",
                "license": "CC-BY-4.0",
            },
        ),
    )
    actors = (
        Actor(
            "buyer-contact",
            "buyer",
            "Buyer Contact",
            "buyer-1",
            ("champion",),
            START,
            "public",
            email="buyer@example.test",
        ),
        Actor(
            "restricted-contact",
            "buyer",
            "Restricted Contact",
            "buyer-1",
            ("economic_buyer",),
            START,
            "restricted",
            email="restricted@example.test",
            visible_roles=("account_executive",),
        ),
        *(
            Actor(
                f"seller-{role}",
                "seller",
                role,
                "seller-1",
                (role,),
                START,
                "public",
                email=f"{role}@seller.example.test",
            )
            for role in ROLES
        ),
    )
    return RunEngine(
        manifest=manifest,
        scenario=scenario,
        actors=actors,
        checkpoints=checkpoints,
        events=events,
        artifacts=artifacts,
        grants=grants(),
        trace_path=trace_path,
    )


class ModelsTest(unittest.TestCase):
    def test_round_trip_uses_normative_fields(self) -> None:
        value = Event(
            "e",
            "world-1",
            0,
            "meeting_booked",
            START,
            START,
            START,
            (),
            "agent_visible",
            {"nested": [1, "two"]},
        )
        serialized = to_dict(value)
        self.assertIn("world_id", serialized)
        self.assertNotIn("scenario_id", serialized)
        self.assertEqual(from_dict(Event, serialized), value)

    def test_role_grant_rejects_coerced_flags(self) -> None:
        value = to_dict(grants()[0])
        value["can_contact_external"] = "false"
        with self.assertRaises(TypeError):
            RoleGrant.from_dict(value)

    def test_visibility_schemas_require_explicit_roles_for_scoped_records(
        self,
    ) -> None:
        for name in ("actor", "event", "artifact"):
            schema = json.loads(
                importlib.resources.files("edlb")
                .joinpath("schemas", f"{name}.json")
                .read_text()
            )
            self.assertIn("visible_roles", schema["properties"])
            rule = schema["allOf"][0]
            self.assertIn("visible_roles", rule["then"]["required"])
            self.assertEqual(rule["then"]["properties"]["visible_roles"]["minItems"], 1)


class ProtocolTest(unittest.TestCase):
    def test_jsonl_round_trip_uses_normative_fields(self) -> None:
        message = Message(
            "v1.0.0",
            "run-1",
            2,
            "message-2",
            START,
            "observation",
            "account_executive",
            payload={"ok": True},
            observation_token="a" * 32,
        )
        serialized = encode(message)
        self.assertEqual(decode(serialized), message)
        self.assertIn('"occurred_at"', serialized)
        self.assertNotIn('"timestamp"', serialized)

    def test_protocol_rejects_spoofing_and_mismatch(self) -> None:
        base = {
            "protocol_version": "v1.0.0",
            "run_id": "run-1",
            "sequence": 0,
            "message_id": "message-0",
            "occurred_at": START,
            "kind": "observation",
            "role": "account_executive",
            "payload": {"ok": True},
        }
        for change in (
            {"role": "system"},
            {"role": "ae"},
            {"role": "unknown"},
            {"kind": "unknown"},
            {"protocol_version": "v9.0.0"},
            {"message_id": "Bad-ID"},
            {"occurred_at": "2026-01-01T00:00:00"},
            {"occurred_at": "2026-01-01 00:00:00+00:00"},
            {"observation_token": "predictable"},
        ):
            value = dict(base)
            value.update(change)
            with self.assertRaises(ProtocolError):
                decode(json.dumps(value))

    def test_read_and_write_tool_messages(self) -> None:
        read = ToolCall("call-1", "crm.search", "account_executive", {"query": "deal"})
        self.assertEqual(
            ToolCall.from_message(
                read.to_message("run-1", 0, START, observation_token=TOKEN)
            ),
            read,
        )
        write = ToolCall(
            "call-2", "crm.update", "account_executive", {"record_id": "deal-1"}
        )
        with self.assertRaises(ProtocolError):
            encode(write.to_message("run-1", 1, START, observation_token=TOKEN))
        for index, tool_name in enumerate(
            ("calendar.reschedule", "approvals.approve", "approvals.reject"), start=3
        ):
            with self.assertRaises(ProtocolError):
                encode(
                    ToolCall(
                        f"call-{index}", tool_name, "account_executive", {}
                    ).to_message("run-1", index, START, observation_token=TOKEN)
                )
        self.assertIn(
            "web.open",
            encode(
                ToolCall("call-6", "web.open", "account_executive", {}).to_message(
                    "run-1", 6, START, observation_token=TOKEN
                )
            ),
        )

    def test_agent_messages_require_observation_tokens(self) -> None:
        value = {
            "protocol_version": "v1.0.0",
            "run_id": "run-1",
            "sequence": 0,
            "message_id": "call-1",
            "occurred_at": START,
            "kind": "tool_call",
            "role": "account_executive",
            "tool_name": "run.status",
            "arguments": {},
        }
        with self.assertRaisesRegex(ProtocolError, "observation_token"):
            decode(json.dumps(value))
        value["observation_token"] = TOKEN
        self.assertEqual(decode(json.dumps(value)).observation_token, TOKEN)


class EngineTest(unittest.TestCase):
    def test_scoped_records_are_visible_only_to_explicit_roles(self) -> None:
        with make_engine() as engine:
            scoped_event = Event(
                "event-scoped",
                "world-1",
                2,
                "message_sent",
                START,
                START,
                START,
                (),
                "role_scoped",
                {"subject": "Restricted"},
                visible_roles=("account_executive",),
            )
            scoped_artifact = Artifact(
                "artifact-scoped",
                "world-1",
                "internal_chat",
                "Restricted note",
                START,
                START,
                "role_scoped",
                {"mime_type": "text/plain", "body": "Restricted", "language": "en"},
                DIGEST,
                {
                    "synthetic_only": True,
                    "source_type": "generated_template",
                    "generator": "edlb",
                    "generator_version": "v1.0.0",
                    "license": "CC-BY-4.0",
                },
                visible_roles=("account_executive",),
            )
            engine.append_event(scoped_event)
            engine.append_artifact(scoped_artifact)
            self.assertIn(
                "event-scoped",
                {item["event_id"] for item in engine.events("account_executive")},
            )
            self.assertNotIn(
                "event-scoped",
                {item["event_id"] for item in engine.events("domain_specialist")},
            )
            self.assertIn(
                "artifact-scoped",
                {item["artifact_id"] for item in engine.artifacts("account_executive")},
            )
            self.assertNotIn(
                "artifact-scoped",
                {item["artifact_id"] for item in engine.artifacts("domain_specialist")},
            )
            with self.assertRaises(EngineError):
                engine.append_event(
                    Event(
                        "event-unscoped",
                        "world-1",
                        3,
                        "message_sent",
                        START,
                        START,
                        START,
                        (),
                        "role_scoped",
                        {"subject": "Missing roles"},
                    )
                )
            with self.assertRaises(EngineError):
                engine.append_artifact(
                    Artifact(
                        "artifact-unscoped",
                        "world-1",
                        "internal_chat",
                        "Missing roles",
                        START,
                        START,
                        "role_scoped",
                        {
                            "mime_type": "text/plain",
                            "body": "Missing roles",
                            "language": "en",
                        },
                        DIGEST,
                        {
                            "synthetic_only": True,
                            "source_type": "generated_template",
                            "generator": "edlb",
                            "generator_version": "v1.0.0",
                            "license": "CC-BY-4.0",
                        },
                    )
                )
            with self.assertRaises(EngineError):
                RunEngine._validate_visibility(
                    "restricted",
                    (),
                    {"public", "internal_role_scoped", "restricted"},
                    "actor",
                )

    def test_nonmember_role_cannot_address_restricted_actor(self) -> None:
        with make_engine() as engine:
            engine.advance_checkpoint(idempotency_key="advance-restricted")
            grant = grants()[1]
            engine.grant(
                RoleGrant(
                    grant.grant_id,
                    grant.principal_id,
                    grant.role,
                    grant.permissions,
                    grant.resource_scopes,
                    True,
                    grant.can_write_crm,
                    grant.can_approve_commercial,
                    grant.can_request_approval,
                    grant.approval_limit_minor_units,
                )
            )
            dispatcher = ToolDispatcher(engine)
            arguments = {
                "channel": "email",
                "recipients": ["restricted@example.test"],
                "subject": "Decision",
                "body": "Please confirm.",
                "semantic_envelope": ENVELOPE,
            }
            hidden = dispatcher.dispatch(
                ToolCall(
                    "restricted-hidden",
                    "communications.send",
                    "domain_specialist",
                    arguments,
                    "restricted-hidden",
                )
            )
            self.assertFalse(hidden.ok)
            self.assertEqual(hidden.error["code"], "not_authorized")
            unknown = dispatcher.dispatch(
                ToolCall(
                    "restricted-unknown",
                    "communications.send",
                    "domain_specialist",
                    {
                        **arguments,
                        "recipients": ["unknown@example.test"],
                    },
                    "restricted-unknown",
                )
            )
            self.assertEqual(hidden.error, unknown.error)
            visible = dispatcher.dispatch(
                ToolCall(
                    "restricted-visible",
                    "communications.send",
                    "account_executive",
                    arguments,
                    "restricted-visible",
                )
            )
            self.assertTrue(visible.ok)

    def test_public_tool_names_and_strict_argument_schemas(self) -> None:
        schemas = {item["tool_name"]: item for item in ToolDispatcher.schemas()}
        for name in (
            "calendar.reschedule",
            "calendar.cancel",
            "approvals.approve",
            "approvals.reject",
            "web.open",
        ):
            self.assertIn(name, schemas)
        for name in ("calendar.update", "approvals.respond", "web.read"):
            self.assertNotIn(name, schemas)
        self.assertTrue(
            all(
                item["arguments"]["additionalProperties"] is False
                for item in schemas.values()
            )
        )
        with make_engine() as engine:
            engine.advance_checkpoint(idempotency_key="advance-schema")
            engine.seed_web_record("signal-1", {"title": "Buyer news"})
            dispatcher = ToolDispatcher(engine)
            result = dispatcher.dispatch(
                ToolCall(
                    "strict-extra",
                    "run.status",
                    "account_executive",
                    {"unexpected": True},
                )
            )
            self.assertFalse(result.ok)
            self.assertEqual(result.error["code"], "protocol_error")
            opened = dispatcher.dispatch(
                ToolCall(
                    "open-signal",
                    "web.open",
                    "account_executive",
                    {"record_id": "signal-1"},
                )
            )
            self.assertTrue(opened.ok)
            legacy = dispatcher.dispatch(
                ToolCall(
                    "legacy-read",
                    "web.read",
                    "account_executive",
                    {"record_id": "signal-1"},
                )
            )
            self.assertFalse(legacy.ok)

    def test_external_writes_require_roster_permissions_and_envelopes(self) -> None:
        with make_engine() as engine:
            engine.advance_checkpoint(idempotency_key="advance-external")
            dispatcher = ToolDispatcher(engine)
            base = {
                "channel": "email",
                "recipients": ["buyer@example.test"],
                "subject": "Next step",
                "body": "Please confirm.",
            }
            missing = dispatcher.dispatch(
                ToolCall(
                    "email-missing",
                    "communications.send",
                    "account_executive",
                    base,
                    "email-missing",
                )
            )
            self.assertFalse(missing.ok)
            self.assertEqual(missing.error["code"], "protocol_error")
            unknown = dispatcher.dispatch(
                ToolCall(
                    "email-unknown",
                    "communications.send",
                    "account_executive",
                    {
                        **base,
                        "recipients": ["unknown@example.test"],
                        "semantic_envelope": ENVELOPE,
                    },
                    "email-unknown",
                )
            )
            self.assertFalse(unknown.ok)
            self.assertEqual(unknown.error["code"], "not_authorized")
            denied = dispatcher.dispatch(
                ToolCall(
                    "email-denied",
                    "communications.send",
                    "revops",
                    {**base, "semantic_envelope": ENVELOPE},
                    "email-denied",
                )
            )
            self.assertFalse(denied.ok)
            self.assertEqual(denied.error["code"], "not_authorized")
            sent = dispatcher.dispatch(
                ToolCall(
                    "email-valid",
                    "communications.send",
                    "account_executive",
                    {**base, "semantic_envelope": ENVELOPE},
                    "email-valid",
                )
            )
            self.assertTrue(sent.ok)
            self.assertEqual(sent.result["metadata"]["semantic_envelope"], ENVELOPE)
            trace = next(
                item
                for item in engine.trace_events()
                if item.kind == "tool_call" and item.payload["call_id"] == "email-valid"
            )
            self.assertEqual(trace.payload["arguments"]["semantic_envelope"], ENVELOPE)

    def test_calendar_writes_are_normalized_and_reject_naive_timestamps(self) -> None:
        with make_engine() as engine:
            engine.advance_checkpoint(idempotency_key="advance-calendar")
            dispatcher = ToolDispatcher(engine)
            naive = dispatcher.dispatch(
                ToolCall(
                    "calendar-naive",
                    "calendar.schedule",
                    "account_executive",
                    {
                        "subject": "Review",
                        "start_at": "2026-01-15T10:00:00",
                        "end_at": "2026-01-15T11:00:00",
                        "participants": ["buyer@example.test"],
                        "semantic_envelope": ENVELOPE,
                    },
                    "calendar-naive",
                )
            )
            self.assertFalse(naive.ok)
            self.assertEqual(naive.error["code"], "protocol_error")
            scheduled = dispatcher.dispatch(
                ToolCall(
                    "calendar-valid",
                    "calendar.schedule",
                    "account_executive",
                    {
                        "subject": "Review",
                        "start_at": "2026-01-15T10:00:00+00:00",
                        "end_at": "2026-01-15T11:00:00+00:00",
                        "participants": ["buyer@example.test"],
                        "semantic_envelope": ENVELOPE,
                    },
                    "calendar-valid",
                )
            )
            self.assertTrue(scheduled.ok)
            calendar_id = scheduled.result["calendar_id"]
            rescheduled = dispatcher.dispatch(
                ToolCall(
                    "calendar-reschedule",
                    "calendar.reschedule",
                    "account_executive",
                    {
                        "calendar_id": calendar_id,
                        "start_at": "2026-01-16T10:00:00+00:00",
                        "end_at": "2026-01-16T11:00:00+00:00",
                        "semantic_envelope": ENVELOPE,
                    },
                    "calendar-reschedule",
                )
            )
            self.assertTrue(rescheduled.ok)
            cancelled = dispatcher.dispatch(
                ToolCall(
                    "calendar-cancel",
                    "calendar.cancel",
                    "account_executive",
                    {"calendar_id": calendar_id, "semantic_envelope": ENVELOPE},
                    "calendar-cancel",
                )
            )
            self.assertTrue(cancelled.ok)
            self.assertEqual(cancelled.result["status"], "cancelled")
            with self.assertRaises(EngineError):
                engine.calendar_schedule(
                    "account_executive",
                    "Bad time",
                    "2026-01-20T10:00:00",
                    "2026-01-20T11:00:00",
                    ["buyer@example.test"],
                    idempotency_key="direct-naive",
                    semantic_envelope=ENVELOPE,
                )
            with self.assertRaises(EngineError):
                engine.calendar_schedule(
                    "account_executive",
                    "Bad separator",
                    "2026-01-20 10:00:00+00:00",
                    "2026-01-20T11:00:00+00:00",
                    ["buyer@example.test"],
                    idempotency_key="direct-space",
                    semantic_envelope=ENVELOPE,
                )

    def test_grant_flags_scopes_approval_limits_and_terminal_immutability(self) -> None:
        with make_engine() as engine:
            engine.advance_checkpoint(idempotency_key="advance-grants")
            engine.seed_crm_record("deal-1", {"stage": "new"})
            with self.assertRaises(AuthorizationError):
                engine.crm_update(
                    "domain_specialist", "deal-1", {"stage": "qualified"}, "domain-crm"
                )
            original = grants()[0]
            engine.grant(
                RoleGrant(
                    original.grant_id,
                    original.principal_id,
                    original.role,
                    original.permissions,
                    ("seller_org",),
                    True,
                    True,
                    False,
                    True,
                )
            )
            with self.assertRaises(AuthorizationError):
                engine.crm_update(
                    "account_executive", "deal-1", {"stage": "qualified"}, "scope-crm"
                )
            engine.seed_approval(
                "approval-high",
                {
                    "approver_role": "sales_manager",
                    "details": {"amount_minor_units": 1_000_001},
                    "status": "pending",
                },
            )
            engine.seed_approval(
                "approval-ok",
                {
                    "approver_role": "sales_manager",
                    "details": {"amount_minor_units": 1_000_000},
                    "status": "pending",
                },
            )
            dispatcher = ToolDispatcher(engine)
            high = dispatcher.dispatch(
                ToolCall(
                    "approval-high",
                    "approvals.approve",
                    "sales_manager",
                    {"approval_id": "approval-high"},
                    "approval-high",
                )
            )
            self.assertFalse(high.ok)
            self.assertEqual(high.error["code"], "not_authorized")
            approved = dispatcher.dispatch(
                ToolCall(
                    "approval-ok",
                    "approvals.approve",
                    "sales_manager",
                    {"approval_id": "approval-ok"},
                    "approval-ok",
                )
            )
            self.assertTrue(approved.ok)
            replay = dispatcher.dispatch(
                ToolCall(
                    "approval-rewrite",
                    "approvals.reject",
                    "sales_manager",
                    {"approval_id": "approval-ok"},
                    "approval-rewrite",
                )
            )
            self.assertFalse(replay.ok)
            with self.assertRaises(ImmutableError):
                engine.seed_approval(
                    "approval-ok",
                    {
                        "approver_role": "sales_manager",
                        "details": {"amount_minor_units": 1},
                        "status": "pending",
                    },
                )

    def test_crm_merge_records_both_histories(self) -> None:
        with make_engine() as engine:
            engine.advance_checkpoint(idempotency_key="advance-merge")
            engine.seed_crm_record("source", {"stage": "duplicate", "owner": "old"})
            engine.seed_crm_record("target", {"stage": "qualified", "owner": "current"})
            result = ToolDispatcher(engine).dispatch(
                ToolCall(
                    "merge-records",
                    "crm.merge",
                    "revops",
                    {"source_id": "source", "target_id": "target"},
                    "merge-records",
                )
            )
            self.assertTrue(result.ok)
            source_history = engine.crm_history("revops", "source")
            target_history = engine.crm_history("revops", "target")
            self.assertEqual(source_history[-1]["changes"], {"merged_into": "target"})
            self.assertEqual(target_history[-1]["snapshot"]["merged_from"], "source")

    def test_failed_calls_consume_checkpoint_cap_and_traces_are_schema_kinds(
        self,
    ) -> None:
        with make_engine(max_tool_calls=2) as engine:
            engine.advance_checkpoint(idempotency_key="advance-budget")
            dispatcher = ToolDispatcher(engine)
            first = dispatcher.dispatch(
                ToolCall("failed-one", "crm.read", "account_executive", {})
            )
            second = dispatcher.dispatch(
                ToolCall("failed-two", "web.open", "account_executive", {})
            )
            third = dispatcher.dispatch(
                ToolCall("over-cap", "run.status", "account_executive", {})
            )
            self.assertFalse(first.ok)
            self.assertFalse(second.ok)
            self.assertFalse(third.ok)
            self.assertEqual(third.error["code"], "budget_exceeded")
            attempts = engine.connection.execute(
                "SELECT attempts FROM checkpoint_tool_usage WHERE checkpoint_id = 'cp-0'"
            ).fetchone()[0]
            self.assertEqual(attempts, 3)
            traces = engine.trace_events()
            self.assertEqual(sum(item.kind == "tool_call" for item in traces), 3)
            self.assertEqual(sum(item.kind == "tool_result" for item in traces), 3)
            self.assertTrue(
                {item.kind for item in traces}.issubset(
                    protocol_module.KINDS | {"system_error"}
                )
            )

    def test_tool_latency_and_trace_metric_fields(self) -> None:
        with make_engine() as engine:
            engine.advance_checkpoint(idempotency_key="advance-metrics")
            result = ToolDispatcher(engine).dispatch(
                ToolCall("metric-call", "run.status", "account_executive", {})
            )
            self.assertTrue(result.ok)
            tool_result = [
                event for event in engine.trace_events() if event.kind == "tool_result"
            ][-1]
            self.assertIsInstance(tool_result.latency_ms, int)
            self.assertGreaterEqual(tool_result.latency_ms or 0, 0)
            metric = engine._trace(
                "observation",
                "account_executive",
                {"model_metrics": {"model_id": "model-1"}},
                latency_ms=7,
                token_usage={"input": 3},
                cost_minor_units=4,
            )
            self.assertEqual(metric.latency_ms, 7)
            self.assertEqual(metric.token_usage, {"input": 3})
            self.assertEqual(metric.cost_minor_units, 4)

    def test_temporal_visibility_and_immutable_events(self) -> None:
        with make_engine() as engine:
            self.assertEqual(
                [item["event_id"] for item in engine.events("account_executive")],
                ["event-now"],
            )
            engine.append_event(
                Event(
                    "event-now",
                    "world-1",
                    0,
                    "meeting_booked",
                    START,
                    START,
                    START,
                    (),
                    "agent_visible",
                    {"meeting": "first"},
                    channel="call_transcript",
                )
            )
            with self.assertRaises(ImmutableError):
                engine.append_event(
                    Event(
                        "event-now",
                        "world-1",
                        0,
                        "terminal_outcome",
                        START,
                        START,
                        START,
                        (),
                        "agent_visible",
                        {"meeting": "first"},
                    )
                )

    def test_only_system_can_advance_and_checkpoint_completions_gate_time(self) -> None:
        with make_engine() as engine:
            dispatcher = ToolDispatcher(engine)
            spoof = dispatcher.dispatch(
                ToolCall(
                    "call-advance", "run.advance", "account_executive", {}, "advance-1"
                )
            )
            self.assertFalse(spoof.ok)
            engine.advance_checkpoint(idempotency_key="advance-1")
            with self.assertRaises(EngineError):
                engine.advance_checkpoint(idempotency_key="advance-2")
            for role in ROLES:
                result = dispatcher.dispatch(
                    ToolCall(
                        f"complete-{role}",
                        "run.complete_checkpoint",
                        role,
                        {"checkpoint_id": "cp-0", "summary": role + " done"},
                        f"complete-{role}",
                    )
                )
                self.assertTrue(result.ok)
            engine.advance_checkpoint(idempotency_key="advance-2")
            self.assertEqual(engine.current_time, NEXT)

    def test_role_grants_idempotency_and_spoof_rejection(self) -> None:
        with make_engine() as engine:
            engine.seed_crm_record("deal-1", {"stage": "new"})
            dispatcher = ToolDispatcher(engine)
            for role in ("system", "ae", "unknown"):
                result = dispatcher.dispatch(
                    ToolCall("bad-role", "crm.search", role, {})
                )
                self.assertFalse(result.ok)
            first = engine.crm_update(
                "account_executive", "deal-1", {"stage": "qualified"}, "key-1"
            )
            second = engine.crm_update(
                "account_executive", "deal-1", {"stage": "closed"}, "key-1"
            )
            self.assertEqual(first, second)
            self.assertEqual(
                engine.crm_read("account_executive", "deal-1")["record"]["stage"],
                "qualified",
            )

    def test_trace_file_matches_sqlite_after_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            trace_path = Path(directory) / "trace.jsonl"
            with make_engine(trace_path) as engine:
                engine.advance_checkpoint(idempotency_key="advance-0")
                before = len(engine.trace_events())

                def fail_after_trace() -> None:
                    engine._trace("observation", "system", {"will": "rollback"})
                    raise EngineError("rollback")

                with self.assertRaises(EngineError):
                    engine._idempotent(
                        "trace-rollback", "test.rollback", fail_after_trace
                    )
                self.assertEqual(len(engine.trace_events()), before)
                with self.assertRaises(EngineError):
                    engine.advance_checkpoint(idempotency_key="advance-fail")
                self.assertEqual(len(engine.trace_events()), before)
                self.assertEqual(
                    len(trace_path.read_text(encoding="utf-8").splitlines()), before
                )
                lines = trace_path.read_text(encoding="utf-8").splitlines()
                self.assertTrue(
                    all(isinstance(json.loads(line), dict) for line in lines)
                )


if __name__ == "__main__":
    unittest.main()
