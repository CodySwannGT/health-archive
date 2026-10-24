"""Download, decrypt and fully restore-verify an encrypted Railway bucket backup."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

DB = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DB / 'tools'))
from common import SECRETS, deployment_record
from verify_restore import verify_restore

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--key', help='Object key. Defaults to the latest encrypted dump.')
parser.add_argument('--image', default='codex-health-railway:test', help='Local image with awscli and age')
args = parser.parse_args()
deployment = deployment_record()
result = subprocess.run(['railway', 'variable', 'list', '--json', '--project', deployment['project_id'],
                         '--environment', deployment['environment_id'], '--service', deployment['backup_service_id']],
                        capture_output=True, text=True)
if result.returncode:
    raise RuntimeError('Unable to retrieve private bucket credentials from Railway.')
variables = json.loads(result.stdout)
folder = DB / 'backups' / ('railway_' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
folder.mkdir(parents=True, mode=0o700)
identity = SECRETS / 'railway/backup.agekey'
if not identity.is_file():
    raise RuntimeError('Recover the backup decryption identity with configure-remote.py first.')
with tempfile.TemporaryDirectory(prefix='health-bucket-client-') as temp:
    env_file = Path(temp) / 'aws.env'
    names = ['AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_DEFAULT_REGION']
    env_file.write_text(''.join(f'{name}={variables[name]}\n' for name in names))
    env_file.chmod(0o600)
    prefix = ['docker', 'run', '--rm', '--env-file', str(env_file),
              '--mount', f'type=bind,src={folder},dst=/backup',
              '--entrypoint', 'aws', args.image, '--endpoint-url', variables['BACKUP_ENDPOINT']]

    def aws(arguments, capture=False):
        return subprocess.run(prefix + arguments, check=True, text=True, capture_output=capture)

    if args.key:
        key = args.key
    else:
        objects = json.loads(aws(['s3api', 'list-objects-v2', '--bucket', variables['BACKUP_BUCKET'],
                                 '--prefix', 'health/', '--output', 'json'], True).stdout).get('Contents', [])
        dumps = [entry for entry in objects if entry['Key'].endswith('.dump.age')]
        if not dumps:
            raise RuntimeError('No encrypted backup object exists in the bucket.')
        key = max(dumps, key=lambda entry: entry['LastModified'])['Key']
    for suffix, local in [('', 'health.dump.age'), ('.sha256', 'health.dump.age.sha256')]:
        aws(['s3', 'cp', 's3://' + variables['BACKUP_BUCKET'] + '/' + key + suffix,
             '/backup/' + local, '--only-show-errors'])
encrypted = folder / 'health.dump.age'
assert hashlib.sha256(encrypted.read_bytes()).hexdigest() == encrypted.with_name(encrypted.name+'.sha256').read_text().strip(), 'Encrypted object checksum mismatch'
subprocess.run(['docker', 'run', '--rm', '--mount', f'type=bind,src={folder},dst=/backup',
                '--mount', f'type=bind,src={identity},dst=/run/identity,readonly', '--entrypoint', 'age', args.image,
                '--decrypt', '--identity', '/run/identity', '--output', '/backup/health.dump', '/backup/health.dump.age'], check=True)
for path in folder.iterdir():
    path.chmod(0o600)
dump = folder / 'health.dump'
verification = verify_restore(dump)
evidence = {'status': 'verified', 'verified_at': datetime.now(timezone.utc).isoformat(),
            'source': 'Railway private bucket encrypted logical backup', 'object_key': key,
            'encrypted_sha256': hashlib.sha256(encrypted.read_bytes()).hexdigest(),
            'decrypted_sha256': hashlib.sha256(dump.read_bytes()).hexdigest(),
            'complete_restore_verification': verification}
(DB / 'artifacts/railway-backup-verification.json').write_text(json.dumps(evidence, indent=2, default=str)+'\n')
print(json.dumps({'status': 'verified', 'object_key': key,
                  'checksum_and_decryption_passed': True, 'complete_isolated_restore_passed': True}))
