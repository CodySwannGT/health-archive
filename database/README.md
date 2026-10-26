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
| `concepts` | Tests, measurements, medications, conditions, symptoms and exposures that join records across sources, with their codes and how each identity was established. Symptom and exposure reports join by their structured key |
| `associations`, `association_versions` | Associations the connections engine computes from the patient's current records at each projection, with every version kept. Evidence kind `derived_association`; never a cause |
| `pooling_consent`, `pooling_consent_events` | Each chart's opt-in to pooled patterns, and its append-only history |
| `pooled_patterns` | Patterns shared by at least five opted-in charts. No patient, record, date or free-text column; a check constraint enforces the floor. Readable by registered accounts, never published |

## Commands

`./database/healthdb` acts as the local profile's account.

| Command | Effect |
| --- | --- |
| `record-narrative`, `record-medication`, `capture-conversation` | Write reviewed facts or conversation in one transaction |
| `put`, `retire`, `export` | Store, retire or export archive files |
| `ingest`, `verify` | Reproject the patient's archive; verify archive, projections, ontology, graph and isolation |
| `search`, `labs`, `medications`, `symptoms`, `vitals`, `anthropometrics`, `activity`, `profile`, `timeline`, `record`, `document`, `graph`, `ontology`, `concepts`, `concept`, `query` | Read-only queries. `concept body_weight` returns one concept's records from every source as a series |
| `associations`, `check-in`, `pooled-patterns`, `pooling` | Connections, the daily check-in plan, pooled patterns and consent status. `pooling --opt-in` or `--opt-out` changes consent |

Writes take a per-patient advisory lock. They materialize the patient's current archive into a private workspace, apply the change, then store new versions and reproject records, resources, graph and embeddings in one transaction. A local checkout is read only by an explicit `put`. Embeddings are computed locally with `BAAI/bge-small-en-v1.5`, so no health text is sent to an embedding service.

The SQL migrations in `sql/` are idempotent and run in order on bootstrap and on each write.

## Connections

`tools/associations.py` computes associations from the patient's dated, current records during every projection. `verify` recomputes them and requires an exact match.

| Method | Finds | Needs |
| --- | --- | --- |
| `exposure_episode_contrast` | How often a symptom occurs on days with and without an exposure (alcohol, a food, a medication), the same day or the next for reported exposures, with a risk ratio, a one-sided Fisher exact p-value and mean amounts | Explicit present and absent reports. A day with no report is unknown, never absent |
| `constant_background` | An exposure in use on every observed day, symptom-free days included, or stopped within 30 days before all of them. It cannot explain why the symptom comes and goes | At least one symptom-free day |
| `exposure_series_change` | Measurement slopes while a medication is on versus off across its starts, stops and restarts, or the change since a single change | A slope needs 3 points across 7 days. A single change needs a baseline within 14 days before it and 7 days of measurements after it to be more than insufficient |

Strength is `insufficient`, `weak`, `moderate` or `strong` by sample size, effect size and consistency, and an on-off-on reversal is the strongest series evidence. Every association lists the other exposures it was compared with, ranked, and its limitations. `DERIVED_ASSOCIATION` graph edges link the exposure and outcome concepts of every association that is not insufficient.

Pooling runs after each projection and on every consent change, under a global advisory lock. It reads only the current associations of opted-in charts and keeps a pattern (method, shared exposure and outcome keys, direction) only when at least five distinct charts share it. `HEALTH_POOL_MIN_CONTRIBUTORS` can raise the floor, never lower it. Curated, coded and structured-key concepts pool by concept ID; medications pool by name without brand, form or dose; concepts known only by one archive's free text are never pooled. Opting out removes a chart's contribution in the same transaction.
