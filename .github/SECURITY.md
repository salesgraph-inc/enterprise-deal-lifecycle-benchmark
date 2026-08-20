# Security policy

## Reporting a vulnerability

Use GitHub's private vulnerability reporting or Security Advisories for this
repository when available. Include:

- the affected commit, tag, or file;
- a concise description and security impact;
- reproduction steps or a minimal proof of concept;
- whether the issue can expose hidden benchmark material, read files outside a
  bundle, execute untrusted code, bypass evaluator controls, or exhaust
  evaluator resources; and
- any suggested mitigation.

Do not open a public issue or pull request for an undisclosed vulnerability. If
private reporting is unavailable, ask the maintainers through the
organization-managed GitHub account for a private channel. Do not include
secrets or sensitive customer information.

## Scope and disclosure

The runtime accepts generated bundles and may execute evaluator workloads. Path
traversal, symlink escapes, unsafe process or container configuration, network
isolation, resource exhaustion, secret leakage, and release-boundary mistakes
are in scope. This project contains synthetic data and must not receive customer
records or production secrets.

Maintainers will assess severity and affected releases, coordinate a fix, and
agree on disclosure timing through the private channel.

## Supported baseline

Changes affecting the runner, bundle loading, blind evaluation, container
execution, release visibility, or generated fixtures must include focused tests
and pass CI. Public v1 data uses CC BY 4.0. Future packs marked
`release_visibility=private` must remain outside public artifacts until retired
and explicitly released.
