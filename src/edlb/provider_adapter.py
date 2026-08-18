from __future__ import annotations

import argparse
import hashlib
import json
import math
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
        "auth",
        "authentication",
        "bearer_token",
        "client_secret",
        "cookie",
        "credential",
        "credentials",
        "headers",
        "password",
        "private_key",
        "refresh_token",
        "set_cookie",
        "session_cookie",
        "secret",
        "token",
        "x_api_key",
    }
)
SECRET_KEY_NAMES = frozenset(key.replace("_", "") for key in SECRET_KEYS)


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
    expected_models: tuple[str, ...] = ()
    expected_providers: tuple[str, ...] = ()
    expected_routed_models: tuple[str, ...] = ()
    router_metadata: bool = False


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


def _reject_credentials(value: Any, api_key: str | None = None) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = str(key).lower().replace("-", "_")
            if (
                normalized in SECRET_KEYS
                or normalized.replace("_", "") in SECRET_KEY_NAMES
            ):
                raise ProviderError(
                    "credentials are only allowed through the environment"
                )
            _reject_credentials(item, api_key)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for item in value:
            _reject_credentials(item, api_key)
    elif api_key and isinstance(value, str) and api_key in value:
        raise ProviderError("provider response contains credential material")


def _safe_json(value: Any, api_key: str) -> Any:
    _reject_credentials(value, api_key)
    try:
        return json.loads(json.dumps(value, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ProviderError("provider response contains invalid JSON values") from exc


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
    _reject_credentials(provider_settings, api_key)
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
    expected_models = provider_settings.get("expected_response_models")
    expected_providers = provider_settings.get("expected_response_providers", ())
    expected_routed_models = provider_settings.get("expected_routed_models", ())
    router_metadata = provider_settings.get("router_metadata", False)
    if provider not in PROVIDERS or not isinstance(base_url, str):
        raise ProviderError(
            "provider and base URL must be explicit in the run manifest"
        )
    if not isinstance(request_settings, Mapping):
        raise ProviderError(
            "provider request settings must be explicit in the run manifest"
        )
    if (
        not isinstance(expected_models, Sequence)
        or isinstance(expected_models, (str, bytes))
        or not expected_models
        or not all(isinstance(value, str) and value for value in expected_models)
    ):
        raise ProviderError("expected response models must be explicit")
    if (
        not isinstance(expected_providers, Sequence)
        or isinstance(expected_providers, (str, bytes))
        or not all(isinstance(value, str) and value for value in expected_providers)
    ):
        raise ProviderError("expected response providers are invalid")
    if (
        not isinstance(expected_routed_models, Sequence)
        or isinstance(expected_routed_models, (str, bytes))
        or not all(isinstance(value, str) and value for value in expected_routed_models)
    ):
        raise ProviderError("expected routed models are invalid")
    if not isinstance(router_metadata, bool):
        raise ProviderError("router_metadata must be boolean")
    if provider != "openai-compatible" and (
        expected_providers or expected_routed_models or router_metadata
    ):
        raise ProviderError("routing identity is only valid for compatible providers")
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
        if provider == "openai-compatible":
            if not expected_providers:
                raise ProviderError(
                    "compatible providers require expected response providers"
                )
            if router_metadata and not expected_routed_models:
                raise ProviderError("router metadata requires expected routed models")
    return ProviderConfig(
        provider,
        model,
        model_digest,
        _validate_base_url(base_url),
        settings,
        api_key,
        prompt_hash,
        api_version,
        tuple(expected_models),
        tuple(expected_providers),
        tuple(expected_routed_models),
        router_metadata,
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


def _continuation(
    raw: Mapping[str, Any], config: ProviderConfig, kind: str
) -> dict[str, Any] | None:
    metadata = raw.get("model_metadata")
    if not isinstance(metadata, Mapping):
        return None
    value = metadata.get("provider_continuation")
    if value is None:
        return None
    if any(
        metadata.get(key) != expected
        for key, expected in (
            ("provider", config.provider),
            ("model_id", config.model),
            ("model_digest", config.model_digest),
            ("prompt_hash", config.prompt_hash),
        )
    ):
        raise ProviderError("provider continuation does not match the model manifest")
    safe = _safe_json(value, config.api_key)
    if not isinstance(safe, Mapping) or safe.get("kind") != kind:
        raise ProviderError("provider continuation is invalid")
    return dict(safe)


def _chat_assistant(
    raw: Mapping[str, Any], config: ProviderConfig
) -> tuple[dict[str, Any], str]:
    continuation = _continuation(raw, config, "chat")
    if continuation is None:
        call_id = str(raw["message_id"])
        return (
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": call_id,
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
            },
            call_id,
        )
    assistant = continuation.get("assistant")
    if not isinstance(assistant, Mapping):
        raise ProviderError("chat provider continuation is invalid")
    calls = assistant.get("tool_calls")
    if (
        not isinstance(calls, Sequence)
        or isinstance(calls, (str, bytes))
        or len(calls) != 1
        or not isinstance(calls[0], Mapping)
        or not isinstance(calls[0].get("function"), Mapping)
    ):
        raise ProviderError("chat provider continuation tool call is invalid")
    call = calls[0]
    function = call["function"]
    native_call_id = call.get("id")
    arguments = function.get("arguments")
    if (
        not isinstance(native_call_id, str)
        or function.get("name") != _provider_name(str(raw["tool_name"]))
        or not isinstance(arguments, str)
    ):
        raise ProviderError("chat provider continuation tool call is invalid")
    try:
        parsed_arguments = json.loads(arguments)
    except json.JSONDecodeError as exc:
        raise ProviderError("chat provider continuation arguments are invalid") from exc
    if parsed_arguments != dict(raw.get("arguments") or {}):
        raise ProviderError("chat provider continuation arguments changed")
    return dict(assistant), native_call_id


def _anthropic_assistant(
    raw: Mapping[str, Any], config: ProviderConfig
) -> tuple[list[Any], str]:
    continuation = _continuation(raw, config, "anthropic")
    if continuation is None:
        call_id = str(raw["message_id"])
        return (
            [
                {
                    "type": "tool_use",
                    "id": call_id,
                    "name": _provider_name(str(raw["tool_name"])),
                    "input": dict(raw.get("arguments") or {}),
                }
            ],
            call_id,
        )
    content = continuation.get("content")
    if not isinstance(content, list):
        raise ProviderError("Anthropic provider continuation is invalid")
    calls = [
        item
        for item in content
        if isinstance(item, Mapping) and item.get("type") == "tool_use"
    ]
    if len(calls) != 1:
        raise ProviderError("Anthropic provider continuation tool call is invalid")
    call = calls[0]
    native_call_id = call.get("id")
    if (
        not isinstance(native_call_id, str)
        or call.get("name") != _provider_name(str(raw["tool_name"]))
        or call.get("input") != dict(raw.get("arguments") or {})
    ):
        raise ProviderError("Anthropic provider continuation tool call is invalid")
    return content, native_call_id


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


def _openai_messages(
    config: ProviderConfig, request: Mapping[str, Any]
) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}]
    call_ids: dict[str, str] = {}
    history = request.get("messages")
    if not isinstance(history, Sequence) or isinstance(history, (str, bytes)):
        raise ProviderError("adapter request messages are invalid")
    for raw in history:
        if not isinstance(raw, Mapping):
            raise ProviderError("adapter request message is invalid")
        kind = raw.get("kind")
        if kind == "tool_call":
            assistant, call_id = _chat_assistant(raw, config)
            messages.append(assistant)
            call_ids[str(raw["message_id"])] = call_id
        elif kind == "tool_result":
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call_ids.get(
                        str(raw["call_id"]), str(raw["call_id"])
                    ),
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


