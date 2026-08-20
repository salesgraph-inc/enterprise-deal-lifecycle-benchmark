# Enterprise Deal Lifecycle Benchmark

Enterprise Deal Lifecycle Benchmark, or EDLB, evaluates agent teams managing
long-running synthetic enterprise sales opportunities. Agents investigate
evidence, coordinate four seller roles, maintain CRM state, and move each world
to a supported outcome.

## Dataset

The public v1 pack contains:

- 72 worlds, split evenly across train, dev, and blind;
- 6 verticals and 4 seller roles;
- 6 to 8 checkpoints per world, 508 total;
- 100 to 120 artifacts per world, 8,060 total;
- 36 counterfactual pairs; and
- 180 shared seller documents.

All entities and records are synthetic. Public v1 includes oracle state, hidden
events, assertions, and reference traces. It is useful for development and
diagnosis, but it is not a leakage-resistant leaderboard pack.

## Quick start

EDLB requires Python 3.14 or newer and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --group dev
uv run edlb validate benchmarks/v1
uv run edlb validate benchmarks/v1/output
```

The CLI supports dataset generation, fixed-harness and open-team runs, replay,
grading, reports, and Podman command construction.

## Documentation

| Guide | Contents |
| --- | --- |
| [Benchmark](docs/benchmark.md) | Task, data, evaluation, provenance, and limitations |
| [Running EDLB](docs/running.md) | Setup, runs, providers, replay, and release checks |
| [Contributing](CONTRIBUTING.md) | Contract, authoring, review, and validation rules |
| [Security](.github/SECURITY.md) | Private vulnerability reporting and scope |

Canonical schemas live in `src/edlb/schemas/`. Generated pack metadata lives in
`benchmarks/v1/output/manifest.json` and
`benchmarks/v1/authoring/validation.json`.

## Status

The runtime, generated pack, deterministic reference traces, and machine checks
are implemented. Generated prose remains template-authored and has not completed
the planned expert reviews. No official three-trial leaderboard result is
claimed.

## License

Source code and schemas use the [MIT License](LICENSE). Public v1 synthetic data,
oracle state, assertions, hidden events, reference traces, and test packs use
[CC BY 4.0](LICENSE-DATA).
