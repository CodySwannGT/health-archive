"""Agent write commands. Each one changes the database-owned archive in a single transaction.

The change is applied to a workspace materialized from the database, validated
there with the ontology the database holds, stored as new archive versions and
reprojected. No local checkout or Git step is involved.
"""
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

from common import ROOT, canonical, current_account
from ingest import ingest

CODE = ROOT


def _place(source, target):
    """Copy an input into the archive. An existing different file requires an explicit revision."""
    content = Path(source).read_bytes()
    if target.exists() and target.read_bytes() != content:
        raise ValueError(f'{target.name} already exists in the database archive with different content')
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)


def _place_json(source, target, replace=False, defaults=None):
    """Place a JSON input, compared by content so formatting differences are not conflicts."""
    value = json.loads(Path(source).read_text())
    for key, default in (defaults or {}).items():
        value.setdefault(key, default)
    if target.exists() and json.loads(target.read_text()) == value:
        return
    if target.exists() and not replace:
        raise ValueError(f'{target.name} already exists in the database archive with different content')
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def _run(args):
    result = subprocess.run([sys.executable, *map(str, args)], capture_output=True, text=True)
    if result.returncode:
        raise ValueError(result.stderr.strip().splitlines()[-1] if result.stderr.strip() else 'Tool failed')
    return json.loads(result.stdout)


def record_narrative(source, extraction, revise=False, dry_run=False):
    """Archive a reviewed symptom/lifestyle extraction against its exact source text."""
    name = Path(source).name.removesuffix('.txt')
    def apply(workspace):
        inputs = workspace / 'symptoms/inputs'
        source_target = inputs / (name + '.txt')
        extraction_target = inputs / (name + '.extraction.json')
        _place(source, source_target)
        account = current_account()
        scope = json.loads(Path(extraction).read_text()).get('patient_scope')
        if scope is not None and scope not in account.patient_scopes:
            raise ValueError(f'patient_scope {scope!r} is not the signed-in patient; omit it to use {account.patient_scope!r}')
        _place_json(extraction, extraction_target, revise,
                    {'patient_scope': account.patient_scope, 'report_timezone': account.timezone})
        command = [CODE / 'symptoms/tools/ingest-narrative.py', source_target, extraction_target, '--root', workspace / 'symptoms']
        result = _run(command + (['--revise'] if revise else []) + (['--dry-run'] if dry_run else []))
        if dry_run:
            raise _DryRun(result)
        result['narrative_verification'] = {k: v for k, v in _run(
            [CODE / 'symptoms/tools/verify-narratives.py', workspace, source_target, extraction_target]).items() if k in ('status', 'schema_validated_records')}
        return result
    return _write(apply, 'record-narrative')


