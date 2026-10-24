---
name: health-record-medication
description: Record patient-reported medication regimens, starts, stops, holds, dose changes and single administered doses with exact quotes. Use when the user reports taking, stopping, pausing or changing a medication or supplement.
---

# Record a medication report

Medication reports are supplemental patient-reported rows. Each has a stable medication identity and exact quoted evidence.

## Choose the record type

| The user reports | `record_type` |
| --- | --- |
| A current regimen, a new dose, or a stop that supersedes the workbook | `medication_regimen` |
| A dated change: start, stop, pause (`on-hold`) or dose change | `medication_change` |
| One completed dose, without an ongoing regimen | `medication_administration` |

## Rules

- Call `medications` with `changes: true` first to find the existing `medication_id`. Reuse it; never create a second identity for the same medication.
- A declared intention is not a completed action. "I'm going to stop" is a declared hold or plan with `action_basis`, not proof the doses were skipped.
- Keep units exactly as written: `tbsp`, `tsp`, `IU`, `mg`. Don't convert spoon volumes to grams.
- A row that replaces a workbook regimen needs `supersedes_regimen` with the exact original row hash and path. Unreviewed collisions are errors.
- Patient-attributed effects are `patient_hypothesis` records, linked to the medication through the narrative skill. They never establish causation.

## Steps

1. Build the row without `provenance` or `content_sha256`, plus `evidence_quotes`: verbatim strings that each occur once in the source. Match the `meds/medication.schema.json` branch for the type. Use `get_record` on an existing row of the same type as a template.
2. Dry run, then record:
   - Connector: `record_medication` with `report` (a folder name such as `2026-10-24-dose-change`), `source_text`, `row` and `dry_run: true`, then the same call without `dry_run`.
   - Local: `healthdb record-medication SOURCE.txt ROW.json --report NAME --dry-run`, then without `--dry-run`.
3. Read back with `medications` and `changes: true`.
