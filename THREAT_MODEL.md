# Threat Model

## Scope

This model covers authoring, generation, packaging, execution, grading, and
release of EDLB v1. It protects future private-pack integrity, synthetic
provenance, agent isolation, reproducibility, and evaluator availability.

The runtime and generated worlds are present. A control marked pending is not
evidence that the release gate has passed.

## Assets

| Asset | Required protection |
| --- | --- |
| Causal truth and terminal conditions | Do not expose through agent tools; v1 public files intentionally publish them |
| Private assertions and answer keys | v1 publishes them; future `release_visibility=private` packs keep them in maintainer-only evaluator data |
| Public world artifacts | Preserve timing, visibility, provenance, and license metadata |
| Role permissions | Enforce in the broker, independent of prompts |
| Run traces and scorecards | Preserve lossless sequence, hashes, and version context |
| Model and judge configuration | Pin by digest, prompt hash, seed, and available provider settings |
| Process reference material | Use as guidance, not copied dataset content |

## Threats and controls

| Threat | Impact | Required control | Current status |
| --- | --- | --- | --- |
| Oracle leakage through files, IDs, metadata, or errors | Invalid benchmark results | Separate oracle storage, public-boundary scans, neutral IDs, redacted errors | Partial automated checks, evaluator test pending |
| Temporal leakage | Agent sees future stakeholder or market facts | Separate effective, recorded, and available times, plus leakage tests | Runtime and generated timestamps present, security test pending |
| Counterfactual leakage | Pair membership or intended outcome is inferable | Keep pairs in one split, neutralize labels and filenames, inspect artifacts | Generator checks present, expert review pending |
| Role escalation | Agent sends unauthorized messages or approves terms | Broker-enforced RoleGrant, resource scopes, and side-effect assertions | Runtime control and RevOps-only merge test present, broader adversarial test pending |
| Destructive collateral action | Unrelated CRM or document state changes | Transactional writes, idempotency keys, immutable history, collateral assertions | Runtime controls present, adversarial test pending |
| Prompt injection in synthetic artifacts | Agent follows untrusted artifact instructions | Treat artifact content as data, isolate tool instructions, test malicious strings | Contracted, test pending |
| Real PII or live company data | Privacy or legal exposure | Fictional entities, reserved domains, configured phrase and entity lists, phone checks, duplicate-identity checks | Targeted privacy fixtures focused-tested, release-evaluator boundary checks pending |
| Copyright or trademark contamination | Unlicensed dataset content | Cite process references, generate original text, retain provenance, review license | Provenance and citations present, legal clearance pending |
| Model realization changes causal truth | Non-reproducible or invalid worlds | Structured causal engine owns truth, constrained realization, cache and digest | Runtime controls present, model calibration pending |
| Judge overreach or criterion cascade | Unreliable scores | Deterministic majority, criterion-scoped evidence slices, two judge passes, human calibration | Judge path present, calibration pending |
| Implicit resource budget | Unfair truncation or incomparable results | No scored model settings by default, explicit per-checkpoint controls, resolved pre-run agent manifest, manifest and configuration hashes; separate finite host-safety ceilings are recorded in the executor policy | Contracted; runtime verification pending |
| Untrusted tool payload or fan-out | Evaluator memory, storage, or side-effect exhaustion | Broker input ceilings for text, queries, lists, recipients, and explicit retrieval requests | Implemented and focused-tested |
| Submission storage exhaustion | Evaluator disk exhaustion | Rootless read-only container, ignored image volumes, no writable host mount, one 64 MiB temporary filesystem | Command construction focused-tested |
| Runtime or model outage | Misclassified infrastructure failure | Distinguish invalid infrastructure from agent failure, optional launch or activation retries, run status, declared evaluator safety policy | Runner controls present, official run pending |
| Test-pack extraction or replay | Blind leaderboard contamination | Rootless execution, canaries, quotas, exact manifests, private evaluator | Lossless `team_message` and `yield` traces, snapshot and diff exports, replay payload and hash validation, state and score hashes, aggregate validation, canary scans, quota ledger, exact-byte manifest hashes, HMAC signatures, and immutable Podman isolation are focused-tested; end-to-end evaluator evidence pending |
| Dependency or image drift | Results cannot be reproduced | Versioned schemas, hashes, image digest, tool and model manifests | Run-manifest fields and immutable image checks present, release image controls pending |

