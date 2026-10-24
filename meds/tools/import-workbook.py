"""Read a medication workbook into versioned JSON/JSONL without editing Excel.

Uses standard-library OOXML extraction and jsonschema validation. Clinical
names, categories, timing, notes, and unknowns remain source-preserving.
"""
import argparse
import collections
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
import zipfile
import xml.etree.ElementTree as ET

from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).resolve().parents[1]
# Patient record slug; set from --patient-scope or the current account. It scopes stable medication IDs.
SCOPE = None
PARSER_VERSION = "1.0"
NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
HEADERS = ["Category", "Name", "Total Dose", "Unit", "Frequency", "Timing", "Pills / Admin",
           "Total Pills / Day", "Dose / Pill or Admin", "Status", "Source / Notes"]


def canonical(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def hash_bytes(data):
    return hashlib.sha256(data).hexdigest()


def identity(prefix, *parts):
    return prefix + "_" + hash_bytes(canonical([SCOPE, *parts]).encode())[:24]


def name_key(text):
    return " ".join(text.casefold().split())


def save(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(obj, ensure_ascii=False, indent=2) + "\n"
    if not path.exists() or path.read_text() != text:
        path.write_text(text)


def save_jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(canonical(record) + "\n" for record in records)
    if not path.exists() or path.read_text() != text:
        path.write_text(text)


def read_workbook(path):
    data = path.read_bytes()
    sha = hash_bytes(data)
    sheets = []
    with zipfile.ZipFile(path) as archive:
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        relations = {r.attrib["Id"]: r.attrib["Target"] for r in ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))}
        shared = []
        if "xl/sharedStrings.xml" in archive.namelist():
            shared = ["".join(n.text or "" for n in si.findall(".//m:t", NS))
                      for si in ET.fromstring(archive.read("xl/sharedStrings.xml")).findall("m:si", NS)]
        for sheet in workbook.findall("m:sheets/m:sheet", NS):
            target = relations[sheet.attrib["{" + REL + "}id"]]
            target = target.lstrip("/") if target.startswith("/") else "xl/" + target
            xml = ET.fromstring(archive.read(target))
            cells = {}
            for cell in xml.findall("m:sheetData/m:row/m:c", NS):
                cell_type = cell.attrib.get("t", "n")
                formula = cell.find("m:f", NS)
                raw_value = cell.find("m:v", NS)
                inline = cell.find("m:is", NS)
                raw = raw_value.text if raw_value is not None else None
                if cell_type == "s" and raw is not None:
                    value = shared[int(raw)]
                elif inline is not None:
                    value = "".join(n.text or "" for n in inline.findall(".//m:t", NS))
                elif cell_type == "n" and raw is not None:
                    decimal = Decimal(raw)
                    value = int(decimal) if decimal == decimal.to_integral_value() else float(decimal)
                elif cell_type == "b" and raw is not None:
                    value = raw == "1"
                else:
                    value = raw
                cells[cell.attrib["r"]] = {"value": value, "raw_value": raw, "type": cell_type,
                    "style_id": cell.attrib.get("s"), "formula": formula.text if formula is not None else None}
            sheets.append({"name": sheet.attrib["name"], "state": sheet.attrib.get("state", "visible"),
                "source_xml": target, "cells": cells,
                "merged_ranges": [m.attrib["ref"] for m in xml.findall("m:mergeCells/m:mergeCell", NS)],
                "row_count": max((int(re.search(r"\d+", c)[0]) for c in cells), default=0)})
        tables = [{"name": t.attrib.get("name"), "range": t.attrib.get("ref")}
                  for member in archive.namelist() if member.startswith("xl/tables/") and member.endswith(".xml")
                  for t in [ET.fromstring(archive.read(member))]]
    return {"source_workbook_sha256": sha, "original_filename": path.name, "original_path": str(path.resolve()),
            "sheets": sheets, "tables": tables, "parser_version": PARSER_VERSION}


def date_value(text):
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return {"date": text, "precision": "day", "source_text": text, "year": int(text[:4]),
                "month": int(text[5:7]), "qualifier": None}
    match = re.fullmatch(r"Early Sep (\d{4})", text, re.I)
    if match:
        return {"date": None, "precision": "approximate_month", "source_text": text,
                "year": int(match[1]), "month": 9, "qualifier": "early"}
    return {"date": None, "precision": "text", "source_text": text, "year": None, "month": None, "qualifier": None}


