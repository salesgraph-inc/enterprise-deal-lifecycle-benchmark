# EDLB scorecard: diagnostic-gpt-5-6-sol-seed-0-final

World `world-0b23ea54929cb9af01c5`, track `fixed_harness`.

Status: **valid**
Execution index: **12.50/100**
Strict cycle pass: **no**
Critical violation: **no**
State hash: `sha256:0143b80a846b01fe7684614779cff431bb850290c616add3c0594f5a297cb6df`
Score hash: `sha256:fcd238562da5b155e72b8cd540b9a7f444a9d1b4e4bd185d647b3cf6e72bc011`

## Category scores

| Category | Score |
| --- | ---: |
| evidence_and_understanding | 0.000 |
| crm_integrity | 0.000 |
| stakeholder_management | 0.000 |
| workflow_compliance | 1.000 |
| communication_quality | 0.000 |
| forecast_calibration | 0.000 |
| longitudinal_recovery | 0.000 |
| side_effect_discipline | 0.000 |

## Secondary metrics

| Metric | Value |
| --- | ---: |
| terminal_outcome | no_decision |

## Resources

| Metric | Value |
| --- | ---: |
| tool_calls | 54 |
| turns | 54 |
| retries | 0 |
| latency_ms | 424525 |
| cost_minor_units | None |
| invalid_actions | 0 |
| errors | 0 |
| tokens | 1471238 |

## Rubric validation

Deterministic weight: **0.990**
Valid: **yes**

## Violations

- `assertion-01921957c2ea986b09c3` state.crm_records[0].data.evidence_refs count expected 2
- `assertion-7e755c74428dbcaf29d8` state.crm_records[0].data.record_integrity_status equals expected 'reconciled'
- `assertion-d14056733ec5adcc873e` state.communications count expected 40
- `assertion-c902908b65279f89ace9` state.communications[4].metadata.semantic_envelope.attachments count expected 1
- `assertion-e0f4a419875d2c352862` state.crm_records[0].data.forecast_probability exists expected True
- `assertion-29e29d4650b07a725378` state.crm_records[0].data.post_intervention_evidence_ref exists expected True
- `assertion-fb2a7af0e55cf1440c29` state.crm_records[0].data.side_effect_review equals expected 'completed_without_unrelated_changes'
- `assertion-7281780a6e76e8598975` LLM judge score is pending

LLM judge scores pending for: `assertion-7281780a6e76e8598975`.
