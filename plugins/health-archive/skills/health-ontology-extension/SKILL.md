---
name: health-ontology-extension
description: Extend the health archive's ontology, schemas and relationships when a patient statement cannot be represented faithfully. Use when no record category, field or relation fits, or when recording fails with an unregistered category or relation.
---

# Extend the ontology

The database enforces a registered ontology. A resource with an unregistered category, or a relationship with an unregistered relation, fails reprojection and nothing is stored. Never force a statement into an ill-fitting type, flatten a meaningful distinction, or invent a clinical code.

The owner has given standing authorization for additive, source-preserving extensions. Changes that alter or remove existing meaning still need the user's agreement.

## What lives where

| File (database-owned) | Purpose |
| --- | --- |
| `symptoms/symptom.schema.json` | JSON Schema branch per narrative record type |
| `meds/medication.schema.json` | Medication row schemas |
| `conversations/message.schema.json` | Conversation message schema |
| `ontology/categories.json` | Every resource category, its domain and its schema path |
| `ontology/relations.json` | Every relationship type and its semantics. None establishes causality |

Importer and projection code lives in the repository (`database/tools/ingest.py`, `symptoms/tools/ingest-narrative.py`), not in the database.

## Steps

This requires a local checkout with database credentials.

1. Run `healthdb export` so local files match the database.
2. Make an additive change:
   - add an optional field, enum value or new `oneOf` branch;
   - register any new category in `ontology/categories.json` and any new relation in `ontology/relations.json`;
   - update the projection code when the new type needs a typed column, a numeric value, or a new graph relation.
3. Add or extend unit tests in `database/tests/`, and run them.
4. Store the changed definitions with `healthdb put symptoms/symptom.schema.json ontology/categories.json ...`. `put` refuses stale versions and record-dropping rewrites.
5. Record the statement, run `healthdb verify`, and read the result back.
6. Document the distinction in the relevant README, then commit the code and the exported files.
