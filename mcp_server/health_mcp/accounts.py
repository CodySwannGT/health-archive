"""Accounts and new patient archives for the multi-user connector.

Each account is a health user who owns one patient chart. A new chart starts
from the template archive (ontology and schemas) in `template/`, stored through
the normal single-transaction write path, so every patient's archive is its own
database-owned file set.
"""
import os
from pathlib import Path
import re
import secrets
import sys

ROOT = Path(os.environ.get('HEALTH_ARCHIVE_ROOT', Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(ROOT / 'database/tools'))

from common import Account, use_account, reset_account  # noqa: E402

TEMPLATE = ROOT / 'template'
USERNAME = re.compile(r'^[a-z][a-z0-9_]{2,31}$')

SCHEMA = '''
CREATE SCHEMA IF NOT EXISTS health_oauth;
REVOKE ALL ON SCHEMA health_oauth FROM PUBLIC;
CREATE TABLE IF NOT EXISTS health_oauth.accounts (
  username text PRIMARY KEY CHECK (username ~ '^[a-z][a-z0-9_]{2,31}$'),
  user_id text NOT NULL UNIQUE REFERENCES health.users,
  password_hash text NOT NULL, disabled boolean NOT NULL DEFAULT false,
  created_at timestamptz NOT NULL DEFAULT now());
-- Sign-up no longer takes invitations. The table keeps the record of those already used.
CREATE TABLE IF NOT EXISTS health_oauth.invites (
  code_sha256 text PRIMARY KEY, note text, created_at timestamptz NOT NULL DEFAULT now(),
  expires_at double precision NOT NULL, used_at timestamptz, used_by text REFERENCES health_oauth.accounts);
'''


def migrate(conn):
    conn.execute(SCHEMA)


def account_for_user(conn, user_id):
    """The user's own patient chart, as an operation account."""
    row = conn.execute('''SELECT u.user_id,u.display_name AS user_name,p.patient_id,p.display_name AS patient_name,p.profile
      FROM health.users u JOIN health.patient_access a USING(user_id) JOIN health.patients p USING(patient_id)
      WHERE u.user_id=%s AND a.role='owner' ORDER BY p.created_at,p.patient_id LIMIT 1''', (user_id,)).fetchone()
    if not row:
        raise PermissionError('This account has no patient chart')
    profile = row['profile'] or {}
    return Account(row['user_id'], row['patient_id'], row['user_name'], row['patient_name'],
                   tuple(profile.get('legacy_patient_scopes', ())), profile.get('timezone', 'UTC'),
                   profile.get('source_patient_keys', {}), profile)


def create_account(conn, username, password_hash, display_name, timezone, user_id=None):
    """Create a user with their own patient chart, or attach an account to an existing user.

    `user_id` binds an account to an existing user, such as the archive's first owner.
    """
    from psycopg.types.json import Jsonb
    if not USERNAME.match(username):
        raise ValueError('Usernames are 3-32 lowercase letters, digits or underscores, starting with a letter')
    if conn.execute('SELECT 1 FROM health_oauth.accounts WHERE username=%s', (username,)).fetchone():
        raise ValueError('That username is taken')
    if user_id:
        if not conn.execute('SELECT 1 FROM health.users WHERE user_id=%s', (user_id,)).fetchone():
            raise ValueError('Unknown user')
        conn.execute('INSERT INTO health_oauth.accounts(username,user_id,password_hash) VALUES (%s,%s,%s)', (username, user_id, password_hash))
        return None
    user_id, patient_id = 'usr_' + secrets.token_hex(16), 'pat_' + secrets.token_hex(16)
    profile = {'patient_id': patient_id, 'display_name': display_name, 'legacy_patient_scopes': [username],
               'timezone': timezone, 'source_patient_keys': {}}
    conn.execute('INSERT INTO health.users VALUES (%s,%s,%s)', (user_id, display_name, 'connector_account'))
    conn.execute('INSERT INTO health.patients(patient_id,display_name,profile) VALUES (%s,%s,%s)', (patient_id, display_name, Jsonb(profile)))
    conn.execute('INSERT INTO health.patient_access VALUES (%s,%s,%s)', (user_id, patient_id, 'owner'))
    conn.execute('INSERT INTO health_oauth.accounts(username,user_id,password_hash) VALUES (%s,%s,%s)', (username, user_id, password_hash))
    return Account(user_id, patient_id, display_name, display_name, (username,), timezone, {})


def seed_archive(account):
    """Store the template archive as a new patient's first archive files."""
    from ingest import ingest
    paths = sorted(str(p.relative_to(TEMPLATE)) for p in TEMPLATE.rglob('*') if p.is_file() and p.name != '.DS_Store')
    token = use_account(account)
    try:
        return ingest(put_paths=paths, put_root=TEMPLATE, operation='seed-archive')
    finally:
        reset_account(token)


# Children before parents: every foreign key is NO ACTION, so this order is what lets each delete succeed.
PATIENT_TABLES = ['chunks', 'documents', 'source_record_aliases', 'relationships', 'association_versions', 'associations',
                  'concepts', 'clinical_types', 'ontology_categories', 'ontology_relations', 'import_issues',
                  'pooling_consent_events', 'pooling_consent', 'source_history', 'source_commits']


def delete_account(conn, user_id):
    """Permanently delete an account, the patient charts it owns and everything stored for them.

    Source file contents shared with another chart, such as the template ontology, stay for that chart.
    Pooled patterns hold no patient identifiers and are recomputed without the deleted chart."""
    from common import canonical, cypher
    username = conn.execute('SELECT username FROM health_oauth.accounts WHERE user_id=%s', (user_id,)).fetchone()
    patients = [r['patient_id'] for r in conn.execute(
        "SELECT patient_id FROM health.patient_access WHERE user_id=%s AND role='owner' ORDER BY patient_id", (user_id,))]
    counts = {}
    for patient in patients:
        # The ingest lock: no write to this chart can be in flight while it is deleted.
        conn.execute("SELECT pg_advisory_xact_lock(hashtext('health_archive_ingest:'||%s))", (patient,))
        cypher(conn, 'MATCH (n:Entity) WHERE n.patient_id=' + canonical(patient) + ' DETACH DELETE n RETURN count(*)')
        def delete(statement, table):
            counts[table] = counts.get(table, 0) + conn.execute(statement, (patient,)).rowcount
        for table in PATIENT_TABLES:
            delete(f'DELETE FROM health.{table} WHERE patient_id=%s', table)
        delete('DELETE FROM health.resource_versions WHERE resource_id IN (SELECT resource_id FROM health.resources WHERE patient_id=%s)', 'resource_versions')
        delete('DELETE FROM health.resources WHERE patient_id=%s', 'resources')
        delete('DELETE FROM health.record_versions WHERE record_id IN (SELECT record_id FROM health.records WHERE patient_id=%s)', 'record_versions')
        delete('DELETE FROM health.records WHERE patient_id=%s', 'records')
        delete('DELETE FROM health.artifacts WHERE patient_id=%s', 'artifacts')
        delete('DELETE FROM health.artifact_versions WHERE patient_id=%s', 'artifact_versions')
        delete('DELETE FROM health.ingestion_runs WHERE patient_id=%s', 'ingestion_runs')
        delete('DELETE FROM health.patient_access WHERE patient_id=%s', 'patient_access')
        delete('DELETE FROM health.patients WHERE patient_id=%s', 'patients')
    counts['artifact_contents'] = conn.execute('''DELETE FROM health.artifact_contents c WHERE
      NOT EXISTS (SELECT 1 FROM health.artifacts a WHERE a.sha256=c.sha256)
      AND NOT EXISTS (SELECT 1 FROM health.artifact_versions v WHERE v.sha256=c.sha256)
      AND NOT EXISTS (SELECT 1 FROM health.source_history h WHERE h.sha256=c.sha256)''').rowcount
    conn.execute('DELETE FROM health.patient_access WHERE user_id=%s', (user_id,))
    conn.execute('DELETE FROM health.pooling_consent_events WHERE user_id=%s', (user_id,))
    conn.execute('DELETE FROM health.pooling_consent WHERE user_id=%s', (user_id,))
    conn.execute('DELETE FROM health_oauth.tokens WHERE user_id=%s', (user_id,))
    if username:
        conn.execute('UPDATE health_oauth.invites SET used_by=NULL WHERE used_by=%s', (username['username'],))
        conn.execute('DELETE FROM health_oauth.login_failures WHERE username=%s', (username['username'],))
    conn.execute('DELETE FROM health_oauth.accounts WHERE user_id=%s', (user_id,))
    conn.execute('DELETE FROM health.users WHERE user_id=%s', (user_id,))
    return {'deleted_patients': len(patients), 'deleted_rows': counts}
