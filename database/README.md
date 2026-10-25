# Health database

PostgreSQL 16 with Apache AGE and pgvector, the system of record for every patient archive.

## Model

| Table or view | Purpose |
| --- | --- |
| `users`, `patients`, `patient_access` | Accounts, charts and access. Row-level security admits only accessible patients |
| `artifact_contents`, `artifacts`, `artifact_versions` | Original archive file bytes and version history, keyed by patient and path |
| `records`, `record_versions`, `current_records` | Stable source identities and complete versioned payloads |
| `resources`, `resource_versions` | FHIR R4-aligned projections with dates, assertions, values, units and provenance. Each carries its `clinical_type` and, where its type has a concept family, its `concept_id` and how it was matched (`concept_basis`) |
| `documents`, `chunks` | Source text with full-text search and local embeddings |
| `relationships` | Auditable relationships with qualifiers. `is_current` marks the edges the latest projection produced; earlier edges stay as history. Current edges are mirrored into the Apache AGE graph per patient |
| `ontology_categories`, `ontology_relations`, `clinical_types` | Each archive's registered categories (with JSON Schemas), relations (with allowed endpoints) and clinical type hierarchy. See [the ontology README](../template/ontology/README.md) |
| `concepts` | Tests, measurements, medications and conditions that join records across sources, with their codes and how each identity was established |

## Commands

`./database/healthdb` acts as the local profile's account.

| Command | Effect |
| --- | --- |
| `record-narrative`, `record-medication`, `capture-conversation` | Write reviewed facts or conversation in one transaction |
| `put`, `retire`, `export` | Store, retire or export archive files |
| `ingest`, `verify` | Reproject the patient's archive; verify archive, projections, ontology, graph and isolation |
| `search`, `labs`, `medications`, `symptoms`, `vitals`, `anthropometrics`, `activity`, `profile`, `timeline`, `record`, `document`, `graph`, `ontology`, `concepts`, `concept`, `query` | Read-only queries. `concept body_weight` returns one concept's records from every source as a series |

Writes take a per-patient advisory lock. They materialize the patient's current archive into a private workspace, apply the change, then store new versions and reproject records, resources, graph and embeddings in one transaction. A local checkout is read only by an explicit `put`. Embeddings are computed locally with `BAAI/bge-small-en-v1.5`, so no health text is sent to an embedding service.

The SQL migrations in `sql/` are idempotent and run in order on bootstrap and on each write.