def _anthropic_messages(
    config: ProviderConfig, request: Mapping[str, Any]
) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    call_ids: dict[str, str] = {}
    history = request.get("messages")
    if not isinstance(history, Sequence) or isinstance(history, (str, bytes)):
        raise ProviderError("adapter request messages are invalid")
    for raw in history:
        if not isinstance(raw, Mapping):
            raise ProviderError("adapter request message is invalid")
        kind = raw.get("kind")
        if kind == "tool_call":
            content, call_id = _anthropic_assistant(raw, config)
            messages.append(
                {
                    "role": "assistant",
                    "content": content,
                }
            )
            call_ids[str(raw["message_id"])] = call_id
        elif kind == "tool_result":
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": call_ids.get(
                                str(raw["call_id"]), str(raw["call_id"])
                            ),
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
            "messages": _anthropic_messages(config, request),
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
            "messages": _openai_messages(config, request),
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
        if config.router_metadata:
            headers["X-OpenRouter-Metadata"] = "enabled"
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


def _usage(provider: str, response: Mapping[str, Any]) -> dict[str, int]:
    raw = response.get("usage")
    if not isinstance(raw, Mapping):
        raise ProviderError("provider response usage is missing")
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
        raise ProviderError("provider response usage is invalid")
    if provider == "anthropic-messages":
        cached = ("cache_creation_input_tokens", "cache_read_input_tokens")
        for key in cached:
            value = raw.get(key, 0)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ProviderError("provider response usage is invalid")
            input_tokens += value
    return {"input": input_tokens, "output": output_tokens}


