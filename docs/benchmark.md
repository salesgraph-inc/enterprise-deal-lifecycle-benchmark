# Benchmark

## Task

EDLB evaluates four-role agent teams managing persistent synthetic enterprise
sales opportunities. A world begins at the first booked meeting and ends at a
supported terminal outcome: closed won, closed lost, no decision, disqualified,
or canceled. A rational loss is valid when evidence and policy support it.

The four roles are Account Executive, Domain Specialist, Sales Manager, and
RevOps. Role grants control access to CRM, communications, calendars, documents,
approvals, frozen external signals, team messages, and run control. The virtual
clock, event release, and authorization rules belong to the environment.

Events have separate effective, recorded, and available times. CRM is a mutable
projection and can disagree with newer source evidence. Agents never see the
causal ledger.

## Data

The public v1 pack contains 72 worlds, 24 per split. Each world contains 100 to
120 artifacts, for 8,060 total. It also contains 508 checkpoints, 180 shared
seller documents, and 36 counterfactual pairs.

Six verticals contribute 12 worlds each:

| Vertical | Modeled scope |
| --- | --- |
| Manufacturing | A synthetic supplier lifecycle based on a company-specific supplier process |
| Construction | A synthetic composite of a direct federal FAR acquisition and nonbinding CM/GC practice |
| Commercial insurance | A synthetic London Market placement workflow |
| Consulting | A synthetic United Kingdom central-government consultancy workflow |
| Legal services | A synthetic California engagement workflow with outside-counsel sourcing practice |
| Corporate banking | A synthetic OCC-supervised United States national-bank lending workflow |

Each vertical has six causal skeletons with two variants. Pairs stay in one
split and differ only at one intervention and its descendants. The families
cover champion departure, late authority entry, budget shock, requirements
change, competitive pressure, and an external event.

Worlds contain call transcripts, email, internal chat, CRM records and history,
calendar events, proposals, diligence documents, and frozen web or news records.
All organizations, people, domains, phone numbers, communications, and external
signals are synthetic and provenance tagged.

The source registry at
[`src/edlb/resources/source_registry.json`](../src/edlb/resources/source_registry.json)
records bounded process claims, applicability, locations, and interpretation
limits. The corresponding
[`source evidence manifest`](../src/edlb/resources/source_evidence/manifest.json)
records official URLs, byte counts, and SHA-256 hashes. Remote originals are not
redistributed.

## Evaluation

The primary score is the Execution Index, with eight equally weighted
categories:

1. Evidence and account understanding
2. CRM integrity
3. Stakeholder management
4. Workflow compliance
5. Communication
6. Forecast discipline
7. Longitudinal recovery
8. Side-effect discipline

A confirmed critical violation sets a world score to zero. Strict Cycle Pass
requires every required assertion and critical condition to pass. Integrity-
valid running runs can receive a provisional partial score but cannot pass the
strict gate. Failed, invalid, or integrity-broken runs score zero.

Terminal outcome, revenue, margin, close date, cycle length, and forecast
accuracy are reported separately. Forecast accuracy uses pre-exposure snapshots
and raw Brier scores. Public outcomes make this a leakage-sensitive diagnostic.
Official comparisons require exactly three trials per system and world under the
same resolved configuration and execution policy.

## Trust boundary

The checked public v1 pack includes oracle state, assertions, hidden events, and
reference traces. It supports transparent development and deterministic replay,
not an official blind leaderboard. A future official evaluation needs an
unreleased pack and an isolated evaluator.

The runtime enforces role grants, timing, visibility, idempotency, semantic write
validation, trace capture, and private-pack access. Blind-evaluator support
includes canary scans, submission quotas, exact manifest hashes, signed results,
network-disabled containers, a read-only root, no writable host mounts, and a
64 MiB temporary filesystem. End-to-end evaluator security evidence and endpoint
allowlisting remain release work.

## Limitations and status

The data models text-based work, not full sales interfaces or real social
context. Six seller organizations do not measure organization generalization.
Template-authored dialogue can miss real hesitation, politics, and specialist
language.

The runtime and public v1 data are runnable and machine-validated. Expert
recruitment, two blinded reviews per world, stakeholder-model selection, judge
calibration, formal legal clearance, resource characterization, official
three-trial runs, and leaderboard publication remain incomplete.

Contract, dataset, runtime, grader, tool schema, and manifest versions are
recorded separately. Compare results only when all recorded versions,
configuration hashes, environment policy, and traces match. Released datasets
are immutable; semantic changes require a new version.

Source code and schemas use the MIT License. Public v1 synthetic data, oracle
state, assertions, hidden events, reference traces, and test packs use CC BY 4.0.
