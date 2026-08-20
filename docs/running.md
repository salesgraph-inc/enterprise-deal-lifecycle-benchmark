# Running EDLB

## Setup

EDLB requires Python 3.14 or newer and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --group dev
uv run edlb validate benchmarks/v1
uv run edlb validate benchmarks/v1/output
```

The generated packs live under `benchmarks/v1/output/public/`. Authoring inputs,
source metadata, shared documents, and validation results live under
`benchmarks/v1/authoring/`.

## CLI

| Command | Purpose |
| --- | --- |
| `edlb validate` | Validate a public output or complete benchmark root |
| `edlb generate` | Generate a benchmark pack |
| `edlb run` | Run an open-team agent, fixed harness, or diagnostic baseline |
| `edlb replay` | Rebuild a run from a recorded trace |
| `edlb grade` | Grade a run against a rubric and optional oracle |
| `edlb report` | Render a scorecard report |
| `edlb podman` | Build an isolated evaluator command |

Use `uv run edlb COMMAND --help` for the full argument list.

Run a checked diagnostic baseline by replacing `PATH_TO_WORLD` with one public
world directory:

```bash
uv run edlb run PATH_TO_WORLD \
  --baseline scripted_oracle \
  --output runs/scripted-oracle
```

The scripted oracle is a diagnostic, not a leaderboard submission.

Regeneration overwrites generated output. Write to a temporary root first. Use
`--force` only when replacement is intentional:

```bash
uv run edlb generate --root /tmp/edlb-v1
uv run edlb generate --root benchmarks/v1 --force
```

## External agents

`open_team` runs an external agent command. `fixed_harness` uses one model entry
for all four roles and can call the built-in standard-library adapter for OpenAI
Chat Completions, Anthropic Messages, or compatible Chat Completions endpoints.

External runs require two resolved JSON records:

- The agent manifest pins model IDs and digests, prompt hashes, provider request
  settings, accepted response identities, and any provider-default digest.
- The environment manifest pins runtime version, immutable package or image
  digest, Git revision, and effective executor-policy digest.

Checked examples are available in
[`benchmarks/v1/baselines/initial-2026-08-18/`](../benchmarks/v1/baselines/initial-2026-08-18/).
Provider names are `openai-chat`, `anthropic-messages`, and
`openai-compatible`. Credentials come only from the named environment variable,
such as `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, or `OPENROUTER_API_KEY`.

```bash
uv run edlb run PATH_TO_WORLD \
  --track fixed_harness \
  --agent-manifest agent-manifest.json \
  --environment-manifest environment-manifest.json \
  --adapter-command 'edlb-provider-adapter --api-key-env OPENAI_API_KEY' \
  --output runs/model
```

The adapter rejects unpinned response models, providers, malformed usage,
truncation, refusals, missing tool calls, and multiple tool calls. Credentials
are excluded from requests, traces, manifests, and errors.

EDLB sets no implicit model or total execution budget. Optional controls cover
tool calls and turns per checkpoint, response timeout, and retries. Omitted
nullable controls are unlimited, and retries default to zero. Security,
authorization, temporal, and business rules always remain active.

## Replay, grading, and reports

Replay requires the same world and recorded trace:

```bash
uv run edlb replay PATH_TO_WORLD runs/model/trace.jsonl \
  --output runs/replay
```

Grade and render a report with the world's rubric and oracle:

```bash
uv run edlb grade runs/replay/result.json \
  --rubric PATH_TO_WORLD/rubric.json \
  --oracle PATH_TO_WORLD/oracle.json \
  --output runs/replay/scorecard.json
uv run edlb report runs/replay/scorecard.json \
  --output runs/replay/report.json \
  --markdown runs/replay/report.md
```

Run manifests record scenario, configuration, model, environment, prompt,
provider, seed, policy, and protocol identity. Traces preserve every action and
result. The transport rejects JSONL messages larger than 8 MiB. Compare results
only when manifests and execution policy match. Official comparisons use exactly
three trials per system and world.

## Release checks

```bash
uv run --group dev python3 -m unittest discover -s tests
uv run --group dev ruff format --check .
uv run --group dev ruff check .
uv run --group dev mypy src
uv run edlb validate benchmarks/v1/output
uv run edlb validate benchmarks/v1
python scripts/verify_source_evidence.py
uv build
```

For generated changes, also regenerate into a temporary directory and require a
byte-identical comparison of generator-owned files. Public and private
validation, reference replay, package schema checks, and `git diff --check`
must pass before release.
