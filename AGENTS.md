# Agent rules for the health archive

These rules apply to agents working in a health archive built from this code: through the MCP connector, the plugin's skills or the `healthdb` command.

A user's health conversation goes through the MCP connector. If its tools are missing, tell the user it is signed out and how to sign in again. Never fall back to `healthdb`, which is for developing this code.

## Durable conversation audit trail

Preserve the whole health conversation and build a durable, source-backed audit trail. Capturing only selected facts, or keeping information only in chat context, is not enough.

1. At each health conversation turn, capture the user-facing messages with their original roles, timestamps and stable source identities. Use the connector's `capture_conversation` tool, or `healthdb capture-conversation --session PATH` locally.
2. Review new patient statements for structured extraction. Preserve clarification questions and answers.
3. Keep the original wording and earlier versions when a later report corrects a dose, chronology or interpretation. Never record a proposed change as an action taken.

If the schema or ontology cannot represent a statement faithfully, stop dependent analysis and extend the schema, the ontology registry and the projections additively. Do not flatten meaningful distinctions or invent clinical codes. Keep these distinct:
- symptoms, explicit negatives and timing;
- regimen doses with their units, treatment intent and response;
- reported associations and workload or context;
- patient hypotheses, agent hypotheses and confirmed clinical findings;
- uncertainty and care preferences.

Assistant advice and hypotheses never become patient diagnoses, medication use or completed treatment changes. Patient-attributed causation is not confirmed causation.

## The database is the system of record

- Record facts with `record_narrative` or `record_medication` (`healthdb record-narrative` and `healthdb record-medication` locally). Run a dry run first, then read the facts back before saying they were saved.
- Local archive files are an export. `healthdb put` stores a deliberately changed file and refuses stale or record-dropping versions. A record leaves the current archive only through explicit correction, supersession or `healthdb retire --reason`.
- Query before answering health questions, and cite source dates, record IDs and evidence kinds. Agent research and raw conversation are retrieved only explicitly.

## Working on this code

- This repository is public. Never commit health data, identity profiles, credentials, connection settings or archive exports. Run `python privacy/scan.py` before pushing.
- Tests use synthetic accounts and data only.
