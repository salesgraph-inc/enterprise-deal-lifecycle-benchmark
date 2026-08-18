# EDLB v1 Review Status

Status date: 2026-08-17

The repository contains 72 public worlds, 5,184 artifacts, and 180 shared
seller documents. This register records release
gates, not benchmark results.

| Gate | Status | Evidence |
| --- | --- | --- |
| Contract and schema review | in_progress | Normative schemas and Draft 2020-12 schema tests exist; formal reviewer sign-off is absent |
| Human review of pilot worlds | pending | Expert recruitment and the 12-world resource characterization pilot are not complete |
| Human review of 72 release worlds | pending | Two blinded expert reviews per world are not complete |
| Automated invariant checks | passed | Authoring and public validation records report no generation errors; runtime and schema tests are available |
| Public-boundary and privacy checks | in_progress | Generator checks public projections and synthetic fields; complete release scan is pending |
| Stakeholder-model selection | pending | No approved candidate run record |
| Stakeholder-model schema adherence | pending | No calibration run record |
| Stakeholder-model human realism | pending | No blinded human review record |
| Language-model judge calibration | pending | No judge calibration record |
| Fixed-harness baseline | pending | Harness and worlds exist; no official baseline run is recorded |
| Open-team baseline | pending | Runner and worlds exist; no official baseline run is recorded |
| Blind evaluator security tests | pending | Future private-pack path exists; evaluator security evidence is not complete |
| Container endpoint allowlisting | pending | Not implemented in the current tree |
| v1 data licensing and public boundary | passed | All checked-in v1 worlds and oracle/assertion/hidden-event/reference-trace material are CC BY 4.0; future `release_visibility=private` packs remain excluded until retired and explicitly released |
| Canary, quota, manifest hash, result signing, Podman isolation, and merge controls | in_progress | Focused tests cover canary scans, quota ledger, exact-byte manifest hashes, HMAC signatures, immutable Podman isolation, and RevOps-only merge; end-to-end evaluator evidence is pending |
| Formal trademark and legal clearance | pending | Preliminary namespace observations are not legal clearance |
| Repository fixture publication | in_progress | The checked-in v1 fixture is prepared for public distribution; owner approval, formal legal clearance, and complete release-evaluator security evidence remain pending |

No benchmark score, realism score, agreement statistic, calibration result,
official three-trial result, or leaderboard entry has been established. The
repository fixture's intended public data boundary is separate from approval of
an official benchmark release or result set.
