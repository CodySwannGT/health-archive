"""Configured database connections and stable identities. Credentials never enter logs."""
import contextvars
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pgvector.psycopg import register_vector

ROOT = Path(__file__).resolve().parents[2]
DB = ROOT / 'database'
# Personal configuration lives outside any code checkout: the local profile, connection
# settings and deployment record. Older checkouts kept them in identity/ and database/.secrets/.
HEALTH_HOME = Path(os.environ.get('HEALTH_HOME', Path.home() / '.config/health-archive'))


def _configured(home_path, legacy_path):
    return home_path if home_path.exists() or not legacy_path.exists() else legacy_path


PROFILE_PATH = Path(os.environ.get('HEALTH_PROFILE', _configured(HEALTH_HOME / 'local-profile.json', ROOT / 'identity/local-profile.json')))
SECRETS = _configured(HEALTH_HOME / 'secrets', DB / '.secrets')


def deployment_record():
    """The operator's private deployment record: hosting IDs, endpoint and certificate fingerprint."""
    path = _configured(HEALTH_HOME / 'deployment.json', DB / 'railway/deployment.json')
    return json.loads(path.read_text())
# Archive directories, the same for every patient. Each patient's archive is separate in the database.
ARCHIVE_PREFIXES = ('lab-results', 'meds', 'symptoms', 'vitals', 'health-records', 'research', 'conversations', 'ontology', 'checks', 'workspace')
MODEL = 'BAAI/bge-small-en-v1.5'
PARSER = 'health-db-1.0'


@dataclass(frozen=True)
class Account:
    """The user an operation acts for, and the patient chart it acts on."""
    user_id: str
    patient_id: str
    user_name: str = ''
    patient_name: str = ''
    patient_scopes: tuple = ()
    timezone: str = 'UTC'
    source_patient_keys: dict = field(default_factory=dict, hash=False, compare=False)
    # The patient profile, including optional source_identity used to match provider documents.
    profile: dict = field(default_factory=dict, hash=False, compare=False)

    @classmethod
    def from_profile(cls, profile):
        patient = next(p for p in profile['patients'] if p['patient_id'] == profile['default_patient_id'])
        user = next(u for u in profile['users'] if u['user_id'] == profile['default_user_id'])
        return cls(user['user_id'], patient['patient_id'], user.get('display_name', ''), patient.get('display_name', ''),
                   tuple(patient.get('legacy_patient_scopes', ())), patient.get('timezone', 'UTC'),
                   patient.get('source_patient_keys', {}), patient)

    @property
    def patient_scope(self):
        return self.patient_scopes[0] if self.patient_scopes else self.patient_id


_account = contextvars.ContextVar('health_account', default=None)


def local_profile():
    if not PROFILE_PATH.exists():
        raise RuntimeError(f'No account selected and no local profile at {PROFILE_PATH}. Copy identity/local-profile.example.json.')
    return json.loads(PROFILE_PATH.read_text())


def current_account():
    """The account set for this operation, or the local profile's default account."""
    return _account.get() or Account.from_profile(local_profile())


def use_account(account):
    """Act as an account for the current context. Returns a token for `reset_account`."""
    return _account.set(account)


def reset_account(token):
    _account.reset(token)


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def connection_settings(admin=False, database='health', target=None):
    """Remote selection fails closed. There is never an implicit local fallback."""
    config_path = Path(os.environ.get('HEALTH_DB_CONFIG', SECRETS / 'connection.json'))
    config = json.loads(config_path.read_text()) if config_path.exists() else {}
    selected = target or os.environ.get('HEALTH_DB_TARGET') or config.get('default', 'local')
    if database == 'health':
        # An isolated database on the same server, such as a temporary end-to-end test copy.
        database = os.environ.get('HEALTH_DB_NAME', database)
    if selected == 'local':
        password = (SECRETS / ('admin-password' if admin else 'reader-password')).read_text().strip()
        return dict(host='127.0.0.1', port=55476, dbname=database,
                    user='health_admin' if admin else 'health_reader', password=password)
    profile = config.get('profiles', {}).get(selected)
    if not profile:
        raise RuntimeError('Selected database profile is missing. Local fallback is disabled.')
    settings = dict(profile['admin' if admin else 'reader'])
    if settings.get('sslmode') != 'verify-full' or not settings.get('sslrootcert'):
        raise RuntimeError('Remote database profiles require verified TLS and a trust certificate.')
    cert = Path(settings['sslrootcert'])
    if not cert.is_absolute():
        cert = config_path.parent / cert
    if not cert.is_file():
        raise RuntimeError('Remote database trust certificate is missing.')
    settings['sslrootcert'] = str(cert)
    settings['dbname'] = database
    return settings


def connect(admin=False, vectors=True, database='health', target=None):
    conn = psycopg.connect(**connection_settings(admin, database, target),
                           row_factory=dict_row, connect_timeout=10)
    # Without an account or local profile, the empty user is denied every patient row.
    account = _account.get() or (Account.from_profile(local_profile()) if PROFILE_PATH.exists() else None)
    conn.execute("SELECT set_config('health.user_id', %s, false), set_config('health.patient_id', %s, false)",
                 (account.user_id if account else '', account.patient_id if account else ''))
    conn.execute("SET search_path = health, ag_catalog, public")
    conn.execute("SET statement_timeout = '120s'")
    if vectors:
        register_vector(conn)
    conn.commit()
    if not admin:
        conn.execute('SET default_transaction_read_only = on')
        conn.commit()
    return conn


def embedding_model():
    from fastembed import TextEmbedding
    return TextEmbedding(MODEL, cache_dir=str(DB / 'models'), threads=2, local_files_only=True)


def cypher(conn, query):
    from psycopg import sql
    delimiter='$q_'+hashlib.sha256(query.encode()).hexdigest()[:24]+'$'
    while delimiter in query:delimiter=delimiter[:-1]+'x$'
    statement="SELECT * FROM ag_catalog.cypher('health_graph', "+delimiter+query+delimiter+") AS (v ag_catalog.agtype)"
    return conn.execute(statement,prepare=False).fetchall()


def json_out(value):
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))
