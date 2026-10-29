---
name: health-archive-grounding
description: Answer questions about the user's health records, labs, medications, symptoms, vitals, weight or trends by querying their health archive first. Use for any clinical or personal-health question about the signed-in user, before giving advice or interpretation.
---

# Ground health answers in the archive

The health archive database is the system of record. Each signed-in person sees only their own patient chart; `status` shows which one. Query it before answering. Never present memory, earlier conversation or agent research as a fresh database finding.

## Tools

Use the `health-archive` MCP tools. If the `health-archive` tools are missing, the connector is signed out or unreachable. Stop and tell the user so, with the fix: in Codex, run `codex mcp login health-archive` and start a new session; in Claude or ChatGPT, reconnect the connector in settings. Never work around it with the `healthdb` command or a repository checkout. Those are developer tools, and a conversation run through them is not archived.

| Question | Tool |
| --- | --- |
| Lab values, ranges, flags | `labs` with `name` (matches the result name or its concept) |
| One test or measurement over time, across every source | `concepts` to find it, then `concept` (for example `body_weight`, `systolic_blood_pressure`) |
| Current regimen, changes, single doses | `medications` with `changes`, `all_sources` |
| Symptoms, exposures, reported associations, hypotheses, preferences | `symptoms` |
| What might be driving a symptom or a trend | `associations`; see the health-connections skill |
| Vital signs from every source | `vitals` |
| Weight, height, BMI, body composition from every source | `anthropometrics` (`history` adds superseded values) |
| Exercise and sleep | `activity`; hours slept over time: `concept` with `sleep` |
| Demographics | `profile` |
| Dates and sequences | `timeline` with `name`, `from_date`, `to_date` |
| Free-text evidence | `search_archive`, or `search` then `fetch` |
| Full record and its versions | `get_record`; full document: `get_document` |
| Relationships | `graph`, around a record, a concept (`concept:ID`) or a clinical type (`type:ID`) |
| Categories, clinical types, relation rules | `ontology` (`types`, `relations`) |
| Cross-domain questions | `query` (bounded read-only SQL; use `is_current=true` on `health.resources`) |

## Rules

- Cite the source date, the record or document ID, and the evidence kind for each fact you rely on.
- `clinical_date` is when something happened. `recorded_at` is when it was reported. Keep them distinct.
- A historical clinical medication list does not override the current self-reported regimen.
- Family history is not a patient diagnosis. A patient hypothesis is not a finding. A reported association does not establish causation, and neither does a derived association from the connections engine.
- Agent research is interpretation. Retrieve it only explicitly, with `search_archive` and `include_research`, and label it as research.
- Raw conversation is audit context. Retrieve it only with `include_conversation`.
- Records that share a concept measure the same thing. They are not the same event, and a concept's `basis` and each record's `concept_basis` say how the match was made.
- Records from different providers may describe the same test. Don't count them as independent events unless their identity has been reconciled. `POSSIBLE_SAME_SOURCE_EVENT` edges are only review candidates.
- `is_current` means the record belongs to the current archive projection. It does not mean the event is happening today.
- If a tool fails or the database can't be reached, say so plainly.
