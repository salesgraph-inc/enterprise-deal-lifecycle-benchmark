# Source evidence

Remote originals are not redistributed. Each registry source has an official URL, exact byte count, and SHA-256 digest mirrored by the strict source-evidence manifest.

Run `python scripts/verify_source_evidence.py` from the repository root to stream every official source and verify its current bytes. The verifier sends a browser user agent and fails closed when a host blocks automated access or returns different bytes.

The California legal source is the State Bar of California's Rules of Professional Conduct effective November 1, 2018. Its Rule 1.6 claim is limited to the printed pp. 7-8 section on client confidentiality, and its Rule 1.7 claim is limited to the printed pp. 10-11 section on current-client conflicts.
