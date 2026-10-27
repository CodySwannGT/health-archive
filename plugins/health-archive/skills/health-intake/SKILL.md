---
name: health-intake
description: Run the first-visit intake that personalizes the daily check-in, or a catch-up for an archive that already holds records, and record the agreed tracking plan. Use when check_in reports intake_needed, when a new user starts, or when the user wants to redo or change what the daily check-in asks.
---

# First-visit intake

The intake is a one-time visit, 10 to 15 minutes, like a new-patient appointment. It learns what the person wants to watch and ends with a daily check-in plan they approved. The daily check-in then asks the same things the same way every day, which is what lets connections form.

Call `intake` first. Each section says what the archive already knows (`known`). Ask only what is missing. For a catch-up (`catch_up: true`), confirm what is known instead of asking from scratch.

## Run it

1. **Say what this is and how long it takes**, and offer the two ways: one section at a time, or `questionnaire` as one numbered list they answer by number.
2. **Go through the sections in order:** goals, symptoms, medications, history, habits, equipment, basics, preferences.
   - Ask one section at a time. Follow up only on what the answer leaves unclear.
   - Never ask what an earlier answer already settled.
   - Check `profile` and the clinical records before asking history or basics.
3. **Record the answers** with the usual skills, as quote-backed records. Use `health-record-narrative` for symptoms, habits, history and basics, and `health-record-medication` for regimens. Suspected triggers are `patient_hypothesis`, never findings.
4. **Propose the daily plan** from the `tracking_plan` section, as a short numbered list:
   - The standard core is always asked and needs no record: the urgent screen, anything new, sleep, yesterday's workout, medication changes and as-needed doses.
   - One measurement for each piece of equipment they have. Blood pressure is one item, `blood_pressure`.
   - One exposure for each habit they report, such as alcohol or caffeine.
   - Each current symptom, by its `symptom_key`.
   - At most three tailored questions tied to their goals, medications or suspected triggers. For each one, say why you suggest it. Keep the wording exact, because it will be asked the same way every day.
5. **Record what they approve.** Use `record_narrative` with one `check_in_tracking` record per approved item, plus a `setting` record for `mode` and one for `check_in_time`. The source text is the proposal and their approval, with the approving words quoted. Use `proposed_by: assistant_approved_by_patient` for your suggestions and `patient` for their own requests. Record nothing they declined.
6. **Read back** the plan from `check_in`. Then say when the first check-in is, and that they can change the plan any time.

## Later changes

- When they ask to stop or start tracking something, record a `check_in_tracking` record with `action` stop or track. Never rewrite an earlier one.
- When you think a new item would help, for example after a new medication, propose it with the reason. Record it only after they agree.
