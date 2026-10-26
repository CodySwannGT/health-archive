"""Archive a reviewed symptom extraction, preserving quoted source evidence.

This validates human/assistant-reviewed extractions; it is not an autonomous
diagnostic or natural-language parser. Exact repeat sources are idempotent.
"""
import argparse
import hashlib
import json
import sys
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'database/tools'))
from measurement_corrections import measurement_supersessions


def canonical(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(obj):
    return hashlib.sha256(canonical(obj).encode()).hexdigest()


def save(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or path.read_text() != content:
        path.write_text(content)


def save_json(path, obj):
    save(path, json.dumps(obj, ensure_ascii=False, indent=2) + "\n")


def main():
    cli = argparse.ArgumentParser()
    cli.add_argument("source", type=Path)
    cli.add_argument("extraction", type=Path)
    cli.add_argument("--dry-run", action="store_true")
    cli.add_argument("--revise", action="store_true", help="Explicitly version a reviewed extraction correction, preserving the prior extraction")
    cli.add_argument("--root", type=Path, help="Symptom archive directory to write, such as a database-materialized workspace")
    args = cli.parse_args()
    global ROOT
    if args.root:
        ROOT = args.root.resolve()
    source = args.source.read_text()
    extraction = json.loads(args.extraction.read_text())
    scope = extraction["patient_scope"]
    sha = hashlib.sha256(source.encode()).hexdigest()
    report_id = "narrative_" + digest([scope, sha])[:24]
    entries = extraction["records"]
    keys = [e["record_key"] for e in entries]
    if len(keys) != len(set(keys)):
        raise ValueError("Duplicate extraction keys")
    record_ids = {key: "symrec_" + digest([scope, report_id, key])[:24] for key in keys}
    records = []
    for entry in entries:
        record = {k: v for k, v in entry.items() if k not in ("record_key", "evidence_quotes")}
        # Resolve source-local references to persistent record IDs.
        def resolve(value):
            if isinstance(value, dict):
                return {k: resolve(v) for k, v in value.items()}
            if isinstance(value, list):
                return [resolve(v) for v in value]
            if isinstance(value, str) and value.startswith("@record:"):
                return record_ids[value.removeprefix("@record:")]
            return value
        record = resolve(record)
        evidence = []
        for quote in entry["evidence_quotes"]:
            start = source.find(quote)
            if start < 0:
                raise ValueError("Evidence quote missing from source: " + quote)
            if source.find(quote, start + 1) >= 0:
                raise ValueError("Evidence quote ambiguous; use a longer quote: " + quote)
            evidence.append({"quote": quote, "start_char": start, "end_char": start + len(quote)})
        record.update({"schema_version": "1.0", "patient_scope": scope,
            "record_id": record_ids[entry["record_key"]], "record_key": entry["record_key"],
            "report_id": report_id, "reported_at": extraction["reported_at"],
            "reported_local_date": extraction["reported_local_date"],
            "report_timezone": extraction["report_timezone"], "evidence_basis": "patient_report",
            "provenance": {"source_sha256": sha, "source_path": f"narratives/{report_id}/source.txt", "evidence": evidence}})
        if record["record_type"] == "symptom_observation":
            record["symptom_id"] = "symptom_" + digest([scope, record["details"]["symptom_key"]])[:24]
        record["content_sha256"] = digest({k: v for k, v in record.items() if k not in ("provenance", "content_sha256")})
        records.append(record)
    validator = Draft202012Validator(json.loads((ROOT / "symptom.schema.json").read_text()), format_checker=FormatChecker())
    validator.check_schema(validator.schema)
    for record in records:
        validator.validate(record)
    for record in records:
        if record['record_type'] == 'body_composition_measurement':
            bounds = record['details']['value_range']
            if bounds['minimum'] > bounds['maximum']:
                raise ValueError('Body composition range minimum exceeds maximum')
        if record['record_type'] == 'vital_sign_measurement':
            expected = 'beats/min' if record['details']['metric'] == 'pulse' else 'mmHg'
            if record['details']['unit'] != expected:
                raise ValueError('Vital sign unit does not match its metric')
            for paired in record['details']['paired_component_record_ids']:
                if paired not in record_ids.values() or paired == record['record_id']:
                    raise ValueError('Blood pressure component pair must reference another record in this report')
        if record['record_type'] == 'sleep_observation':
            d = record['details']
            if (d['value'] is None) != (d['unit'] is None) or (d['value'] is None) != (d['value_basis'] is None):
                raise ValueError('Sleep duration, unit and value basis are reported together or not at all')
            if d['value'] is None and d['quality'] is None and d['awakenings'] is None and not d['quality_source']:
                raise ValueError('A sleep record needs a duration, quality or awakenings')
        if record['record_type'] == 'exercise_routine':
            for group in record['details']['schedule']:
                for activity in group['activities']:
                    duration = activity['duration_per_session']
                    if duration and duration['minimum'] > duration['maximum']:
                        raise ValueError('Exercise duration minimum exceeds maximum')
    counts = {kind: sum(r["record_type"] == kind for r in records) for kind in sorted({r["record_type"] for r in records})}
    archive = ROOT / "narratives" / report_id
    existing = [json.loads(line) for line in (ROOT / "records.jsonl").read_text().splitlines()] if (ROOT / "records.jsonl").exists() else []
    other = [r for r in existing if r["report_id"] != report_id]
    prior = {r["record_id"]: r for r in existing if r["report_id"] == report_id}
    measurement_supersessions(other + records)
    changed_extraction = bool(prior and {r["record_id"]: r for r in records} != prior)
    if changed_extraction and not args.revise:
        raise ValueError("This source is already archived with a different extraction; preserve and explicitly version its correction.")
    result = {"report_id": report_id, "counts": counts, "new_records": sum(r["record_id"] not in prior for r in records),
              "changed_records": sum(r["record_id"] in prior and r != prior[r["record_id"]] for r in records),
              "unchanged_records": sum(r["record_id"] in prior and r == prior[r["record_id"]] for r in records)}
    if args.dry_run:
        print(json.dumps(result, indent=2))
        return
    if changed_extraction:
        old_extraction = json.loads((archive / "extraction.json").read_text())
        old_version = digest(old_extraction)
        save_json(archive / "revisions" / old_version / "extraction.json", old_extraction)
        save(archive / "revisions" / old_version / "records.jsonl", (archive / "records.jsonl").read_text())
        new_version = digest(extraction)
        save_json(archive / "revisions" / new_version / "extraction.json", extraction)
        save(archive / "revisions" / new_version / "records.jsonl", "".join(canonical(r) + "\n" for r in records))
    save(archive / "source.txt", source)
    save_json(archive / "extraction.json", extraction)
    save(archive / "records.jsonl", "".join(canonical(r) + "\n" for r in records))
    all_records = sorted(other + records, key=lambda r: (r["reported_at"], r["report_id"], r["record_key"]))
    save(ROOT / "records.jsonl", "".join(canonical(r) + "\n" for r in all_records))
    latest_states = {}
    for r in all_records:
        if r["record_type"] == "symptom_observation" and r["details"]["temporal_scope"] in ("current_state", "treatment_response") and r["details"]["current_presence"] != "unknown":
            latest_states[r["details"]["symptom_key"]] = {"symptom_id": r["symptom_id"], "record_id": r["record_id"],
                "label": r["details"]["label"], "assertion": r["details"]["assertion"],
                "current_presence": r["details"]["current_presence"], "reported_at": r["reported_at"]}
    save_json(ROOT / "current-state.json", {"schema_version": "1.0", "patient_scope": scope,
        "last_report_at": max(r["reported_at"] for r in all_records),
        "interpretation": "Last explicitly reported states, each with its own report time. Not a live assessment. Unreported symptoms remain unknown.",
        "reported_states": [latest_states[k] for k in sorted(latest_states)]})
    for filename, kind in [("observations.jsonl", "symptom_observation"), ("patterns.jsonl", "recurring_pattern"),
                           ("exposures.jsonl", "reported_exposure"), ("associations.jsonl", "reported_association"),
                           ("hypotheses.jsonl", "patient_hypothesis"), ("medication-responses.jsonl", "medication_response"),
                           ("care-preferences.jsonl", "care_preference"), ("exercise-routines.jsonl", "exercise_routine"), ("anthropometrics.jsonl", "anthropometric_measurement"), ("body-composition.jsonl", "body_composition_measurement"), ("demographics.jsonl", "patient_demographics"), ("measurement-contexts.jsonl", "measurement_context"), ("vital-signs.jsonl", "vital_sign_measurement"), ("sleep.jsonl", "sleep_observation")]:
        save(ROOT / filename, "".join(canonical(r) + "\n" for r in all_records if r["record_type"] == kind))
    inventory = json.loads((ROOT / "inventory.json").read_text()) if (ROOT / "inventory.json").exists() else {"schema_version": "1.0", "patient_scope": scope, "reports": []}
    if not any(r["report_id"] == report_id for r in inventory["reports"]):
        inventory["reports"].append({"report_id": report_id, "reported_at": extraction["reported_at"],
            "reported_local_date": extraction["reported_local_date"], "source_sha256": sha,
            "source_path": f"narratives/{report_id}/source.txt", "counts": counts})
    inventory["total_records"] = len(all_records)
    inventory["unique_symptom_ids"] = len({r["symptom_id"] for r in all_records if r["record_type"] == "symptom_observation"})
    save_json(ROOT / "inventory.json", inventory)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
