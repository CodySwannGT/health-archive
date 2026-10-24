"""Validate captured OneBlood chart data, retain versions and upsert observations.

Collection happens through the signed-in Chrome UI. This tool is local-only.
"""
import argparse
import collections
import csv
from datetime import datetime
import hashlib
import io
import json
import sys
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parents[1] / "database/tools"))
from common import current_account  # noqa: E402
METRICS = {
    "pulse": ("Pulse", "Heart Beats Per Minute", "beats/min"),
    "systolic_blood_pressure": ("Systolic", "mmHg", "mmHg"),
    "diastolic_blood_pressure": ("Diastolic", "mmHg", "mmHg"),
    "temperature": ("Temperature", "Temperature (Fahrenheit)", "degF"),
    "cholesterol": ("Cholesterol", "Cholesterol (mg/dL)", "mg/dL"),
    "iron_count": ("Iron Count", "Iron Count (g/dL)", "g/dL"),
}


def canonical(obj):
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def save(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or path.read_text() != text:
        path.write_text(text)


def save_json(path, obj):
    save(path, json.dumps(obj, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def load_jsonl(path):
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def main():
    cli = argparse.ArgumentParser()
    cli.add_argument("chart_data", type=Path)
    cli.add_argument("--dry-run", action="store_true")
    args = cli.parse_args()
    raw_bytes = args.chart_data.read_bytes()
    raw = json.loads(raw_bytes)
    assert raw["patient_scope"] in current_account().patient_scopes and raw["source"] == "oneblood"
    datetime.strptime(raw["captured_on"], "%Y-%m-%d")
    capture_sha = sha(raw_bytes)
    capture_id = "capture_" + capture_sha[:24]
    observations, excluded = [], []
    seen_pages = set()
    for page in raw["pages"]:
        if page["page_key"] in seen_pages:
            raise ValueError("Duplicate source page")
        seen_pages.add(page["page_key"])
        dates = [datetime.strptime(d, "%m/%d/%Y").date().isoformat() for d in page["dates_source"]]
        if len(dates) != len(set(dates)) or dates != sorted(dates):
            raise ValueError("Ambiguous or unordered dates; reconcile before assigning observation identities")
        for series_index, series in enumerate(page["series"]):
            if len(series["values"]) != len(dates):
                raise ValueError("Chart category/series length mismatch")
            if series["series_kind"] == "chart_reference_line":
                excluded.append({"page_key": page["page_key"], "name_source": series["name_source"], "points": len(dates), "reason": "Chart reference line, not a patient measurement"})
                continue
            if series["series_kind"] != "patient_measurement" or series["metric"] not in METRICS:
                raise ValueError("Unknown series; review before import")
            metric = series["metric"]
            expected_name, expected_unit, normalized_unit = METRICS[metric]
            if series["name_source"] != expected_name or series["unit_source"] != expected_unit:
                raise ValueError("Source metric label or unit changed; review required")
            for point_index, (date, value) in enumerate(zip(dates, series["values"])):
                if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
                    raise ValueError("Unsupported numeric value")
                identity = [raw["patient_scope"], "oneblood", date, metric]
                observation_id = "vital_" + sha(canonical(identity).encode())[:24]
                date_group_id = "dategroup_" + sha(canonical(identity[:3]).encode())[:24]
                semantic = {"patient_scope": raw["patient_scope"], "source": "oneblood", "measurement_date": date,
                    "metric": metric, "name_source": expected_name, "unit": normalized_unit,
                    "unit_source": expected_unit, "value": value, "status": "reported" if value is not None else "not_provided"}
                observations.append({"schema_version": "1.0", "observation_id": observation_id,
                    "date_group_id": date_group_id, **semantic, "content_sha256": sha(canonical(semantic).encode()),
                    "measurement_time": None, "measurement_timezone": None, "clinical_terminology_code": None,
                    "setting": "donor_health_summary", "captured_on": raw["captured_on"],
                    "provenance": {"capture_id": capture_id, "chart_data_sha256": capture_sha,
                        "chart_data_path": f"captures/{capture_id}/chart-data.json", "page_key": page["page_key"],
                        "source_url": page["source_url"], "chart_title_source": page["title_source"],
                        "series_index": series_index, "point_index": point_index,
                        "date_source": page["dates_source"][point_index], "value_source": value,
                        "extraction_method": "read_only_displayed_chart_interface"}})
    if seen_pages != {"pulse", "blood-pressure", "temperature", "cholesterol", "iron-count"}:
        raise ValueError("Incomplete five-page capture")
    if len(observations) != len({r["observation_id"] for r in observations}):
        raise ValueError("Duplicate observation identities")
    validator = Draft202012Validator(json.loads((ROOT.parent / "observation.schema.json").read_text()), format_checker=FormatChecker())
    for record in observations:
        validator.validate(record)
    prior_records = load_jsonl(ROOT / "observations.jsonl")
    prior = {r["observation_id"]: r for r in prior_records}
    delta = {"new": [], "changed": [], "unchanged": [], "absent_from_latest_capture": []}
    for record in observations:
        key = record["observation_id"]
        kind = "new" if key not in prior else "unchanged" if prior[key]["content_sha256"] == record["content_sha256"] else "changed"
        delta[kind].append(key)
    seen = {r["observation_id"] for r in observations}
    delta["absent_from_latest_capture"] = sorted(set(prior) - seen)
    coverage = dict(sorted(collections.Counter(r["metric"] for r in observations).items()))
    result = {"capture_id": capture_id, "capture_observations": len(observations), "coverage": coverage,
              "recorded_dates": len({r["measurement_date"] for r in observations}), "delta_counts": {k: len(v) for k,v in delta.items()}}
    if args.dry_run:
        print(json.dumps(result, indent=2))
        return
    capture = ROOT / "captures" / capture_id
    source_copy = capture / "chart-data.json"
    source_copy.parent.mkdir(parents=True, exist_ok=True)
    if source_copy.exists():
        assert source_copy.read_bytes() == raw_bytes
    else:
        source_copy.write_bytes(raw_bytes)
    save(capture / "observations.jsonl", "".join(canonical(r) + "\n" for r in observations))
    save_json(capture / "excluded-reference-series.json", excluded)
    if not (capture / "delta.json").exists():
        save_json(capture / "delta.json", delta)
    for record in observations:
        version = ROOT / "versions" / record["observation_id"] / (record["content_sha256"] + ".json")
        if not version.exists():
            save_json(version, record)
        prior[record["observation_id"]] = record
    # Preserve prior measurements missing from a later portal capture.
    current = sorted(prior.values(), key=lambda r: (r["measurement_date"], r["metric"]))
    save(ROOT / "observations.jsonl", "".join(canonical(r) + "\n" for r in current))
    date_groups = collections.defaultdict(list)
    for r in current:
        date_groups[r["measurement_date"]].append(r)
    encounters = [{"schema_version": "1.0", "date_group_id": rs[0]["date_group_id"], "patient_scope": rs[0]["patient_scope"], "source": "oneblood",
        "measurement_date": date, "observation_ids": [r["observation_id"] for r in rs],
        "grouping_basis": "same source and calendar date; source encounter ID and measurement time unavailable"}
        for date, rs in sorted(date_groups.items())]
    save(ROOT / "encounters.jsonl", "".join(canonical(r) + "\n" for r in encounters))
    buffer = io.StringIO(newline="")
    columns = ["date", *METRICS]
    writer = csv.DictWriter(buffer, fieldnames=columns)
    writer.writeheader()
    for date, rs in sorted(date_groups.items()):
        writer.writerow({"date": date, **{r["metric"]: r["value"] for r in rs}})
    save(ROOT / "history.csv", buffer.getvalue())
    inventory = json.loads((ROOT / "inventory.json").read_text()) if (ROOT / "inventory.json").exists() else {"schema_version": "1.0", "source": "oneblood", "patient_scope": rs[0]["patient_scope"], "captures": []}
    if not any(c["capture_id"] == capture_id for c in inventory["captures"]):
        inventory["captures"].append({"capture_id": capture_id, "captured_on": raw["captured_on"], "chart_data_sha256": capture_sha,
            "capture_path": str(capture.relative_to(ROOT)), "coverage": coverage, "recorded_dates": result["recorded_dates"]})
    inventory.update({"current_capture_id": capture_id, "observations": len(current), "recorded_dates": len(encounters),
        "earliest_date": min(date_groups), "latest_date": max(date_groups), "coverage": dict(sorted(collections.Counter(r["metric"] for r in current).items())),
        "excluded_reference_points": sum(e["points"] for e in excluded), "missing_from_capture_retained": delta["absent_from_latest_capture"],
        "schema_path": "../observation.schema.json", "observation_path": "observations.jsonl", "csv_path": "history.csv"})
    save_json(ROOT / "inventory.json", inventory)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
