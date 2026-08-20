from __future__ import annotations

import json
import math
import re
import time
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from .engine import (
    SELLER_ROLES,
    AuthorizationError,
    EngineError,
    IdempotencyError,
    RunEngine,
    ToolLimitError,
)
from .protocol import ProtocolError, ToolCall, ToolResult

CALL_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
IDEMPOTENCY = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,191}$")
IDENTIFIER_PATTERN = r"^[a-z0-9][a-z0-9._-]{0,127}$"
WRITE_ACTIONS = frozenset(
    {
        "update",
        "merge",
        "send",
        "schedule",
        "reschedule",
        "cancel",
        "create",
        "revise",
        "attach",
        "request",
        "approve",
        "reject",
        "complete_checkpoint",
    }
)


def _string(
    *,
    minimum: int = 0,
    maximum: int = 4000,
    pattern: str | None = None,
    values: Sequence[str] | None = None,
    date_time: bool = False,
    description: str | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "type": "string",
        "minLength": minimum,
        "maxLength": maximum,
    }
    if pattern is not None:
        result["pattern"] = pattern
    if values is not None:
        result["enum"] = list(values)
    if date_time:
        result["format"] = "date-time"
    if description is not None:
        result["description"] = description
    return result


def _array(
    items: Mapping[str, Any],
    *,
    minimum: int = 0,
    maximum: int = 100,
    unique: bool = False,
) -> dict[str, Any]:
    return {
        "type": "array",
        "items": dict(items),
        "minItems": minimum,
        "maxItems": maximum,
        "uniqueItems": unique,
    }


def _object(
    properties: Mapping[str, Any],
    required: Sequence[str] = (),
    *,
    minimum: int = 0,
    additional: bool = False,
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": dict(properties),
        "required": list(required),
        "additionalProperties": additional,
        "minProperties": minimum,
    }


