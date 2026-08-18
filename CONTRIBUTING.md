# Contributing

EDLB v1 includes the contract, generated public and private packs, runtime,
CLI, harnesses, causal checks, grading, reporting, and release documentation.
Do not add customer data, live integrations, fabricated benchmark results, or
unreviewed changes to blind evaluator data.

## Contract rules

- Treat src/edlb/schemas/ as normative. A field change requires a versioning decision.
- Use stable lowercase IDs. Do not encode outcomes, split membership, or
  causal variants in public IDs or filenames.
- Use RFC 3339 date-time strings with explicit timezone information.
- Represent money as integer minor units plus an ISO 4217 currency code.
- Keep causal truth, private assertions, and blind test data outside public
  bundles.
- Keep comments out of generated schema and data files. Explain contract
  decisions in documentation or review records.

## Execution-resource policy

EDLB sets no model or execution budget by default. Do not add implicit token
caps or temperature, top-p, reasoning-effort, or cost settings, or default
checkpoint tool-call, turn, response-time, total wall-time, context-history, or
retrieval-result caps. Runner retries default to zero. The implemented operator
controls are per-checkpoint tool calls, turns, response timeout, and retries;
nullable controls use null for unlimited. External systems must declare model
IDs, digests, prompt hashes, and provider settings in a resolved pre-run agent
manifest. EDLB records that declaration and binds it to configuration and
manifest hashes. External execution also pins runtime, artifact, revision, and
effective host and job policy provenance in a resolved environment manifest.
The executor-policy digest records inherited rlimits and other policy and adds
no resource cap. Keep execution policy and configuration identical for comparisons.

Business, authorization, and temporal rules, protocol trust-boundary
validation, blind submission quotas and canaries, network isolation, and
declared evaluator safety policy remain required controls.

## Data authoring rules

Author structured causal facts, event timing, visibility, policies, and rubric
assertions before writing dialogue. Dialogue and document prose must be
generated from an allowed-facts packet and must not change state, permissions,
approvals, or outcomes.

Use fictional entities, reserved domains, and non-routable phone numbers. Do
not copy customer records or passages from process references. Cite process
sources for design context and record generator, model, prompt, seed, and
license provenance.

The current pack contains 48 public worlds, 24 train and 24 dev, plus 24
private blind worlds. Each world contains 72 artifacts, for 5,184 artifacts
overall, and 180 shared seller documents.

The current records are generated from structured template blueprints. Expert
authoring, recruitment, and review gates remain pending.

## Review rules

Every world requires automated schema and invariant checks, then two blinded
expert reviews. Reviewers must not be told the intended outcome or the
counterfactual intervention. Record actual results only in
benchmarks/v1/reviews/.

The reviewer template, status register, reproducibility guide, and release
checklist are part of this repository. Expert recruitment, both reviews per
world, stakeholder-model selection, model and judge calibration, the 12-world
resource characterization pilot, official three-trial model runs, endpoint
allowlisting, end-to-end blind evaluator security evidence, and public release
remain pending. Canary scanning, quota enforcement, exact-byte manifest hashing,
HMAC result signing, immutable Podman isolation, and RevOps-only CRM merge are
implemented and focused-tested.

## Validation

Run the complete machine checks before proposing a contract or runtime change:

~~~bash
uv run --group dev python3 -m unittest discover -s tests
uv run --group dev python3 -m unittest tests.test_schema
uv run --group dev ruff check .
python3 -m compileall -q src tests
edlb validate benchmarks/v1/output
edlb validate benchmarks/v1 --allow-private
~~~

The schema suite uses Draft 2020-12 validators with an explicit RFC 3339
date-time checker. It validates generated manifests, actors, artifacts,
events, checkpoints, assertions, rubric assertions, reference protocol
messages, and model serialization fixtures.

## Licensing

By contributing source, schemas, or documentation, you contribute under the
MIT License unless the file states otherwise. Public synthetic data and retired
test packs use CC BY 4.0. Do not submit material that requires a different
license without maintainer approval.
