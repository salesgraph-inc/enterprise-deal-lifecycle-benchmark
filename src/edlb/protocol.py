from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, TextIO

PROTOCOL_VERSION = "v1.0.0"
MAX_PROTOCOL_MESSAGE_BYTES = 8 * 1024 * 1024
EXTERNAL_ROLES = frozenset(
    {"account_executive", "domain_specialist", "sales_manager", "revops"}
)
ALL_ROLES = EXTERNAL_ROLES | {"system"}
KINDS = frozenset(
    {
        "start",
        "observation",
        "tool_call",
        "tool_result",
        "team_message",
        "yield",
        "checkpoint_complete",
        "run_end",
    }
)
WRITE_ACTIONS = frozenset(
    {
        "send",
        "update",
        "merge",
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
IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
IDEMPOTENCY = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,191}$")
TOOL_NAME = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")
OBSERVATION_TOKEN = re.compile(r"^[A-Za-z0-9_-]{32,128}$")


class ProtocolError(ValueError):
    pass


def _identifier(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ProtocolError(f"{field_name} must match {IDENTIFIER.pattern}")
    return value


RFC3339_TIMESTAMP = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})"
)


def validate_timestamp(value: Any) -> str:
    if not isinstance(value, str) or not RFC3339_TIMESTAMP.fullmatch(value):
        raise ValueError("timestamp must be an RFC3339 string with a timezone")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("timestamp must be an RFC3339 string with a timezone") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return value


def _timestamp(value: Any) -> str:
    try:
        return validate_timestamp(value)
    except ValueError as exc:
        raise ProtocolError(
            "occurred_at must be an RFC3339 string with a timezone"
        ) from exc


def _mapping(value: Any, field_name: str, nonempty: bool = False) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ProtocolError(f"{field_name} must be an object")
    result = dict(value)
    if nonempty and not result:
        raise ProtocolError(f"{field_name} must not be empty")
    return result


@dataclass(frozen=True, slots=True)
class Message:
    protocol_version: str
    run_id: str
    sequence: int
    message_id: str
    occurred_at: str
    kind: str
    role: str
    payload: Mapping[str, Any] | None = None
    tool_name: str | None = None
    arguments: Mapping[str, Any] | None = None
    idempotency_key: str | None = None
    call_id: str | None = None
    ok: bool | None = None
    result: Mapping[str, Any] | None = None
    error: Mapping[str, Any] | None = None
    recipient_role: str | None = None
    checkpoint_id: str | None = None
    summary: str | None = None
    status: str | None = None
    reason: str | None = None
    observation_token: str | None = None

    def to_dict(self, allow_system: bool = False) -> dict[str, Any]:
        value: dict[str, Any] = {
            "protocol_version": self.protocol_version,
            "run_id": self.run_id,
            "sequence": self.sequence,
            "message_id": self.message_id,
            "occurred_at": self.occurred_at,
            "kind": self.kind,
            "role": self.role,
        }
        if self.observation_token is not None:
            value["observation_token"] = self.observation_token
        if self.kind in {"start", "observation", "team_message"}:
            value["payload"] = dict(self.payload or {})
        elif self.kind == "tool_call":
            value["tool_name"] = self.tool_name
            value["arguments"] = dict(self.arguments or {})
            if self.idempotency_key is not None:
                value["idempotency_key"] = self.idempotency_key
        elif self.kind == "tool_result":
            value["call_id"] = self.call_id
            value["ok"] = self.ok
            if self.ok:
                value["result"] = dict(self.result or {})
            else:
                value["error"] = dict(self.error or {})
        elif self.kind == "yield":
            if self.reason is not None:
                value["reason"] = self.reason
        elif self.kind == "checkpoint_complete":
            value["checkpoint_id"] = self.checkpoint_id
            value["summary"] = self.summary
        elif self.kind == "run_end":
            value["status"] = self.status
            if self.reason is not None:
                value["reason"] = self.reason
        _validate(value, allow_system=allow_system)
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], allow_system: bool = False) -> Message:
        data = _validate(value, allow_system=allow_system)
        return cls(
            protocol_version=str(data["protocol_version"]),
            run_id=str(data["run_id"]),
            sequence=int(data["sequence"]),
            message_id=str(data["message_id"]),
            occurred_at=str(data["occurred_at"]),
            kind=str(data["kind"]),
            role=str(data["role"]),
            payload=dict(data["payload"]) if "payload" in data else None,
            tool_name=data.get("tool_name"),
            arguments=dict(data["arguments"]) if "arguments" in data else None,
            idempotency_key=data.get("idempotency_key"),
            call_id=data.get("call_id"),
            ok=data.get("ok"),
            result=dict(data["result"]) if "result" in data else None,
            error=dict(data["error"]) if "error" in data else None,
            recipient_role=data.get("recipient_role"),
            checkpoint_id=data.get("checkpoint_id"),
            summary=data.get("summary"),
            status=data.get("status"),
            reason=data.get("reason"),
            observation_token=data.get("observation_token"),
        )