def quantity(number, unit, period, basis, cells=None):
    if number is None:
        return None
    if unit is None:
        raise ValueError("Quantity has an amount without a unit")
    return {"value": number, "unit": unit, "period": period, "basis": basis, "source_cells": cells or []}


def normalize_unit(unit_source):
    if not unit_source:
        return None, None
    match = re.fullmatch(r"(mg|g|mcg|IU)(?:/(day|week))?", unit_source, re.I)
    if not match:
        raise ValueError("Unsupported source dose unit: " + unit_source)
    return ("IU" if match[1].casefold() == "iu" else match[1].casefold()), match[2]


def timing(text):
    result = []
    for part in text.split("+"):
        match = re.fullmatch(r"\s*(?:(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+)?(AM|PM)\s*", part)
        if match:
            result.append({"weekday": match[1], "time_of_day": match[2]})
        elif part.strip() != "Daily":
            raise ValueError("Unsupported timing: " + text)
    return result


def change_state(text):
    # This extracts only quantities and schedules explicitly written in the log.
    result = {"text": text, "status": None, "quantities": [], "scheduled_administrations": []}
    if text == "Not listed":
        result["status"] = "not_listed"
    elif text.startswith("Stopped"):
        result["status"] = "stopped"
    elif "active" in text.casefold():
        result["status"] = "active"
    elif text.startswith("Planned discontinuation"):
        result["status"] = "planned_discontinuation"
    for match in re.finditer(r"(\d+(?:\.\d+)?)\s*(mg|mcg|g|IU)/(day|week)\b", text, re.I):
        unit, _ = normalize_unit(match[2])
        result["quantities"].append(quantity(float(match[1]), unit, match[3], "change_log_text"))
    for match in re.finditer(r"(\d+(?:\.\d+)?)\s*(mg|mcg|g|IU)\s+(daily|every morning)\b", text, re.I):
        unit, _ = normalize_unit(match[2])
        result["quantities"].append(quantity(float(match[1]), unit, "day", "change_log_text"))
    for match in re.finditer(r"(\d+(?:\.\d+)?)\s*(mg|mcg|g|IU)\s+(Thu|Sun)\s+(AM|PM)\b", text, re.I):
        unit, _ = normalize_unit(match[2])
        result["scheduled_administrations"].append({"weekday": match[3].title(), "time_of_day": match[4].upper(),
            "quantity": quantity(float(match[1]), unit, "administration", "change_log_text")})
    return result


def semantic_hash(record):
    # A newer observation date or relocated source row is not a regimen change.
    # Exact source locations remain on the exported record and source snapshot.
    ignored = {"provenance", "content_sha256", "source_order", "source_cells",
               "as_of_date", "as_of_date_basis"}
    def semantic(value):
        if isinstance(value, dict):
            return {k: sorted(v) if k == "change_event_ids" else semantic(v)
                    for k, v in value.items() if k not in ignored}
        if isinstance(value, list):
            return [semantic(v) for v in value]
        return value
    return hash_bytes(canonical(semantic(record)).encode())


