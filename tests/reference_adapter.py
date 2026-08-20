from __future__ import annotations

import json
import sys
from pathlib import Path

trace = [
    json.loads(line)
    for line in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines()
    if line
]
mode = sys.argv[2] if len(sys.argv) > 2 else "reference"


def calls(
    request: dict[str, object], role: str | None = None
) -> list[dict[str, object]]:
    selected = [
        dict(row)
        for row in trace
        if row["kind"] == "tool_call"
        and row["occurred_at"] == request["occurred_at"]
        and (role is None or row["role"] == role)
    ]
    for row in selected:
        row["run_id"] = request["run_id"]
        row["observation_token"] = request["observation_token"]
        row["sequence"] = int(row["sequence"]) * 10 + 2
    return selected


def fixed(request: dict[str, object]) -> None:
    role = str(request["role"])
    available = calls(request, role)
    if not available:
        return
    checkpoint = dict(request["checkpoint"])
    history = list(request.get("messages", []))
    activations = sum(
        isinstance(item, dict)
        and item.get("kind") == "observation"
        and dict(item.get("checkpoint", {})).get("checkpoint_id")
        == checkpoint["checkpoint_id"]
        for item in history
    )
    executed = {
        item.get("message_id")
        for item in history
        if isinstance(item, dict) and item.get("kind") == "tool_call"
    }
    selected = [
        next(
            (row for row in available if row.get("message_id") not in executed),
            available[-1],
        )
    ]
    recovery_send = (
        selected[0].get("tool_name") == "communications.send"
        and dict(selected[0].get("arguments", {}))
        .get("semantic_envelope", {})
        .get("purpose_code")
        == "recover_gate"
    )
    plan_ready = any(
        isinstance(item, dict)
        and item.get("kind") == "team_message"
        and dict(item.get("payload", {})).get("body") == "Remediation plan ready."
        for item in history
    )
    if recovery_send and not plan_ready:
        return
    first_sequence = int(selected[0]["sequence"])
    prefix: list[dict[str, object]] = []
    if mode == "yield-first" and activations == 0:
        prefix.append(
            {
                "protocol_version": request["protocol_version"],
                "run_id": request["run_id"],
                "sequence": first_sequence - 1,
                "message_id": f"yield-{checkpoint['checkpoint_id']}-{role}",
                "occurred_at": request["occurred_at"],
                "kind": "yield",
                "role": role,
                "reason": "waiting",
                "observation_token": request["observation_token"],
            }
        )
        selected = []
    elif mode == "team-yield" and activations == 0:
        if role == "account_executive":
            prefix.append(
                {
                    "protocol_version": request["protocol_version"],
                    "run_id": request["run_id"],
                    "sequence": first_sequence - 2,
                    "message_id": f"team-{checkpoint['checkpoint_id']}",
                    "occurred_at": request["occurred_at"],
                    "kind": "team_message",
                    "role": role,
                    "recipient_role": "domain_specialist",
                    "payload": {"body": "Review the evidence."},
                    "observation_token": request["observation_token"],
                }
            )
        prefix.append(
            {
                "protocol_version": request["protocol_version"],
                "run_id": request["run_id"],
                "sequence": first_sequence - 1,
                "message_id": f"yield-{checkpoint['checkpoint_id']}-{role}",
                "occurred_at": request["occurred_at"],
                "kind": "yield",
                "role": role,
                "reason": "Waiting for evidence.",
                "observation_token": request["observation_token"],
            }
        )
    suffix: list[dict[str, object]] = []
    if selected:
        arguments = dict(selected[0].get("arguments", {}))
        remediation_calls = {
            item.get("message_id")
            for item in history
            if isinstance(item, dict)
            and item.get("kind") == "tool_call"
            and item.get("tool_name") == "documents.create"
            and dict(item.get("arguments", {})).get("kind") == "remediation_plan"
        }
        remediation_document_ids = {
            dict(item.get("result", {})).get("document_id")
            for item in history
            if isinstance(item, dict)
            and item.get("kind") == "tool_result"
            and item.get("call_id") in remediation_calls
        }
        if (
            selected[0].get("tool_name") == "documents.attach"
            and arguments.get("document_id") in remediation_document_ids
        ):
            suffix.append(
                {
                    "protocol_version": request["protocol_version"],
                    "run_id": request["run_id"],
                    "sequence": int(selected[0]["sequence"]) + 1,
                    "message_id": f"plan-ready-{checkpoint['checkpoint_id']}",
                    "occurred_at": request["occurred_at"],
                    "kind": "team_message",
                    "role": role,
                    "recipient_role": "account_executive",
                    "payload": {"body": "Remediation plan ready."},
                    "observation_token": request["observation_token"],
                }
            )
    for row in prefix + selected + suffix:
        print(json.dumps(row), flush=True)


first = json.loads(sys.stdin.readline())
if first.get("kind") == "adapter_request":
    fixed(first)
else:
    seen: set[str] = set()
    request = first
    while True:
        if request.get("kind") != "observation":
            pass
        else:
            occurred_at = str(request["occurred_at"])
            if occurred_at not in seen:
                seen.add(occurred_at)
                for row in calls(request):
                    print(json.dumps(row), flush=True)
        line = sys.stdin.readline()
        if not line:
            break
        request = json.loads(line)