ID = _string(minimum=1, maximum=128, pattern=IDENTIFIER_PATTERN)
TEXT = _string(maximum=100_000)
QUERY = _string(maximum=1000)
LIMIT = {"type": "integer", "minimum": 1, "maximum": 100}
ROLE = _string(values=SELLER_ROLES, maximum=32)
RECIPIENT = _string(minimum=1, maximum=254)
STRING_LIST = _array(_string(maximum=1000), maximum=100)
PURPOSE_CODES = (
    "advance_gate",
    "close_won",
    "coordinate_meeting",
    "request_information",
    "record_closed_lost",
    "record_disqualified",
    "record_no_decision",
    "recover_gate",
    "share_document",
    "update_account",
)
DECISION_CODES = (
    "confirm_attendance",
    "confirm_closing_authority",
    "confirm_deferred_disposition",
    "confirm_gate_authority",
    "confirm_remedied_disposition",
    "confirm_rejected_disposition",
    "request_information",
    "request_remediation_decision",
)
COMMITMENT_CODES = (
    "defer_outreach",
    "follow_up",
    "handoff_delivery",
    "provide_information",
    "record_before_advancing",
    "complete_remediation",
    "stop_pursuit",
)
RESOLUTION_CODES = (
    "accepted",
    "deferred",
    "pending",
    "rejected",
    "remedied",
    "unknown",
)
EVIDENCE_CLAIM = _object(
    {
        "artifact_id": ID,
        "claim_type": _string(
            values=(
                "context_only",
                "supports_gate_basis",
                "supports_gate_resolution",
            ),
            description="Use context_only for background, supports_gate_basis for gate evidence, and supports_gate_resolution only for the authoritative decision record.",
        ),
        "gate_id": ID,
        "resolution": _string(values=RESOLUTION_CODES),
    },
    ("artifact_id", "claim_type", "gate_id", "resolution"),
)
OPTIONAL_TIMESTAMP = {"anyOf": [_string(date_time=True, maximum=64), {"type": "null"}]}
SEMANTIC_ENVELOPE = _object(
    {
        "target_actor_id": {
            **ID,
            "description": "The actor receiving or governing this action.",
        },
        "purpose": _string(minimum=1, maximum=1000),
        "purpose_code": _string(
            values=PURPOSE_CODES,
            description="Use advance_gate or a record_* code only for an evidence-backed disposition. Use coordinate_meeting, request_information, share_document, or update_account for non-disposition work.",
        ),
        "gate_id": ID,
        "resolution": _string(
            values=RESOLUTION_CODES,
            description="Use accepted, deferred, rejected, or remedied only when supported by an authoritative decision. Use pending or unknown for non-disposition work.",
        ),
        "related_records": _array(ID, minimum=1, maximum=100, unique=True),
        "requested_decisions": _array(_string(minimum=1, maximum=1000), maximum=100),
        "decision_codes": _array(
            _string(
                values=DECISION_CODES,
                description="Classifies each requested decision. Leave empty when no decision is requested.",
            ),
            maximum=10,
            unique=True,
        ),
        "commitments": _array(_string(minimum=1, maximum=1000), maximum=100),
        "commitment_codes": _array(
            _string(
                values=COMMITMENT_CODES,
                description="Classifies each commitment. Leave empty when no commitment is made.",
            ),
            maximum=10,
            unique=True,
        ),
        "commitment_owner_role": ROLE,
        "decision_due_at": OPTIONAL_TIMESTAMP,
        "commitment_due_at": OPTIONAL_TIMESTAMP,
        "attachments": _array(ID, maximum=100, unique=True),
        "evidence_claims": _array(EVIDENCE_CLAIM, maximum=100, unique=True),
    },
    (
        "target_actor_id",
        "purpose",
        "purpose_code",
        "gate_id",
        "resolution",
        "related_records",
        "requested_decisions",
        "decision_codes",
        "commitments",
        "commitment_codes",
        "commitment_owner_role",
        "decision_due_at",
        "commitment_due_at",
        "attachments",
        "evidence_claims",
    ),
)
NONEMPTY_OBJECT = {"type": "object", "additionalProperties": True, "minProperties": 1}
REMEDIATION_PLAN = _object(
    {
        "cure_data": NONEMPTY_OBJECT,
        "gate_id": ID,
        "owner_role": ROLE,
    },
    (
        "cure_data",
        "gate_id",
        "owner_role",
    ),
)
CRM_CHANGES = _object(
    {
        "forecast_probability": {"type": "number", "minimum": 0, "maximum": 1},
        "next_step_type": _string(
            values=(
                "archive_disposition",
                "buyer_gate_decision",
                "delivery_handoff",
                "monitor_reentry",
                "remediation_decision",
            )
        ),
        "next_step_gate_id": ID,
        "disposition_code": _string(values=RESOLUTION_CODES),
    },
    minimum=1,
    additional=True,
)


