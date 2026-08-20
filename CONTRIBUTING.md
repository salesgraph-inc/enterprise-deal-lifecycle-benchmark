# Contributing

Keep changes small, deterministic, and reviewable. Read
[Running EDLB](docs/running.md) before changing generated data.

## Contract rules

- Treat `src/edlb/schemas/` as normative. Field changes require a versioning
  decision.
- Reject unknown fields at trust boundaries.
- Use RFC 3339 date-time strings, integer minor currency units, and ISO 4217
  currency codes.
- Preserve role grants, visibility, timing, idempotency, trace integrity, and
  release visibility.
- Update schemas, models, validation, fixtures, and tests together.

## Data rules

- Author causal facts, timing, visibility, policy, and rubric assertions before
  prose.
- Generate prose only from allowed facts. Prose must not change state,
  permissions, approvals, or outcomes.
- Use fictional entities, reserved domains, and non-routable phone numbers.
- Do not copy customer records or passages from process references.
- Record source, generator, model, prompt, seed, and license provenance.
- Keep counterfactual variants identical before their declared intervention.

Every world must pass automated schema and invariant checks. The planned release
process also requires two blinded expert reviews without revealing the intended
outcome or intervention.

## Validation

Run these checks before committing:

```bash
uv run --group dev python3 -m unittest discover -s tests
uv run --group dev ruff format --check .
uv run --group dev ruff check .
uv run --group dev mypy src
uv run edlb validate benchmarks/v1/output
uv run edlb validate benchmarks/v1
```

Changes to generated data must be deterministic. Regenerate into a temporary
directory and compare it with the checked pack before replacing files. See the
[release checks](docs/running.md#release-checks).

## Licensing

Source, schema, and documentation contributions use the MIT License unless a
file states otherwise. Public synthetic data and retired test packs use CC BY
4.0. Do not submit material requiring another license without maintainer
approval.
