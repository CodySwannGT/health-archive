"""Index local Quest downloads without altering PDF bytes or duplicating reports."""
import datetime
import hashlib
import json
import re
import sys
from pathlib import Path
from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parents[1] / "database/tools"))
from common import current_account  # noqa: E402

# Reports are matched to the patient using the profile's source_identity.
IDENTITY = current_account().profile.get("source_identity", {})


def load(path):
    return json.loads(path.read_text())


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def field(text, name):
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.strip().startswith(name + ":"):
            value = line.strip().split(":", 1)[1].strip()
            if value:
                return value
            if index + 1 < len(lines):
                candidate = lines[index + 1].strip()
                if candidate and not re.match(r"^[A-Za-z #]+:", candidate):
                    return candidate
    return None


def iso_date(value):
    return datetime.datetime.strptime(value[:10], "%m/%d/%Y").date().isoformat() if value else None


inventory = load(ROOT / "inventory.json")
queue = load(ROOT / "resume-queue.json")
now = datetime.datetime.now(datetime.timezone.utc).isoformat()
for pdf in sorted((ROOT / "pdfs").glob("quest_*.pdf")):
    match = re.fullmatch(r"quest_(\d{4}-\d{2}-\d{2})_([A-Z0-9]+)\.pdf", pdf.name)
    if not match:
        raise ValueError("Unexpected download name: " + pdf.name)
    listed_iso, specimen = match.groups()
    listed_date = datetime.date.fromisoformat(listed_iso).strftime("%m/%d/%Y")
    reader = PdfReader(pdf)
    pages = [{"page_number": n + 1, "text": page.extract_text()} for n, page in enumerate(reader.pages)]
    first = pages[0]["text"]
    if not pages or not all(p["text"].strip() for p in pages):
        raise ValueError("Missing page text: " + pdf.name)
    if field(first, "Specimen") != specimen:
        raise ValueError("Specimen mismatch: " + pdf.name)
    collected = field(first, "Collected")
    received = field(first, "Received")
    reported = field(first, "Reported")
    service = field(first, "Date of Service")
    if not any(v and v.startswith(listed_date) for v in [collected, received, reported, service]):
        raise ValueError("Listed date mismatch: " + pdf.name)
    requisition = field(first, "Requisition")
    collection_date = iso_date(collected)
    candidates = [r for r in inventory["reports"] if r["specimen_id"] == specimen and (
        r.get("listed_service_date_source") == listed_date or r.get("collection_date") == collection_date and collection_date is not None
    )]
    if len(candidates) > 1:
        raise ValueError("Ambiguous report reconciliation: " + pdf.name)
    if candidates:
        record = candidates[0]
    else:
        report_id = f"{specimen}_{requisition}_{collection_date}" if requisition and collection_date else f"{specimen}_{listed_iso}"
        record = {"schema_version": "1.0", "source": "quest", "report_id": report_id, "specimen_id": specimen, "archive_kind": "pdf_only"}
        inventory["reports"].append(record)
    data = pdf.read_bytes()
    if not data.startswith(b"%PDF-"):
        raise ValueError("Invalid PDF signature")
    digest = hashlib.sha256(data).hexdigest()
    receipt_path = ROOT / "download-receipts" / (pdf.name + ".json")
    receipt = load(receipt_path) if receipt_path.exists() else {}
    if record.get("pdf_sha256") and record["pdf_sha256"] != digest:
        raise ValueError("Existing PDF changed; preserve and reconcile the previous version first: " + pdf.name)
    patient_match = re.search(r"\b" + IDENTITY["printed_name_pattern"] + r"\b", first, re.I)
    record.update(requisition_id=requisition, collection_date=collection_date,
                  listed_service_date_source=listed_date, collected_at_source=collected,
                  received_at_source=received, reported_at_source=reported,
                  report_status=field(first, "Report Status"),
                  pdf_path=str(pdf.relative_to(ROOT)), pdf_sha256=digest,
                  pdf_pages=len(pages), pdf_status="saved")
    if patient_match:
        record["patient_name"] = patient_match.group().replace("\n", " ")
    if receipt.get("source_url"):
        record["source_url"] = receipt["source_url"]
    record.setdefault("captured_at", receipt.get("captured_at", now))
    text_path = ROOT / "pdf-text" / ("quest_" + record["report_id"] + ".txt")
    text_path.parent.mkdir(exist_ok=True)
    text_path.write_text("\n\n".join(f'--- PAGE {p["page_number"]} ---\n{p["text"]}' for p in pages) + "\n")
    record["pdf_text_path"] = str(text_path.relative_to(ROOT))
    envelope_path = ROOT / "pdf-envelopes" / ("quest_" + record["report_id"] + ".json")
    envelope = {**record, "archive_kind": "downloaded_pdf_text", "pages": pages,
                "text_extraction": {"tool": "pypdf", "clinical_observations_normalized": False}}
    write(envelope_path, envelope)
    record["pdf_envelope_path"] = str(envelope_path.relative_to(ROOT))
    rows = [r for r in queue["rows"] if r["listed_service_date_source"] == listed_date]
    if any(r.get("report_id") and r["report_id"] != record["report_id"] for r in rows):
        raise ValueError("Listing has a conflicting report key; reconcile before marking saved")
    for row in rows:
        row.update(report_id=record["report_id"], pdf_status="saved", pdf_path=record["pdf_path"],
                   result_archive_status="saved" if record.get("archive_path") else "saved_pdf")

keys = [r["report_id"] for r in inventory["reports"]]
assert len(keys) == len(set(keys))
envelopes = [load(ROOT / (r.get("archive_path") or r["pdf_envelope_path"])) for r in inventory["reports"] if r.get("archive_path") or r.get("pdf_envelope_path")]
(ROOT / "reports.jsonl").write_text("".join(json.dumps(e, ensure_ascii=False, separators=(",", ":")) + "\n" for e in sorted(envelopes, key=lambda e: e["report_id"])))
inventory["list_rows"] = [{"index": r["index"], "list_text": r["list_text"], "report_id": r["report_id"]} for r in queue["rows"] if r.get("report_id")]
inventory["coverage"].update(listing_rows=len(queue["rows"]),
    saved_pdfs=sum(r.get("pdf_status") == "saved" for r in inventory["reports"]),
    archived_listing_rows=sum(r["result_archive_status"] in ["saved", "saved_pdf"] for r in queue["rows"]),
    listing_rows_without_result_archive=sum(r["result_archive_status"] not in ["saved", "saved_pdf"] for r in queue["rows"]),
    unique_reports_with_results_saved=len(envelopes))
inventory["capture_updated_at"] = now
queue["updated_at"] = now
write(ROOT / "inventory.json", inventory)
write(ROOT / "resume-queue.json", queue)
print(json.dumps(inventory["coverage"]))