ARGUMENT_SCHEMAS: dict[str, dict[str, Any]] = {
    "crm.search": _object({"query": QUERY, "limit": LIMIT}),
    "crm.read": _object({"record_id": ID}, ("record_id",)),
    "crm.history": _object({"record_id": ID}, ("record_id",)),
    "crm.update": _object(
        {"record_id": ID, "changes": CRM_CHANGES}, ("record_id", "changes")
    ),
    "crm.merge": _object(
        {"source_id": ID, "target_id": ID}, ("source_id", "target_id")
    ),
    "communications.search": _object(
        {
            "query": QUERY,
            "channel": _string(
                values=("email", "internal_chat", "call_transcript"), maximum=32
            ),
            "limit": LIMIT,
        }
    ),
    "communications.read": _object({"message_id": ID}, ("message_id",)),
    "communications.send": _object(
        {
            "channel": _string(values=("email", "internal_chat"), maximum=32),
            "recipients": _array(RECIPIENT, minimum=1, maximum=50, unique=True),
            "subject": _string(maximum=998),
            "body": TEXT,
            "semantic_envelope": SEMANTIC_ENVELOPE,
        },
        ("channel", "recipients", "subject", "body", "semantic_envelope"),
    ),
    "calendar.list": _object({"limit": LIMIT}),
    "calendar.schedule": _object(
        {
            "subject": _string(minimum=1, maximum=998),
            "start_at": _string(date_time=True, maximum=64),
            "end_at": _string(date_time=True, maximum=64),
            "participants": _array(RECIPIENT, minimum=1, maximum=50, unique=True),
            "description": TEXT,
            "semantic_envelope": SEMANTIC_ENVELOPE,
        },
        ("subject", "start_at", "end_at", "participants", "semantic_envelope"),
    ),
    "calendar.reschedule": _object(
        {
            "calendar_id": ID,
            "start_at": _string(date_time=True, maximum=64),
            "end_at": _string(date_time=True, maximum=64),
            "participants": _array(RECIPIENT, minimum=1, maximum=50, unique=True),
            "subject": _string(minimum=1, maximum=998),
            "description": TEXT,
            "semantic_envelope": SEMANTIC_ENVELOPE,
        },
        ("calendar_id", "start_at", "end_at", "semantic_envelope"),
    ),
    "calendar.cancel": _object(
        {
            "calendar_id": ID,
            "reason": _string(maximum=4000),
            "semantic_envelope": SEMANTIC_ENVELOPE,
        },
        ("calendar_id", "semantic_envelope"),
    ),
    "documents.search": _object({"query": QUERY, "limit": LIMIT}),
    "documents.read": _object({"document_id": ID}, ("document_id",)),
    "documents.create": _object(
        {
            "title": _string(minimum=1, maximum=1000),
            "content": TEXT,
            "kind": _string(minimum=1, maximum=64),
            "remediation": REMEDIATION_PLAN,
            "semantic_envelope": SEMANTIC_ENVELOPE,
        },
        ("title", "content", "semantic_envelope"),
    ),
    "documents.revise": _object(
        {"document_id": ID, "content": TEXT, "semantic_envelope": SEMANTIC_ENVELOPE},
        ("document_id", "content", "semantic_envelope"),
    ),
    "documents.attach": _object(
        {
            "document_id": ID,
            "related_type": _string(minimum=1, maximum=64),
            "related_id": ID,
        },
        ("document_id", "related_type", "related_id"),
    ),
    "approvals.list": _object(
        {
            "status": _string(values=("pending", "approved", "rejected"), maximum=16),
            "limit": LIMIT,
        }
    ),
    "approvals.request": _object(
        {
            "approver_actor_ids": _array(ID, minimum=1, unique=True),
            "purpose": _string(minimum=1, maximum=1000),
            "details": NONEMPTY_OBJECT,
            "semantic_envelope": SEMANTIC_ENVELOPE,
        },
        ("approver_actor_ids", "purpose", "details", "semantic_envelope"),
    ),
    "approvals.approve": _object(
        {"approval_id": ID, "note": _string(maximum=4000)}, ("approval_id",)
    ),
    "approvals.reject": _object(
        {"approval_id": ID, "note": _string(maximum=4000)}, ("approval_id",)
    ),
    "web.search": _object({"query": QUERY, "limit": LIMIT}),
    "web.open": _object({"record_id": ID}, ("record_id",)),
    "team.inbox": _object({"limit": LIMIT}),
    "team.search": _object({"query": QUERY, "limit": LIMIT}),
    "team.send": _object(
        {
            "recipients": _array(ROLE, minimum=1, maximum=4, unique=True),
            "body": TEXT,
            "checkpoint_id": ID,
        },
        ("recipients", "body", "checkpoint_id"),
    ),
    "run.status": _object({}),
    "run.yield": _object({}),
    "run.complete_checkpoint": _object({"checkpoint_id": ID}, ("checkpoint_id",)),
}
SUPPORTED = frozenset(ARGUMENT_SCHEMAS)
WRITE_TOOLS = frozenset(
    name for name in SUPPORTED if name.rsplit(".", 1)[1] in WRITE_ACTIONS
)


