---
name: health-record-narrative
description: Record patient-reported symptoms, vital signs, weights, exposures such as alcohol, exercise, hypotheses, associations and care preferences as reviewed, quote-backed records. Use when the user reports new health information that is not a medication regimen change.
---

# Record a reviewed narrative extraction

Each patient statement becomes reviewed records tied to verbatim quotes from the exact source text. The tool validates against the ontology held in the database, then stores the source, records, graph links and search index in one transaction.

## Steps

1. **Take the source text exactly as written**, including typos. It is the evidence.
2. **Resolve ambiguity first.** Ask a clarification question when a date, unit, dose or value could reasonably be read more than one way. Example: a weight far outside the recent range, which may be a typo. Record the answer as its own source, with `measurement_context` records linked by `related_record_ids`.
3. **Choose record types** from the ontology. Call `ontology` to list categories, and `ontology` with `category` for a category's JSON Schema. Common types:
   - `symptom_observation`: present, absent or uncertain. Use `temporal_scope: dated_episode` for a past dated episode.
   - `vital_sign_measurement`: systolic, diastolic and pulse as separate records. Link the two blood pressure components with `paired_component_record_ids`.
   - `anthropometric_measurement`: weight in lb or height in in. Use `value_basis: patient_clarified_value` with `original_written_value` when a typo is confirmed.
   - `reported_exposure`: for example alcohol, with a drink-count `amount` and `time_of_day`.
   - `sleep_observation`: one night, with `occurrence` on the morning it ended. Hours slept as `value` in `h` (`computed_from_reported_times` when derived from stated bed and wake times), plus `awakenings`, `quality` and naps as stated; leave the rest null.
   - `reported_association`: `causality: not_established`.
   - `patient_hypothesis`: the patient's own explanation. Never a finding.
   - `check_in_tracking`: one agreed change to what the daily check-in asks (`action` track or stop; `item_kind` measurement, exposure, symptom, question or setting). Record only what the patient asked for or approved, never a proposal they did not accept.
   - `measurement_context`, `exercise_session`, `care_preference`, `patient_context`.
4. **Write the extraction:**
   ```json
   {"reported_at": "<message ISO time>", "reported_local_date": "YYYY-MM-DD",
    "records": [{"record_key": "pulse_2026-10-24", "record_type": "vital_sign_measurement",
                 "clinical_diagnosis": null, "terminology_code": null, "source_context": "<why and what is unknown>",
                 "details": {"...": "per the category schema"}, "evidence_quotes": ["<verbatim, unique in source>"]}]}
   ```
   - `patient_scope` and `report_timezone` default to the signed-in person's profile. Call `status` to see them.
   - Each evidence quote must appear exactly once in the source. Use a longer quote to disambiguate.
   - Use `@record:<record_key>` to reference another record in the same extraction.
   - Put `occurrence` dates on dated events, distinct from `reported_at`.
   - List what was not reported in `unknowns`. Never invent values, clinical codes or diagnoses.
5. **Dry run**, then record:
   - Connector: `record_narrative` with `source_name` (a stable kebab-case name such as `2026-10-24-weight-bp-pulse`), `source_text`, `extraction` and `dry_run: true`, then the same call without `dry_run`.
   - Local: `healthdb record-narrative SOURCE.txt EXTRACTION.json --dry-run`, then without `--dry-run`.
6. **Read back** with `vitals`, `anthropometrics`, `symptoms` or `get_record`. Confirm dates and values before telling the user the facts were saved.

A correction to an already-recorded source uses `revise: true` (`--revise`). Prior versions are kept. A new day's report is a new source, never a revision.

If no category can represent the statement faithfully, stop and use the `health-ontology-extension` skill. Don't flatten the statement into an ill-fitting type.
