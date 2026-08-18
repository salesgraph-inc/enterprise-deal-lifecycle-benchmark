# Threat Model

## Scope

This model covers authoring, generation, packaging, execution, grading, and
release of EDLB v1. It protects oracle state, blind test integrity, synthetic
provenance, agent isolation, reproducibility, and evaluator availability.

The runtime and generated worlds are present. A control marked pending is not
evidence that the release gate has passed.

## Assets

| Asset | Required protection |
| --- | --- |
| Causal truth and terminal conditions | Never expose to an agent or public blind bundle |
| Private assertions and answer keys | Keep in maintainer-only evaluator data |
| Public world artifacts | Preserve timing, visibility, provenance, and license metadata |
| Role permissions | Enforce in the broker, independent of prompts |
| Run traces and scorecards | Preserve lossless sequence, hashes, and version context |
| Model and judge configuration | Pin by digest, prompt hash, and seed |
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
| Judge overreach or criterion cascade | Unreliable scores | Deterministic majority, bounded evidence slices, two judge passes, human calibration | Judge path present, calibration pending |
| Runtime or model outage | Misclassified infrastructure failure | Distinguish invalid infrastructure from agent failure, bounded retries, run status | Runner controls present, official run pending |
| Test-pack extraction or replay | Blind leaderboard contamination | Rootless execution, canaries, quotas, exact manifests, private evaluator | Lossless `team_message` and `yield` traces, snapshot and diff exports, replay payload and hash validation, state and score hashes, aggregate validation, canary scans, quota ledger, exact-byte manifest hashes, HMAC signatures, and immutable Podman isolation are focused-tested; end-to-end evaluator evidence pending |
| Dependency or image drift | Results cannot be reproduced | Versioned schemas, hashes, image digest, tool and model manifests | Run-manifest fields and immutable image checks present, release image controls pending |

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
6. Maintainer evaluator to submitted team. Blind worlds and private assertions
   remain outside the submission container.

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

The 48 checked reference traces match oracle and score EI 100.0 with Strict
Cycle Pass, and all 16 closed-won traces ablate to `no_decision`. No complete
release-evaluator security result is available yet. A release cannot claim
blind test integrity until these tests pass against the actual evaluator.
