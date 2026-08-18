# Reproducibility Guide

## Inputs

Use Python 3.14 or newer and uv. The contract and generated data version is
v1.0.0. Public train and dev worlds are under
benchmarks/v1/output/public/. Blind worlds are under
benchmarks/v1/private/blind/ and require maintainer access.

The full pack contains 48 public worlds, 24 private blind worlds, 72 artifacts
per world, 5,184 artifacts, 716 checkpoint windows, 180 shared seller
documents, 36 counterfactual pairs, and 4 rich renderings. The public manifest
and validation records are committed with the generated pack.

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
uv run --group dev edlb validate benchmarks/v1 --allow-private
~~~

The tests cover generator invariants, runtime behavior, causal checks, grading,
runner behavior, CLI behavior, Draft 2020-12 schemas, RFC 3339 date-time
formats, generated normative records, rubric assertions, and reference
protocol messages. The dataset validator covers public and private bundle
identity, synthetic provenance, artifact paths, event identity and
availability, dev oracle absence, and private blind access.

Focused tests cover canary scanning, quota enforcement, exact-byte SHA-256
manifest hashing, HMAC result signing, immutable network-disabled Podman
isolation, and RevOps-only CRM merge. End-to-end blind evaluator security
evidence and container endpoint allowlisting remain pending.

The final machine gate passes 137 functional tests. Automated
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

All 48 checked reference traces match their oracle and score EI 100.0 with
Strict Cycle Pass. All 16 closed-won traces ablate to `no_decision`.

No official three-trial model run, 12-world model budget pilot, calibration
record, or leaderboard result exists yet. The fixed-harness and open-team
leaderboard JSON files therefore contain empty result arrays.

## Data boundary

Do not copy private blind worlds, oracle state, private assertions, hidden
events, or unreleased traces into public artifacts. Do not treat preliminary
namespace checks as trademark or legal clearance.
