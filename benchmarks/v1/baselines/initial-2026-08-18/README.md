# Initial model trials

This directory contains two public, diagnostic fixed-harness trials on one
public manufacturing world:

- world: `world-0b23ea54929cb9af01c5`
- benchmark version: `v1.0.0`
- seed: `0`
- track: `fixed_harness`
- checkpoints: `8`
- judge status: pending for `assertion-7281780a6e76e8598975`
- execution index status: provisional until the pending judge assertion is resolved

These are not official leaderboard results. Official EDLB comparisons require
the complete scenario set and three trials per world. The run traces and
scorecards are published for provider compatibility, harness debugging, and
reproduction. The generated world is template-based and has not been
expert-reviewed.

## Results

| Model | Observed route | Execution index | Strict cycle pass | Terminal outcome | Calls | Tool errors | Invalid actions | Input tokens | Output tokens | Latency | OpenRouter cost |
| --- | --- | ---: | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `openai/gpt-5.6-sol` | OpenAI, `openai/gpt-5.6-sol-20260709` | 12.50 | no | `no_decision` | 54 | 0 | 0 | 1,461,511 | 9,727 | 424,525 ms | $4.130574 |
| `anthropic/claude-opus-5` | Anthropic, `anthropic/claude-opus-5-20260723` | 12.50 | no | `no_decision` | 313 | 4 | 4 | 43,487,074 | 173,164 | 6,109,150 ms | $221.764470 |

The execution index is provisional because one LLM judge assertion is pending.
Both runs scored `1.0` only for workflow compliance. The remaining category
scores were `0.0`; no critical violation was recorded. The four Opus invalid
actions were two reads of nonexistent CRM records and two unauthorized sends
with empty recipients. They were model actions, not provider or harness
failures; the raw result records `errors: []`.

The costs above are the sum of OpenRouter provider usage in USD. EDLB
`cost_minor_units` is unavailable for these provider-backed runs and remains
`null` in the raw result files. The temporary provider keys used a $500 credit
limit and one-day expiry as account safeguards. Those safeguards are not
benchmark or model limits.

## Files

The two model directories each contain the resolved agent manifest, provider
defaults source, exact OpenRouter model catalog entry, run manifest, result,
scorecard, JSON and Markdown reports, and lossless JSONL trace. Shared
`environment.json`, `executor-policy.json`, and `catalog-source.json` bind the
trials to the reviewed runtime, host policy, and provider catalog snapshot.

The public artifact omits `run.sqlite`, `state.json`, `snapshots.jsonl`, and
`state-diffs.jsonl`. Temporary paths in the copied result files were replaced
with local artifact names. No credentials are included.

## Reproduction

From the repository root, replay either published trace against the public
world, then grade the replayed database with the public rubric and oracle:

```bash
uv run --group dev edlb replay \
  benchmarks/v1/output/public/train/world-0b23ea54929cb9af01c5 \
  benchmarks/v1/baselines/initial-2026-08-18/gpt-5.6-sol/trace.jsonl \
  --output runs/replay-gpt-5.6-sol

uv run --group dev edlb grade runs/replay-gpt-5.6-sol \
  --rubric benchmarks/v1/output/public/train/world-0b23ea54929cb9af01c5/rubric.json \
  --oracle benchmarks/v1/output/public/train/world-0b23ea54929cb9af01c5/oracle.json \
  --output runs/replay-gpt-5.6-sol/scorecard.json

uv run --group dev edlb report runs/replay-gpt-5.6-sol/scorecard.json \
  --output runs/replay-gpt-5.6-sol/report.json \
  --markdown runs/replay-gpt-5.6-sol/report.md
```

The Opus trace uses the same commands with its model directory and a fresh
output directory. Replay reproduced each original state hash and execution
index. It is a diagnostic replay with `configuration_resolved: false`, so its
generated manifest and score hashes differ from the original run files.
Replay does not make a leaderboard claim.

The benchmark controls in `executor-policy.json` leave model token, turn,
wall-time, response-timeout, reasoning, and temperature settings unset. The
recorded `retries: 0` value is the runner retry policy, not a model budget.

## Provider provenance

`catalog-source.json` records the OpenRouter models endpoint, retrieval time,
and full catalog SHA-256. Each model directory contains the exact catalog
entry used for its digest. The entry digest is SHA-256 over UTF-8 JSON with
recursively sorted object keys, compact separators, and unescaped Unicode.