def _validate(value: Mapping[str, Any], allow_system: bool = False) -> dict[str, Any]:
    data = dict(value)
    required = {
        "protocol_version",
        "run_id",
        "sequence",
        "message_id",
        "occurred_at",
        "kind",
        "role",
    }
    missing = sorted(required - data.keys())
    if missing:
        raise ProtocolError(f"missing message fields: {', '.join(missing)}")
    if data["protocol_version"] != PROTOCOL_VERSION:
        raise ProtocolError(
            f"unsupported protocol version: {data['protocol_version']!r}"
        )
    _identifier(data["run_id"], "run_id")
    _identifier(data["message_id"], "message_id")
    sequence = data["sequence"]
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0:
        raise ProtocolError("sequence must be a non-negative integer")
    _timestamp(data["occurred_at"])
    kind = data["kind"]
    if kind not in KINDS:
        raise ProtocolError(f"unknown message kind: {kind!r}")
    role = data["role"]
    process_run_end = kind == "run_end" and role == "system"
    if role not in ALL_ROLES or (
        not allow_system and role not in EXTERNAL_ROLES and not process_run_end
    ):
        raise ProtocolError(f"invalid external role: {role!r}")
    if kind in {"start", "run_end"} and role != "system":
        raise ProtocolError(f"{kind} messages require system role")
    if kind not in {"start", "run_end"} and role == "system":
        raise ProtocolError("system role is reserved for internal orchestration")
    shared_optional = {"observation_token"}
    allowed: dict[str, set[str]] = {
        "start": required | shared_optional | {"payload"},
        "observation": required | shared_optional | {"payload"},
        "tool_call": required
        | shared_optional
        | {"tool_name", "arguments", "idempotency_key"},
        "tool_result": required
        | shared_optional
        | {"call_id", "ok", "result", "error"},
        "team_message": required | shared_optional | {"recipient_role", "payload"},
        "yield": required | shared_optional | {"reason"},
        "checkpoint_complete": required
        | shared_optional
        | {"checkpoint_id", "summary"},
        "run_end": required | shared_optional | {"status", "reason"},
    }
    unknown = sorted(set(data) - allowed[kind])
    if unknown:
        raise ProtocolError(f"unknown fields for {kind}: {', '.join(unknown)}")
    observation_token = data.get("observation_token")
    if (
        kind
        in {
            "tool_call",
            "team_message",
            "yield",
            "checkpoint_complete",
            "run_end",
        }
        and observation_token is None
    ):
        raise ProtocolError(f"{kind} messages require an observation_token")
    if observation_token is not None and (
        not isinstance(observation_token, str)
        or not OBSERVATION_TOKEN.fullmatch(observation_token)
    ):
        raise ProtocolError("observation_token has an invalid format")
    if kind in {"start", "observation", "team_message"}:
        _mapping(data.get("payload"), "payload", nonempty=True)
    if kind == "tool_call":
        tool_name = data.get("tool_name")
        if not isinstance(tool_name, str) or not TOOL_NAME.fullmatch(tool_name):
            raise ProtocolError("tool_name must be a dotted lowercase name")
        _mapping(data.get("arguments"), "arguments")
        action = tool_name.split(".", 1)[1]
        idempotency_key = data.get("idempotency_key")
        if idempotency_key is not None and (
            not isinstance(idempotency_key, str)
            or not IDEMPOTENCY.fullmatch(idempotency_key)
        ):
            raise ProtocolError("idempotency_key has an invalid format")
        if action in WRITE_ACTIONS and idempotency_key is None:
            raise ProtocolError("write tool calls require an idempotency key")
    if kind == "tool_result":
        _identifier(data.get("call_id"), "call_id")
        if not isinstance(data.get("ok"), bool):
            raise ProtocolError("tool_result ok must be boolean")
        if data["ok"]:
            _mapping(data.get("result"), "result")
            if "error" in data:
                raise ProtocolError("successful tool_result cannot include error")
        else:
            error = _mapping(data.get("error"), "error")
            if not isinstance(error.get("code"), str) or not re.fullmatch(
                r"^[a-z0-9_]+$", error["code"]
            ):
                raise ProtocolError("tool error code has an invalid format")
            if not isinstance(error.get("message"), str) or not error["message"]:
                raise ProtocolError("tool error message is invalid")
            if "result" in data:
                raise ProtocolError("failed tool_result cannot include result")
    if kind == "team_message" and data.get("recipient_role") not in EXTERNAL_ROLES:
        raise ProtocolError("recipient_role must be an external seller role")
    if (
        kind == "yield"
        and data.get("reason") is not None
        and not isinstance(data["reason"], str)
    ):
        raise ProtocolError("yield reason is invalid")
    if kind == "checkpoint_complete":
        _identifier(data.get("checkpoint_id"), "checkpoint_id")
        if not isinstance(data.get("summary"), str) or not data["summary"]:
            raise ProtocolError("checkpoint summary is invalid")
    if kind == "run_end":
        if data.get("status") not in {"completed", "failed", "invalid"}:
            raise ProtocolError("run_end status is invalid")
        if data.get("reason") is not None and (not isinstance(data["reason"], str)):
            raise ProtocolError("run_end reason is invalid")
    return data


