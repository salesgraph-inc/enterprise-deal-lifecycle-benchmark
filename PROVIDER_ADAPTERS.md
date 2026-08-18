# Provider adapters

EDLB includes a standard-library fixed-harness adapter for native OpenAI Chat
Completions, native Anthropic Messages, and OpenAI-compatible Chat Completions
endpoints such as OpenRouter. The adapter does not use provider SDKs.

The resolved agent manifest is authoritative. For the selected fixed-harness
model, `provider_settings` must contain `provider`, `base_url`, and the complete
`request` settings object. Anthropic also requires `api_version`. The adapter
rejects a prompt hash other than
`sha256:ab1b4a6cc9b45f7687015c227b014fd503f8eab0c384691231c2383209f209f6`.

OpenAI model entry:

~~~json
{
  "model_id": "REPLACE_WITH_MODEL_ID",
  "model_digest": "sha256:REPLACE_WITH_64_HEX_CHARACTERS",
  "prompt_hash": "sha256:ab1b4a6cc9b45f7687015c227b014fd503f8eab0c384691231c2383209f209f6",
  "provider_settings": {
    "provider": "openai-chat",
    "base_url": "https://api.openai.com/v1",
    "request": {
      "tool_choice": "required",
      "parallel_tool_calls": false
    }
  },
  "provider_defaults": true,
  "provider_defaults_digest": "sha256:REPLACE_WITH_64_HEX_CHARACTERS"
}
~~~

OpenRouter model entry:

~~~json
{
  "model_id": "REPLACE_WITH_OPENROUTER_MODEL_ID",
  "model_digest": "sha256:REPLACE_WITH_64_HEX_CHARACTERS",
  "prompt_hash": "sha256:ab1b4a6cc9b45f7687015c227b014fd503f8eab0c384691231c2383209f209f6",
  "provider_settings": {
    "provider": "openai-compatible",
    "base_url": "https://openrouter.ai/api/v1",
    "request": {
      "tool_choice": "required",
      "parallel_tool_calls": false
    }
  },
  "provider_defaults": true,
  "provider_defaults_digest": "sha256:REPLACE_WITH_64_HEX_CHARACTERS"
}
~~~

Anthropic model entry:

~~~json
{
  "model_id": "REPLACE_WITH_MODEL_ID",
  "model_digest": "sha256:REPLACE_WITH_64_HEX_CHARACTERS",
  "prompt_hash": "sha256:ab1b4a6cc9b45f7687015c227b014fd503f8eab0c384691231c2383209f209f6",
  "provider_settings": {
    "provider": "anthropic-messages",
    "base_url": "https://api.anthropic.com/v1",
    "api_version": "2023-06-01",
    "request": {
      "max_tokens": 4096,
      "tool_choice": {
        "type": "any",
        "disable_parallel_tool_use": true
      }
    }
  },
  "provider_defaults": true,
  "provider_defaults_digest": "sha256:REPLACE_WITH_64_HEX_CHARACTERS"
}
~~~

`max_tokens` is required by Anthropic. Its value is an entrant setting, not an
EDLB default. OpenAI and OpenRouter requests omit temperature, output-token,
reasoning, request-timeout, and retry settings unless the resolved manifest
declares them. The harness requires one tool call per activation and rejects
truncation, refusal, provider errors, missing tool calls, and multiple tool
calls.

Use one model entry for all four role keys in the fixed-harness agent manifest.
Then run:

~~~bash
export OPENAI_API_KEY='replace locally'
edlb run world-0b23ea54929cb9af01c5 \
  --track fixed_harness \
  --agent-manifest agent-manifest.json \
  --environment-manifest environment-manifest.json \
  --adapter-command 'edlb-provider-adapter --api-key-env OPENAI_API_KEY' \
  --output runs/openai-initial
~~~

Use `ANTHROPIC_API_KEY` or `OPENROUTER_API_KEY` in the same command for those
providers. Credential values are read only from the named environment variable.
They are not included in adapter requests, messages, traces, run manifests, or
errors.

Raw provider usage, routing metadata, and any provider-reported cost are
preserved. `cost_minor_units` remains unavailable because EDLB does not yet
define a currency and unit contract, and direct APIs may not return per-request
price.

The OpenAI adapter uses Chat Completions. It does not claim Responses API
support because a stateless fixed-harness activation does not retain opaque
Responses reasoning items or response identifiers.

API mappings follow the official
[OpenAI function-calling contract](https://developers.openai.com/api/docs/guides/function-calling),
[Anthropic tool-use contract](https://platform.claude.com/docs/en/agents-and-tools/tool-use/how-tool-use-works),
and [OpenRouter tool-calling contract](https://openrouter.ai/docs/guides/features/tool-calling).