def _provider_usage(response: Mapping[str, Any]) -> dict[str, int | float]:
    raw = response.get("usage")
    if not isinstance(raw, Mapping):
        raise ProviderError("provider response usage is missing")
    result: dict[str, int | float] = {}
    for key in (
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
        "cost",
    ):
        if key not in raw:
            continue
        value = raw[key]
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(value)
            or value < 0
        ):
            raise ProviderError("provider response usage is invalid")
        result[key] = value
    return result


def _router_identity(
    config: ProviderConfig, response: Mapping[str, Any], response_model: str
) -> dict[str, Any]:
    if not config.router_metadata:
        provider = response.get("provider")
        if not isinstance(provider, str) or provider not in config.expected_providers:
            raise ProviderError("provider response identity is not allowed")
        return {"selected_provider": provider, "selected_model": response_model}
    raw = response.get("openrouter_metadata")
    if not isinstance(raw, Mapping) or raw.get("requested") != config.model:
        raise ProviderError("OpenRouter response metadata is invalid")
    endpoints = raw.get("endpoints")
    available = endpoints.get("available") if isinstance(endpoints, Mapping) else None
    if not isinstance(available, Sequence) or isinstance(available, (str, bytes)):
        raise ProviderError("OpenRouter response metadata is invalid")
    selected = [
        item
        for item in available
        if isinstance(item, Mapping) and item.get("selected") is True
    ]
    if len(selected) != 1:
        raise ProviderError("OpenRouter response metadata is invalid")
    provider = selected[0].get("provider")
    routed_model = selected[0].get("model")
    if (
        not isinstance(provider, str)
        or provider not in config.expected_providers
        or not isinstance(routed_model, str)
        or routed_model not in config.expected_routed_models
    ):
        raise ProviderError("OpenRouter routed identity is not allowed")
    result: dict[str, Any] = {
        "requested": config.model,
        "selected_provider": provider,
        "selected_model": routed_model,
    }
    strategy = raw.get("strategy")
    region = raw.get("region")
    attempt = raw.get("attempt")
    is_byok = raw.get("is_byok")
    if "strategy" in raw and not isinstance(strategy, str):
        raise ProviderError("OpenRouter response metadata is invalid")
    if region is not None and not isinstance(region, str):
        raise ProviderError("OpenRouter response metadata is invalid")
    if "attempt" in raw and (
        not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1
    ):
        raise ProviderError("OpenRouter response metadata is invalid")
    if "is_byok" in raw and not isinstance(is_byok, bool):
        raise ProviderError("OpenRouter response metadata is invalid")
    for key, value in (
        ("strategy", strategy),
        ("region", region),
        ("attempt", attempt),
        ("is_byok", is_byok),
    ):
        if key in raw:
            result[key] = value
    return result


