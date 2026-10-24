#!/usr/bin/env python3
"""Import local Cleveland Clinic C-CDA ZIP exports without uploading patient data."""
import argparse
import collections
import copy
import hashlib
import json
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path, PurePosixPath

import jsonschema

ROOT = Path(__file__).resolve().parents[1]
NS = {"c": "urn:hl7-org:v3"}
C = "{urn:hl7-org:v3}"
XSI = "{http://www.w3.org/2001/XMLSchema-instance}"
DOMAINS = {
    "48765-2": "allergies", "10160-0": "medications", "11450-4": "problems",
    "11369-6": "immunizations", "29762-2": "social_history", "8716-3": "vital_panels",
    "47519-4": "procedures", "30954-2": "result_reports", "85847-2": "care_teams",
    "46240-8": "encounters", "18776-5": "care_plans", "51848-0": "visit_diagnoses",
    "61146-7": "goals", "29299-5": "visit_reasons", "69730-0": "patient_instructions",
    "10164-2": "clinical_notes", "48768-6": "insurance", "66149-6": "prescriptions",
    "29549-3": "administered_medications",
}
OBS_TEMPLATES = {"2.16.840.1.113883.10.20.22.4.2", "2.16.840.1.113883.10.20.22.4.27"}


def serialized(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(value if isinstance(value, bytes) else serialized(value).encode()).hexdigest()


def write(path, value, jsonl=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    data = ("".join(serialized(x) + "\n" for x in value) if jsonl else
            json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()
    if not path.exists() or path.read_bytes() != data:
        path.write_bytes(data)


def local(tag):
    return tag.rsplit("}", 1)[-1]


def attrs(node):
    return {("xsi:" + local(k) if k.startswith(XSI) else k): v for k, v in node.attrib.items()}


def text(node):
    """Retain meaningful table boundaries, paragraphs, and line breaks."""
    if node is None:
        return ""
    out = [node.text or ""]
    for child in node:
        out.append(text(child))
        tag = local(child.tag)
        if tag in {"br", "paragraph", "tr", "list", "item", "table"}:
            out.append("\n")
        elif tag in {"td", "th"}:
            out.append(" | ")
        out.append(child.tail or "")
    return re.sub(r"[ \t]+", " ", "".join(out)).strip()


def tree(node, anchors):
    if node is None:
        return None
    result = {"tag": node.tag, "attributes": attrs(node), "text": node.text or "",
              "tail": node.tail or "", "children": [tree(c, anchors) for c in node]}
    if local(node.tag) == "reference" and node.get("value", "").startswith("#"):
        key = node.get("value")[1:]
        if key not in anchors:
            raise ValueError("Unresolved narrative reference " + key)
        result["resolved_text"] = text(anchors[key])
    return result


def projection(value):
    """Clinical fingerprint ignores XML anchor labels and packaging-only author data."""
    if isinstance(value, dict):
        if "tag" in value:
            if local(value["tag"]) in {"author", "informant", "templateId"}:
                return None
            a = {k: v for k, v in value.get("attributes", {}).items() if k != "ID"}
            if local(value["tag"]) == "reference" and "resolved_text" in value:
                a.pop("value", None)
            children = [projection(c) for c in value.get("children", [])]
            return {"tag": value["tag"], "attributes": a, "text": value.get("text", "").strip(),
                    "resolved_text": value.get("resolved_text"),
                    "children": [c for c in children if c is not None]}
        return {k: projection(v) for k, v in value.items()}
    if isinstance(value, list):
        return [projection(v) for v in value]
    return value


def ids(node):
    return [attrs(i) for i in node.findall("c:id", NS)
            if i.get("root") and not i.get("nullFlavor")]


def code(node, anchors):
    if node is None:
        return None
    result = attrs(node)
    original = node.find("c:originalText", NS)
    if original is not None:
        result["original_text"] = resolved(original, anchors)
    result["translations"] = [code(t, anchors) for t in node.findall("c:translation", NS)]
    return result


def resolved(node, anchors):
    if node is None:
        return ""
    pieces = [text(node)]
    for ref in node.iter(C + "reference"):
        target = ref.get("value", "")
        if target.startswith("#"):
            if target[1:] not in anchors:
                raise ValueError("Unresolved reference " + target)
            pieces.append(text(anchors[target[1:]]))
    return "\n".join(dict.fromkeys(p for p in pieces if p))


def times(node):
    out = []
    for t in node.findall("c:effectiveTime", NS):
        out.append({"attributes": attrs(t), "bounds": [
            {"kind": local(b.tag), **attrs(b)} for b in t]})
    return out


def values(node, anchors):
    out = []
    for v in node.findall("c:value", NS):
        item = {"attributes": attrs(v), "text": resolved(v, anchors), "structured": tree(v, anchors)}
        number = v.get("value")
        if not v.get("nullFlavor") and v.get(XSI + "type") in {"PQ", "INT", "REAL"} and number:
            if re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", number):
                item["number"] = float(number)
        out.append(item)
    return out


def fields(node, anchors):
    c = code(node.find("c:code", NS), anchors)
    return {"element": local(node.tag), "attributes": attrs(node), "source_ids": ids(node),
            "code": c, "name": (c or {}).get("displayName") or (c or {}).get("original_text") or "",
            "status": attrs(node.find("c:statusCode", NS)) if node.find("c:statusCode", NS) is not None else None,
            "effective_times": times(node), "values": values(node, anchors),
            "text": resolved(node.find("c:text", NS), anchors),
            "interpretations": [code(i, anchors) for i in node.findall("c:interpretationCode", NS)],
            "reference_ranges": [tree(i, anchors) for i in node.findall("c:referenceRange", NS)]}


def summary(node, anchors, category):
    result = fields(node, anchors)
    if category in {"medications", "prescriptions", "administered_medications", "immunizations"}:
        material = node.find("c:consumable/c:manufacturedProduct/c:manufacturedMaterial/c:code", NS)
        result["medication"] = {"code": code(material, anchors),
                                "route": code(node.find("c:routeCode", NS), anchors),
                                "dose": attrs(node.find("c:doseQuantity", NS)) if node.find("c:doseQuantity", NS) is not None else None}
        mc = result["medication"]["code"] or {}
        result["name"] = mc.get("displayName") or mc.get("original_text") or result["name"]
    if category in {"problems", "allergies"}:
        ob = node.find("c:entryRelationship/c:observation", NS)
        if ob is not None:
            result["assertion"] = fields(ob, anchors)
            value = ob.find("c:value", NS)
            result["assertion"]["value_code"] = code(value, anchors)
            result["name"] = resolved(ob.find("c:text", NS), anchors) or result["name"]
    return result


def first_time(node):
    if node is None:
        return ""
    t = node.find("c:effectiveTime", NS)
    if t is None:
        return ""
    if t.get("value"):
        return t.get("value")
    low = t.find("c:low", NS)
    return low.get("value", "") if low is not None else ""


def tables(section, xp):
    result = []
    narrative = section.find("c:text", NS)
    if narrative is None:
        return result
    for table in narrative.iter(C + "table"):
        rows = []
        for tr in table.findall("c:thead/c:tr", NS) + table.findall("c:tbody/c:tr", NS) + table.findall("c:tr", NS):
            rows.append({"xpath": xp[id(tr)], "attributes": attrs(tr), "cells": [
                {"kind": local(cell.tag), "attributes": attrs(cell), "text": text(cell)}
                for cell in tr if local(cell.tag) in {"td", "th"}]})
        result.append({"xpath": xp[id(table)], "attributes": attrs(table), "rows": rows})
    return result


def paths(root):
    out = {id(root): "/c:ClinicalDocument"}
    def visit(node):
        counts = collections.Counter()
        for child in node:
            counts[child.tag] += 1
            prefix = "c:" if child.tag.startswith(C) else "*[local-name()='" + local(child.tag) + "']"
            label = prefix + local(child.tag) if prefix == "c:" else prefix
            out[id(child)] = out[id(node)] + "/" + label + "[" + str(counts[child.tag]) + "]"
            visit(child)
    visit(root)
    return out


def parse_zip(path, expected_patient=None):
    sha = digest(path.read_bytes())
    docs, occurrences, members, patient = [], [], [], None
    with zipfile.ZipFile(path) as archive:
        if archive.testzip() is not None:
            raise ValueError("ZIP CRC failure")
        for info in archive.infolist():
            member = PurePosixPath(info.filename)
            if member.is_absolute() or ".." in member.parts or "\\" in info.filename:
                raise ValueError("Unsafe archive member " + info.filename)
            if info.is_dir():
                continue
            body = archive.read(info)
            if len(body) > 100_000_000:
                raise ValueError("Oversized archive member")
            members.append({"path": info.filename, "bytes": len(body), "sha256": digest(body)})
            if not re.search(r"/DOC\d+\.XML$", info.filename, re.I):
                continue
            if b"<!DOCTYPE" in body.upper() or b"<!ENTITY" in body.upper():
                raise ValueError("Unsupported XML entity declarations")
            root = ET.fromstring(body)
            if root.tag != C + "ClinicalDocument":
                raise ValueError("Not a CDA document")
            prole = root.find("c:recordTarget/c:patientRole", NS)
            identity = sorted((i.get("root"), i.get("extension", "")) for i in prole.findall("c:id", NS) if not i.get("nullFlavor"))
            patient_key = digest(identity)
            if not identity or (patient is not None and patient != patient_key) or (expected_patient and patient_key != expected_patient):
                raise ValueError("Patient scope mismatch")
            patient = patient_key
            anchors = {n.get("ID"): n for n in root.iter() if n.get("ID")}
            xp = paths(root)
            doc_key = digest([patient, ids(root)])
            title = root.findtext("c:title", default="", namespaces=NS)
            exported = root.find("c:effectiveTime", NS).get("value", "")
            encounter = root.find("c:componentOf/c:encompassingEncounter", NS)
            as_of = first_time(encounter)
            is_summary = title in {"Patient Health Summary", "Continuity of Care Document"}
            # Summary snapshots outrank older visit snapshots. Generation order is not encounter chronology.
            rank = [exported[:8] if is_summary else as_of[:8], int(is_summary),
                    int(title == "Continuity of Care Document"), exported]
            document = {"document_id": doc_key, "source_ids": ids(root), "title": title,
                        "effective_time_source": exported, "encounter_time_source": as_of,
                        "patient": tree(prole, anchors), "header": [tree(n, anchors) for n in root if local(n.tag) != "component"],
                        "provenance": {"zip_sha256": sha, "member_path": info.filename, "xml_sha256": digest(body)}, "sections": []}
            for sidx, section in enumerate(root.findall("c:component/c:structuredBody/c:component/c:section", NS), 1):
                scode = section.find("c:code", NS)
                category = DOMAINS.get(scode.get("code", "") if scode is not None else "", "other")
                section_record = {"ordinal": sidx, "title": section.findtext("c:title", default="", namespaces=NS),
                                  "code": code(scode, anchors), "attributes": attrs(section),
                                  "narrative_text": text(section.find("c:text", NS)),
                                  "narrative_structure": tree(section.find("c:text", NS), anchors),
                                  "narrative_tables": tables(section, xp), "record_ids": [], "observation_ids": []}
                for entry in section.findall("c:entry", NS):
                    candidates = [n for n in entry if local(n.tag) not in {"templateId"}]
                    if len(candidates) != 1:
                        raise ValueError("Unsupported CDA entry shape")
                    node = candidates[0]
                    candidates = [(node, category, None)]
                    if category in {"result_reports", "vital_panels"}:
                        for ob in node.iter(C + "observation"):
                            roots = {t.get("root") for t in ob.findall("c:templateId", NS)}
                            if roots & OBS_TEMPLATES:
                                candidates.append((ob, "lab_results" if category == "result_reports" else "vitals", node))
                    for candidate, domain, panel in candidates:
                        payload = summary(candidate, anchors, domain)
                        payload["structured_data"] = tree(candidate, anchors)
                        if panel is not None:
                            payload["panel"] = {"source_ids": ids(panel), "code": code(panel.find("c:code", NS), anchors),
                                                "effective_times": times(panel),
                                                "specimen": [tree(n, anchors) for n in panel.findall("c:specimen", NS)]}
                        native = ids(candidate)
                        # Template roles distinguish reused Epic IDs for comments, attachments and values.
                        role = sorted({t.get("root") for t in candidate.findall("c:templateId", NS) if t.get("root")})
                        primary_code = candidate.find("c:code", NS)
                        concept = [primary_code.get("codeSystem"), primary_code.get("code")] if primary_code is not None else []
                        identity_key = [[i.get("root"), i.get("extension", "")] for i in native]
                        fallback = projection(payload)
                        # No-ID instructions are encounter-specific, never merge solely by name or date.
                        key = [patient, domain, local(candidate.tag), identity_key, role, concept] if native else [patient, domain, fallback, as_of if domain == "patient_instructions" else ""]
                        record_id = digest(key)
                        fingerprint = digest(projection(payload))
                        provenance = {"zip_sha256": sha, "document_id": doc_key, "member_path": info.filename,
                                      "xml_sha256": digest(body), "section_title": section_record["title"],
                                      "section_ordinal": sidx, "xpath": xp[id(candidate)], "snapshot_rank": rank}
                        occurrences.append({"record_id": record_id, "source": "cleveland_clinic", "patient_key": patient,
                                            "category": domain, "identity_strategy": "native_id_and_role" if native else "clinical_content_fingerprint",
                                            "version_sha256": fingerprint, "data": payload, "provenance": provenance})
                        field = "observation_ids" if panel is not None else "record_ids"
                        section_record[field].append(record_id)
                document["sections"].append(section_record)
            docs.append(document)
    if not docs:
        raise ValueError("No clinical documents found")
    return {"sha256": sha, "patient_key": patient, "members": members, "documents": docs, "occurrences": occurrences}


def schema():
    h = {"type": "string", "pattern": "^[a-f0-9]{64}$"}
    prov = {"type": "object", "required": ["zip_sha256", "document_id", "member_path", "xml_sha256", "xpath", "snapshot_rank"],
            "properties": {"zip_sha256": h, "document_id": h, "xml_sha256": h, "member_path": {"type": "string"}, "xpath": {"type": "string"}, "snapshot_rank": {"type": "array"}}}
    data = {"type": "object", "required": ["element", "source_ids", "code", "status", "effective_times", "values", "text", "structured_data"],
            "properties": {"element": {"type": "string"}, "source_ids": {"type": "array"}, "code": {"type": ["object", "null"]},
                           "status": {"type": ["object", "null"]}, "effective_times": {"type": "array"}, "values": {"type": "array"},
                           "text": {"type": "string"}, "structured_data": {"type": "object"}}}
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", "title": "Cleveland Clinic C-CDA source record",
            "type": "object", "additionalProperties": False,
            "required": ["schema_version", "record_id", "source", "patient_key", "category", "identity_strategy", "version_sha256", "data", "provenance", "versions_path", "version_count", "selection"],
            "properties": {"schema_version": {"const": "1.0"}, "record_id": h, "source": {"const": "cleveland_clinic"}, "patient_key": h,
                           "category": {"enum": sorted(set(DOMAINS.values()) | {"lab_results", "vitals", "other"})},
                           "identity_strategy": {"enum": ["native_id_and_role", "clinical_content_fingerprint"]}, "version_sha256": h,
                           "data": data, "provenance": {"type": "array", "minItems": 1, "items": prov},
                           "versions_path": {"type": "string"}, "version_count": {"type": "integer", "minimum": 1}, "selection": {"type": "object"}}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("zip", type=Path)
    parser.add_argument("--archive-input", action="store_true", help="Move the input ZIP out of its original location after successful validation")
    args = parser.parse_args()
    source = args.zip.resolve()
    previous = json.loads((ROOT / "inventory.json").read_text()) if (ROOT / "inventory.json").exists() else {"packages": []}
    expected = previous.get("patient_key")
    candidate = parse_zip(source, expected)
    package = next((p for p in previous["packages"] if p["sha256"] == candidate["sha256"]), None)
    if package is None:
        package = {"sha256": candidate["sha256"], "original_filename": source.name,
                   "zip_path": "raw/zips/" + candidate["sha256"][:12] + "_" + source.name,
                   "extracted_path": "raw/extracted/" + candidate["sha256"]}
        previous["packages"].append(package)
    archive_path = ROOT / package["zip_path"]
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    if archive_path.exists():
        assert digest(archive_path.read_bytes()) == candidate["sha256"]
    else:
        shutil.copyfile(source, archive_path)
    documents, occurrences, member_count = [], [], 0
    for p in sorted(previous["packages"], key=lambda p: p["sha256"]):
        parsed = parse_zip(ROOT / p["zip_path"], candidate["patient_key"])
        assert p["sha256"] == parsed["sha256"]
        p["members"] = parsed["members"]
        p["document_count"] = len(parsed["documents"])
        member_count += len(parsed["members"])
        with zipfile.ZipFile(ROOT / p["zip_path"]) as z:
            for member in parsed["members"]:
                dest = ROOT / p["extracted_path"] / member["path"]
                body = z.read(member["path"])
                dest.parent.mkdir(parents=True, exist_ok=True)
                if dest.exists():
                    assert digest(dest.read_bytes()) == member["sha256"]
                else:
                    dest.write_bytes(body)
                if dest.suffix.lower() == ".pdf":
                    textpath = ROOT / "text" / (p["sha256"][:12] + "_summary.txt")
                    textpath.parent.mkdir(parents=True, exist_ok=True)
                    subprocess.run(["pdftotext", "-layout", str(dest), str(textpath)], check=True)
                    p["pdf_text_path"] = str(textpath.relative_to(ROOT))
        documents.extend(parsed["documents"])
        occurrences.extend(parsed["occurrences"])
    grouped = collections.defaultdict(dict)
    for item in occurrences:
        group = grouped[item["record_id"]]
        version = group.setdefault(item["version_sha256"], {**item, "provenance": []})
        if item["provenance"] not in version["provenance"]:
            version["provenance"].append(item["provenance"])
    current = []
    validator = jsonschema.Draft202012Validator(schema())
    all_versions = 0
    for key, versions in sorted(grouped.items()):
        for v in versions.values():
            v["provenance"].sort(key=serialized)
        ordered = sorted(versions.values(), key=lambda v: (max(p["snapshot_rank"] for p in v["provenance"]), v["version_sha256"]))
        best = copy.deepcopy(ordered[-1])
        best.update({"schema_version": "1.0", "versions_path": "versions/" + key + ".json", "version_count": len(versions),
                     "selection": {"rule": "summary_snapshot_then_encounter_chronology_then_export_time", "clinical_status_not_inferred": True,
                                   "tie_with_different_payload": len(ordered) > 1 and max(p["snapshot_rank"] for p in ordered[-1]["provenance"]) == max(p["snapshot_rank"] for p in ordered[-2]["provenance"])}})
        validator.validate(best)
        current.append(best)
        all_versions += len(ordered)
        write(ROOT / best["versions_path"], {"record_id": key, "versions": ordered})
    # Document versions keep complete section narratives and the source header.
    distinct_docs = {(d["document_id"], d["provenance"]["xml_sha256"]): d for d in documents}
    docs = [distinct_docs[k] for k in sorted(distinct_docs)]
    write(ROOT / "documents.jsonl", docs, True)
    narrative_tables = [{"document_id": d["document_id"], "section_title": s["title"], "section_code": s["code"],
                         "section_ordinal": s["ordinal"], "document_provenance": d["provenance"], **t}
                        for d in docs for s in d["sections"] for t in s["narrative_tables"]]
    write(ROOT / "narrative-tables.jsonl", narrative_tables, True)
    write(ROOT / "records.jsonl", current, True)
    for category in sorted({r["category"] for r in current}):
        write(ROOT / "data" / (category + ".jsonl"), [r for r in current if r["category"] == category], True)
    write(ROOT / "record.schema.json", schema())
    dschema = {"$schema": "https://json-schema.org/draft/2020-12/schema", "title": "C-CDA document and section archive", "type": "object",
               "required": ["document_id", "source_ids", "title", "effective_time_source", "patient", "header", "provenance", "sections"],
               "properties": {"document_id": {"type": "string", "pattern": "^[a-f0-9]{64}$"}, "sections": {"type": "array", "items": {"type": "object", "required": ["ordinal", "title", "code", "narrative_text", "narrative_structure", "narrative_tables", "record_ids", "observation_ids"]}}}}
    for d in docs:
        jsonschema.validate(d, dschema)
        for s in d["sections"]:
            assert all(i in grouped for i in s["record_ids"] + s["observation_ids"])
    write(ROOT / "document.schema.json", dschema)
    counts = dict(sorted(collections.Counter(r["category"] for r in current).items()))
    coverage = {"packages": len(previous["packages"]), "archive_members": member_count, "document_versions": len(docs),
                "sections": sum(len(d["sections"]) for d in docs), "narrative_tables": len(narrative_tables),
                "entry_occurrences": sum(len(s["record_ids"]) for d in documents for s in d["sections"]),
                "observation_occurrences": sum(len(s["observation_ids"]) for d in documents for s in d["sections"]),
                "record_occurrences": len(occurrences), "unique_source_records": len(current), "clinical_payload_versions": all_versions,
                "repeated_occurrences_collapsed": len(occurrences) - all_versions, "by_category": counts}
    write(ROOT / "inventory.json", {"schema_version": "1.0", "source": "cleveland_clinic", "patient_key": candidate["patient_key"],
                                    "packages": sorted(previous["packages"], key=lambda p: p["sha256"]), "coverage": coverage})
    assert len({r["record_id"] for r in current}) == len(current)
    if args.archive_input and source != archive_path:
        assert digest(source.read_bytes()) == candidate["sha256"]
        source.unlink()
    print(json.dumps(coverage, indent=2))


if __name__ == "__main__":
    main()
