---
name: health-daily-check-in
description: Run the user's morning health check-in like a careful triage nurse, then record every answer, including "no", as source-backed records. Use when the user asks to check in, log their day or morning, or says "do my check-in".
---

# Morning check-in

Treat the check-in as a short daily visit with a good triage nurse: safety first, one question at a time, every answer anchored in time, and nothing interpreted until the end. It should take three to five minutes.

Call `check_in` first. It returns the steps in order, already tailored to the user's records: which measurements they track, whether they drink, which symptoms to ask about and what each one was last time, and which days have no check-in. Follow it. Skip nothing it includes and add nothing it leaves out, apart from follow-ups on what the user raises.

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
9. **Symptoms, one at a time.** "Do you still have …?"
   - Yes: when (last night, this morning, all day), severity 0 to 10, better, same or worse. For bowel symptoms, the Bristol type (1 separate hard lumps, 2 lumpy sausage, 3 cracked sausage, 4 smooth sausage, 5 soft blobs, 6 mushy, 7 liquid).
   - No, and it was present last time (`if_no` says so): "When did it stop?"
   - No otherwise: a plain no for today.
10. **Missed days.** First ask whether they want to fill them in; they may not remember. Only on yes, ask about the days and items they recall.
11. **Close.** Read back everything you will record, get a yes, record it, then call `associations` and tell them what changed in a line or two, with any safety note.

## Recording

Record once, after the read-back, as one narrative source named `YYYY-MM-DD-morning-check-in`. The source text is the check-in transcript with each turn on its own line, `Q:` for your questions and `A:` for their answers, verbatim. Quote question-and-answer pairs as evidence, so a bare "no" is still unique.

| Answer | Record |
| --- | --- |
| Weight, blood pressure, pulse | `anthropometric_measurement`, `vital_sign_measurement` (pair the blood pressure components), dated today |
| Sleep | `sleep_observation` dated today (the morning it ended) |
| Workout | `exercise_session` dated yesterday, one per session with `modality` and `duration`; `status: none_reported` with null modality and duration for no workout |
| Drinks | `reported_exposure` with a drink-count `amount`; zero drinks is `assertion: absent` |
| Symptom yes | `symptom_observation`, `assertion: present`, `temporal_scope: dated_episode`, `occurrence` on the day, `time_of_day`, `numeric_severity` (`{"value": 4, "scale": "0-10", "source_text": "4"}`), and `stool_form` (`{"bristol_type": 2, "source_text": "2"}`) for stool |
| Symptom no | `symptom_observation`, `assertion: absent`, `occurrence` today, or the day it stopped |
| Medication change, as-needed dose | `record_medication` (`medication_change`, `medication_administration`) |

Missed-day answers carry "recalled on DATE" in `source_context`. Dry run, record, then read back as the narrative skill describes before saying anything was saved.