@dataclass(frozen=True, slots=True)
class ToolCall:
    call_id: str
    tool_name: str
    role: str
    arguments: Mapping[str, Any] = field(default_factory=dict)
    idempotency_key: str | None = None

    @property
    def tool(self) -> str:
        return self.tool_name.split(".", 1)[0]

    @property
    def action(self) -> str:
        return self.tool_name.split(".", 1)[1]

    def to_message(
        self,
        run_id: str,
        sequence: int,
        occurred_at: str,
        message_id: str | None = None,
        observation_token: str | None = None,
    ) -> Message:
        return Message(
            PROTOCOL_VERSION,
            run_id,
            sequence,
            message_id or self.call_id,
            occurred_at,
            "tool_call",
            self.role,
            tool_name=self.tool_name,
            arguments=self.arguments,
            idempotency_key=self.idempotency_key,
            observation_token=observation_token,
        )

    @classmethod
    def from_message(cls, message: Message) -> ToolCall:
        if (
            message.kind != "tool_call"
            or message.tool_name is None
            or message.arguments is None
        ):
            raise ProtocolError("message is not a valid tool_call")
        return cls(
            message.call_id or message.message_id,
            message.tool_name,
            message.role,
            message.arguments,
            message.idempotency_key,
        )


@dataclass(frozen=True, slots=True)
class ToolResult:
    call_id: str
    ok: bool
    result: Mapping[str, Any] | None = None
    error: Mapping[str, Any] | None = None

    def to_message(
        self,
        run_id: str,
        sequence: int,
        role: str,
        occurred_at: str,
        message_id: str | None = None,
    ) -> Message:
        return Message(
            PROTOCOL_VERSION,
            run_id,
            sequence,
            message_id or f"{self.call_id}.result",
            occurred_at,
            "tool_result",
            role,
            call_id=self.call_id,
            ok=self.ok,
            result=self.result,
            error=self.error,
        )

    @classmethod
    def from_message(cls, message: Message) -> ToolResult:
        if (
            message.kind != "tool_result"
            or message.call_id is None
            or message.ok is None
        ):
            raise ProtocolError("message is not a valid tool_result")
        return cls(message.call_id, message.ok, message.result, message.error)


def encode(message: Message) -> str:
    return json.dumps(
        message.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def decode(line: str) -> Message:
    try:
        size = len(line.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise ProtocolError("JSONL message is not valid UTF-8") from exc
    if size > MAX_PROTOCOL_MESSAGE_BYTES:
        raise ProtocolError(
            f"JSONL message exceeds the {MAX_PROTOCOL_MESSAGE_BYTES}-byte transport ceiling"
        )
    try:
        value = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"invalid JSONL message: {exc.msg}") from exc
    if not isinstance(value, Mapping):
        raise ProtocolError("JSONL message must be an object")
    return Message.from_dict(value)


def read_messages(stream: TextIO) -> Iterable[Message]:
    for line_number, line in enumerate(stream, start=1):
        if not line.strip():
            continue
        try:
            yield decode(line)
        except ProtocolError as exc:
            raise ProtocolError(f"line {line_number}: {exc}") from exc


def write_message(stream: TextIO, message: Message) -> None:
    stream.write(encode(message) + "\n")
    stream.flush()


__all__ = [
    "ALL_ROLES",
    "EXTERNAL_ROLES",
    "KINDS",
    "MAX_PROTOCOL_MESSAGE_BYTES",
    "PROTOCOL_VERSION",
    "Message",
    "ProtocolError",
    "ToolCall",
    "ToolResult",
    "decode",
    "encode",
    "read_messages",
    "validate_timestamp",
    "write_message",
]
