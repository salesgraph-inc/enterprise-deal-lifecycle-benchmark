from __future__ import annotations

import json
import threading
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from edlb.protocol import Message
from edlb.provider_adapter import (
    PROMPT_HASH,
    ProviderConfig,
    ProviderError,
    adapt,
)

DIGEST = "sha256:" + "a" * 64


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length))
        self.server.requests.append((self.path, dict(self.headers), body))
        self.send_response(self.server.status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(self.server.response).encode())

    def log_message(self, format: str, *args: Any) -> None:
        pass


class _Server(ThreadingHTTPServer):
    requests: list[tuple[str, dict[str, str], dict[str, Any]]]
    response: dict[str, Any]
    status: int


@contextmanager
def _server(response: dict[str, Any], status: int = 200):
    server = _Server(("127.0.0.1", 0), _Handler)
    server.requests = []
    server.response = response
    server.status = status
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def _request(config: ProviderConfig, tool_name: str = "crm.search") -> dict[str, Any]:
    provider_settings: dict[str, Any] = {
        "provider": config.provider,
        "base_url": config.base_url,
        "request": dict(config.settings),
    }
    if config.api_version is not None:
        provider_settings["api_version"] = config.api_version
    return {
        "protocol_version": "v1.0.0",
        "kind": "adapter_request",
        "run_id": "provider-test",
        "role": "account_executive",
        "occurred_at": "2025-01-01T00:00:00Z",
        "observation_token": "a" * 32,
        "checkpoint": {"checkpoint_id": "checkpoint-1"},
        "model_config": {
            "model_id": config.model,
            "model_digest": config.model_digest,
            "prompt_hash": config.prompt_hash,
            "provider_settings": provider_settings,
            "provider_defaults": True,
            "provider_defaults_digest": "sha256:" + "b" * 64,
        },
        "tool_schemas": [
            {
                "tool": tool_name.split(".", 1)[0],
                "actions": [tool_name.split(".", 1)[1]],
                "tool_name": tool_name,
                "write": tool_name == "run.complete_checkpoint",
                "arguments": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "additionalProperties": False,
                },
            }
        ],
        "messages": [
            {
                "kind": "observation",
                "role": "account_executive",
                "checkpoint": {"checkpoint_id": "checkpoint-0"},
                "alerts": [{"event_id": "event-1"}],
                "unread_team_messages": [],
                "budget": {
                    "tool_calls_per_checkpoint": None,
                    "turns_per_checkpoint": None,
                },
            },
            {
                "protocol_version": "v1.0.0",
                "run_id": "provider-test",
                "sequence": 2,
                "message_id": "prior-call",
                "occurred_at": "2025-01-01T00:00:00Z",
                "kind": "tool_call",
                "role": "account_executive",
                "tool_name": "crm.search",
                "arguments": {"query": "account"},
                "observation_token": "b" * 32,
                "model_metadata": {"model_id": "old-model"},
            },
            {
                "protocol_version": "v1.0.0",
                "run_id": "provider-test",
                "sequence": 3,
                "message_id": "prior-call.result",
                "occurred_at": "2025-01-01T00:00:00Z",
                "kind": "tool_result",
                "role": "account_executive",
                "call_id": "prior-call",
                "ok": True,
                "result": {"records": []},
            },
        ],
        "alerts": [],
        "unread_team_messages": [],
        "budget": {
            "tool_calls_per_checkpoint": None,
            "turns_per_checkpoint": None,
        },
    }


