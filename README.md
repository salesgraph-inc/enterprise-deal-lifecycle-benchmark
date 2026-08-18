# Enterprise Deal Lifecycle Benchmark

Enterprise Deal Lifecycle Benchmark, or EDLB, is a synthetic benchmark for
agent teams operating enterprise sales opportunities over six to twelve
simulated months. A world starts when a first meeting is booked and ends at a
valid terminal outcome.

## Current release state

The v1 runtime and generated data are present. The repository contains all 72
worlds publicly, 24 each in train, dev, and blind splits. Every world has 72
artifacts, for 5,184 artifacts, and the authoring pack contains 180 shared
seller documents.

Across the full pack there are 72 worlds, 5,184 artifacts, 716 checkpoint
windows, 180 shared seller documents, 36 counterfactual pairs, and 4 rich
renderings.

The current world and document records are generated from structured template
blueprints. Generated prose is templated and has not been expert-reviewed.
Expert authoring, recruitment, and review gates remain pending.

The checked-in v1 fixture is intentionally prepared for public distribution:
all 72 worlds, including oracle state, assertions, hidden events, and reference
traces, are public synthetic data under CC BY 4.0. Repository fixture
publication is distinct from declaring an official benchmark release or result
set. Official model results, human-review results, stakeholder-model selection,
and judge calibration remain separate release gates.

## Contract

The v1 contract defines six verticals:

- Manufacturing
- Construction
- Commercial insurance
- Consulting
- Legal services
- Corporate banking

It defines four seller seats, Account Executive, Domain Specialist, Sales
Manager, and RevOps. Persistent event timing, role-scoped visibility, mutable
CRM projections, communications, calendars, documents, approvals, frozen web
signals, and JSONL runner messages are implemented.

Lossless trace paths for `team_message` and `yield`, snapshot and state-diff
exports, replay payload and hash validation, state and score hashes, and
aggregate dataset validation are implemented.

The schemas in src/edlb/schemas/ are normative for v1. Fields not defined by a
schema are not part of the public contract. Dates use RFC 3339 date-time
strings. Money uses integer minor units and an ISO 4217 currency code. IDs are
stable, lowercase strings.

## Runtime and CLI

src/edlb/cli.py exposes validate, generate, run, replay, grade, report, and
podman commands. src/edlb/runner.py provides fixed-harness and open-team
execution, optional operator controls, trace capture, and replay.
src/edlb/causal.py provides event-first causal interventions and constrained
realization checks.
src/edlb/grading.py, src/edlb/statistics.py, and src/edlb/reporting.py provide
deterministic grading, reliability calculations, and reports.

Blind-evaluator support includes canary scanning, a submission quota ledger,
exact-byte SHA-256 manifest hashing, HMAC result signing, immutable
network-disabled Podman isolation, and RevOps-only CRM merge. In production,
the evaluator must derive each submission's team ID from its authenticated
identity context; it must never accept a team ID from the submission payload.
Focused tests cover these controls. End-to-end blind evaluator security
evidence and container endpoint allowlisting remain pending.

## Resource policy

EDLB has no implicit model or execution budget. By default it sets no token
caps or temperature, top-p, reasoning-effort, or cost settings, and has no
default checkpoint tool-call, turn, response-time, total wall-time,
context-history, or retrieval-result cap. Open Team launch retries and Fixed
Harness activation retries default to zero. A resolved external manifest pins
each model digest and prompt hash, declares provider settings, and states whether
unspecified settings use provider defaults. EDLB binds that declaration to
configuration and manifest hashes. Adapter-reported usage, latency, and cost are
recorded only when supplied.

External execution also requires `--environment-manifest`. Its resolved JSON
record contains the exact `runtime_version`, immutable `image_digest`, and full
`git_revision`. It also contains an `executor_policy_digest` covering effective
inherited rlimits and other evaluator host and job policies. The digest records
policy and creates no resource cap. The environment is configuration-bound.
Local runs without that provenance remain runnable but unofficial. Container
and evaluator hosts must also enforce explicit safety ceilings for processes,
memory, CPU, file descriptors, and execution duration; those ceilings are
security controls and are recorded in the executor policy rather than treated
as benchmark budgets.

When a system relies on provider defaults, its resolved manifest must pin a
SHA-256 digest of the canonical provider-default and API configuration. A
changed default configuration therefore changes the EDLB configuration hash.
If no provider defaults are used, the digest may be null.

