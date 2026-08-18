# Release Readiness Checklist

Current generated prose is templated and not expert-reviewed. Expert
authorship and review remain pending release gates.

## Implemented and machine-checked

- [x] 72 total worlds, 48 public and 24 private blind.
- [x] 72 artifacts per world and 5,184 total artifacts.
- [x] 716 checkpoint windows across the full pack.
- [x] 180 shared seller documents.
- [x] 36 counterfactual pairs.
- [x] 4 rich renderings, 2 PDF and 2 XLSX.
- [x] CLI, fixed-harness runner, open-team runner, causal module, grader, and
      reporting paths present.
- [x] Draft 2020-12 schemas and generated-record validation implemented.
- [x] Explicit RFC 3339 date-time checking in the schema test.
- [x] The functional test suite passes in the final machine gate.
- [x] Generated validation and runtime test commands documented.
- [x] Lossless `team_message` and `yield` trace paths, snapshot and diff
      exports, replay payload and hash validation, state and score hashes, and
      aggregate dataset validation.
- [x] 48 checked reference traces match oracle and score EI 100.0 with Strict
      Cycle Pass; all 16 closed-won traces ablate to `no_decision`.
- [x] Automated privacy negative fixtures reject live domains, non-reserved
      phone numbers, configured copied phrases and entities, and duplicate
      person identities. These are configured-list checks, not a global
      real-person or copy scan.
- [x] Canary scanning, quota ledger, exact-byte manifest hashing, HMAC result
      signing, immutable network-disabled Podman isolation, and RevOps-only CRM
      merge have focused tests.

## Resource policy

EDLB has no implicit model or execution budget. It sets no token caps or
temperature, top-p, reasoning-effort, or cost settings, and no default
checkpoint tool-call, turn, response-time, total wall-time, context-history, or
retrieval-result cap.
Open Team launch retries and Fixed Harness activation retries default to zero.
The implemented operator controls are per-checkpoint tool calls, turns,
response timeout, and track-scoped retries. For nullable controls, null means
unlimited. External systems declare model IDs, digests, prompt hashes, and
provider settings in a resolved pre-run agent manifest. They also declare the
runtime version, immutable image or package digest, full Git revision, and
SHA-256 digest of effective inherited rlimits and other evaluator host and job
policies in a resolved environment manifest. The digest records policy and
creates no resource cap. EDLB binds both declarations to configuration and
manifest hashes. Comparisons require identical execution policy and configuration.
Unresolved aggregates are unofficial.

Business, authorization, and temporal rules, protocol trust-boundary
validation, blind submission quotas and canaries, network isolation, and
declared evaluator safety policy remain required controls.

## Pending owner action

- [ ] Complete formal contract and schema review.
- [ ] Recruit experts and complete two blinded reviews per world.
- [ ] Complete stakeholder-model selection and schema-adherence review.
- [ ] Calibrate the language-model judge against blinded human labels.
- [ ] Run the 12-world resource characterization pilot.
- [ ] Run official three-trial fixed-harness and open-team evaluations.
- [ ] Implement and test container endpoint allowlisting.
- [ ] Complete release-evaluator privacy, public-boundary, replay, and
      end-to-end blind-evaluator security tests.
- [ ] Obtain formal trademark and legal clearance.
- [ ] Publish only reviewed, reproducible leaderboard results.
- [ ] Approve public release and retire any blind pack being published.

The fixed-harness and open-team leaderboards remain empty until official runs
and release approval exist. Preliminary PyPI and GitHub namespace observations
are not legal clearance.
