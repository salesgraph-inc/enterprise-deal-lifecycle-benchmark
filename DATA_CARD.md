# Data Card

## Summary

EDLB v1 is a generated synthetic dataset for longitudinal enterprise sales
evaluation. The pack contains 48 public worlds, 24 train and 24 dev, plus 24
private blind worlds. Each world contains 72 artifacts, for 5,184 artifacts
overall, 716 checkpoint windows are defined across the pack, 180 shared seller
documents are indexed in the authoring pack, there are 36 counterfactual pairs,
and 4 rich renderings.

Current world and document records are generated from structured template
blueprints. Generated prose is templated and has not been expert-reviewed.
Expert authoring, recruitment, and review gates remain pending.

The generator, runner, causal module, grader, statistics, and reporting paths
are present. Human review, stakeholder-model selection, judge calibration,
official model runs, and public release remain pending.

## Intended use

The data is intended for benchmark development, agent evaluation, verifier
testing, and research into long-horizon coordination. It is not intended for
customer targeting, credit or underwriting decisions, legal advice, employee
evaluation, or claims about real companies or markets.

## Contents

Each world spans 180 to 365 simulated days and contains 8 to 12 checkpoint
windows. Artifact channels include call transcripts, email, internal chat, CRM
records and history, calendar, commercial or diligence documents, and frozen
synthetic web or news signals.

Canonical data uses JSON, JSONL, Markdown, and CSV. Selected rich artifacts
may also be rendered as PDF or XLSX. The current pack has 4 rich renderings,
2 PDF and 2 XLSX. Every released artifact must carry a stable ID, world ID,
timestamps, visibility, checksum, and provenance.

## Generation method

Generation follows an event-first process:

1. Structured template blueprints define causal events, policy gates, visibility,
   terminal conditions, and rubric assertions. The current pack is generated
   from these blueprints; expert authoring and review remain release gates.
2. A deterministic compiler creates the event ledger, time windows, identities,
   CRM projection, artifact inventory, and shared-document index.
3. A constrained language model may realize bounded dialogue or document prose
   from an allowed-facts packet. It cannot alter causal state, permissions,
   approvals, or terminal outcome.
4. Realizations are cached by world state, input, prompt, model digest, and
   seed. Stakeholder-model selection and model calibration remain pending.
5. Automated invariants, schema checks, and blinded expert review gate
   publication.

This separates dense state transitions from sparse language realization, as in
the event-first approach described by [ESL-Bench](https://arxiv.org/html/2604.02834).
The approach also preserves expert-authored work products, a principle used in
[GDPval](https://openai.com/index/gdpval/).

## Validation

Generation checks world counts and split assignment, six-vertical coverage,
counterfactual pairing, checkpoint and duration bounds, channel counts,
artifact checksums and paths, event timing, role visibility, synthetic
provenance, public projections, blind separation, and post-intervention
artifact differences. Dataset validation checks manifest identity, synthetic
provenance, artifact paths, event identity and availability, dev oracle
absence, and private blind access.

The Draft 2020-12 schema suite validates every generated normative record,
rubric assertion, and reference protocol message. It registers an explicit
RFC 3339 date-time checker instead of relying on a format-ignoring validator.
Exact commands are in README.md and REPRODUCIBILITY.md.

Focused tests also cover canary scanning, quota enforcement, exact-byte
manifest hashing, HMAC result signing, immutable network-disabled Podman
isolation, and RevOps-only CRM merge. End-to-end blind evaluator security
evidence and container endpoint allowlisting remain pending.

The final machine gate passes 137 functional tests. Lossless
`team_message` and `yield` trace paths, snapshot and state-diff exports, replay
payload and hash validation, state and score hashes, aggregate dataset
validation, and the 48 reference-trace checks are implemented. All 48 checked
reference traces match oracle and score EI 100.0 with Strict Cycle Pass; all 16
closed-won traces ablate to `no_decision`.

## Synthetic and privacy boundary

No customer records, private communications, production CRM exports, or real
person profiles may enter the dataset. Organizations and people are fictional.
External domains and email addresses use reserved or non-routable names. Phone
numbers use reserved examples. Generated news and web records are frozen and
do not imply real events.

Automated privacy fixtures reject live domains, non-reserved phone numbers,
configured copied phrases and entities, and duplicate person identities. These
are configured-list checks, not a global real-person or copy scan. Release-
evaluator boundary checks for private answer fields and other leakage remain
pending. The data card and provenance record must identify generator version, source
process references, model or template digest when applicable, prompt hash when
applicable, seed, creation time, and license.

These controls address synthetic-data privacy and provenance risks described in
[NIST SP 800-226](https://nvlpubs.nist.gov/nistpubs/SpecialPublications/NIST.SP.800-226.pdf).

## Splits and leakage controls

The release pack contains 24 train, 24 dev, and 24 blind worlds. Counterfactual
pairs remain in one split. Blind causal truth, private assertions, oracle state,
and unreleased traces stay outside public artifacts. Retired blind packs may
be published as CC BY 4.0 data after the maintainer release decision.

Every event distinguishes effective, recorded, and available time. Tests prove
that no artifact or signal is visible before its available time. Public bundles
must not contain hidden labels in filenames, IDs, metadata, or checksums.

## Quality and review

Release policy requires automated invariant checks and two blinded expert reviews.
The target is a mean realism and solvability rating of at least 4 out of 5, no
rating below 3, and Krippendorff alpha of at least 0.67 for categorical review
labels. Failures require revision and re-review.

The automated checks and generated validation records exist. Expert recruitment
and both reviews per world have not been completed. Stakeholder-model
selection, model and judge calibration, the 12-world model budget pilot,
official three-trial model runs, endpoint allowlisting, end-to-end blind
evaluator security evidence, and public release are also pending. Canary
scanning, quota enforcement, exact-byte manifest hashing, HMAC result signing,
immutable Podman isolation, and RevOps-only CRM merge are implemented and
focused-tested.

## Preliminary namespace checks

On 2026-08-17, a preliminary PyPI check for edlb returned 404 and an exact
phrase GitHub search returned zero results. These observations are not formal
trademark or legal clearance.

## Licensing

Source code and schemas are licensed under MIT. Public synthetic data and
retired test packs use CC BY 4.0. Process references are cited for design
context and do not grant rights to reproduce their text, marks, or third-party
material. Blind worlds, oracle state, private assertions, and unreleased traces
are not public data.
