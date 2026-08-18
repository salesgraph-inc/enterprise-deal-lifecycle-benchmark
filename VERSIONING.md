# Versioning

EDLB has separate versions for the contract, benchmark world set, tool
protocol, runtime, stakeholder configuration, and grader. A result is
comparable only when its run manifest records all of these versions.

## Version identifiers

- Contract and schemas use vMAJOR.MINOR.PATCH.
- Protocol changes that alter message meaning or required fields increment the
  contract major or minor version as appropriate.
- Runtime and grader versions are independent and must be recorded in the run
  manifest.
- External systems declare execution policy and provider settings in a resolved
  pre-run agent manifest. They separately declare the exact runtime version,
  immutable image or package digest, full Git revision, and SHA-256 digest of
  effective inherited rlimits and other evaluator host and job policies in a
  resolved environment manifest. The digest records policy and creates no
  resource cap. EDLB binds both to configuration and manifest hashes.
  Nullable controls use null for unlimited.
- A benchmark release names the world set, split assignment, tool schema,
  stakeholder configuration, judge configuration, and grader together.
- The current generated world and schema version is v1.0.0.

## Compatibility

- Patch releases fix wording, metadata, or validation defects without changing
  accepted meaning or scores.
- Minor releases add optional fields, tools, or worlds without changing the
  meaning of existing required fields.
- Major releases change required fields, permissions, scoring meaning, timing,
  split membership, or terminal-state semantics.
- Consumers must reject an unsupported major version and must not silently
  coerce unknown required fields.

## Dataset releases

The current pack contains 48 public worlds, 24 train and 24 dev, plus 24
maintainer-only blind worlds. Each world contains 72 artifacts, for 5,184
artifacts overall, and the authoring pack contains 180 shared seller
documents.

World bundles are immutable after a release decision. Each bundle and artifact
has a checksum. Counterfactual pairs remain in the same split. A correction
creates a new benchmark release and preserves the old release for
reproducibility.

The public train split contains reference traces and runnable graders. The
public dev split contains runnable rubrics and graders, but no reference trace
or oracle. Blind worlds, causal truth, private assertions, and oracle state
stay maintainer-only until the blind pack is retired. Retired packs may be
released as CC BY 4.0 data.

## Reproducibility

Every official run must record contract, benchmark, runtime, tool, stakeholder,
judge, and model versions, plus scenario hash, prompt hash, image digest,
random seeds, execution policy, and provider settings. A replay is valid only
when these inputs and the action trace match the recorded manifest. EDLB
supplies no model or execution budget by default, no token caps, no total
wall-time cap, and no default checkpoint tool-call, turn, or response-time cap.
Open Team launch retries and Fixed Harness activation retries default to zero.
The implemented operator controls are per-checkpoint tool calls, turns,
response timeout, and track-scoped retries; comparisons require identical
execution policy and configuration. Unresolved aggregates are unofficial.

The current implementation records run manifests, traces, state hashes,
resource usage, and protocol versions. Blind-evaluator support records
exact-byte manifest hashes, enforces a submission quota ledger, scans canaries,
signs public results with HMAC, constructs immutable network-disabled Podman
commands, and restricts CRM merge to RevOps. These controls have focused tests.
No official three-trial model run has been completed.

## Change process

Contract changes require a schema diff, compatibility classification, updated
documentation, validation, and a review record. Scoring changes require a
version bump, baseline rerun, and a release note describing affected metrics.
No human review, calibration, or benchmark result may be backfilled from an
unverified run.

Formal trademark and legal clearance, expert recruitment, two expert reviews
per world, stakeholder-model selection, model and judge calibration, the
12-world resource characterization pilot, container endpoint allowlisting,
end-to-end blind evaluator security evidence, and public release remain pending.
Canary
scanning, quota enforcement, exact-byte manifest hashing, HMAC result signing,
immutable Podman isolation, and RevOps-only CRM merge are implemented and
focused-tested.
