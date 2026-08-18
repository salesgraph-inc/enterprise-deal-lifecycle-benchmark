from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from .protocol import PROTOCOL_VERSION, WRITE_ACTIONS, Message, write_message

SYSTEM_PROMPT = """You are the active role in the Enterprise Deal Lifecycle Benchmark fixed harness. Use only role-scoped evidence and call exactly one supplied tool. The Account Executive owns buyer communication, meetings, opportunities, and proposals within approved terms. The Domain Specialist handles technical, delivery, underwriting, or subject-matter work and cannot approve pricing or forecasts. The Sales Manager owns coaching, forecast acceptance, escalation, and delegated commercial exceptions. RevOps repairs CRM data, merges duplicates, reconciles activity, and cannot contact buyers or approve commercial terms. Retrieve evidence before consequential writes. Never invent records, approvals, commitments, or stakeholder facts. Use run__complete_checkpoint when your role's work is complete. Provider tool names use a double underscore where EDLB uses a dot."""
PROMPT_HASH = "sha256:" + hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest()
PROVIDERS = ("openai-chat", "openai-compatible", "anthropic-messages")
RESERVED_SETTINGS = frozenset(
    {"input", "instructions", "messages", "model", "stream", "system", "tools"}
)
SECRET_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "authorization",
        "bearer_token",
        "headers",
        "password",
        "secret",
        "token",
        "x_api_key",
    }
)


class ProviderError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    provider: str
    model: str
    model_digest: str
    base_url: str
    settings: Mapping[str, Any]
    api_key: str = field(repr=False)
    prompt_hash: str = PROMPT_HASH
    api_version: str | None = None


def _provider_name(name: str) -> str:
    return name.replace(".", "__", 1)


def _endpoint(config: ProviderConfig) -> str:
    suffix = (
        "/messages" if config.provider == "anthropic-messages" else "/chat/completions"
    )
    base = config.base_url.rstrip("/")
    return base if base.endswith(suffix) else base + suffix


