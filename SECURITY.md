# Security policy

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting or Security Advisories for
this repository when available. A private report should include:

- the affected commit, tag, or file;
- a concise description and security impact;
- reproduction steps or a minimal proof of concept;
- whether the issue can expose hidden benchmark material, read files outside a
  bundle, execute untrusted code, bypass blind-evaluator controls, or exhaust
  evaluator resources; and
- any suggested mitigation.

Do not open a public issue or pull request for an undisclosed vulnerability.
If private vulnerability reporting is not enabled, contact the repository
maintainers through the organization-managed GitHub account and request a
private channel. Do not include secrets or sensitive customer information in a
report.

## Scope and disclosure

The runtime accepts generated bundles and may execute evaluator workloads.
Reports involving path traversal, symlink escapes, unsafe subprocess/container
configuration, network isolation, resource exhaustion, secret leakage, or
release-boundary mistakes are in scope. This project contains synthetic
benchmark data and must not receive customer records or production secrets.

Maintainers will acknowledge reports through the private channel, assess
severity and affected releases, and coordinate a fix and disclosure timeline
with the reporter. Please allow time for a fix before public disclosure.

## Supported security baseline

Changes that affect the runner, bundle loading, blind evaluation, container
execution, release visibility, or generated fixtures should include focused
tests and pass the repository CI checks. Public v1 fixture data is licensed
under CC BY 4.0; future or unretired packs marked
`release_visibility=private` must remain outside public artifacts until
retired and explicitly released.
