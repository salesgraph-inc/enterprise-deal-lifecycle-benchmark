# v1 Review Records

This directory records human review, model calibration, and release-gate
status for EDLB v1. It contains no fabricated ratings, agreement statistics,
calibration results, benchmark scores, or leaderboard entries.

## Reviewer template

Create one record per reviewer and world using
reviewer-template.md. Do not include the intended outcome, counterfactual
intervention, oracle state, private assertion text, or answer-bearing metadata
in material shown to a reviewer.

Each record must identify the world, vertical, split, reviewer role, review
date, source revision, and gate outcome. Record findings for realism,
solvability, chronology, channel coverage, policy gates, artifact grounding,
causal consistency, and privacy or leakage concerns. Mark each gate
pending, in_progress, passed, failed, or superseded.

## Required records before release

- Two blinded expert reviews for every world.
- Reviewer role and vertical, without exposing the intended outcome or paired
  intervention.
- Realism, solvability, chronology, channel, policy, and causal-consistency
  findings.
- Automated schema, invariant, and public-boundary test results.
- Stakeholder-model schema adherence and human realism results.
- Language-model judge agreement and critical false-positive measurements.
- Adjudication and re-review for every failed gate.

## Status convention

Use pending, in_progress, passed, failed, or superseded. A pending record is
not evidence of success. Do not enter a result until the underlying run,
reviewer record, or calibration artifact exists and is reproducible.

The current register is in status.md. The generated data and runtime exist,
but expert recruitment, two reviews per world, model and judge calibration,
the resource characterization pilot, official three-trial runs, and public
release remain pending.
