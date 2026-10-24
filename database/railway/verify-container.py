"""Restore the archive in an isolated cloud image and exercise its security boundary."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

DB = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DB / 'tools'))
from common import SECRETS, connect, current_account
from verify_restore import snapshot

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('dump', type=Path)
parser.add_argument('--image', default='codex-health-railway:test')
parser.add_argument('--port', type=int, default=55477)
args = parser.parse_args()
secret = SECRETS / 'railway'
env = os.environ.copy()
env.update(POSTGRES_PASSWORD=(secret/'admin-password').read_text().strip(),
           POSTGRES_USER='health_admin', POSTGRES_DB='health',
           TLS_KEY_PEM=(secret/'server.key').read_text(),
           TLS_CERT_PEM=(secret/'server.crt').read_text())
name = 'health-railway-test-' + uuid.uuid4().hex[:12]
profile = {'default': 'railway-test', 'profiles': {'railway-test': {}}}
for role in ['admin', 'reader']:
    profile['profiles']['railway-test'][role] = {
        'host': 'localhost', 'port': args.port, 'user': 'health_'+role,
        'password': (secret/(role+'-password')).read_text().strip(),
        'sslmode': 'verify-full', 'sslrootcert': 'server.crt'}
config = secret / 'test-connection.json'
config.write_text(json.dumps(profile))
config.chmod(0o600)
os.environ['HEALTH_DB_CONFIG'] = str(config)
os.environ['HEALTH_DB_TARGET'] = 'railway-test'
created = False
try:
    keys = ['POSTGRES_PASSWORD', 'POSTGRES_USER', 'POSTGRES_DB', 'TLS_KEY_PEM', 'TLS_CERT_PEM']
    subprocess.run(['docker', 'run', '-d', '--name', name, '-p', f'127.0.0.1:{args.port}:5432',
                    *sum((['-e', key] for key in keys), []), args.image],
                   env=env, check=True, stdout=subprocess.DEVNULL)
    created = True
    for attempt in range(30):
        try:
            with connect(admin=True, vectors=False) as conn:
                conn.execute('SELECT 1')
            break
        except Exception:
            time.sleep(1)
    else:
        raise RuntimeError('TLS test server did not start')
    with args.dump.open('rb') as stream:
        subprocess.run(['docker', 'exec', '-i', name, 'pg_restore', '-U', 'health_admin',
                        '-d', 'health', '--single-transaction', '--exit-on-error',
                        '--no-owner', '--no-privileges'], stdin=stream, check=True)
    from repair_restored_age import repair_restored_age
    from bootstrap import bootstrap
    with connect(admin=True) as conn:
        repair = repair_restored_age(conn)
    bootstrap()
    with connect(admin=True) as restored, connect(admin=True, target='local') as original:
        assert snapshot(restored) == snapshot(original), 'Cloud container restore content mismatch'
        assert restored.execute("SELECT count(*) AS n FROM health.artifact_contents WHERE sha256 <> encode(sha256(bytes),'hex')").fetchone()['n'] == 0
    import psycopg
    with connect() as reader:
        tls = reader.execute('SELECT ssl,version,cipher FROM pg_stat_ssl WHERE pid=pg_backend_pid()').fetchone()
        assert tls['ssl']
        assert reader.execute('SELECT count(*) AS n FROM health.observations').fetchone()['n'] > 0
        assert reader.execute('SELECT health.graph_counts(%s) AS counts', (current_account().patient_id,)).fetchone()
        try:
            reader.execute('CREATE TABLE public.forbidden_test (x int)')
        except (psycopg.errors.ReadOnlySqlTransaction, psycopg.errors.InsufficientPrivilege):
            pass
        else:
            raise AssertionError('Reader unexpectedly allowed writes')
    base = dict(host='localhost', port=args.port, dbname='health', user='health_admin',
                password=env['POSTGRES_PASSWORD'], connect_timeout=5)
    for settings in [dict(base, sslmode='disable'),
                     dict(base, host='127.0.0.1', sslmode='verify-full', sslrootcert=str(secret/'server.crt'))]:
        try:
            with psycopg.connect(**settings):
                pass
        except psycopg.OperationalError:
            pass
        else:
            raise AssertionError('Insecure connection unexpectedly accepted')
    result = {'status': 'verified', 'scope': 'local Railway container candidate, not a cloud deployment',
              'tls': tls, 'plaintext_rejected': True, 'wrong_hostname_rejected': True,
              'complete_database_restore_compared': True, 'source_bytes_verified': True,
              'reader_writes_denied': True, 'age_repair': repair}
    (DB/'artifacts/railway-container-verification.json').write_text(json.dumps(result, indent=2)+'\n')
    print(json.dumps(result))
finally:
    if created:
        subprocess.run(['docker', 'rm', '-f', '-v', name], check=True, stdout=subprocess.DEVNULL)
