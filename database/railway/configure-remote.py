"""Recover private connection settings using the owner's authenticated Railway CLI."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess

DB = Path(__file__).resolve().parents[1]
# Runs with the system Python, so it resolves the personal configuration directory itself,
# matching database/tools/common.py: HEALTH_HOME, then the legacy in-checkout locations.
HOME = Path(os.environ.get('HEALTH_HOME', Path.home() / '.config/health-archive'))
DEPLOYMENT = HOME / 'deployment.json' if (HOME / 'deployment.json').exists() or not (DB / 'railway/deployment.json').exists() else DB / 'railway/deployment.json'
SECRETS = HOME / 'secrets' if (HOME / 'secrets').exists() or not (DB / '.secrets').exists() else DB / '.secrets'
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--reader-only', action='store_true', help='Do not install ingestion/admin credentials or backup identity')
parser.add_argument('--activate', action='store_true', help='Make the verified remote profile the default')
args = parser.parse_args()
deployment = json.loads(DEPLOYMENT.read_text())
if args.activate and deployment.get('status') != 'verified':
    raise RuntimeError('Remote activation requires a verified migration and backup deployment record.')
result = subprocess.run(['railway', 'variable', 'list', '--json',
                         '--project', deployment['project_id'],
                         '--environment', deployment['environment_id'],
                         '--service', deployment['database_service_id']],
                        capture_output=True, text=True)
if result.returncode:
    raise RuntimeError('Railway settings could not be retrieved. Authenticate to your personal Railway account.')
variables = json.loads(result.stdout)
certificate = variables['TLS_CERT_PEM'].strip() + '\n'
if hashlib.sha256(certificate.encode()).hexdigest() != deployment['tls_certificate_sha256']:
    raise RuntimeError('Railway certificate differs from the reviewed deployment trust certificate.')
secret_dir = SECRETS
secret_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
os.chmod(secret_dir, 0o700)
railway_dir = secret_dir / 'railway'
railway_dir.mkdir(mode=0o700, exist_ok=True)
os.chmod(railway_dir, 0o700)


def private_file(path, text):
    # Open with restrictive permissions from creation, even on another machine.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, 'w') as stream:
        stream.write(text)


private_file(railway_dir / 'server.crt', certificate)
config_path = secret_dir / 'connection.json'
config = json.loads(config_path.read_text()) if config_path.exists() else {}
profile = {}
for role, password_key in [('reader', 'HEALTH_READER_PASSWORD'), ('admin', 'POSTGRES_PASSWORD')]:
    if role == 'admin' and args.reader_only:
        continue
    profile[role] = dict(host=deployment['tcp_host'], port=deployment['tcp_port'],
                        user='health_' + role, password=variables[password_key],
                        sslmode='verify-full', sslrootcert='railway/server.crt')
config.setdefault('profiles', {})['railway'] = profile
config['default'] = 'railway' if args.activate else config.get('default', 'local')
private_file(config_path, json.dumps(config, indent=2) + '\n')
if not args.reader_only:
    private_file(railway_dir / 'backup.agekey', variables['BACKUP_DECRYPTION_IDENTITY'])
print('Private Railway connection settings recovered. Credential values were not printed.')