def _validate_base_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ProviderError("base URL must be an absolute HTTP or HTTPS URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ProviderError("base URL cannot contain credentials, query, or fragment")
    if parsed.scheme == "http" and parsed.hostname not in {
        "127.0.0.1",
        "::1",
        "localhost",
    }:
        raise ProviderError("non-local provider base URLs must use HTTPS")
    return value.rstrip("/")


def _settings(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ProviderError("settings JSON is invalid") from exc
    if not isinstance(parsed, Mapping):
        raise ProviderError("settings JSON must be an object")
    settings = dict(parsed)
    reserved = sorted(RESERVED_SETTINGS & settings.keys())
    if reserved:
        raise ProviderError(f"settings cannot override {', '.join(reserved)}")
    try:
        json.dumps(settings, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ProviderError("settings must contain finite JSON values") from exc
    return settings


def _reject_credentials(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = str(key).lower().replace("-", "_")
            if normalized in SECRET_KEYS:
                raise ProviderError(
                    "credentials are only allowed through the environment"
                )
            _reject_credentials(item)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for item in value:
            _reject_credentials(item)


def _config_from_request(request: Mapping[str, Any], api_key: str) -> ProviderConfig:
    raw = request.get("model_config")
    if not isinstance(raw, Mapping):
        raise ProviderError("adapter request model configuration is missing")
    model = raw.get("model_id")
    model_digest = raw.get("model_digest")
    prompt_hash = raw.get("prompt_hash")
    provider_settings = raw.get("provider_settings")
    provider_defaults_digest = raw.get("provider_defaults_digest")
    if (
        not isinstance(model, str)
        or not isinstance(model_digest, str)
        or not isinstance(prompt_hash, str)
        or not isinstance(provider_settings, Mapping)
    ):
        raise ProviderError("adapter request model configuration is invalid")
    _reject_credentials(provider_settings)
    if (
        raw.get("provider_defaults") is not True
        or not isinstance(provider_defaults_digest, str)
        or len(provider_defaults_digest) != 71
        or not provider_defaults_digest.startswith("sha256:")
        or any(
            character not in "0123456789abcdef"
            for character in provider_defaults_digest[7:]
        )
    ):
        raise ProviderError(
            "provider defaults and their digest must be pinned in the run manifest"
        )
    if prompt_hash != PROMPT_HASH:
        raise ProviderError("adapter prompt hash does not match the run manifest")
    provider = provider_settings.get("provider")
    base_url = provider_settings.get("base_url")
    request_settings = provider_settings.get("request")
    api_version = provider_settings.get("api_version")
    if provider not in PROVIDERS or not isinstance(base_url, str):
        raise ProviderError(
            "provider and base URL must be explicit in the run manifest"
        )
    if not isinstance(request_settings, Mapping):
        raise ProviderError(
            "provider request settings must be explicit in the run manifest"
        )
    settings = _settings(json.dumps(dict(request_settings), allow_nan=False))
    if provider == "anthropic-messages":
        max_tokens = settings.get("max_tokens")
        tool_choice = settings.get("tool_choice")
        if (
            not isinstance(max_tokens, int)
            or isinstance(max_tokens, bool)
            or max_tokens <= 0
        ):
            raise ProviderError(
                "Anthropic settings require an explicit positive max_tokens"
            )
        if not isinstance(api_version, str) or not api_version:
            raise ProviderError("Anthropic requires an explicit API version")
        if not (
            isinstance(tool_choice, Mapping)
            and tool_choice.get("type") == "any"
            and tool_choice.get("disable_parallel_tool_use") is True
        ):
            raise ProviderError(
                "Anthropic requires single-tool tool_choice in provider settings"
            )
    else:
        if api_version is not None:
            raise ProviderError("API version is only valid for Anthropic")
        if not (
            settings.get("tool_choice") == "required"
            and settings.get("parallel_tool_calls") is False
        ):
            raise ProviderError(
                "chat providers require single-tool settings in the run manifest"
            )
    return ProviderConfig(
        provider,
        model,
        model_digest,
        _validate_base_url(base_url),
        settings,
        api_key,
        prompt_hash,
        api_version,
    )


def _tool_maps(
    request: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    tools: list[dict[str, Any]] = []
    reverse: dict[str, str] = {}
    raw_tools = request.get("tool_schemas")
    if not isinstance(raw_tools, Sequence) or isinstance(raw_tools, (str, bytes)):
        raise ProviderError("adapter request tool schemas are invalid")
    for raw in raw_tools:
        if not isinstance(raw, Mapping):
            raise ProviderError("adapter request tool schema is invalid")
        name = raw.get("tool_name")
        arguments = raw.get("arguments")
        if not isinstance(name, str) or not isinstance(arguments, Mapping):
            raise ProviderError("adapter request tool schema is incomplete")
        provider_name = _provider_name(name)
        if provider_name in reverse:
            raise ProviderError("provider tool name collision")
        reverse[provider_name] = name
        tools.append(
            {
                "name": provider_name,
                "description": f"EDLB {name} tool.",
                "parameters": dict(arguments),
            }
        )
    return tools, reverse


def _safe_context(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: item
        for key, item in value.items()
        if key not in {"model_metadata", "observation_token"}
    }


def _current_context(request: Mapping[str, Any]) -> str:
    return json.dumps(
        {
            "role": request.get("role"),
            "occurred_at": request.get("occurred_at"),
            "checkpoint": request.get("checkpoint"),
            "alerts": request.get("alerts"),
            "unread_team_messages": request.get("unread_team_messages"),
            "budget": request.get("budget"),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _openai_messages(request: Mapping[str, Any]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}]
    history = request.get("messages")
    if not isinstance(history, Sequence) or isinstance(history, (str, bytes)):
        raise ProviderError("adapter request messages are invalid")
    for raw in history:
        if not isinstance(raw, Mapping):
            raise ProviderError("adapter request message is invalid")
        kind = raw.get("kind")
        if kind == "tool_call":
            messages.append(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": str(raw["message_id"]),
                            "type": "function",
                            "function": {
                                "name": _provider_name(str(raw["tool_name"])),
                                "arguments": json.dumps(
                                    raw.get("arguments") or {},
                                    ensure_ascii=False,
                                    sort_keys=True,
                                    separators=(",", ":"),
                                ),
                            },
                        }
                    ],
                }
            )
        elif kind == "tool_result":
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": str(raw["call_id"]),
                    "content": json.dumps(
                        {
                            "ok": raw.get("ok"),
                            "result": raw.get("result"),
                            "error": raw.get("error"),
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                }
            )
        else:
            messages.append(
                {
                    "role": "user",
                    "content": json.dumps(
                        _safe_context(raw),
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                }
            )
    messages.append({"role": "user", "content": _current_context(request)})
    return messages


def _anthropic_messages(request: Mapping[str, Any]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    history = request.get("messages")
    if not isinstance(history, Sequence) or isinstance(history, (str, bytes)):
        raise ProviderError("adapter request messages are invalid")
    for raw in history:
        if not isinstance(raw, Mapping):
            raise ProviderError("adapter request message is invalid")
        kind = raw.get("kind")
        if kind == "tool_call":
            messages.append(
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": str(raw["message_id"]),
                            "name": _provider_name(str(raw["tool_name"])),
                            "input": dict(raw.get("arguments") or {}),
                        }
                    ],
                }
            )
        elif kind == "tool_result":
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": str(raw["call_id"]),
                            "content": json.dumps(
                                raw.get("result")
                                if raw.get("ok")
                                else raw.get("error"),
                                ensure_ascii=False,
                                sort_keys=True,
                                separators=(",", ":"),
                            ),
                            "is_error": not bool(raw.get("ok")),
                        }
                    ],
                }
            )
        else:
            messages.append(
                {
                    "role": "user",
                    "content": json.dumps(
                        _safe_context(raw),
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                }
            )
    messages.append({"role": "user", "content": _current_context(request)})
    return messages


def _request_body(
    config: ProviderConfig,
    request: Mapping[str, Any],
    tools: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    if config.provider == "anthropic-messages":
        body: dict[str, Any] = {
            "model": config.model,
            "system": SYSTEM_PROMPT,
            "messages": _anthropic_messages(request),
            "tools": [
                {
                    "name": tool["name"],
                    "description": tool["description"],
                    "input_schema": tool["parameters"],
                }
                for tool in tools
            ],
        }
    else:
        body = {
            "model": config.model,
            "messages": _openai_messages(request),
            "tools": [
                {
                    "type": "function",
                    "function": dict(tool),
                }
                for tool in tools
            ],
        }
    body.update(config.settings)
    return body


def _post(config: ProviderConfig, body: Mapping[str, Any]) -> dict[str, Any]:
    headers = {"Content-Type": "application/json"}
    if config.provider == "anthropic-messages":
        if not config.api_version:
            raise ProviderError("Anthropic requires an explicit API version")
        headers.update(
            {"x-api-key": config.api_key, "anthropic-version": config.api_version}
        )
    else:
        headers["Authorization"] = f"Bearer {config.api_key}"
    request = Request(
        _endpoint(config),
        data=json.dumps(body, ensure_ascii=False, allow_nan=False).encode(),
        headers=headers,
        method="POST",
    )
    try:
        response = urlopen(request)
        with response:
            value = json.loads(response.read())
    except HTTPError as exc:
        status = exc.code
        exc.close()
        raise ProviderError(f"provider request failed with HTTP {status}") from exc
    except URLError as exc:
        raise ProviderError("provider request failed") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProviderError("provider returned invalid JSON") from exc
    if not isinstance(value, Mapping):
        raise ProviderError("provider response must be a JSON object")
    return dict(value)


def _usage(provider: str, response: Mapping[str, Any]) -> dict[str, int] | None:
    raw = response.get("usage")
    if not isinstance(raw, Mapping):
        return None
    input_tokens = raw.get(
        "input_tokens" if provider == "anthropic-messages" else "prompt_tokens"
    )
    output_tokens = raw.get(
        "output_tokens" if provider == "anthropic-messages" else "completion_tokens"
    )
    if not (
        isinstance(input_tokens, int)
        and not isinstance(input_tokens, bool)
        and input_tokens >= 0
        and isinstance(output_tokens, int)
        and not isinstance(output_tokens, bool)
        and output_tokens >= 0
    ):
        return None
    if provider == "anthropic-messages":
        cached = ("cache_creation_input_tokens", "cache_read_input_tokens")
        for key in cached:
            value = raw.get(key, 0)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                return None
            input_tokens += value
    return {"input": input_tokens, "output": output_tokens}


def _tool_calls(
    config: ProviderConfig,
    response: Mapping[str, Any],
    reverse: Mapping[str, str],
) -> list[tuple[str, str, dict[str, Any]]]:
    if config.provider == "anthropic-messages":
        if response.get("stop_reason") != "tool_use":
            raise ProviderError("Anthropic response did not complete with tool use")
        content = response.get("content")
        if not isinstance(content, Sequence) or isinstance(content, (str, bytes)):
            raise ProviderError("Anthropic response content is invalid")
        calls = [
            item
            for item in content
            if isinstance(item, Mapping) and item.get("type") == "tool_use"
        ]
        if not calls:
            raise ProviderError("Anthropic response did not contain a tool call")
        parsed_calls = [
            (raw.get("id"), raw.get("name"), raw.get("input")) for raw in calls
        ]
    else:
        choices = response.get("choices")
        if (
            not isinstance(choices, Sequence)
            or isinstance(choices, (str, bytes))
            or not choices
            or not isinstance(choices[0], Mapping)
            or not isinstance(choices[0].get("message"), Mapping)
        ):
            raise ProviderError("chat completion response is invalid")
        choice = choices[0]
        if choice.get("finish_reason") != "tool_calls":
            raise ProviderError("chat response did not complete with tool calls")
        raw_calls = choice["message"].get("tool_calls")
        if not raw_calls:
            raise ProviderError("chat response did not contain a tool call")
        if not isinstance(raw_calls, Sequence) or isinstance(raw_calls, (str, bytes)):
            raise ProviderError("provider returned invalid tool calls")
        parsed_calls = []
        for raw in raw_calls:
            if not isinstance(raw, Mapping) or not isinstance(
                raw.get("function"), Mapping
            ):
                raise ProviderError("provider returned invalid tool calls")
            function = raw["function"]
            parsed_calls.append(
                (raw.get("id"), function.get("name"), function.get("arguments"))
            )
    result: list[tuple[str, str, dict[str, Any]]] = []
    for call_id, name, raw_arguments in parsed_calls:
        if not isinstance(call_id, str) or not isinstance(name, str):
            raise ProviderError("provider tool call identity is invalid")
        if name not in reverse:
            raise ProviderError("provider returned an unknown tool")
        if config.provider == "anthropic-messages":
            arguments = raw_arguments
        else:
            if not isinstance(raw_arguments, (str, bytes, bytearray)):
                raise ProviderError("provider tool arguments are invalid JSON")
            try:
                arguments = json.loads(raw_arguments)
            except (TypeError, json.JSONDecodeError) as exc:
                raise ProviderError("provider tool arguments are invalid JSON") from exc
        if not isinstance(arguments, Mapping):
            raise ProviderError("provider tool arguments must be an object")
        result.append((call_id, reverse[name], dict(arguments)))
    return result


def _metadata(
    config: ProviderConfig, response: Mapping[str, Any], latency_ms: int
) -> dict[str, Any]:
    settings: dict[str, Any] = {
        "base_url": config.base_url,
        **dict(config.settings),
    }
    if config.api_version is not None:
        settings["api_version"] = config.api_version
    result: dict[str, Any] = {
        "provider": config.provider,
        "model_id": config.model,
        "model_digest": config.model_digest,
        "prompt_hash": config.prompt_hash,
        "model_settings": settings,
        "model_latency_ms": latency_ms,
    }
    response_metadata = {
        key: response[key]
        for key in (
            "id",
            "model",
            "provider",
            "service_tier",
            "system_fingerprint",
        )
        if key in response
        and (
            isinstance(response.get(key), (str, int, float, bool))
            or response.get(key) is None
        )
    }
    if isinstance(response.get("openrouter_metadata"), Mapping):
        response_metadata["openrouter_metadata"] = dict(response["openrouter_metadata"])
    if response_metadata:
        result["response_metadata"] = response_metadata
    if isinstance(response.get("usage"), Mapping):
        result["provider_usage"] = dict(response["usage"])
    usage = _usage(config.provider, response)
    if usage is not None:
        result["token_usage"] = usage
    return result


def adapt(config: ProviderConfig, request: Mapping[str, Any]) -> tuple[Message, ...]:
    required = {
        "protocol_version",
        "run_id",
        "role",
        "occurred_at",
        "observation_token",
    }
    if required - request.keys() or request.get("protocol_version") != PROTOCOL_VERSION:
        raise ProviderError("adapter request is invalid")
    if _config_from_request(request, config.api_key) != config:
        raise ProviderError("adapter configuration does not match the run manifest")
    tools, reverse = _tool_maps(request)
    body = _request_body(config, request, tools)
    started = time.monotonic()
    response = _post(config, body)
    latency_ms = int((time.monotonic() - started) * 1000)
    metadata = _metadata(config, response, latency_ms)
    history = request.get("messages")
    sequence = (
        max(
            (
                int(item["sequence"])
                for item in history
                if isinstance(item, Mapping)
                and isinstance(item.get("sequence"), int)
                and not isinstance(item.get("sequence"), bool)
            ),
            default=-1,
        )
        + 1
        if isinstance(history, Sequence) and not isinstance(history, (str, bytes))
        else 0
    )
    common: dict[str, Any] = {
        "protocol_version": PROTOCOL_VERSION,
        "run_id": str(request["run_id"]),
        "occurred_at": str(request["occurred_at"]),
        "role": str(request["role"]),
        "observation_token": str(request["observation_token"]),
    }
    calls = _tool_calls(config, response, reverse)
    if len(calls) != 1:
        raise ProviderError("provider returned multiple tool calls")
    result: list[Message] = []
    for index, (provider_call_id, tool_name, arguments) in enumerate(calls):
        digest = hashlib.sha256(
            f"{request['observation_token']}:{provider_call_id}:{tool_name}".encode()
        ).hexdigest()
        value = {
            **common,
            "sequence": sequence + index,
            "message_id": f"model-call-{digest[:24]}",
            "kind": "tool_call",
            "tool_name": tool_name,
            "arguments": arguments,
        }
        if index == 0:
            value["model_metadata"] = metadata
        if tool_name.rsplit(".", 1)[-1] in WRITE_ACTIONS:
            value["idempotency_key"] = f"model-write-{digest[:40]}"
        result.append(Message.from_dict(value))
    return tuple(result)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="edlb-provider-adapter")
    parser.add_argument("--api-key-env", required=True)
    return parser


def _api_key(args: argparse.Namespace) -> str:
    api_key = os.environ.get(args.api_key_env)
    if not api_key:
        raise ProviderError(f"API key environment variable {args.api_key_env} is unset")
    return api_key


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        value = json.loads(sys.stdin.readline())
        if not isinstance(value, Mapping):
            raise ProviderError("adapter request must be a JSON object")
        config = _config_from_request(value, _api_key(args))
        for message in adapt(config, value):
            write_message(sys.stdout, message)
        return 0
    except (ProviderError, json.JSONDecodeError) as exc:
        print(f"provider adapter failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
