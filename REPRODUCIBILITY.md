# Reproducibility Guide

## Inputs

Use Python 3.14 or newer and uv. The contract and generated data version is
v1.0.0. All train, dev, and blind worlds are under
benchmarks/v1/output/public/ and are released with their answer material.

The full pack contains 72 public worlds, 72 artifacts per world, 5,184
artifacts, 716 checkpoint windows, 180 shared seller documents, 36
counterfactual pairs, and 4 rich renderings. The public manifest and validation
records are committed with the generated pack.

Current generated prose is templated and not expert-reviewed. Expert
authorship and review remain pending release gates.

## Validation

Run from the repository root:

~~~bash
uv run --group dev python3 -m unittest discover -s tests
uv run --group dev python3 -m unittest tests.test_schema
uv run --group dev ruff check .
python3 -m compileall -q src tests
uv run --group dev edlb validate benchmarks/v1/output
uv run --group dev edlb validate benchmarks/v1
~~~

The tests cover generator invariants, runtime behavior, causal checks, grading,
runner behavior, CLI behavior, Draft 2020-12 schemas, RFC 3339 date-time
formats, generated normative records, rubric assertions, and reference
protocol messages. The dataset validator covers public and private bundle
identity, synthetic provenance, artifact paths, event identity and
availability, public oracle and hidden-event files, and private-pack access
when explicitly enabled.

Focused tests cover canary scanning, quota enforcement, exact-byte SHA-256
manifest hashing, HMAC result signing, immutable network-disabled Podman
isolation, and RevOps-only CRM merge. End-to-end blind evaluator security
evidence and container endpoint allowlisting remain pending.

The final machine gate passes the functional test suite. Automated
privacy fixtures reject live domains, non-reserved phone numbers, configured
copied phrases and entities, and duplicate person identities. These are
configured-list checks, not a global real-person or copy scan.

Lossless `team_message` and `yield` trace paths, snapshot and state-diff
exports, replay payload and hash validation, state and score hashes, and
aggregate dataset validation are implemented.

## Runs and replay

Record the run manifest, scenario hash, protocol and tool schema versions,
runtime and grader versions, stakeholder and judge configuration, model digest,
prompt hash, image digest, random seeds, trace, state hash, and resource usage.
Use the CLI run command with either the fixed-harness or open-team track, then
grade the run and write a report. Replay only with the matching world,
manifest inputs, and trace.

EDLB sets no model or scored benchmark budget by default. It injects no token caps or
temperature, top-p, reasoning-effort, or cost settings. Open Team launch retries and
Fixed Harness activation retries default to zero. The implemented scored controls are
per-checkpoint tool calls, turns, response timeout, and track-scoped retries; nullable
controls use null for unlimited. External systems declare model IDs, digests, prompt
hashes, and provider settings in a resolved pre-run agent manifest. EDLB records that
declaration and binds it to configuration and manifest hashes. Direct `open_world` setup
defaults to unresolved configuration. External execution also requires
`--environment-manifest` with the exact runtime version, immutable image or package
digest, full Git revision, and SHA-256 executor-policy digest. That digest covers
effective inherited rlimits and other evaluator host and job policies. It records
policy provenance and creates no EDLB resource cap. Both manifests are configuration-
bound, and unresolved runs are unofficial. Comparisons require identical execution
policy and configuration. Official scoring uses exactly three trials per system and
world.

EDLB does not impose a total wall-time, CPU, memory, process, or file-descriptor cap.
The evaluator host owns those policies and must record them in the hash-bound
executor policy for comparable runs. Blind containers still use a read-only root,
no writable host mounts, disabled network, and one 64 MiB
`noexec,nosuid,nodev` temporary filesystem for storage isolation. Those controls do
not change benchmark semantics or model settings.

Business, authorization, and temporal rules, protocol trust-boundary validation,
blind submission quotas and canaries, network isolation, and declared evaluator
safety policy remain in force as semantic or security controls.

The evaluator enforces an 8 MiB per-message JSONL transport ceiling, sized
above the broker's bounded semantic envelope after worst-case JSON escaping.
Crossing it invalidates the protocol exchange. The evaluator does not truncate
or score the content.

Public train reference traces are deterministic action fixtures, not model
results. Their start message carries the resolved `REFERENCE_AGENT_MANIFEST`,
null execution limits, and a configuration hash over those fields. Replay
uses that binding and rejects a changed fixture configuration.

Replay carries the exact source environment to reproduce the source state hash.
It records `diagnostic_replay: true` and is always unofficial.

All 72 checked reference traces match their oracle and score EI 100.0 with
Strict Cycle Pass. All 24 closed-won traces ablate to `no_decision`.

No official three-trial model run, 12-world resource characterization pilot,
calibration record, or leaderboard result exists yet. The fixed-harness and
open-team leaderboard JSON files therefore contain empty result arrays.

## Data boundary

All v1 worlds, oracle state, private assertions, hidden events, and reference
traces are public synthetic data. Future packs marked
`release_visibility=private` must remain outside public artifacts until
retirement. Do not treat preliminary namespace checks as trademark or legal
clearance.