def import_data(path, as_of):
    raw = read_workbook(path)
    sheets = {s["name"]: s for s in raw["sheets"]}
    if set(sheets) != {"Current Regimen", "Change Log"}:
        raise ValueError("Unexpected sheets: inspect before importing " + str(list(sheets)))
    formulas = [(s["name"], ref) for s in raw["sheets"] for ref,c in s["cells"].items() if c["formula"] is not None]
    errors = [(s["name"], ref) for s in raw["sheets"] for ref,c in s["cells"].items() if c["type"] == "e"]
    if formulas or errors:
        raise ValueError("Formula/error cells require verified calculation before import: " + str(formulas + errors))
    cells = sheets["Current Regimen"]["cells"]
    get = lambda ref: cells.get(ref, {}).get("value")
    if [get(f"{chr(65+i)}4") for i in range(11)] != HEADERS:
        raise ValueError("Regimen headers changed; review before importing")
    convention = get("A2")
    if convention != 'Dose convention: listed mg/g is the TOTAL daily dose. "BOTH" means that total is split between AM and PM.':
        raise ValueError("Dose convention changed; review before interpreting doses")
    sha = raw["source_workbook_sha256"]
    provenance = lambda sheet, refs: {"workbook_sha256":sha, "sheet":sheet, "cells":refs,
        "source_values":{ref:sheets[sheet]["cells"].get(ref,{"value":None})["value"] for ref in refs}}
    medications, issues = [], []
    for row in range(5, sheets["Current Regimen"]["row_count"] + 1):
        refs = [f"{chr(65+i)}{row}" for i in range(11)]
        values = [get(ref) for ref in refs]
        if not any(v is not None for v in values):
            continue
        category, name, total, unit_source, frequency, time_source, pills_admin, pills_day, dose_unit, status, notes = values
        if not name or status not in ("Active", "Stopped"):
            raise ValueError("Missing/unsupported medication identity or status at row " + str(row))
        unit, unit_period = normalize_unit(unit_source)
        period = unit_period or ("day" if unit in ("mg", "g") else None)
        dose_basis = "source_unit" if unit_period else "workbook_daily_total_convention"
        route_family = "injection" if "/ Injection" in category else "oral" if category.startswith("Oral /") or "/ Oral" in category else "unspecified"
        if frequency == "Daily, split twice":
            frequency_period, times, frequency_basis = "day", 2, "explicit_split_twice"
        elif frequency == "2x weekly":
            frequency_period, times, frequency_basis = "week", 2, "explicit_two_times_weekly"
        elif frequency == "Daily":
            frequency_period, times, frequency_basis = "day", None, "daily_frequency_only"
            if pills_admin is not None and pills_day is not None:
                times = float(Decimal(str(pills_day))/Decimal(str(pills_admin)))
                frequency_basis = "daily_pill_count_divided_by_pills_per_administration"
        else:
            raise ValueError("Unsupported frequency: " + str(frequency))
        admin_quantity = None
        dose_per_pill = None
        if dose_unit is not None:
            if route_family == "oral":
                dose_per_pill = quantity(dose_unit, unit, "pill", "source_cell", [f"I{row}",f"D{row}"])
                if pills_admin is not None:
                    amount = float(Decimal(str(dose_unit))*Decimal(str(pills_admin)))
                    admin_quantity = quantity(amount, unit, "administration", "dose_per_pill_times_pills_per_administration", [f"I{row}",f"G{row}",f"D{row}"])
            else:
                admin_quantity = quantity(dose_unit, unit, "administration", "source_cell", [f"I{row}",f"D{row}"])
        med_id = identity("med", name_key(name))
        if total is None:
            issues.append({"kind":"missing_total_dose", "medication_id":med_id, "source_cells":[f"C{row}",f"D{row}"],
                           "detail":"Total amount and unit are blank; pill counts remain known."})
        record = {"schema_version":"1.0", "record_type":"medication_regimen", "patient_scope":SCOPE,
            "medication_id":med_id, "name_source":name, "category_source":category, "route_family":route_family,
            "clinical_route_code":None, "terminology_code":None, "status":status.casefold(), "status_source":status,
            "as_of_date":as_of, "as_of_date_basis":"filename" if as_of else "unspecified",
            "dose_scope":"current_recorded_regimen" if status == "Active" else "last_recorded_regimen_before_stop",
            "total_dose":quantity(total,unit,period,dose_basis,[f"C{row}",f"D{row}","A2"]),
            "dose_per_pill":dose_per_pill, "dose_per_administration":admin_quantity,
            "pills_per_administration":pills_admin, "pills_per_day":pills_day,
            "schedule":{"frequency_source":frequency,"timing_source":time_source,"period":frequency_period,
                        "administrations_per_period":times,"frequency_basis":frequency_basis,"timings":timing(time_source)},
            "notes_source":notes,"change_event_ids":[],"known_started_on":None,"known_stopped_on":None,
            "added_to_list_on":None,"provenance":provenance("Current Regimen",refs)}
        medications.append(record)
    if len(medications) != len({m["medication_id"] for m in medications}):
        raise ValueError("Duplicate medication names; reconcile identities before import")
    lookup = {name_key(m["name_source"]):m for m in medications}
    # Brand prefix aliases are supplied by the workbook's explicit parenthesized name.
    for med in medications:
        prefix = re.sub(r" \([^)]*\)$", "", med["name_source"])
        if prefix != med["name_source"]:
            lookup[name_key(prefix)] = med
    log = sheets["Change Log"]["cells"]
    logget = lambda ref: log.get(ref,{}).get("value")
    if [logget(c+"1") for c in "ABCD"] != ["Date","Change","Previous","Current"]:
        raise ValueError("Change-log headers changed")
    changes = []
    for row in range(2,sheets["Change Log"]["row_count"]+1):
        date_source,subject,previous,current = [logget(f"{col}{row}") for col in "ABCD"]
        if not any(v is not None for v in (date_source,subject,previous,current)):
            continue
        if not all(isinstance(v,str) for v in (date_source,subject,previous,current)):
            raise ValueError("Change-log fields need explicit date/text: row " + str(row))
        parsed_date = date_value(date_source)
        if subject == "Dose notation interpretation":
            change_type,med_id,date_role = "notation_clarification",None,"notation_recorded_date"
            med = None
        else:
            if subject.endswith(" stopped"):
                change_type,date_role = "stopped","change_date"
            elif subject.endswith(" started"):
                change_type,date_role = "started","change_date"
            elif subject.endswith(" added"):
                change_type,date_role = "added_to_list","listing_added_date"
            elif current.startswith("Planned discontinuation"):
                change_type,date_role = "planned_discontinuation","plan_recorded_date"
            else:
                change_type,date_role = "regimen_changed","change_date"
            drug_name = re.sub(r" (stopped|started|added)$", "", subject)
            med = lookup.get(name_key(drug_name))
            if not med:
                raise ValueError("Unmatched change-log medication: " + drug_name)
            med_id = med["medication_id"]
        event_id = identity("event",med_id or "global",change_type,date_source)
        event = {"schema_version":"1.0","record_type":"medication_change","patient_scope":SCOPE,
            "event_id":event_id,"medication_id":med_id,"scope":"medication" if med else "global",
            "change_type":change_type,"subject_source":subject,"date":parsed_date,"date_role":date_role,
            "previous":change_state(previous),"current":change_state(current),"planned_effective_time":None,
            "provenance":provenance("Change Log",[f"{c}{row}" for c in "ABCD"])}
        if change_type == "planned_discontinuation":
            event["planned_effective_time"] = {"date":None,"precision":"relative","source_text":current.removeprefix("Planned discontinuation").strip(),
                                               "anchor_date":parsed_date["date"]}
        if parsed_date["precision"] != "day":
            issues.append({"kind":"approximate_event_date","event_id":event_id,"source_cells":[f"A{row}"],
                           "detail":"The source does not specify an exact day; no day or numeric date bounds were invented."})
        if med:
            med["change_event_ids"].append(event_id)
            if change_type == "started":
                med["known_started_on"] = {"event_id":event_id,"date":parsed_date}
            elif change_type == "stopped":
                med["known_stopped_on"] = {"event_id":event_id,"date":parsed_date}
            elif change_type == "added_to_list":
                med["added_to_list_on"] = {"event_id":event_id,"date":parsed_date}
        changes.append(event)
    if len(changes) != len({e["event_id"] for e in changes}):
        raise ValueError("Ambiguous duplicate change events; reconcile before import")
    for record in medications + changes:
        record["content_sha256"] = semantic_hash(record)
    return raw,medications,changes,issues