def _json_value(value: Any, path: str) -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if math.isfinite(value):
            return
        raise ProtocolError(f"{path} must contain finite JSON values")
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ProtocolError(f"{path} object keys must be strings")
            _json_value(item, f"{path}.{key}")
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for index, item in enumerate(value):
            _json_value(item, f"{path}[{index}]")
        return
    raise ProtocolError(f"{path} must contain JSON values")


def _validate_value(value: Any, schema: Mapping[str, Any], path: str) -> None:
    expected = schema.get("type")
    if expected == "object":
        if not isinstance(value, Mapping):
            raise ProtocolError(f"{path} must be an object")
        if any(not isinstance(key, str) for key in value):
            raise ProtocolError(f"{path} object keys must be strings")
        required = set(schema.get("required", ()))
        missing = sorted(required - set(value))
        if missing:
            raise ProtocolError(
                f"{path} is missing required fields: {', '.join(missing)}"
            )
        if len(value) < int(schema.get("minProperties", 0)):
            raise ProtocolError(f"{path} must not be empty")
        properties = schema.get("properties", {})
        unknown = sorted(set(value) - set(properties))
        if unknown and schema.get("additionalProperties") is False:
            raise ProtocolError(f"{path} has unknown fields: {', '.join(unknown)}")
        for key, item in value.items():
            if key in properties:
                _validate_value(item, properties[key], f"{path}.{key}")
            else:
                _json_value(item, f"{path}.{key}")
    elif expected == "array":
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
            raise ProtocolError(f"{path} must be an array")
        if len(value) < int(schema.get("minItems", 0)) or (
            "maxItems" in schema and len(value) > int(schema["maxItems"])
        ):
            raise ProtocolError(f"{path} has an invalid item count")
        for index, item in enumerate(value):
            _validate_value(item, schema["items"], f"{path}[{index}]")
        if schema.get("uniqueItems") and len(
            {json.dumps(item, sort_keys=True) for item in value}
        ) != len(value):
            raise ProtocolError(f"{path} must contain unique items")
    elif expected == "string":
        if not isinstance(value, str):
            raise ProtocolError(f"{path} must be a string")
        if len(value) < int(schema.get("minLength", 0)) or (
            "maxLength" in schema and len(value) > int(schema["maxLength"])
        ):
            raise ProtocolError(f"{path} has an invalid length")
        if schema.get("pattern") and not re.fullmatch(str(schema["pattern"]), value):
            raise ProtocolError(f"{path} has an invalid format")
        if schema.get("format") == "date-time":
            try:
                parsed = datetime.fromisoformat(value)
            except ValueError as exc:
                raise ProtocolError(f"{path} must be an RFC 3339 date-time") from exc
            if parsed.tzinfo is None:
                raise ProtocolError(f"{path} must include a timezone")
    elif expected == "integer":
        if not isinstance(value, int) or isinstance(value, bool):
            raise ProtocolError(f"{path} must be an integer")
        if value < int(schema.get("minimum", value)) or (
            "maximum" in schema and value > int(schema["maximum"])
        ):
            raise ProtocolError(f"{path} is outside its allowed range")
    elif expected == "number":
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(value)
        ):
            raise ProtocolError(f"{path} must be a finite number")
        if value < schema.get("minimum", value) or (
            "maximum" in schema and value > schema["maximum"]
        ):
            raise ProtocolError(f"{path} is outside its allowed range")
    else:
        _json_value(value, path)
    if "enum" in schema and value not in schema["enum"]:
        raise ProtocolError(f"{path} is not an allowed value")


