# Health Archive

A source-backed personal health archive that AI assistants can query and add to. Each person gets a private archive of lab results, clinical records, medications, symptoms, vitals and the conversations behind them. Assistants answer from those records, record new reports as reviewed, quote-backed facts, and draw connections between them: which medications, drinks or foods track which symptoms and trends, and how strong the evidence is.

This repository holds the code only. It never contains anyone's health data.

## What it does

- **The database is the system of record.** PostgreSQL holds every source file's original bytes and version history. It also holds reviewed records, FHIR-aligned projections, an Apache AGE graph and pgvector embeddings.
- **Writes are atomic.** Each one materializes the patient's archive from the database, applies the change, validates it against that archive's ontology and stores everything in one transaction. A record leaves the current archive only by explicit correction, supersession or retirement.
- **The ontology is queryable data.** Registered record categories, a FHIR-aligned clinical type hierarchy, concepts and relationship types live in the database and the graph. Concepts join records that measure the same thing across sources, such as a clinic's coded weight and a patient's reported weight, and record how each match was made. Recording an unregistered category or relation, or an edge outside its relation's allowed endpoints, fails, so new distinctions are defined before they are used.
- **Connections are the point, graded honestly.** A connections engine recomputes associations from each person's dated records whenever the archive changes. It compares symptom days with symptom-free days, identifies background exposures that cannot explain a symptom that comes and goes, and follows measurements across a medication's starts, stops and restarts. Each association carries its method, strength, supporting and contrary records, competing explanations and limitations, and is labelled as computed, never causal.
- **Pooled patterns are opt-in and private.** People can choose to contribute their associations, never their records, to patterns shared by at least five people. The patterns only give assistants better pattern recognition for each person. They hold no identifiers, dates or text, the database rejects smaller groups, and nothing is published.
- **Evidence kinds stay distinct.** Patient reports, clinical findings, family history, patient hypotheses, agent research and assistant advice are kept apart. Reported associations never establish causation, and advice never becomes a completed action.
- **Every account is isolated.** It owns one patient chart, and row-level security limits the read-only role to the signed-in user's patients.

## Use it

| Part | Where |
| --- | --- |
| Plugin with seven skills and an MCP connector, for Claude, ChatGPT and Codex | [plugins/health-archive](plugins/health-archive/README.md) |
| MCP server with OAuth 2.1, invitations and accounts | [mcp_server](mcp_server/README.md) |
| Database, write path, ontology and query tools | [database](database/README.md) |
| Template archive every new patient starts from | [template](template/) |

In Claude Code:

```
/plugin marketplace add CodySwannGT/health-archive
/plugin install health-archive@health-archive-marketplace
```

The connector is invitation-only: ask its operator for an invitation, or run your own.

## Run your own

1. Build the PostgreSQL 16 image with Apache AGE and pgvector from `database/Dockerfile`, or provision an equivalent server with verified TLS.
2. Copy `identity/local-profile.example.json` to `~/.config/health-archive/local-profile.json` and describe yourself as the first user; see [identity/README.md](identity/README.md). Personal configuration (profile, connection settings and deployment record) lives in that folder, never in a checkout.
3. Run `database/setup.sh` for a local Docker database. For a remote one, follow [database/railway/README.md](database/railway/README.md) and run `database/setup-remote.sh`.
4. Seed your archive from the template with `./database/healthdb bootstrap` followed by `./database/healthdb seed`. Accounts created through the connector are seeded automatically.
5. Deploy `mcp_server/` with the variables its README lists, then bind your user to a sign-in account and create invitations for anyone else.

## Privacy model

- Archive directories (`symptoms/`, `meds/`, `lab-results/` and the rest) are git-ignored here, apart from the code under `symptoms/tools/` and `meds/tools/`.
- `healthdb export` refuses to write into a checkout that contains `.public-repository`.
- CI runs `privacy/scan.py` on every change. It fails on archive data paths, data file types, email addresses, patient or user identifiers, password hashes, keys and connection strings.
- Report security issues privately; see [SECURITY.md](SECURITY.md).

## License

[Apache License 2.0](LICENSE).