EDLB sets no model or scored benchmark budget by default. It injects no token caps or
temperature, top-p, reasoning-effort, or cost settings. Open Team launch retries and
Fixed Harness activation retries default to zero. The implemented scored controls are
per-checkpoint tool calls, turns, response timeout, and track-scoped retries;
nullable controls use null for unlimited. External systems declare model IDs, digests,
prompt hashes, and provider settings in a resolved pre-run agent manifest. They
separately declare the exact runtime version, immutable image or package digest, full
Git revision, and a SHA-256 digest of effective inherited rlimits and other evaluator
host and job policies in a resolved environment manifest. The digest records policy
and creates no scored benchmark cap. EDLB binds both manifests to configuration and
manifest hashes. Direct `open_world` setup may remain unresolved, external execution
requires both manifests, and aggregates with unresolved runs are unofficial.

Blind container execution has finite host-safety ceilings independent of scored
benchmark budgets: 512 processes, 16 GiB memory, 8 CPUs, 4,096 open files, 512
`nproc`, and a 3,600-second wall-clock limit by default. `build_podman_command`
prefixes execution with GNU `timeout`, sending `TERM` at the limit and force-killing
after 30 seconds. All limits are configurable only to validated finite values. The
read-only root, ignored image volumes, no writable host mounts, disabled network,
and one 64 MiB temporary filesystem remain in force. These controls protect
evaluator availability against malicious or runaway submitted code; they are
recorded in the executor-policy digest and do not set model settings, token
budgets, checkpoint scoring, or benchmark semantics. Business, authorization, and
temporal rules, protocol trust-boundary validation, blind submission quotas and
canaries, network isolation, and declared evaluator safety policy remain required
controls.
The tool broker accepts at most 100,000 characters in a free-text field, 1,000
characters in a query or semantic-list item, 100 semantic-list items, 50
external recipients or meeting participants, and an explicit retrieval request
of 100 records. Omitting a retrieval limit remains unlimited. These are input
and side-effect safety ceilings, not model, context, checkpoint, or total-run
budgets. Blind containers have a read-only root, ignore image-declared volumes,
mount no writable host path, and receive one 64 MiB temporary filesystem at
`/tmp`. Results travel through the JSONL standard streams. These storage
controls protect evaluator availability and do not set a run wall time, model
memory, token budget, or model setting. The isolation flags follow the
[Podman run contract](https://docs.podman.io/en/stable/markdown/podman-run.1.html).
The command also sets `--pids-limit=-1`, which Podman's
[current flag definition](https://github.com/containers/podman/blob/main/cmd/podman/common/create.go)
defines as unlimited. It sets `--ulimit=host`, which Podman's
[rlimit implementation](https://github.com/containers/podman/blob/main/pkg/specgenutil/specgen.go)
maps to an empty OCI rlimit list instead of injected `nofile` or `nproc`
values. The inherited host rlimits remain executor-environment policy, not an
EDLB limit. Official evaluators must pin and record that environment before
claiming comparable results.

Lossless `team_message` and `yield` trace paths, snapshot and diff exports,
replay payload and hash validation, state and score hashes, aggregate dataset
validation, canary scanning, quota enforcement, exact-byte manifest hashing,
HMAC result signing, immutable network-disabled Podman isolation, and RevOps-only
CRM merge are implemented and focused-tested. Automated privacy fixtures reject
live domains, non-reserved phone numbers, configured copied phrases and
entities, and duplicate person identities. These are configured-list checks,
not a global real-person or copy scan. Container endpoint allowlisting,
end-to-end blind evaluator security evidence, and formal trademark or legal
clearance remain pending.

## Trust boundaries

1. Maintainer authoring files to the generator. Authoring input is
   schema-validated and subject to pending expert review gates.
2. Generator to public bundle. Only synthetic, provenance-tagged artifacts may
   cross this boundary.
3. Agent team to tool broker. The agent receives observations and tool results,
   not database access or oracle files.
4. Tool broker to state store. All writes pass permission, schema, timing, and
   idempotency checks.
5. Runtime to grader. The grader receives a trace and state snapshot. It does
   not ask the agent for hidden truth.
6. Maintainer evaluator to submitted team. Future private worlds and private
   assertions remain outside the submission container.

## Required security tests

- Attempt to read oracle paths, private assertions, and causal truth through
  every public tool.
- Attempt role escalation, cross-world access, unauthorized external contact,
  and commercial approval.
- Inject future dates, hidden labels, tool instructions, and fake approval
  messages into artifact content.
- Verify duplicate writes, retries, stale updates, and unrelated-record edits.
- Run release-evaluator checks for live domains, non-reserved phone numbers,
  configured copied phrases and entities, duplicate identities, and private
  answer fields. No global real-person or copy scan is claimed.
- Verify identical replay from the same world, tool, model, and seed manifest.

The 72 checked reference traces match oracle and score EI 100.0 with Strict
Cycle Pass, and all 24 closed-won traces ablate to `no_decision`. No complete
release-evaluator security result is available yet. A release cannot claim
blind test integrity until these tests pass against the actual evaluator.
