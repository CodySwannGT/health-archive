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


LAB_OBSERVATION_FIELDS = ('panel_name', 'analyte_name', 'value_text', 'unit_text', 'reference_text', 'flag', 'result_status', 'note')


def _locate(text, quote, occurrence=None):
    """Character span of a quote in the source; a repeated quote needs its 1-based occurrence."""
    starts, start = [], text.find(quote)
    while start >= 0:
        starts.append(start)
        start = text.find(quote, start + 1)
    if not starts:
        raise ValueError('Evidence quote is not in the source text: ' + quote)
    if occurrence is None and len(starts) > 1:
        raise ValueError(f'Evidence quote occurs {len(starts)} times; quote more of the line or pass occurrence: ' + quote)
    index = (occurrence or 1) - 1
    if not 0 <= index < len(starts):
        raise ValueError(f'Evidence quote occurs {len(starts)} times, not {occurrence}: ' + quote)
    return {'quote': quote, 'start_char': starts[index], 'end_char': starts[index] + len(quote)}


def record_lab_report(source, report_path, name, revise=False, dry_run=False):
    """Archive a transcribed laboratory report whose every result is quoted from the transcription.

    The report file holds the report fields and observations; each observation has
    `evidence_quote` (its line, copied verbatim) and optionally `occurrence`. The
    printed value, and the unit and reference range when given, must appear in the quote.
    """
    from datetime import datetime, timezone
    from jsonschema import Draft202012Validator, FormatChecker
    from jsonschema.exceptions import best_match
    report = json.loads(Path(report_path).read_text())
    folder = f'lab-results/transcribed/{name}'
    def apply(workspace):
        source_target = workspace / folder / 'source.txt'
        report_target = workspace / folder / 'report.json'
        if revise:
            source_target.parent.mkdir(parents=True, exist_ok=True)
            source_target.write_bytes(Path(source).read_bytes())
        else:
            _place(source, source_target)
        text = source_target.read_text()
        observations = []
        for i, row in enumerate(report.get('observations') or []):
            row = dict(row)
            quote, occurrence = row.pop('evidence_quote', None), row.pop('occurrence', None)
            if not quote:
                raise ValueError(f'observations[{i}] needs evidence_quote: its line copied verbatim from source_text')
            evidence = _locate(text, quote, occurrence)
            for field in ('value_text', 'unit_text', 'reference_text'):
                if row.get(field) and row[field] not in quote:
                    raise ValueError(f'observations[{i}].{field} {row[field]!r} is not in its evidence quote; copy it as printed')
            unknown = set(row) - set(LAB_OBSERVATION_FIELDS)
            if unknown:
                raise ValueError(f'observations[{i}] has unknown fields: {sorted(unknown)}')
            observations.append({**row, 'evidence': evidence})
        prior = json.loads(report_target.read_text()) if report_target.exists() else None
        record = {'schema_version': '1.0', 'report_id': name,
                  **{k: v for k, v in report.items() if k not in ('observations', 'report_id', 'schema_version', 'provenance')},
                  'observations': observations,
                  'provenance': {'source_path': f'{folder}/source.txt', 'source_sha256': hashlib.sha256(text.encode()).hexdigest(),
                                 'recorded_at': prior['provenance']['recorded_at'] if prior else
                                 datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')}}
        schema = json.loads((CODE / 'lab-results/tools/transcribed-report.schema.json').read_text())
        error = best_match(Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(record))
        if error:
            raise ValueError(f"{'/'.join(map(str, error.absolute_path)) or 'report'}: {error.message}")
        if prior and not revise and {**prior, 'provenance': None} != {**record, 'provenance': None}:
            raise ValueError(f'Lab report {name} already exists with different content; pass revise to store a corrected version')
        summary = {'report_id': name, 'collection_date': record['collection_date'], 'observations': len(observations),
                   'replaces_prior_version': prior is not None and prior != record}
        if dry_run:
            raise _DryRun({**summary, 'record': record})
        report_target.write_text(json.dumps(record, ensure_ascii=False, indent=2) + '\n')
        return summary
    return _write(apply, 'record-lab-report')


def capture_conversation(session, excluded_ranges=()):
    import capture_conversation as capture
    def apply(workspace):
        return capture.capture(session, excluded_ranges, archive_root=workspace)
    return _write(apply, 'capture-conversation')


def capture_connector_messages(session_id, client, messages, project=False):
    """Capture messages submitted by an authenticated connector client.

    By default the messages are stored only as the session's source file, in one
    short transaction. The next write of any kind captures them into archive
    messages and reprojects; `project=True` does that now.
    """
    from datetime import datetime, timezone
    import capture_conversation as capture
    received_at = datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
    if project:
        def apply(workspace):
            path, appended = capture.append_connector_messages(workspace, session_id, client, messages, received_at)
            return {**capture.capture(path, archive_root=workspace), 'appended_source_messages': appended}
        return _write(apply, 'capture-connector-messages')
    return _append_connector_source(session_id, client, messages, received_at)


def _append_connector_source(session_id, client, messages, received_at):
    import tempfile
    import ingest as pipeline
    from capture_conversation import CONNECTOR_SESSIONS, append_connector_messages
    from common import PARSER, Jsonb, connect, digest
    from workspace import check_put, materialize
    patient = current_account().patient_id
    rel = f'{CONNECTOR_SESSIONS}/{session_id}.jsonl'
    with connect(admin=True) as conn, tempfile.TemporaryDirectory(prefix='health-workspace-') as tmp:
        conn.execute("SELECT pg_advisory_xact_lock(hashtext('health_archive_ingest:'||%s))", (patient,))
        workspace = Path(tmp)
        current = conn.execute('SELECT sha256 FROM health.artifacts WHERE patient_id=%s AND path=%s AND retired_run IS NULL', (patient, rel)).fetchone()
        # A deferred source that later conflicts would block every write, so refuse an ID another capture already owns.
        if not current and conn.execute("SELECT 1 FROM health.artifacts WHERE patient_id=%s AND path LIKE %s AND retired_run IS NULL LIMIT 1",
                                        (patient, f'conversations/sessions/{session_id}/%')).fetchone():
            raise ValueError(f'Session {session_id} is already captured from another source, such as its local transcript; '
                             'use a different session_id for connector messages')
        if current:
            materialize(conn, workspace, {rel: current['sha256']})
        path, appended = append_connector_messages(workspace, session_id, client, messages, received_at)
        result = {'session_id': session_id, 'appended_source_messages': appended, 'projected': False,
                  'note': 'Stored as source. The next recorded write, or a capture with project=true, makes these messages searchable records.'}
        if not appended:
            return result
        check_put(conn, rel, path.read_bytes(), current and current['sha256'])
        run = conn.execute('INSERT INTO health.ingestion_runs(parser_version,manifest_sha256,operation,patient_id) VALUES (%s,%s,%s,%s) RETURNING run_id',
                           (PARSER, digest([rel, hashlib.sha256(path.read_bytes()).hexdigest()]), 'append-connector-source', patient)).fetchone()['run_id']
        checkout, pipeline.ROOT = pipeline.ROOT, workspace
        try:
            imp = pipeline.Importer(conn, run)
            imp.artifact(path)
        finally:
            pipeline.ROOT = checkout
        summary = {**imp.stats, 'deferred_projection': True}
        conn.execute('UPDATE health.ingestion_runs SET completed_at=now(),summary=%s WHERE run_id=%s', (Jsonb(summary), run))
    return {**result, 'run_id': run}


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
