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
- [x] 137 functional tests pass in the final machine gate.
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

## Pending owner action

- [ ] Complete formal contract and schema review.
- [ ] Recruit experts and complete two blinded reviews per world.
- [ ] Complete stakeholder-model selection and schema-adherence review.
- [ ] Calibrate the language-model judge against blinded human labels.
- [ ] Run the 12-world model budget pilot.
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
