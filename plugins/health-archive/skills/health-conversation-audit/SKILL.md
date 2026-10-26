---
name: health-conversation-audit
description: Preserve the health conversation as a durable audit trail every turn. Use at each turn of a health conversation, including clarification questions and answers, and whenever the user shares new symptoms, measurements, doses or decisions.
---

# Keep the health conversation audit trail

The archive's top rule is to preserve the whole health conversation and build a source-backed audit trail. Keeping facts only in chat context, or writing research notes without importing the patient's own reports, is not enough.

## Every turn

1. **Capture the conversation.**
   - With a connector, call `capture_conversation` with a stable `session_id` for this conversation (a UUID), a `client` name such as `claude.ai` or `chatgpt`, and the user-facing messages since the last capture. Each message has `role`, exact `text`, an ISO `timestamp` when known, and a stable `message_ref` when the client exposes one.
   - In a local Claude Code or Codex session, run `healthdb capture-conversation --session /absolute/path/to/session.jsonl`.
   - Repeat captures are no-ops for messages already archived.
   - A connector capture stores the messages at once as the conversation's source. They become searchable archive messages at the next `record_narrative`, `record_medication` or other write. When nothing will be recorded and the messages should be searchable now, pass `project: true`, which takes longer.
2. **Review the user's new statements for structured facts.** Record them with the `health-record-narrative` or `health-record-medication` skill.
3. **Read the facts back** with the query tools before saying anything was saved.

## Preserve faithfully

- Keep exact wording, original roles and timestamps. Never paraphrase a patient statement inside a quote.
- Preserve clarification questions and the answers to them.
- When a later report corrects a dose, date or interpretation, keep the earlier version. Record a correction as a correction; never overwrite.
- Never record a proposed change as an action taken. Assistant advice and hypotheses never become patient diagnoses, medication use or completed treatment changes.
- If earlier messages are unavailable, say so. Don't claim coverage you don't have.
