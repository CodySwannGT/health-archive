"""Import supported Labcorp Enterprise 2.00 PDFs with column-aware provenance.

Requires pypdf, fonttools, jsonschema, and Poppler pdftotext.
Source PDFs are copied byte-for-byte; previous-result columns stay contextual.
"""
import argparse
import collections
import datetime as dt
import hashlib
import json
from pathlib import Path
import re
import sys
import subprocess
import tempfile
import xml.etree.ElementTree as ET

from jsonschema import Draft202012Validator
from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[1]
NS = {"x": "http://www.w3.org/1999/xhtml"}
sys.path.insert(0, str(ROOT.parent / "database/tools"))
from common import current_account  # noqa: E402

# Provider documents are matched to the patient using the profile's source_identity.
ACCOUNT = current_account()
PATIENT_SCOPE = ACCOUNT.patient_name
IDENTITY = ACCOUNT.profile.get("source_identity", {})


def digest(data):
    return hashlib.sha256(data).hexdigest()


def load(path):
    return json.loads(path.read_text())


def write(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(obj, indent=2, ensure_ascii=False) + "\n"
    if not path.exists() or path.read_text() != data:
        path.write_text(data)


def iso(date):
    return dt.datetime.strptime(date, "%m/%d/%Y").date().isoformat()


def value(text):
    m = re.fullmatch(r"([<>]=?)?\s*(-?\d+(?:\.\d+)?)", text)
    if m:
        return {"kind": "quantity", "text": text, "comparator": m[1] or "=", "number": float(m[2])}
    return {"kind": "pending" if text == "Will Follow" else "text", "text": text}


def table_rows(pdf):
    with tempfile.TemporaryDirectory() as temp:
        xml = Path(temp) / "layout.html"
        subprocess.run(["pdftotext", "-bbox-layout", str(pdf), str(xml)], check=True)
        document = ET.parse(xml).getroot()
    rows, panel = [], None
    for page_number, page in enumerate(document.findall(".//x:page", NS), 1):
        if abs(float(page.attrib["width"]) - 612) > 1:
            raise ValueError("Unsupported page geometry: " + str(pdf))
        words = [{"text": word.text or "", **{k: float(v) for k, v in word.attrib.items()}}
                 for word in page.findall(".//x:word", NS)]
        groups = []
        for word in sorted(words, key=lambda w: (w["yMin"], w["xMin"])):
            if groups and word["yMin"] - groups[-1][0]["yMin"] <= 1.5:
                groups[-1].append(word)
            else:
                groups.append([word])
        last = None
        for group in groups:
            group.sort(key=lambda w: w["xMin"])
            text = " ".join(w["text"] for w in group)
            y = min(w["yMin"] for w in group)
            if text in ("Disclaimer", "Previous Result", "Icon Legend", "Comments", "Performing Labs"):
                break
            if y < 70 or y > 708:
                continue
            if group[0]["xMin"] < 25 and max(w["yMax"] - w["yMin"] for w in group) >= 14:
                panel = re.sub(r" \(Cont\.\)$", "", text)
                last = None
                continue
            name_words = [w for w in group if w["xMin"] < 163]
            codes = [w for w in name_words if w["text"] in ("01", "02") and w["yMax"] - w["yMin"] < 8]
            if not codes:
                if last is not None and text and not text.startswith("Test Current"):
                    last["notes"].append({"page_number": page_number, "text": text})
                continue
            if not panel:
                raise ValueError("Result without a panel: " + text)
            small = [w["text"] for w in name_words if w["yMax"] - w["yMin"] < 8]
            name = " ".join(w["text"] for w in name_words if w["yMax"] - w["yMin"] >= 8)
            columns = [" ".join(w["text"] for w in group if lo <= w["xMin"] < hi)
                       for lo, hi in [(163,235), (235,296), (296,374), (374,444), (444,506), (506,612)]]
            current, flag, previous, previous_date, units, reference = columns
            kind = ("administrative" if name == "Status Report" else "note" if name == "Note:"
                    else "structural" if not current else "narrative" if current == "See below:" else "result")
            if flag and flag not in ("High", "Low", "Abnormal", "Critical"):
                raise ValueError("Unsupported result/flag split: " + text)
            if previous_date and not re.fullmatch(r"\d{2}/\d{2}/\d{4}", previous_date):
                raise ValueError("Unsupported previous-date column: " + text)
            row = {"ordinal": len(rows) + 1, "kind": kind, "panel": panel, "analyte_name": name,
                   "result_text": current, "flag": flag or None, "unit_text": units or None,
                   "reference_text": reference or None, "previous_result": None,
                   "laboratory_site_code": codes[-1]["text"], "footnote_markers": [s for s in small if s not in ("01", "02")],
                   "page_number": page_number, "bbox": [min(w["xMin"] for w in group), y,
                     max(w["xMax"] for w in group), max(w["yMax"] for w in group)],
                   "raw_text": text, "notes": []}
            if previous or previous_date:
                row["previous_result"] = {"text": previous, "date": iso(previous_date) if previous_date else None,
                                          "context_only": True}
            rows.append(row)
            last = row
    if not rows:
        raise ValueError("No table rows extracted: " + str(pdf))
    return rows


def parse(pdf):
    data = pdf.read_bytes()
    reader = PdfReader(pdf)
    pages = [{"page_number": n + 1, "text": p.extract_text()} for n, p in enumerate(reader.pages)]
    if not data.startswith(b"%PDF-") or not pages or not all(p["text"].strip() for p in pages):
        raise ValueError("Unreadable PDF: " + str(pdf))
    text = "\n\n".join(p["text"] for p in pages)
    if "Enterprise Report Version 2.00" not in text or "Laboratory Corporation of America" not in text:
        raise ValueError("Unsupported report template: " + str(pdf))
    patient = re.match(r"(" + IDENTITY["printed_name_pattern"] + r") DOB: (\d{2}/\d{2}/\d{4})", pages[0]["text"], re.I)
    if not patient or iso(patient[2]) != IDENTITY["date_of_birth"]:
        raise ValueError("Patient identity mismatch: " + str(pdf))
    def field(pattern):
        m = re.search(pattern, text)
        if not m:
            raise ValueError("Missing metadata " + pattern + ": " + str(pdf))
        return m[1]
    specimen = field(r"Specimen ID: ([\d-]+)")
    control = field(r"Control ID: (\d+)")
    collected = field(r"Date Collected: (\d{2}/\d{2}/\d{4} \d{4} +Local)")
    reported = field(r"Date Reported: (\d{2}/\d{2}/\d{4} \d{4} +ET)")
    status = field(r"(Final|Preliminary) Report +Page")
    account = field(r"Account Number: (\d+)")
    collection_date = iso(collected[:10])
    report_id = f"pdf_{specimen}_{control}_{collection_date}"
    sha = digest(data)
    rows = table_rows(pdf)
    comment_match = re.search(r"Comments\n(.*?)\nPerforming Labs", text, re.S)
    comment_text = comment_match[1].strip() if comment_match else None
    occurrences = collections.Counter()
    observations = []
    for row in rows:
        if row["kind"] != "result":
            continue
        identity = (row["panel"], row["analyte_name"])
        occurrences[identity] += 1
        key = digest(json.dumps([report_id, *identity, occurrences[identity]], separators=(",", ":")).encode())
        context_notes = [{"page_number": other["page_number"], "text": other["raw_text"], "notes": other["notes"]}
                         for other in rows if other["panel"] == row["panel"] and other["kind"] in ("note", "narrative")]
        observations.append({"observation_id": key, "source": "labcorp", "report_id": report_id,
            "panel_name": row["panel"], "analyte_name": row["analyte_name"], "value": value(row["result_text"]),
            "status": "pending" if row["result_text"] == "Will Follow" else status.lower(),
            "unit_text": row["unit_text"], "reference_text": row["reference_text"], "flag": row["flag"],
            "collection_date": collection_date, "previous_result": row["previous_result"], "notes": row["notes"],
            "panel_context_notes": context_notes,
            "source_comment_text": comment_text if row["footnote_markers"] else None,
            "provenance": {"pdf_sha256": sha, "page_number": row["page_number"], "row_ordinal": row["ordinal"],
                "bbox": row["bbox"], "raw_text": row["raw_text"], "laboratory_site_code": row["laboratory_site_code"],
                "footnote_markers": row["footnote_markers"]}})
    pdf_path = f"imports/pdfs/labcorp_{control}_{collection_date}_{sha[:12]}.pdf"
    return {"schema_version": "1.0", "source": "labcorp", "report_id": report_id, "archive_kind": "local_pdf_results",
        "patient_scope": PATIENT_SCOPE, "patient_id": None, "patient_name_source": patient[1], "date_of_birth": iso(patient[2]),
        "specimen_id": specimen, "control_id": control, "account_number_source": account,
        "source_url": None, "collection_date": collection_date, "service_date": collection_date,
        "collected_at_source": collected, "reported_at_source": reported, "report_date": iso(reported[:10]),
        "report_status": status.lower(), "original_filename": pdf.name, "original_path": str(pdf.resolve()),
        "pdf_sha256": sha, "pdf_path": pdf_path, "pdf_pages": len(pages), "pages": pages, "text": text,
        "rows": rows, "observations": observations, "source_comment_text": comment_text,
        "extraction": {"tool": "pypdf + Poppler pdftotext bbox", "parser": "labcorp-enterprise-2.00-columns-v1",
            "source_values_structured": True, "clinical_codes_mapped": False, "units_converted": False,
            "previous_results_emitted_as_observations": False}}


def main():
    args = argparse.ArgumentParser()
    args.add_argument("pdfs", nargs="+", type=Path)
    args.add_argument("--dry-run", action="store_true")
    options = args.parse_args()
    schema = Draft202012Validator(load(ROOT / "report-envelope.schema.json"))
    envelopes = [parse(pdf) for pdf in options.pdfs]
    for envelope in envelopes:
        schema.validate(envelope)
    if options.dry_run:
        print(json.dumps([{k:e[k] for k in ("original_filename", "report_id", "report_status", "pdf_pages")} |
                         {"rows":len(e["rows"]), "observations":len(e["observations"])} for e in envelopes], indent=2))
        return
    inventory = load(ROOT / "inventory.json")
    manifest_path = ROOT / "imports/inventory.json"
    manifest = load(manifest_path) if manifest_path.exists() else {"schema_version":"1.0", "source":"labcorp", "patient_scope":PATIENT_SCOPE, "files":[]}
    grouped = collections.defaultdict(list)
    for file in manifest["files"]:
        grouped[file["report_id"]].append(load(ROOT / file["version_path"]))
    for envelope in envelopes:
        version_path = f"imports/versions/{envelope['report_id']}_{envelope['pdf_sha256']}.json"
        envelope["version_path"] = version_path
        if not any(v["pdf_sha256"] == envelope["pdf_sha256"] for v in grouped[envelope["report_id"]]):
            grouped[envelope["report_id"]].append(envelope)
            manifest["files"].append({k:envelope[k] for k in ("report_id","pdf_sha256","pdf_path","version_path","original_path","original_filename","report_status","reported_at_source")})
            write(ROOT / version_path, envelope)
        destination = ROOT / envelope["pdf_path"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            assert digest(destination.read_bytes()) == envelope["pdf_sha256"]
        else:
            destination.write_bytes(Path(envelope["original_path"]).read_bytes())
    current_imports = []
    for report_id, versions in sorted(grouped.items()):
        current = max(versions, key=lambda v:(v["report_status"] == "final", dt.datetime.strptime(v["reported_at_source"][:15], "%m/%d/%Y %H%M")))
        current = {**current, "versions":[{k:v[k] for k in ("pdf_sha256","pdf_path","version_path","report_status","reported_at_source")} for v in versions]}
        archive_path = f"imports/reports/labcorp_{report_id}.json"
        write(ROOT / archive_path, current)
        text_path = f"imports/text/labcorp_{report_id}.txt"
        (ROOT / text_path).parent.mkdir(parents=True, exist_ok=True)
        (ROOT / text_path).write_text("\n\n".join(f'--- PAGE {p["page_number"]} ---\n{p["text"]}' for p in current["pages"]))
        record = {k:current[k] for k in ("report_id","patient_id","specimen_id","control_id","collection_date","service_date","report_date","report_status","archive_kind","pdf_path","pdf_sha256","pdf_pages")}
        record.update(source="labcorp", patient_scope=PATIENT_SCOPE, status="pdf_imported", archive_path=archive_path,
                      text_path=text_path, observation_count=len(current["observations"]), version_count=len(versions))
        existing = next((r for r in inventory["reports"] if r["report_id"] == report_id), None)
        if existing:
            existing.update(record)
        else:
            inventory["reports"].append(record)
        current_imports.append(current)
    keys = [r["report_id"] for r in inventory["reports"]]
    assert len(keys) == len(set(keys)), "Duplicate report keys"
    all_envelopes = [load(ROOT / r["archive_path"]) for r in inventory["reports"]]
    for envelope in all_envelopes:
        schema.validate(envelope)
    (ROOT / "reports.jsonl").write_text("".join(json.dumps(e,ensure_ascii=False,separators=(",", ":"))+"\n" for e in sorted(all_envelopes,key=lambda e:e["report_id"])))
    observations = [o for e in current_imports for o in e["observations"]]
    assert len(observations) == len({o["observation_id"] for o in observations})
    (ROOT / "imports/observations.jsonl").write_text("".join(json.dumps(o,ensure_ascii=False,separators=(",", ":"))+"\n" for o in observations))
    manifest["current_report_ids"] = [e["report_id"] for e in current_imports]
    manifest["coverage"] = {"pdf_files":len(manifest["files"]),"unique_reports":len(current_imports),"current_observations":len(observations),"source_pdf_pages":sum(load(ROOT / f["version_path"])["pdf_pages"] for f in manifest["files"])}
    write(manifest_path, manifest)
    inventory["local_pdf_imports"] = {**manifest["coverage"], "manifest_path":"imports/inventory.json", "observations_path":"imports/observations.jsonl"}
    write(ROOT / "inventory.json", inventory)
    print(json.dumps(manifest["coverage"]))


if __name__ == "__main__":
    main()