def main():
    cli = argparse.ArgumentParser()
    cli.add_argument("workbook",type=Path)
    cli.add_argument("--as-of",help="Explicit snapshot date when absent from the filename")
    cli.add_argument("--dry-run",action="store_true")
    cli.add_argument("--patient-scope",help="Patient record slug; defaults to the current account's")
    args = cli.parse_args()
    global SCOPE
    if args.patient_scope:
        SCOPE = args.patient_scope
    else:
        import sys
        sys.path.insert(0, str(ROOT.parent / "database/tools"))
        from common import current_account
        SCOPE = current_account().patient_scope
    date_match = re.search(r"(\d{4}-\d{2}-\d{2})",args.workbook.name)
    as_of = args.as_of or (date_match[1] if date_match else None)
    raw,medications,changes,issues = import_data(args.workbook,as_of)
    if args.as_of:
        for med in medications:
            med["as_of_date_basis"] = "explicit_import_argument"
            med["content_sha256"] = semantic_hash(med)
    schema = Draft202012Validator(json.loads((ROOT/"medication.schema.json").read_text()),format_checker=FormatChecker())
    for record in medications+changes:
        schema.validate(record)
    coverage = {"regimen_entries":len(medications),"active_entries":sum(m["status"]=="active" for m in medications),
                "stopped_entries":sum(m["status"]=="stopped" for m in medications),"change_log_entries":len(changes),
                "medication_change_events":sum(e["scope"]=="medication" for e in changes),
                "global_change_events":sum(e["scope"]=="global" for e in changes),"source_sheets":len(raw["sheets"])}
    if args.dry_run:
        print(json.dumps({"coverage":coverage,"issues":issues},indent=2))
        return
    snapshot_id = f"{as_of or 'undated'}_{raw['source_workbook_sha256'][:24]}"
    snapshot = ROOT/"snapshots"/snapshot_id
    source_copy = ROOT/"source-workbooks"/(raw["source_workbook_sha256"]+".xlsx")
    source_copy.parent.mkdir(parents=True,exist_ok=True)
    if source_copy.exists():
        assert hash_bytes(source_copy.read_bytes()) == raw["source_workbook_sha256"]
    else:
        source_copy.write_bytes(args.workbook.read_bytes())
    prior = json.loads((ROOT/"inventory.json").read_text()) if (ROOT/"inventory.json").exists() else None
    save(snapshot/"workbook.json",raw)
    save_jsonl(snapshot/"medications.jsonl",medications)
    save_jsonl(snapshot/"change-log.jsonl",changes)
    save(snapshot/"issues.json",issues)
    prior_hashes = {}
    if prior:
        for filename,id_field in [("medications.jsonl","medication_id"),("change-log.jsonl","event_id")]:
            prior_hashes.update({r[id_field]:r["content_sha256"] for r in [json.loads(l) for l in (ROOT/filename).read_text().splitlines()]})
    delta = {"new":[],"changed":[],"unchanged":[],"absent_from_latest_source":[]}
    seen = set()
    for record in medications+changes:
        key = record.get("medication_id") if record["record_type"]=="medication_regimen" else record["event_id"]
        seen.add(key)
        kind = "new" if key not in prior_hashes else "unchanged" if prior_hashes[key]==record["content_sha256"] else "changed"
        delta[kind].append(key)
    delta["absent_from_latest_source"] = sorted(set(prior_hashes)-seen)
    save_jsonl(ROOT/"medications.jsonl",medications)
    save_jsonl(ROOT/"active-regimen.jsonl",[m for m in medications if m["status"]=="active"])
    save_jsonl(ROOT/"change-log.jsonl",changes)
    save(ROOT/"issues.json",issues)
    versions = prior["snapshots"] if prior else []
    if not any(v["snapshot_id"]==snapshot_id for v in versions):
        versions.append({"snapshot_id":snapshot_id,"as_of_date":as_of,"workbook_sha256":raw["source_workbook_sha256"],
                         "snapshot_path":str(snapshot.relative_to(ROOT)),"coverage":coverage})
    inventory = {"schema_version":"1.0","patient_scope":SCOPE,"parser_version":PARSER_VERSION,
        "source_workbook":args.workbook.name,"source_workbook_sha256":raw["source_workbook_sha256"],
        "source_archive_path":str(source_copy.relative_to(ROOT)),"as_of_date":as_of,"current_snapshot_id":snapshot_id,
        "coverage":coverage,"snapshots":versions,"medication_schema_path":"medication.schema.json",
        "regimen_path":"medications.jsonl","active_regimen_path":"active-regimen.jsonl","change_log_path":"change-log.jsonl",
        "issues_path":"issues.json","normalization":{"clinical_terms_mapped":False,"dose_units_converted":False,
            "current_regimen_authority":"Current Regimen sheet; change log is retained separately, not replayed as a complete history."}}
    save(ROOT/"inventory.json",inventory)
    # Only record a new source-version delta once; re-importing must not replace it.
    if not (snapshot/"delta.json").exists():
        save(snapshot/"delta.json",delta)
    print(json.dumps({"coverage":coverage,"snapshot_id":snapshot_id,"delta_counts":{k:len(v) for k,v in delta.items()}}))


if __name__=="__main__":
    main()
