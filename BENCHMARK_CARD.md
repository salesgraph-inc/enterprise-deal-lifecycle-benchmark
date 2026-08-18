# Benchmark Card

## Identity

- Name: Enterprise Deal Lifecycle Benchmark
- Short name: EDLB
- Contract: v1
- Status: runnable implementation and generated v1 data, release gates pending
- Intended use: research evaluation of enterprise sales agent teams
- Out of scope: production sales automation, customer ranking, credit decisions,
  underwriting decisions, legal advice, or employee evaluation

The generated data contains 48 public worlds, 24 train and 24 dev, and 24
maintainer-only blind worlds. Each world has 72 artifacts. The full pack
therefore contains 72 worlds, 5,184 artifacts, 716 checkpoint windows, 180
shared seller documents, 36 counterfactual pairs, and 4 rich renderings. No
benchmark performance, human-review result, stakeholder-model selection, or
judge-calibration result is claimed.

Current worlds and shared documents are generated from structured template
blueprints. Generated prose is templated and has not been expert-reviewed.
Expert authoring, recruitment, and two-review gates remain pending.

## Task

An agent team receives a persistent synthetic enterprise opportunity at the
first booked meeting. It must investigate, coordinate, communicate, maintain
records, satisfy vertical workflow gates, manage new stakeholders and external
signals, and reach a supported terminal state over 180 to 365 simulated days.
The terminal state can be closed won, closed lost, no decision, disqualified,
or canceled. A rational loss or disqualification is valid execution when the
evidence and policy support it.

The benchmark evaluates actions and resulting state. It does not require a
single reference sequence unless a policy requires one authorized action.

## Worlds and splits

The v1 world set covers six verticals, with 12 worlds per vertical:

- Manufacturing
- Construction
- Commercial insurance
- Consulting
- Legal services
- Corporate banking

There are 24 train, 24 dev, and 24 blind worlds, with four worlds per vertical
in each split. Each vertical has six causal skeletons and two counterfactual
variants per skeleton. Every pair remains in one split.

Each world has 8 to 12 irregular checkpoint windows. Agents retain their own
memory and all side effects across checkpoints. The environment owns the
virtual clock and does not accept arbitrary time jumps from agents.

## Seller seats

The environment exposes four fixed logical seats:

| Seat | Allowed responsibility |
| --- | --- |
| Account Executive | Buyer communication, meetings, opportunity work, and proposals within approved terms |
| Domain Specialist | Technical, delivery, underwriting, or subject-matter work |
| Sales Manager | Forecast, coaching, escalation, and authorized commercial exceptions |
| RevOps | CRM repair, deduplication, activity reconciliation, and pipeline records |

The broker enforces permissions. A prompt or message cannot grant a seat a
permission absent from its RoleGrant.

## Observations and tools

Each world can expose call transcripts, email, internal chat, CRM records and
history, calendar events, proposals or diligence documents, and frozen web or
news signals. The public contract defines role-scoped APIs for CRM,
communications, calendar, documents, approvals, web, team messages, and run
control. Audio, browser UI, live SaaS integrations, and live web access are
excluded from v1.

Events have separate effective, recorded, and available times. This prevents
an agent from discovering a fact before it should be visible. CRM is a mutable
projection and can be stale or duplicated. The causal ledger is not exposed to
agents.

## Implementation

The CLI supports dataset validation and generation, fixed-harness and open-team
runs, trace replay, grading, reports, and container command construction.
The harness enforces checkpoint tool-call and turn limits, protocol validation,
role grants, timing, visibility, idempotency, and trace capture. The causal
module owns event-first interventions and bounded realization checks. Grading
evaluates deterministic assertions, tracks pending judge assertions, computes
secondary metrics and reliability statistics, and renders scorecards.
Lossless `team_message` and `yield` trace paths, snapshot and state-diff
exports, replay payload and hash validation, state and score hashes, and
aggregate dataset validation are implemented.
Blind-evaluator support also includes canary scanning, a submission quota
ledger, exact-byte SHA-256 manifest hashing, HMAC result signing, immutable
network-disabled Podman isolation, and RevOps-only CRM merge. These controls
have focused tests.
Automated privacy fixtures reject live domains, non-reserved phone numbers,
configured copied phrases and entities, and duplicate person identities. These
are configured-list checks, not a global real-person or copy scan.

## Causal coverage

Every world includes at least one material disruption and evidence-backed CRM
defects. The six v1 skeletons cover champion departure, late authority entry,
budget shock, requirements change, incumbent or competitor pressure, and an
external business or regulatory event. Counterfactual variants change one
intervention and its legitimate descendants.

Vertical process gates follow process references, not copied records:

