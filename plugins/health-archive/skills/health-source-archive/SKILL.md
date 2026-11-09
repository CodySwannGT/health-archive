---
name: health-source-archive
description: Add, retire or back up original health source files, such as new lab reports, clinical record exports or portal captures, in the database-owned archive. Use when the user provides new provider documents, or asks to back up, export or restore the archive.
---

# Manage source files in the database-owned archive

## Lab reports through the connector

When the user shares a lab report (a PDF, portal page or photo), record it with the connector's `record_lab_report` tool. No local checkout is needed.

1. Query `labs` for the collection date first. If the report is already in the archive, don't add it again.
2. Transcribe the whole report to text as `source_text`, including the header, flags and comments.
3. Give one observation per printed result. Each observation needs:
   - `analyte_name`;
   - `value_text`, exactly as printed;
   - `unit_text`, `reference_text`, `flag` and `panel_name` as printed, when present;
   - `evidence_quote`: the result's line copied verbatim from `source_text`. It must contain the value and any unit and range.
4. Never convert units, add clinical codes or fill in a result the report does not show. Previous-result columns are context, not new results.
5. Set `transcribed_by` honestly (`agent` when you transcribed it) and describe what was transcribed in `source_description`.
6. Run with `dry_run: true`, fix any refused quote, then record. Read the results back with `labs` before saying they were saved.
7. To correct a transcription, record it again under the same `report_name` with `revise: true`. The earlier version stays in history.

The steps below are for original files and need a local checkout.

The database holds every archive file's original bytes and version history. Local files are an export and are never read implicitly. These operations need a local checkout with database credentials.

## Add or update source files

1. Run `healthdb export` first, so local files are current.
2. Place the original files under their archive directory (`lab-results/`, `health-records/`, `vitals/` or `research/`). Never rewrite a source to fit the database.
3. Parse provider files with the repository's importers when they need structured rows.
4. Run `healthdb put PATH...`. It stores new versions in one transaction and reprojects.
   - It refuses a file the database already holds as an older version.
   - It refuses a record collection missing records the database holds.
   - It refuses to restore a retired file unless you name that file explicitly.
5. Run `healthdb verify`, then read back the new facts with the query tools.

## Retire

`healthdb retire PATH... --reason "why"` removes files from the current archive. Their bytes, versions and records stay in history. Records sourced only from those files leave the current projection. Use it only when the user asks, or when a file was archived by mistake.

## Back up

- Railway runs encrypted daily database backups.
- For an additional Git copy, run `healthdb export`, review the diff, and commit.
- `healthdb ingest` reprojects the database's own archive without reading local files. `--from-disk` puts every new or changed local file and reports refused stale ones.