class ProviderAdapterTest(unittest.TestCase):
    def test_openai_compatible_translation_usage_and_key_non_disclosure(self) -> None:
        response = {
            "model": "openai/test-resolved",
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "tool_calls": [
                            {
                                "id": "call:unsafe/provider-id",
                                "type": "function",
                                "function": {
                                    "name": "crm__search",
                                    "arguments": '{"query":"renewal"}',
                                },
                            }
                        ]
                    },
                }
            ],
            "usage": {"prompt_tokens": 11, "completion_tokens": 7},
        }
        with _server(response) as server:
            config = ProviderConfig(
                provider="openai-compatible",
                model="openai/test",
                model_digest=DIGEST,
                base_url=f"http://127.0.0.1:{server.server_port}/v1",
                settings={
                    "reasoning_effort": "high",
                    "tool_choice": "required",
                    "parallel_tool_calls": False,
                },
                api_key="top-secret-key",
            )
            message = adapt(config, _request(config))[0]
        path, headers, body = server.requests[0]
        self.assertEqual(path, "/v1/chat/completions")
        self.assertEqual(headers["Authorization"], "Bearer top-secret-key")
        self.assertEqual(body["reasoning_effort"], "high")
        self.assertNotIn("temperature", body)
        self.assertNotIn("max_tokens", body)
        self.assertNotIn("timeout", body)
        self.assertEqual(body["tools"][0]["function"]["name"], "crm__search")
        self.assertEqual(body["messages"][2]["tool_calls"][0]["id"], "prior-call")
        self.assertEqual(body["messages"][3]["tool_call_id"], "prior-call")
        self.assertNotIn("top-secret-key", json.dumps(body))
        value = message.to_dict()
        self.assertEqual(value["tool_name"], "crm.search")
        self.assertRegex(value["message_id"], r"^model-call-[0-9a-f]{24}$")
        self.assertEqual(
            value["model_metadata"]["token_usage"], {"input": 11, "output": 7}
        )
        self.assertEqual(value["model_metadata"]["prompt_hash"], PROMPT_HASH)
        self.assertNotIn("top-secret-key", json.dumps(value))
        self.assertEqual(Message.from_dict(value), message)

    def test_direct_openai_chat_uses_the_same_native_chat_contract(self) -> None:
        response = {
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "function": {
                                    "name": "crm__search",
                                    "arguments": "{}",
                                },
                            }
                        ]
                    },
                }
            ]
        }
        with _server(response) as server:
            config = ProviderConfig(
                provider="openai-chat",
                model="gpt-test",
                model_digest=DIGEST,
                base_url=f"http://127.0.0.1:{server.server_port}/v1",
                settings={"tool_choice": "required", "parallel_tool_calls": False},
                api_key="openai-secret",
            )
            message = adapt(config, _request(config))[0]
        self.assertEqual(message.tool_name, "crm.search")
        self.assertEqual(server.requests[0][0], "/v1/chat/completions")

    def test_anthropic_translation_requires_explicit_tokens_and_writes_idempotently(
        self,
    ) -> None:
        response = {
            "model": "claude-test-resolved",
            "stop_reason": "tool_use",
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "run__complete_checkpoint",
                    "input": {
                        "checkpoint_id": "checkpoint-1",
                        "summary": "Reviewed evidence.",
                    },
                }
            ],
            "usage": {
                "input_tokens": 13,
                "cache_creation_input_tokens": 2,
                "cache_read_input_tokens": 3,
                "output_tokens": 5,
            },
        }
        with _server(response) as server:
            config = ProviderConfig(
                provider="anthropic-messages",
                model="claude-test",
                model_digest=DIGEST,
                base_url=f"http://127.0.0.1:{server.server_port}/v1",
                settings={
                    "max_tokens": 4096,
                    "tool_choice": {
                        "type": "any",
                        "disable_parallel_tool_use": True,
                    },
                },
                api_key="anthropic-secret",
                api_version="2023-06-01",
            )
            message = adapt(config, _request(config, "run.complete_checkpoint"))[0]
        path, headers, body = server.requests[0]
        self.assertEqual(path, "/v1/messages")
        self.assertEqual(headers["X-Api-Key"], "anthropic-secret")
        self.assertEqual(headers["Anthropic-Version"], "2023-06-01")
        self.assertEqual(body["max_tokens"], 4096)
        self.assertNotIn("temperature", body)
        self.assertEqual(body["tools"][0]["name"], "run__complete_checkpoint")
        self.assertEqual(body["messages"][1]["content"][0]["type"], "tool_use")
        self.assertEqual(body["messages"][2]["content"][0]["type"], "tool_result")
        self.assertEqual(message.tool_name, "run.complete_checkpoint")
        self.assertRegex(message.idempotency_key or "", r"^model-write-[0-9a-f]{40}$")
        self.assertEqual(
            message.model_metadata["token_usage"], {"input": 18, "output": 5}
        )
        self.assertNotIn("anthropic-secret", json.dumps(message.to_dict()))

    def test_provider_errors_do_not_disclose_response_body(self) -> None:
        with _server({"error": {"message": "top-secret-key"}}, status=401) as server:
            config = ProviderConfig(
                provider="openai-chat",
                model="gpt-test",
                model_digest=DIGEST,
                base_url=f"http://127.0.0.1:{server.server_port}/v1",
                settings={"tool_choice": "required", "parallel_tool_calls": False},
                api_key="top-secret-key",
            )
            with self.assertRaises(ProviderError) as raised:
                adapt(config, _request(config))
        self.assertEqual(str(raised.exception), "provider request failed with HTTP 401")
        self.assertNotIn("top-secret-key", str(raised.exception))

    def test_manifest_prompt_binding_is_enforced_before_request(self) -> None:
        config = ProviderConfig(
            provider="openai-chat",
            model="gpt-test",
            model_digest=DIGEST,
            base_url="http://127.0.0.1:1/v1",
            settings={"tool_choice": "required", "parallel_tool_calls": False},
            api_key="secret",
        )
        request = _request(config)
        request["model_config"]["prompt_hash"] = "sha256:" + "0" * 64
        with self.assertRaisesRegex(ProviderError, "prompt hash"):
            adapt(config, request)

    def test_manifest_credentials_are_rejected_before_request(self) -> None:
        config = ProviderConfig(
            provider="openai-chat",
            model="gpt-test",
            model_digest=DIGEST,
            base_url="http://127.0.0.1:1/v1",
            settings={"tool_choice": "required", "parallel_tool_calls": False},
            api_key="secret",
        )
        request = _request(config)
        request["model_config"]["provider_settings"]["authorization"] = "secret"
        with self.assertRaisesRegex(ProviderError, "only allowed through"):
            adapt(config, request)

    def test_multiple_tool_calls_fail_activation(self) -> None:
        call = {
            "id": "call-1",
            "function": {"name": "crm__search", "arguments": "{}"},
        }
        response = {
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {"tool_calls": [call, {**call, "id": "call-2"}]},
                }
            ]
        }
        with _server(response) as server:
            config = ProviderConfig(
                provider="openai-compatible",
                model="test/model",
                model_digest=DIGEST,
                base_url=f"http://127.0.0.1:{server.server_port}/v1",
                settings={"tool_choice": "required", "parallel_tool_calls": False},
                api_key="secret",
            )
            with self.assertRaisesRegex(ProviderError, "multiple tool calls"):
                adapt(config, _request(config))

    def test_non_tool_finish_fails_activation(self) -> None:
        response = {
            "choices": [
                {"finish_reason": "length", "message": {"content": "I am done."}}
            ],
            "usage": {"prompt_tokens": 3, "completion_tokens": 4},
        }
        with _server(response) as server:
            config = ProviderConfig(
                provider="openai-chat",
                model="gpt-test",
                model_digest=DIGEST,
                base_url=f"http://127.0.0.1:{server.server_port}/v1",
                settings={"tool_choice": "required", "parallel_tool_calls": False},
                api_key="secret",
            )
            with self.assertRaisesRegex(ProviderError, "did not complete"):
                adapt(config, _request(config))


if __name__ == "__main__":
    unittest.main()