def _response_metadata(
    config: ProviderConfig, response: Mapping[str, Any]
) -> dict[str, Any]:
    response_id = response.get("id")
    response_model = response.get("model")
    if not isinstance(response_id, str) or not response_id:
        raise ProviderError("provider response id is missing")
    if (
        not isinstance(response_model, str)
        or response_model not in config.expected_models
    ):
        raise ProviderError("provider response model is not allowed")
    result: dict[str, Any] = {"id": response_id, "model": response_model}
    for key in ("service_tier", "system_fingerprint"):
        if key not in response:
            continue
        value = response[key]
        if not isinstance(value, (str, type(None))):
            raise ProviderError("provider response metadata is invalid")
        result[key] = value
    if config.provider == "openai-compatible":
        result["routing"] = _router_identity(config, response, response_model)
    return result


def _provider_continuation(
    config: ProviderConfig, response: Mapping[str, Any]
) -> dict[str, Any]:
    if config.provider == "anthropic-messages":
        content = response.get("content")
        if not isinstance(content, list) or any(
            not isinstance(item, Mapping)
            or item.get("type")
            not in {"text", "thinking", "redacted_thinking", "tool_use"}
            for item in content
        ):
            raise ProviderError("Anthropic response contains unsupported content")
        return {"kind": "anthropic", "content": content}
    choices = response.get("choices")
    if (
        not isinstance(choices, list)
        or len(choices) != 1
        or not isinstance(choices[0], Mapping)
        or not isinstance(choices[0].get("message"), Mapping)
    ):
        raise ProviderError("chat completion response is invalid")
    assistant = choices[0]["message"]
    allowed = {
        "role",
        "content",
        "refusal",
        "reasoning",
        "reasoning_content",
        "reasoning_details",
        "tool_calls",
    }
    if set(assistant) - allowed or assistant.get("role", "assistant") != "assistant":
        raise ProviderError("chat response contains unsupported assistant content")
    return {"kind": "chat", "assistant": dict(assistant)}


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
            or len(choices) != 1
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
    config: ProviderConfig,
    response_metadata: Mapping[str, Any],
    usage: Mapping[str, int],
    provider_usage: Mapping[str, int | float],
    continuation: Mapping[str, Any],
    latency_ms: int,
) -> dict[str, Any]:
    settings: dict[str, Any] = {
        "base_url": config.base_url,
        "expected_response_models": list(config.expected_models),
        **dict(config.settings),
    }
    if config.api_version is not None:
        settings["api_version"] = config.api_version
    if config.expected_providers:
        settings["expected_response_providers"] = list(config.expected_providers)
    if config.expected_routed_models:
        settings["expected_routed_models"] = list(config.expected_routed_models)
    if config.router_metadata:
        settings["router_metadata"] = True
    result: dict[str, Any] = {
        "provider": config.provider,
        "model_id": config.model,
        "model_digest": config.model_digest,
        "prompt_hash": config.prompt_hash,
        "model_settings": settings,
        "model_latency_ms": latency_ms,
        "response_metadata": dict(response_metadata),
        "provider_usage": dict(provider_usage),
        "token_usage": dict(usage),
        "provider_continuation": dict(continuation),
    }
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
    raw_response = _post(config, body)
    latency_ms = int((time.monotonic() - started) * 1000)
    response = _safe_json(raw_response, config.api_key)
    if not isinstance(response, Mapping):
        raise ProviderError("provider response must be a JSON object")
    response_metadata = _response_metadata(config, response)
    usage = _usage(config.provider, response)
    provider_usage = _provider_usage(response)
    continuation = _provider_continuation(config, response)
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
    metadata = _metadata(
        config,
        response_metadata,
        usage,
        provider_usage,
        continuation,
        latency_ms,
    )
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