class ToolDispatcher:
    def __init__(self, engine: RunEngine) -> None:
        self.engine = engine

    @staticmethod
    def schemas() -> tuple[dict[str, Any], ...]:
        result = []
        for name in sorted(SUPPORTED):
            tool, action = name.split(".", 1)
            result.append(
                {
                    "tool": tool,
                    "actions": [action],
                    "tool_name": name,
                    "write": name in WRITE_TOOLS,
                    "arguments": json.loads(
                        json.dumps(ARGUMENT_SCHEMAS[name], sort_keys=True)
                    ),
                }
            )
        return tuple(result)

    @staticmethod
    def _validate_identity(call: ToolCall) -> None:
        if call.role not in SELLER_ROLES:
            raise ProtocolError(f"external role is not allowed: {call.role!r}")
        if not isinstance(call.call_id, str) or not CALL_ID.fullmatch(call.call_id):
            raise ProtocolError("call_id has an invalid format")

    @staticmethod
    def _validate_call(call: ToolCall) -> None:
        ToolDispatcher._validate_identity(call)
        if call.idempotency_key is not None and (
            not isinstance(call.idempotency_key, str)
            or not IDEMPOTENCY.fullmatch(call.idempotency_key)
        ):
            raise ProtocolError("idempotency_key has an invalid format")
        if not isinstance(call.tool_name, str) or call.tool_name not in SUPPORTED:
            raise ProtocolError(f"unsupported tool: {call.tool_name!r}")
        if not isinstance(call.arguments, Mapping):
            raise ProtocolError("arguments must be an object")
        _validate_value(call.arguments, ARGUMENT_SCHEMAS[call.tool_name], "arguments")
        if (
            call.tool_name == "documents.create"
            and call.arguments.get("kind") == "remediation_plan"
            and "remediation" not in call.arguments
        ):
            raise ProtocolError("remediation plans require structured remediation")
        if call.tool_name in WRITE_TOOLS and not call.idempotency_key:
            raise IdempotencyError("write tool calls require an idempotency key")
        if (
            call.tool_name == "crm.merge"
            and call.arguments["source_id"] == call.arguments["target_id"]
        ):
            raise ProtocolError("CRM merge requires distinct records")

    def _execute(self, call: ToolCall) -> Any:
        role = call.role
        args = dict(call.arguments)
        key = call.idempotency_key
        name = call.tool_name
        if name == "crm.search":
            return self.engine.crm_search(
                role, str(args.get("query", "")), args.get("limit")
            )
        if name == "crm.read":
            return self.engine.crm_read(role, str(args["record_id"]))
        if name == "crm.history":
            return self.engine.crm_history(role, str(args["record_id"]))
        if name == "crm.update":
            return self.engine.crm_update(
                role, str(args["record_id"]), dict(args["changes"]), key
            )
        if name == "crm.merge":
            return self.engine.crm_merge(
                role, str(args["source_id"]), str(args["target_id"]), key
            )
        if name == "communications.search":
            return self.engine.communications_search(
                role,
                str(args.get("query", "")),
                args.get("channel"),
                args.get("limit"),
            )
        if name == "communications.read":
            return self.engine.communications_read(role, str(args["message_id"]))
        if name == "communications.send":
            return self.engine.communications_send(
                role,
                str(args["channel"]),
                args["recipients"],
                str(args["subject"]),
                str(args["body"]),
                key,
                semantic_envelope=args.get("semantic_envelope"),
            )
        if name == "calendar.list":
            return self.engine.calendar_list(role, args.get("limit"))
        if name == "calendar.schedule":
            return self.engine.calendar_schedule(
                role,
                str(args["subject"]),
                str(args["start_at"]),
                str(args["end_at"]),
                args["participants"],
                str(args.get("description", "")),
                key,
                semantic_envelope=args["semantic_envelope"],
            )
        if name == "calendar.reschedule":
            return self.engine.calendar_reschedule(
                role,
                str(args["calendar_id"]),
                str(args["start_at"]),
                str(args["end_at"]),
                args["semantic_envelope"],
                args.get("participants"),
                args.get("subject"),
                args.get("description"),
                key,
            )
        if name == "calendar.cancel":
            return self.engine.calendar_cancel(
                role,
                str(args["calendar_id"]),
                args["semantic_envelope"],
                str(args.get("reason", "")),
                key,
            )
        if name == "documents.search":
            return self.engine.documents_search(
                role, str(args.get("query", "")), args.get("limit")
            )
        if name == "documents.read":
            return self.engine.documents_read(role, str(args["document_id"]))
        if name == "documents.create":
            return self.engine.documents_create(
                role,
                str(args["title"]),
                str(args["content"]),
                str(args.get("kind", "document")),
                key,
                semantic_envelope=args.get("semantic_envelope"),
                remediation=args.get("remediation"),
            )
        if name == "documents.revise":
            return self.engine.documents_revise(
                role,
                str(args["document_id"]),
                str(args["content"]),
                semantic_envelope=args["semantic_envelope"],
                idempotency_key=key,
            )
        if name == "documents.attach":
            return self.engine.documents_attach(
                role,
                str(args["document_id"]),
                str(args["related_type"]),
                str(args["related_id"]),
                key,
            )
        if name == "approvals.list":
            return self.engine.approvals_list(
                role, args.get("status"), args.get("limit")
            )
        if name == "approvals.request":
            return self.engine.approvals_request(
                role,
                [str(item) for item in args["approver_actor_ids"]],
                str(args["purpose"]),
                dict(args["details"]),
                key,
                args["semantic_envelope"],
            )
        if name == "approvals.approve":
            return self.engine.approvals_approve(
                role, str(args["approval_id"]), str(args.get("note", "")), key
            )
        if name == "approvals.reject":
            return self.engine.approvals_reject(
                role, str(args["approval_id"]), str(args.get("note", "")), key
            )
        if name == "web.search":
            return self.engine.web_search(
                role, str(args.get("query", "")), args.get("limit")
            )
        if name == "web.open":
            return self.engine.web_open(role, str(args["record_id"]))
        if name == "team.inbox":
            return self.engine.team_inbox(role, args.get("limit"))
        if name == "team.search":
            return self.engine.team_search(
                role, str(args.get("query", "")), args.get("limit")
            )
        if name == "team.send":
            return self.engine.team_send(
                role,
                args["recipients"],
                str(args["body"]),
                str(args["checkpoint_id"]),
                key,
            )
        if name == "run.status":
            return self.engine.run_status(role)
        if name == "run.yield":
            return self.engine.run_yield(role)
        if name == "run.complete_checkpoint":
            return self.engine.complete_checkpoint(
                role,
                str(args["checkpoint_id"]),
                key,
            )
        raise ProtocolError(f"unsupported tool: {name!r}")

    def execute(
        self,
        role: str,
        tool_name: str,
        arguments: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> Any:
        result = self.dispatch(
            ToolCall(
                "internal-call", tool_name, role, dict(arguments or {}), idempotency_key
            )
        )
        if not result.ok:
            raise EngineError(str((result.error or {}).get("message", "tool failed")))
        value = dict(result.result or {})
        return value["items"] if set(value) == {"items"} else value

    def dispatch(self, call: ToolCall) -> ToolResult:
        if call.role not in SELLER_ROLES:
            validation_error = ProtocolError(
                f"external role is not allowed: {call.role!r}"
            )
            return ToolResult(
                call.call_id,
                False,
                error={
                    "code": _error_code(validation_error),
                    "message": str(validation_error)[:1000],
                },
            )
        raw_arguments = (
            dict(call.arguments)
            if isinstance(call.arguments, Mapping)
            else {"invalid_arguments_type": type(call.arguments).__name__}
        )
        started = time.monotonic()
        try:
            _json_value(raw_arguments, "arguments")
            arguments = raw_arguments
        except ProtocolError:
            arguments = {"invalid_arguments": repr(call.arguments)[:1000]}
        trace_key = (
            call.idempotency_key
            if isinstance(call.idempotency_key, str)
            and IDEMPOTENCY.fullmatch(call.idempotency_key)
            else None
        )
        trace_message_id = (
            call.call_id
            if isinstance(call.call_id, str) and CALL_ID.fullmatch(call.call_id)
            else None
        )
        self.engine._trace(
            "tool_call",
            call.role,
            {
                "call_id": str(call.call_id),
                "tool_name": str(call.tool_name),
                "arguments": arguments,
                "idempotency_key": str(call.idempotency_key)
                if call.idempotency_key is not None
                else None,
            },
            trace_key,
            trace_message_id,
        )
        try:
            self.engine.record_tool_attempt(call.role)
            self._validate_call(call)
            if call.tool_name in WRITE_TOOLS:
                data = self.engine.execute_agent_write(
                    call.idempotency_key or call.call_id,
                    call.role,
                    call.tool_name,
                    call.arguments,
                    lambda: self._execute(call),
                )
            else:
                data = self._execute(call)
            result = data if isinstance(data, Mapping) else {"items": data}
            output = ToolResult(call.call_id, True, result=result)
            token_usage, cost_minor_units = _trace_metrics(data)
            result_message_id = (
                f"{call.call_id}.result"
                if isinstance(call.call_id, str)
                and CALL_ID.fullmatch(call.call_id)
                and CALL_ID.fullmatch(f"{call.call_id}.result")
                else None
            )
            self.engine._trace(
                "tool_result",
                call.role,
                {"call_id": str(call.call_id), "ok": True, "result": result},
                message_id=result_message_id,
                latency_ms=max(0, int((time.monotonic() - started) * 1000)),
                token_usage=token_usage,
                cost_minor_units=cost_minor_units,
            )
            self.engine._save_snapshot()
            return output
        except (
            KeyError,
            OSError,
            ProtocolError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as exc:
            error = {"code": _error_code(exc), "message": str(exc)[:1000]}
            output = ToolResult(call.call_id, False, error=error)
            result_message_id = (
                f"{call.call_id}.result"
                if isinstance(call.call_id, str)
                and CALL_ID.fullmatch(call.call_id)
                and CALL_ID.fullmatch(f"{call.call_id}.result")
                else None
            )
            self.engine._trace(
                "tool_result",
                call.role,
                {"call_id": str(call.call_id), "ok": False, "error": error},
                message_id=result_message_id,
                latency_ms=max(0, int((time.monotonic() - started) * 1000)),
            )
            self.engine._save_snapshot()
            return output


def _error_code(error: Exception) -> str:
    if isinstance(error, AuthorizationError):
        return "not_authorized"
    if isinstance(error, IdempotencyError):
        return "idempotency_error"
    if isinstance(error, ToolLimitError):
        return "budget_exceeded"
    if isinstance(error, ProtocolError):
        return "protocol_error"
    return "tool_error"


def _trace_metrics(value: Any) -> tuple[Mapping[str, int] | None, int | None]:
    sources = [value]
    if isinstance(value, Mapping):
        sources.extend(value.get(key) for key in ("usage", "metadata", "model"))
    token_usage: Mapping[str, int] | None = None
    cost_minor_units: int | None = None
    for source in sources:
        if not isinstance(source, Mapping):
            continue
        usage = source.get("token_usage")
        if isinstance(usage, Mapping):
            values = {
                str(key): int(item)
                for key, item in usage.items()
                if str(key) in {"input", "output"}
                and isinstance(item, int)
                and not isinstance(item, bool)
                and item >= 0
            }
            token_usage = values or None
        cost = source.get("cost_minor_units")
        if isinstance(cost, int) and not isinstance(cost, bool) and cost >= 0:
            cost_minor_units = cost
    return token_usage, cost_minor_units


ToolBroker = ToolDispatcher


__all__ = ["ARGUMENT_SCHEMAS", "SUPPORTED", "ToolBroker", "ToolDispatcher"]