The implemented operator controls are per-checkpoint tool calls, per-checkpoint
turns, per-response timeout, and track-scoped retries. For nullable controls,
null means unlimited. Direct `open_world` setup defaults to unresolved
configuration. External execution requires resolved agent and environment
manifests, and aggregates containing unresolved runs are marked unofficial.
Compare results only when the execution policy and configuration are
identical. Official fixed-harness and open-team scoring still uses exactly
three trials per system and world. This follows the
[Tau2 CLI reference](https://github.com/sierra-research/tau2-bench/blob/main/docs/cli-reference.md),
which treats run controls as explicit operator settings.

Business, authorization, and temporal rules remain part of the benchmark.
Protocol trust-boundary validation, blind submission quotas and canaries,
network isolation, and declared evaluator safety policy remain security
controls, not model or execution budgets.

The generated packs are stored at:

~~~text
benchmarks/v1/output/public/train/   public train worlds
benchmarks/v1/output/public/dev/     public dev worlds
benchmarks/v1/output/public/blind/   public blind worlds
benchmarks/v1/authoring/             blueprints and shared documents
~~~

Regenerating an existing non-empty output directory is destructive and requires
an explicit `edlb generate --force` confirmation.

## Machine validation

The generator validates world counts and split assignment, six-vertical
coverage, counterfactual pairing, checkpoint and duration bounds, channel
counts, artifact checksums and paths, event timing, role visibility, synthetic
provenance, public release visibility, and post-intervention artifact
differences. runner.validate_dataset validates manifest identity, the
synthetic flag, artifact paths, event identity and availability, public oracle
and hidden-event files, and private-pack handling when explicitly enabled.

The Draft 2020-12 schema test validates every generated manifest, actor,
artifact, event, checkpoint, assertion, rubric assertion, and public reference
protocol trace. It also exercises every protocol variant and model serialization
fixture with RFC 3339 format checking.

Focused runtime tests cover canary scanning, quota enforcement, exact manifest
hashing, HMAC signatures, immutable Podman command construction, and RevOps-only
merge authorization.

Automated privacy fixtures reject live domains, non-reserved phone numbers,
configured copied phrases and entities, and duplicate person identities. These
fixtures are list-based and do not establish a global real-person or copy scan.

The final machine gate passes the functional test suite. All 72 checked
reference traces match their oracle and score EI 100.0 with Strict Cycle Pass;
all 24 closed-won traces ablate to `no_decision`.

Run the checks with:

~~~bash
uv run --group dev python3 -m unittest discover -s tests
uv run --group dev python3 -m unittest tests.test_schema
uv run --group dev ruff check .
python3 -m compileall -q src tests
edlb validate benchmarks/v1/output
edlb validate benchmarks/v1
~~~

The current generated records are summarized in
benchmarks/v1/authoring/validation.json and
benchmarks/v1/output/manifest.json. The public manifest reports 72 worlds,
5,184 artifacts, and 36 counterfactual pair diffs.

## Pending release gates

The following remain unclaimed and are not implied by the generated data or
machine checks:

- Formal trademark and legal clearance.
- Expert recruitment and two blinded expert reviews per world.
- Stakeholder-model selection and model/judge calibration.
- The 12-world resource characterization pilot.
- Official three-trial model runs.
- Container endpoint allowlisting.
- End-to-end blind evaluator security evidence against the release evaluator.
- Publication of official leaderboard results.

The fixed-harness and open-team leaderboard files are intentionally empty until
official runs exist.

## Preliminary namespace checks

On 2026-08-17, a preliminary PyPI check for edlb returned 404 and an exact
phrase GitHub search returned zero results. These observations are not formal
trademark or legal clearance.

## Research basis

The contract draws on outcome-based evaluation in [Tau2-Bench](https://github.com/sierra-research/tau2-bench/blob/main/docs/evaluation.md), final-state verification in [EnterpriseOps Gym](https://github.com/ServiceNow/EnterpriseOps-Gym), the discussion of expert-authored work products in [GDPval](https://openai.com/index/gdpval/), event-first synthetic generation in [ESL-Bench](https://arxiv.org/html/2604.02834), and verifier design guidance in [The Art of Building Verifiers](https://arxiv.org/html/2604.06240v1). Harvey LAB informed separation between task data, runtime, agents, and evaluation, but its code and structure are not copied.

Vertical process references are listed in BENCHMARK_CARD.md and are used as
process guidance only. They are not data sources for copied customer records.

## Licensing

Source code and schemas are licensed under the MIT License in LICENSE.
All v1 synthetic benchmark data, oracle state, assertions, reference traces,
hidden events, and test packs are licensed under CC BY 4.0 in LICENSE-DATA.
Future packs marked `release_visibility=private` remain outside the public
release until retired.

The benchmark contains no customer data. All organizations, people, domains,
phone numbers, communications, and external signals must be generated and
must carry provenance metadata before release.
