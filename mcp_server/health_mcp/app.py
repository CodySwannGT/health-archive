"""MCP server exposing the health archive, the database being its system of record.

Read tools use the read-only database role with account-scoped policies. Write
tools require the `health.write` OAuth scope and run the same single-transaction
write path as the `healthdb` command.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(os.environ.get('HEALTH_ARCHIVE_ROOT', Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(ROOT / 'database/tools'))

from mcp.server.auth.middleware.auth_context import get_access_token  # noqa: E402
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions  # noqa: E402
from mcp.server.mcpserver import MCPServer  # noqa: E402
from mcp.types import ToolAnnotations  # noqa: E402

from common import connect, reset_account, use_account  # noqa: E402

INSTRUCTIONS = '''Personal health archive. Each signed-in person reads and records only their own patient chart.
The database is the system of record.
Query before answering health questions, and cite source dates and record IDs.
Patient reports, clinical findings, family history, patient hypotheses and agent research are distinct.
Reported associations never establish causation. Assistant advice is never a completed action.
Record new patient statements with record_narrative or record_medication only after reviewing exact quotes,
then read the facts back before saying they were saved. Capture user-facing conversation turns with capture_conversation.'''

READ = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)


def _jsonable(value):
    return json.loads(json.dumps(value, default=str))


def _account():
    """The signed-in user's patient chart. Local stdio runs use the local profile's account."""
    token = get_access_token()
    if token is None:
        return None
    if 'health.read' not in token.scopes:
        raise PermissionError('This tool requires the health.read scope')
    from health_mcp import accounts
    with connect(admin=True, vectors=False) as conn:
        account = accounts.account_for_user(conn, token.subject)
        seeded = conn.execute('SELECT 1 FROM health.artifacts WHERE patient_id=%s LIMIT 1', (account.patient_id,)).fetchone()
    if not seeded:
        # Seeding normally completes at sign-up; finish it if it was interrupted.
        accounts.seed_archive(account)
    return account


def _as_account(fn):
    """Run fn in a worker thread as the signed-in user's account."""
    def run():
        account = _account()
        token = use_account(account) if account else None
        try:
            return fn()
        finally:
            if token:
                reset_account(token)
    return asyncio.to_thread(run)


def _query(command, **kwargs):
    """Run one read-only query command shared with the `healthdb` CLI, as the signed-in user.

    List results are wrapped as {count, results} so every tool returns one structured object."""
    from cli import execute
    def run():
        with connect() as conn:
            value = _jsonable(execute(conn, argparse.Namespace(command=command, **kwargs)))
        if value is None:
            raise ValueError('Not found')
        return {'count': len(value), 'results': value} if isinstance(value, list) else value
    return _as_account(run)


def _require_write():
    token = get_access_token()
    # Local stdio runs have no OAuth context and use the operator's credentials.
    if token is not None and 'health.write' not in token.scopes:
        raise PermissionError('This tool requires the health.write scope')


