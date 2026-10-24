"""Accounts, invitations and new patient archives for the multi-user connector.

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
import time

ROOT = Path(os.environ.get('HEALTH_ARCHIVE_ROOT', Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(ROOT / 'database/tools'))

from common import Account, use_account, reset_account  # noqa: E402

TEMPLATE = ROOT / 'template'
USERNAME = re.compile(r'^[a-z][a-z0-9_]{2,31}$')
INVITE_TTL_DAYS = 14

SCHEMA = '''
CREATE SCHEMA IF NOT EXISTS health_oauth;
REVOKE ALL ON SCHEMA health_oauth FROM PUBLIC;
CREATE TABLE IF NOT EXISTS health_oauth.accounts (
  username text PRIMARY KEY CHECK (username ~ '^[a-z][a-z0-9_]{2,31}$'),
  user_id text NOT NULL UNIQUE REFERENCES health.users,
  password_hash text NOT NULL, disabled boolean NOT NULL DEFAULT false,
  created_at timestamptz NOT NULL DEFAULT now());
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


def create_invite(conn, note=None, days=INVITE_TTL_DAYS):
    from health_mcp.hashing import sha
    code = 'inv-' + secrets.token_urlsafe(18)
    conn.execute('INSERT INTO health_oauth.invites(code_sha256,note,expires_at) VALUES (%s,%s,%s)',
                 (sha(code), note, time.time() + days * 86400))
    return code


def create_account(conn, username, password_hash, display_name, timezone, invite=None, user_id=None):
    """Create a user with their own patient chart, or attach an account to an existing user.

    New users need an unused, unexpired invitation. `user_id` binds an account to
    an existing user, such as the archive's first owner, without an invitation.
    """
    from psycopg.types.json import Jsonb
    from health_mcp.hashing import sha
    if not USERNAME.match(username):
        raise ValueError('Usernames are 3-32 lowercase letters, digits or underscores, starting with a letter')
    if conn.execute('SELECT 1 FROM health_oauth.accounts WHERE username=%s', (username,)).fetchone():
        raise ValueError('That username is taken')
    if user_id:
        if not conn.execute('SELECT 1 FROM health.users WHERE user_id=%s', (user_id,)).fetchone():
            raise ValueError('Unknown user')
        conn.execute('INSERT INTO health_oauth.accounts(username,user_id,password_hash) VALUES (%s,%s,%s)', (username, user_id, password_hash))
        return None
    claimed = conn.execute('UPDATE health_oauth.invites SET used_at=now() WHERE code_sha256=%s AND used_at IS NULL AND expires_at>%s RETURNING code_sha256',
                           (sha(invite or ''), time.time())).fetchone()
    if not claimed:
        raise ValueError('That invitation is invalid, expired or already used')
    user_id, patient_id = 'usr_' + secrets.token_hex(16), 'pat_' + secrets.token_hex(16)
    profile = {'patient_id': patient_id, 'display_name': display_name, 'legacy_patient_scopes': [username],
               'timezone': timezone, 'source_patient_keys': {}}
    conn.execute('INSERT INTO health.users VALUES (%s,%s,%s)', (user_id, display_name, 'connector_account'))
    conn.execute('INSERT INTO health.patients(patient_id,display_name,profile) VALUES (%s,%s,%s)', (patient_id, display_name, Jsonb(profile)))
    conn.execute('INSERT INTO health.patient_access VALUES (%s,%s,%s)', (user_id, patient_id, 'owner'))
    conn.execute('INSERT INTO health_oauth.accounts(username,user_id,password_hash) VALUES (%s,%s,%s)', (username, user_id, password_hash))
    conn.execute('UPDATE health_oauth.invites SET used_by=%s WHERE code_sha256=%s', (username, sha(invite)))
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
