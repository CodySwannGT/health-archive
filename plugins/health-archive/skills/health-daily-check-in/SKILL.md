---
name: health-daily-check-in
description: Run the user's morning health check-in like a careful triage nurse, then record every answer, including "no", as source-backed records. Use when the user asks to check in, log their day or morning, or says "do my check-in".
---

# Morning check-in

Treat the check-in as a short daily visit with a good triage nurse: safety first, one question at a time, every answer anchored in time, and nothing interpreted until the end. It should take three to five minutes, and never ask the same thing twice.

Call `check_in` first.
- If `intake_needed` is true, the person has no agreed plan yet. Run the `health-intake` skill instead, then today's check-in only if they want it.
- Otherwise it returns the steps in order: a standard core, plus everything their plan and records add (measurements from their equipment, the habits they track, their symptoms with what each was last time, tailored questions, and days with no check-in). Follow it. Skip nothing it includes and add nothing it leaves out, apart from follow-ups on what the user raises.
- Every daily question comes from the plan, so it is asked the same way every day. Never improvise a new daily question. Propose it instead (see "Changing the plan").

## Two ways to run it

- **One at a time**: follow the steps below.
- **All at once**: send `questionnaire` from `check_in` exactly as numbered. They answer by number. Then send one numbered list of only the follow-ups their answers left open, such as a missing date or severity. Then read back.

Use `preferences.mode` from `check_in`, unless the user asks for the other way today. Mention the other way in the opening line, for example: "Say 'all at once' to get every question as a numbered list."

## Never ask the same thing twice

Users tire fast, and a repeated question also invites inconsistent answers.
- Before each question, check what earlier answers already settled. If an answer covers a later item, don't ask it; include it in the read-back for them to confirm. For example, Bristol type 6 already says the stool wasn't watery, and "no black stool" in the urgent screen already answers the dark-stool symptom.
- Ask each symptom group in `groups` as its one question, with the Bristol type once. Then ask only what the answer left open, per the group's `follow_up`.
- An item marked `asked_in_urgent_screen` is answered by the urgent screen. Don't ask it again.
- Don't ask the user for what their records already say. The trend comes from `last_severity`, and when a symptom stopped is asked only where `ask_when_stopped` is true.
- Ask 0 to 10 severity only where `severity` is true. Things seen rather than felt, such as stool shape or urine color, get yes or no and when.

## Pace

Between questions, make no tool call except `capture_conversation` for the turn just finished. It stores the messages in about a second; leave `project` unset, because the save at the close turns them into searchable records. If the check-in ends with nothing to record, make the last capture with `project: true`.

## Steps

1. **Opening.** Say the date and the time it covers since the last check-in.
2. **Urgent screen.** Ask the listed items as one question. On any yes, stop the check-in, advise urgent care or emergency services as fits, and record what they said. Never continue to routine questions after a red flag.
3. **Open question.** "Anything new or different since …, good or bad?" Follow up briefly on anything new.
4. **Measurements.** Only those listed. Ask for this morning's values.
5. **Sleep.** How they slept, bed and wake times, and night wakings.
6. **Workout.** "Did you work out yesterday? If so, describe it, for example 40 minutes of cardio twice, or 90 minutes of weight training."
7. **Medications.** Two questions: anything added, changed or stopped since the last check-in, and any as-needed medications taken since then. Holds and restarts are changes.
8. **Exposures.** Only those listed, such as yesterday's drinks.
9. **Symptoms.** First each group in `groups` as one question, such as all bowel symptoms with one Bristol type (1 separate hard lumps, 2 lumpy sausage, 3 cracked sausage, 4 smooth sausage, 5 soft blobs, 6 mushy, 7 liquid). Then each remaining item, one at a time: "Do you still have …?"
   - Yes: when (last night, this morning, all day). Where `severity` is true, also 0 to 10.
   - No, and `ask_when_stopped` is true: "When did it stop?"
   - No otherwise: a plain no for today.

   Never ask whether something is better, the same or worse, or anything else the records already hold, such as last night's rating or when a symptom last happened. Compare today's rating with `last_severity` yourself and say it in the read-back, for example "night sweats 3/10, down from 7 on Oct 26".
10. **Tailored questions.** Each item in `tailored`, worded exactly as given.
11. **Missed days.** First ask whether they want to fill them in; they may not remember. Only on yes, ask about the days and items they recall.
12. **Review**, when `review` is present: ask each item. Record each agreed change as a `check_in_tracking` record.
13. **Close.** Read back everything you will record, get a yes, record it, then call `associations` and tell them what changed in a line or two, with any safety note.

## Changing the plan

- "Stop asking about X" or "also ask me about Y": record a `check_in_tracking` record (`action` stop or track) with their words as evidence, then confirm.
- When an answer suggests something worth tracking daily, such as a new medication or a new recurring symptom, propose it once with the reason. Record it only if they agree.

## Recording

Record once, after the read-back, as one narrative source named `YYYY-MM-DD-morning-check-in`. The source text is the check-in transcript with each turn on its own line, `Q:` for your questions and `A:` for their answers, verbatim. Quote question-and-answer pairs as evidence, so a bare "no" is still unique.

| Answer | Record |
| --- | --- |
| Weight, blood pressure, pulse | `anthropometric_measurement`, `vital_sign_measurement` (pair the blood pressure components), dated today |
| Sleep | `sleep_observation` dated today (the morning it ended) |
| Workout | `exercise_session` dated yesterday, one per session with `modality` and `duration`; `status: none_reported` with null modality and duration for no workout |
| Drinks | `reported_exposure` with a drink-count `amount`; zero drinks is `assertion: absent` |
| Symptom yes | `symptom_observation`, `assertion: present`, `temporal_scope: dated_episode`, `occurrence` on the day, `time_of_day`, `numeric_severity` (`{"value": 4, "scale": "0-10", "source_text": "4"}`) when asked, and `stool_form` (`{"bristol_type": 2, "source_text": "2"}`) on the stool records the Bristol answer describes |
| Symptom no | `symptom_observation`, `assertion: absent`, `occurrence` today, or the day it stopped |
| Medication change, as-needed dose | `record_medication` (`medication_change`, `medication_administration`) |

Missed-day answers carry "recalled on DATE" in `source_context`. Dry run, record, then read back as the narrative skill describes before saying anything was saved.