def build(auth_provider=None, issuer_url=None):
    auth = None
    if auth_provider:
        from health_mcp.oauth import SCOPES
        auth = AuthSettings(issuer_url=issuer_url, resource_server_url=issuer_url.rstrip('/') + '/mcp',
                            required_scopes=['health.read'], validate_token_resource=True,
                            client_registration_options=ClientRegistrationOptions(enabled=True, valid_scopes=SCOPES, default_scopes=SCOPES),
                            revocation_options=RevocationOptions(enabled=True),
                            service_documentation_url=os.environ.get('HEALTH_MCP_DOCS_URL', 'https://github.com/CodySwannGT/health-archive'))
    server = MCPServer(name='health-archive', title='Health Archive', instructions=INSTRUCTIONS, version='1.0.0',
                       auth_server_provider=auth_provider, auth=auth)

    @server.tool(annotations=READ)
    async def search(query: str) -> dict:
        """Search the health archive's source passages (semantic plus full text). Returns result IDs for fetch.

        Excludes agent research, historical versions and raw conversation, which `search_archive` can include."""
        result = await _query('search', question=query, limit=10, include_research=False, history=False, include_conversation=False)
        base = (issuer_url or 'https://health-archive.invalid').rstrip('/')
        return {'results': [{'id': m['document_id'], 'title': m['title'], 'url': f"{base}/documents/{m['document_id']}"} for m in result['matches']]}

    @server.tool(annotations=READ)
    async def fetch(id: str) -> dict:
        """Fetch the full text and provenance of a document returned by search."""
        doc = await _query('document', document_id=id)
        if not doc:
            raise ValueError('Document not found')
        base = (issuer_url or 'https://health-archive.invalid').rstrip('/')
        metadata = {k: doc.get(k) for k in ('source', 'source_path', 'record_id', 'clinical_date', 'evidence_kind', 'is_current')}
        return {'id': id, 'title': doc['title'], 'text': doc['text_content'], 'url': f'{base}/documents/{id}', 'metadata': metadata}

    @server.tool(annotations=READ)
    async def search_archive(question: str, limit: int = 10, include_research: bool = False,
                             include_conversation: bool = False, history: bool = False) -> dict:
        """Search source passages with excerpts, offsets, dates and evidence kinds. Optionally include agent research,
        the raw conversation archive or historical document versions."""
        return await _query('search', question=question, limit=limit, include_research=include_research,
                            history=history, include_conversation=include_conversation)

    @server.tool(annotations=READ)
    async def status() -> dict:
        """Database connection, TLS, extensions and record counts."""
        return await _query('status')

    @server.tool(annotations=READ)
    async def labs(name: str = '', limit: int = 100) -> dict:
        """Exact laboratory values with source dates, units, reference ranges and flags. Filter by analyte name."""
        return await _query('labs', name=name, limit=limit)

    @server.tool(annotations=READ)
    async def medications(changes: bool = False, all_sources: bool = False) -> dict:
        """Current self-reported medication regimen. `changes` adds the change log; `all_sources` adds clinical records."""
        return await _query('medications', changes=changes, all_sources=all_sources)

    @server.tool(annotations=READ)
    async def symptoms() -> dict:
        """Patient symptom reports, exposures, associations, hypotheses, responses and preferences with quoted evidence."""
        return await _query('symptoms')

    @server.tool(annotations=READ)
    async def vitals() -> dict:
        """Patient-reported blood pressure and pulse plus donor-center vital signs."""
        return await _query('vitals')

    @server.tool(annotations=READ)
    async def anthropometrics(history: bool = False) -> dict:
        """Weight, height, body composition and measurement context. `history` includes superseded measurements."""
        return await _query('anthropometrics', history=history)

    @server.tool(annotations=READ)
    async def activity() -> dict:
        """Exercise routines and completed exercise sessions."""
        return await _query('activity')

    @server.tool(annotations=READ)
    async def profile() -> dict:
        """Patient-reported demographics with report dates."""
        return await _query('profile')

    @server.tool(annotations=READ)
    async def timeline(name: str = '', from_date: str | None = None, to_date: str | None = None, limit: int = 100) -> dict:
        """Current resources by clinical date, filtered by name and an ISO date range."""
        return await _query('timeline', name=name, from_date=from_date, to_date=to_date, limit=limit)

    @server.tool(annotations=READ)
    async def get_record(record_id: str) -> dict:
        """Complete source payload and every version of one record."""
        result = await _query('record', record_id=record_id)
        if 'record' not in result and not result.get('matches'):
            raise ValueError('Not found')
        return result

    @server.tool(annotations=READ)
    async def get_document(document_id: str) -> dict:
        """One complete source document with provenance."""
        return await _query('document', document_id=document_id)

    @server.tool(annotations=READ)
    async def graph(record_id: str | None = None, limit: int = 40) -> dict:
        """Graph relationships around a resource, or around the patient when omitted."""
        return await _query('graph', record_id=record_id, limit=limit)

    @server.tool(annotations=READ)
    async def ontology(category: str | None = None, relations: bool = False) -> dict:
        """Registered record categories and relationship types. `category` returns its JSON Schema for recording."""
        return await _query('ontology', category=category, relations=relations)

    @server.tool(annotations=READ)
    async def query(sql: str, limit: int = 200) -> dict:
        """Bounded read-only SQL over the health schema, using the read-only role. Prefer the domain tools."""
        return await _query('query', sql=sql, file=None, limit=min(max(limit, 1), 1000))

    @server.tool(annotations=WRITE)
    async def record_narrative(source_name: str, source_text: str, extraction: dict,
                               revise: bool = False, dry_run: bool = False) -> dict:
        """Record a reviewed symptom/lifestyle extraction against the patient's exact source text.

        `extraction` follows the narrative extraction format: reported_at, reported_local_date and records, each
        with record_key, record_type, details and verbatim evidence_quotes. patient_scope and report_timezone
        default to the signed-in patient's profile (see `status`).
        Run with dry_run first. `source_name` is a stable kebab-case name such as 2026-10-23-weight-bp-pulse."""
        _require_write()
        import writes
        def run():
            with tempfile.TemporaryDirectory() as tmp:
                source = Path(tmp) / f'{_safe_name(source_name)}.txt'
                source.write_text(source_text)
                extraction_path = Path(tmp) / 'extraction.json'
                extraction_path.write_text(json.dumps(extraction))
                return _jsonable(writes.record_narrative(source, extraction_path, revise, dry_run))
        return await _as_account(run)

    @server.tool(annotations=WRITE)
    async def record_medication(report: str, source_text: str, row: dict, dry_run: bool = False) -> dict:
        """Record a reviewed supplemental medication regimen, change or administration row.

        `row` is the medication record without provenance or content_sha256, plus evidence_quotes copied
        verbatim from source_text. `report` names the patient-report folder, such as 2026-10-24-dose-change."""
        _require_write()
        import writes
        def run():
            with tempfile.TemporaryDirectory() as tmp:
                source = Path(tmp) / 'source.txt'
                source.write_text(source_text)
                row_path = Path(tmp) / 'row.json'
                row_path.write_text(json.dumps(row))
                return _jsonable(writes.record_medication(source, row_path, _safe_name(report), dry_run))
        return await _as_account(run)

    @server.tool(annotations=WRITE)
    async def capture_conversation(session_id: str, client: str, messages: list[dict]) -> dict:
        """Preserve user-facing conversation turns in the audit archive. Repeat submissions are no-ops.

        `session_id` is a stable hexadecimal-and-hyphen ID for this conversation, such as a UUID. Each message has
        role (user or assistant), text, an optional ISO timestamp and an optional stable message_ref."""
        _require_write()
        import writes
        return await _as_account(lambda: _jsonable(writes.capture_connector_messages(session_id.lower(), client, messages)))

    return server


def _safe_name(name):
    cleaned = ''.join(c if c.isalnum() or c in '-_.' else '-' for c in name).strip('.-')
    if not cleaned or len(cleaned) > 100:
        raise ValueError('Use a short kebab-case name')
    return cleaned
