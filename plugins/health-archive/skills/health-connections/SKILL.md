---
name: health-connections
description: Draw connections in the user's health records, such as whether a medication, alcohol or a food tracks a symptom, weight or blood pressure, and run a daily check-in. Use when the user asks what might be causing, helping or worsening something, asks about trends after a change, or wants to log their day.
---

# Draw connections

Drawing connections is the archive's purpose. Being careful about what is known is how it does that. The connections engine recomputes evidence-graded associations from the user's dated records every time the archive changes. Your job is to relay them faithfully, add what is known about the exposures, and close the gaps in the data.

## Tools

| Need | Tool |
| --- | --- |
| Associations for a symptom, measurement, medication or exposure | `associations` with `concept` (ID or label) and `min_strength` |
| How others' records look, as background | `pooled_patterns`, and `pooled_context` on each association |
| What to ask in a check-in | `check_in` |
| Pooling status and consent text | `pooling`; change it only on request with `set_pooling` |

## Methods

- `exposure_episode_contrast`: how often a symptom occurs on days with and without an exposure, the same day or the next. It needs explicit symptom-free and exposure-free days.
- `constant_background`: an exposure present on every observed day, symptom-free days included, or stopped before all of them, cannot explain why a symptom comes and goes. It may still affect susceptibility.
- `exposure_series_change`: how a measurement series such as weight moves while a medication is on versus off, across starts, stops and restarts.

## Rules

- Relay the strength word as the engine gives it: `insufficient` ("not enough data yet"), `weak` ("may be associated"), `moderate` ("may be contributing"), `strong` ("consistently associated"). Never upgrade it, and never call an association a cause or a diagnosis.
- Compare candidates using the `competing` list. When one exposure varies with the symptom and others are constant background, say so: that is the most useful thing the engine finds.
- State the limitations that matter, especially few symptom-free days, few measurements since a change, and other changes made at the same time.
- Add pharmacology and the medical literature as context, labelled as general knowledge, not as a finding in the user's records. A plausible mechanism makes an association more credible; it does not prove it.
- Pooled patterns are aggregates of at least five opted-in people. Use them as background ("this pattern is common among people who share it"), never as evidence about this user, and never imply anyone's identity.
- Suggest a dechallenge or rechallenge, such as pausing alcohol for a week, only as an idea to discuss. Medication changes are for the user and their clinician; advice is never a completed action.
- Urgent symptoms come before pattern-finding.

## Daily check-in

1. Call `check_in`. It lists the symptoms and exposures in open associations, and the recent days with no report.
2. Ask about each briefly, including how much for an exposure. Ask about missed days only if the user can recall them.
3. Record every answer with `record_narrative`, including absences ("no swelling today", "no alcohol") as records with assertion `absent`. An unreported day is unknown, not symptom-free.
4. Read the records back, then call `associations` again and tell the user what changed.

## Pooling consent

Pooling is off unless the user turns it on. Show the consent text from `pooling` before calling `set_pooling`, and only on the user's explicit request. Pooled patterns are never published; they only give the assistant better pattern recognition for each user.