- Manufacturing uses supplier qualification and quality controls informed by
  the [Neapco Supplier Requirements Manual](https://www.neapco.com/wp-content/uploads/2024/01/Neapco-Supplier-Requirements-Manual.pdf).
- Construction uses qualification, bidding, safety, bonding, and award stages
  informed by [AGC guidelines](https://www.agc.org/sites/default/files/Files/Programs%20%26%20Industry%20Relations/CM_GC_Guidelines.pdf).
- Commercial insurance uses submission, underwriting, quote, order, binding,
  and issuance concepts informed by [ACORD ePlacing guidance](https://www.acord.org/docs/default-source/ruschlikon-documents-newsletters/ruschlikon-member-resources/best-practice-guide-%28eplacing%29.pdf?sfvrsn=791d006f_4).
- Consulting uses discovery, scope, procurement, and approval concepts
  informed by the [Consultancy Playbook](https://assets.publishing.service.gov.uk/government/uploads/system/uploads/attachment_data/file/1103954/The_Consultancy_Playbook_Version_1.1_September_2022.pdf).
- Legal services uses conflicts, panel selection, fee, security, and
  engagement concepts informed by [GSK's outside-counsel initiative](https://www.acc.com/sites/default/files/2019-12/GlaxoSmithKline-Outside-Counsel-Selection-Initiative-OCSI.pdf).
- Corporate banking uses diligence, underwriting, approval, documentation,
  and closing concepts informed by the [OCC lending handbook](https://www.occ.gov/publications-and-resources/publications/comptrollers-handbook/files/lending-loan-portfolio-risk-management/pub-ch-lending-loan-portfolio.pdf).

## Evaluation

The primary result is an Execution Index from eight equally weighted
categories: evidence and account understanding, CRM integrity, stakeholder
management, workflow compliance, communication, forecast calibration,
longitudinal recovery, and side-effect discipline. A confirmed critical
violation sets the world score to zero. Strict Cycle Pass requires every
required assertion and every critical condition to pass.

Terminal outcome, revenue, margin, close date, cycle length, and forecast error
are reported separately. Win rate and revenue are not the headline score.
Every official system run must be repeated three times. Reports include pass@1,
pass@3, pass^3, paired confidence intervals, resource use, and invalid-action
counts.

At least 75 percent of rubric weight must use deterministic checks. Language
model judges are limited to bounded communication, grounding, and strategy
criteria and must be calibrated against blinded human labels before affecting a
headline score. Independent criteria and process versus outcome separation
follow [verifier guidance](https://arxiv.org/html/2604.06240v1). Repeated trials
follow the reliability concerns in [On Randomness in Agentic Evals](https://arxiv.org/html/2602.07150v2).

The fixed-harness and open-team leaderboard files contain no entries. No
official three-trial model run or 12-world model budget pilot has been run.

## Machine validation

Generation checks world and split counts, vertical and channel coverage,
counterfactual pairs, timing bounds, artifact checksums and paths, visibility,
synthetic provenance, public projections, blind separation, and
post-intervention differences. Dataset validation checks manifest identity,
synthetic provenance, artifact paths, event identity and availability, dev
oracle absence, and private blind access.

The schema suite uses Draft 2020-12 validators with an explicit RFC 3339
date-time checker and validates all generated normative records plus all JSONL
protocol variants. The reproducible check commands are documented in
README.md and REPRODUCIBILITY.md.

The final machine gate passes 137 functional tests. All 48 checked
reference traces match their oracle and score EI 100.0 with Strict Cycle Pass;
all 16 closed-won traces ablate to `no_decision`.

## Known limitations and pending gates

The v1 benchmark models text-based work, not the full interface or social
context of real sales. Six seller organizations do not measure organization
generalization. Synthetic dialogue can miss real hesitation, politics, or
industry-specific language.

Formal trademark and legal clearance, expert recruitment, two expert reviews
per world, stakeholder-model selection, model and judge calibration, the
12-world model budget pilot, official three-trial model runs, container
endpoint allowlisting, end-to-end blind evaluator security evidence, and public
release remain pending. Canary scanning, quota enforcement, exact-byte manifest
hashing, HMAC result signing, immutable Podman isolation, and RevOps-only CRM
merge are implemented and focused-tested.

## Data and licensing

All entities, communications, documents, and external signals are synthetic,
fictional, and provenance tagged. Process references are used for workflow
design and are not copied into generated artifacts. Source code and schemas use
the MIT License. Public synthetic data and retired test packs use CC BY 4.0.
Oracle state, private assertions, unreleased traces, and blind worlds remain
maintainer-only until retirement.

## Preliminary namespace checks

On 2026-08-17, a preliminary PyPI check for edlb returned 404 and an exact
phrase GitHub search returned zero results. These observations are not formal
trademark or legal clearance.