def _semantic_hash():
    spec = importlib.util.spec_from_file_location('import_workbook', CODE / 'meds/tools/import-workbook.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.semantic_hash


MEDICATION_FILES = {'medication_regimen': ('meds/reported-medications.jsonl', 'medication_id'),
                    'medication_change': ('meds/reported-change-log.jsonl', 'event_id'),
                    'medication_administration': ('meds/reported-administrations.jsonl', 'administration_id')}


def record_medication(source, row_path, report, dry_run=False):
    """Archive a reviewed supplemental medication report row with exact quoted evidence.

    The row file holds the record without `provenance` or `content_sha256`, plus
    `evidence_quotes`. Quotes are located in the source and the hash is computed.
    """
    from jsonschema import Draft202012Validator, FormatChecker
    from jsonschema.exceptions import best_match
    row = json.loads(Path(row_path).read_text())
    quotes = row.pop('evidence_quotes')
    if row.get('record_type') not in MEDICATION_FILES:
        raise ValueError(f'record_type must be one of {sorted(MEDICATION_FILES)}')
    filename, key = MEDICATION_FILES[row['record_type']]
    # Administrations carry reported_at as a field; regimens and changes keep it only in provenance.
    reported_at = row.get('reported_at') if row['record_type'] == 'medication_administration' else row.pop('reported_at', None)
    if not reported_at:
        raise ValueError('reported_at is required: the ISO 8601 time, with offset, when the patient reported this')
    def apply(workspace):
        source_rel = f'meds/patient-reports/{report}/source.txt'
        _place(source, workspace / source_rel)
        text = (workspace / source_rel).read_text()
        evidence = []
        for quote in quotes:
            start = text.find(quote)
            if start < 0 or text.find(quote, start + 1) >= 0:
                raise ValueError('Evidence quote must occur exactly once in the source: ' + quote)
            evidence.append({'end_char': start + len(quote), 'quote': quote, 'start_char': start})
        record = {**row, 'provenance': {'evidence': evidence, 'reported_at': reported_at, 'source_path': source_rel,
                                        'source_sha256': hashlib.sha256(text.encode()).hexdigest()}}
        record['content_sha256'] = _semantic_hash()(record)
        schema = json.loads((workspace / 'meds/medication.schema.json').read_text())
        # Validate against the record type's own branch so the error names the failing field, not just the oneOf.
        branch = {**schema, 'oneOf': [{'$ref': f"#/$defs/{record['record_type']}"}]}
        error = best_match(Draft202012Validator(branch, format_checker=FormatChecker()).iter_errors(record))
        if error:
            raise error
        target = workspace / filename
        rows = [json.loads(line) for line in target.read_text().splitlines() if line.strip()] if target.exists() else []
        prior = next((r for r in rows if r[key] == record[key]), None)
        if dry_run:
            raise _DryRun({'record': record, 'replaces_existing': prior is not None})
        rows = [record if r[key] == record[key] else r for r in rows] + ([] if prior else [record])
        target.write_text(''.join(canonical(r) + '\n' for r in rows))
        return {'record_type': record['record_type'], key: record[key], 'replaced_prior_version': prior is not None,
                'content_sha256': record['content_sha256']}
    return _write(apply, 'record-medication')


def capture_conversation(session, excluded_ranges=()):
    import capture_conversation as capture
    def apply(workspace):
        return capture.capture(session, excluded_ranges, archive_root=workspace)
    return _write(apply, 'capture-conversation')


def capture_connector_messages(session_id, client, messages):
    """Capture messages submitted by an authenticated connector client."""
    from datetime import datetime, timezone
    import capture_conversation as capture
    received_at = datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
    def apply(workspace):
        path, appended = capture.append_connector_messages(workspace, session_id, client, messages, received_at)
        return {**capture.capture(path, archive_root=workspace), 'appended_source_messages': appended}
    return _write(apply, 'capture-connector-messages')


def set_pooling_consent(opt_in):
    """Opt the signed-in patient's chart in to or out of pooled patterns, and refresh them in the same transaction."""
    import ingest
    from associations import CONSENT_TEXT, CONSENT_VERSION
    from common import connect, current_account
    account = current_account()
    with connect(admin=True) as conn:
        conn.execute("SELECT pg_advisory_xact_lock(hashtext('health_archive_schema'))")
        ingest.migrate(conn)
        conn.execute("""INSERT INTO health.pooling_consent(patient_id, user_id, opted_in, consent_version) VALUES (%s, %s, %s, %s)
          ON CONFLICT (patient_id) DO UPDATE SET user_id=EXCLUDED.user_id, opted_in=EXCLUDED.opted_in,
          consent_version=EXCLUDED.consent_version, updated_at=now()""", (account.patient_id, account.user_id, opt_in, CONSENT_VERSION))
        conn.execute('INSERT INTO health.pooling_consent_events(patient_id, user_id, opted_in, consent_version) VALUES (%s, %s, %s, %s)',
                     (account.patient_id, account.user_id, opt_in, CONSENT_VERSION))
        patterns = ingest.refresh_pooled_patterns(conn)
    return {'opted_in': opt_in, 'consent_version': CONSENT_VERSION, 'consent_text': CONSENT_TEXT, 'pooled_patterns': patterns}


def put(paths):
    return ingest(put_paths=paths, operation='put')


def retire(paths, reason):
    return ingest(retire_paths=paths, retire_reason=reason, operation='retire')


class _DryRun(Exception):
    def __init__(self, result):
        self.result = result


def _write(apply, operation):
    try:
        return ingest(apply=apply, operation=operation)
    except _DryRun as dry:
        # Raised inside the transaction, so nothing was stored.
        return {'dry_run': True, 'result': dry.result}
