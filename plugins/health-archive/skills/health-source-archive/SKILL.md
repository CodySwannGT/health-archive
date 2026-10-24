---
name: health-source-archive
description: Add, retire or back up original health source files, such as new lab reports, clinical record exports or portal captures, in the database-owned archive. Use when the user provides new provider documents, or asks to back up, export or restore the archive.
---

# Manage source files in the database-owned archive

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
