# Enterprise Deal Lifecycle Benchmark

Enterprise Deal Lifecycle Benchmark, or EDLB, is a synthetic benchmark for
agent teams operating enterprise sales opportunities over six to twelve
simulated months. A world starts when a first meeting is booked and ends at a
valid terminal outcome.

## Current release state

The v1 runtime and generated data are present. The repository contains 48
public worlds, 24 train and 24 dev, plus 24 private blind worlds. Each world
has 72 artifacts, for 5,184 artifacts across all 72 worlds, and the authoring
pack contains 180 shared seller documents.

Across the full pack there are 72 worlds, 5,184 artifacts, 716 checkpoint
windows, 180 shared seller documents, 36 counterfactual pairs, and 4 rich
renderings.

The current world and document records are generated from structured template
blueprints. Generated prose is templated and has not been expert-reviewed.
Expert authoring, recruitment, and review gates remain pending.

The implementation is runnable, but this is not a public benchmark release.
No official model result, human-review result, stakeholder-model selection,
or judge-calibration result is represented as complete.

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

The schemas in src/edlb/schemas/ are normative for v1. Fields not defined by a schema
are not part of the public contract. Dates use RFC 3339 date-time strings.
Money uses integer minor units and an ISO 4217 currency code. IDs are stable,
lowercase strings.

## Runtime and CLI

src/edlb/cli.py exposes validate, generate, run, replay, grade, report, and
podman commands. src/edlb/runner.py provides fixed-harness and open-team
execution, limits, trace capture, and replay. src/edlb/causal.py provides
event-first causal interventions and constrained realization checks.
src/edlb/grading.py, src/edlb/statistics.py, and src/edlb/reporting.py provide
deterministic grading, reliability calculations, and reports.

Blind-evaluator support includes canary scanning, a submission quota ledger,
exact-byte SHA-256 manifest hashing, HMAC result signing, immutable
network-disabled Podman isolation, and RevOps-only CRM merge. Focused tests
cover these controls. End-to-end blind evaluator security evidence and
container endpoint allowlisting remain pending.

The generated packs are stored at:

~~~text
benchmarks/v1/output/public/train/   public train worlds
benchmarks/v1/output/public/dev/     public dev worlds
benchmarks/v1/private/blind/         maintainer-only blind worlds
benchmarks/v1/authoring/             blueprints and shared documents
~~~

## Machine validation

The generator validates world counts and split assignment, six-vertical
coverage, counterfactual pairing, checkpoint and duration bounds, channel
counts, artifact checksums and paths, event timing, role visibility, synthetic
provenance, public-boundary projections, blind-data separation, and
post-intervention artifact differences. runner.validate_dataset validates
manifest identity, the synthetic flag, artifact paths, event identity and
availability, dev oracle absence, and private blind handling.

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

The final machine gate passes 137 functional tests. All 48 checked
reference traces match their oracle and score EI 100.0 with Strict Cycle Pass;
all 16 closed-won traces ablate to `no_decision`.

Run the checks with:

~~~bash
uv run --group dev python3 -m unittest discover -s tests
uv run --group dev python3 -m unittest tests.test_schema
uv run --group dev ruff check .
python3 -m compileall -q src tests
edlb validate benchmarks/v1/output
edlb validate benchmarks/v1 --allow-private
~~~

The current generated records are summarized in
benchmarks/v1/authoring/validation.json,
benchmarks/v1/output/manifest.json, and
benchmarks/v1/private/validation.json. The public manifest reports 48 worlds
and 3,456 public artifacts. The private validation record reports 5,184
artifacts when the 24 blind worlds are included.

## Pending release gates

The following remain unclaimed and are not implied by the generated data or
machine checks:

- Formal trademark and legal clearance.
- Expert recruitment and two blinded expert reviews per world.
- Stakeholder-model selection and model/judge calibration.
- The 12-world model budget pilot.
- Official three-trial model runs.
- Container endpoint allowlisting.
- End-to-end blind evaluator security evidence against the release evaluator.
- Public release and publication of leaderboard results.

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
Synthetic benchmark data and retired public test packs are licensed under CC BY
4.0 in LICENSE-DATA. Blind test data, oracle state, private assertions, and
unreleased traces are not public data until a pack is retired.

The benchmark contains no customer data. All organizations, people, domains,
phone numbers, communications, and external signals must be generated and
must carry provenance metadata before release.
