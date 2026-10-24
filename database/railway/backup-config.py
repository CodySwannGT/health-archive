"""Back up or restore the personal configuration folder through the private backup bucket.

    python3 database/railway/backup-config.py              # back up HEALTH_HOME
    python3 database/railway/backup-config.py --list
    python3 database/railway/backup-config.py --restore KEY [--to DIR]

The folder (~/.config/health-archive by default) holds the local profile,
connection settings, deployment record and keys. It is archived, encrypted with
the backup service's age recipient and uploaded beside the database backups,
with a SHA-256 checksum. Restoring needs only an authenticated Railway CLI in a
checkout linked to the project: the decryption identity is read from the
database service's protected variables. Requires the `age` and `aws` CLIs.
Credential values are passed through the environment and never printed.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile

HOME = Path(os.environ.get('HEALTH_HOME', Path.home() / '.config/health-archive'))
PREFIX = 'config/'


def variables(service):
    result = subprocess.run(['railway', 'variable', 'list', '--json', '--service', service], capture_output=True, text=True)
    if result.returncode:
        raise SystemExit('Railway variables unavailable. Run `railway login` and `railway link` in this checkout first.')
    return json.loads(result.stdout)


def aws(storage, *args, capture=False):
    # Isolated from any local AWS profile. S3-compatible stores may reject the newer CLI's
    # default request checksums, so checksums are sent only when an operation requires them.
    env = {**os.environ, 'AWS_ACCESS_KEY_ID': storage['AWS_ACCESS_KEY_ID'], 'AWS_SECRET_ACCESS_KEY': storage['AWS_SECRET_ACCESS_KEY'],
           'AWS_DEFAULT_REGION': storage.get('AWS_DEFAULT_REGION', 'auto'), 'AWS_CONFIG_FILE': os.devnull,
           'AWS_SHARED_CREDENTIALS_FILE': os.devnull, 'AWS_REQUEST_CHECKSUM_CALCULATION': 'when_required',
           'AWS_RESPONSE_CHECKSUM_VALIDATION': 'when_required'}
    quiet = [] if args[0] == 'ls' else ['--only-show-errors']
    return subprocess.run(['aws', '--endpoint-url', storage['BACKUP_ENDPOINT'], 's3', *args, *quiet],
                          env=env, check=True, capture_output=capture, text=capture)


def backup(storage):
    if not HOME.is_dir():
        raise SystemExit(f'No configuration folder at {HOME}')
    key = PREFIX + 'health-archive-config-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '.tar.gz.age'
    with tempfile.TemporaryDirectory() as tmp:
        os.chmod(tmp, 0o700)
        archive, encrypted = Path(tmp) / 'config.tar.gz', Path(tmp) / 'config.tar.gz.age'
        with tarfile.open(archive, 'w:gz') as tar:
            tar.add(HOME, arcname=HOME.name)
        subprocess.run(['age', '--recipient', storage['BACKUP_RECIPIENT'], '--output', str(encrypted), str(archive)], check=True)
        digest = hashlib.sha256(encrypted.read_bytes()).hexdigest()
        (Path(tmp) / 'sha256').write_text(digest + '\n')
        aws(storage, 'cp', str(encrypted), f"s3://{storage['BACKUP_BUCKET']}/{key}")
        aws(storage, 'cp', str(Path(tmp) / 'sha256'), f"s3://{storage['BACKUP_BUCKET']}/{key}.sha256")
        files = sorted(m.name for m in tarfile.open(archive).getmembers() if m.isfile())
    return {'key': key, 'encrypted_sha256': digest, 'files': len(files)}


def restore(storage, identity, key, target):
    if target.exists() and any(target.iterdir()):
        raise SystemExit(f'{target} is not empty; restore into an empty folder')
    with tempfile.TemporaryDirectory() as tmp:
        os.chmod(tmp, 0o700)
        encrypted, identity_file = Path(tmp) / 'config.tar.gz.age', Path(tmp) / 'identity'
        aws(storage, 'cp', f"s3://{storage['BACKUP_BUCKET']}/{key}", str(encrypted))
        aws(storage, 'cp', f"s3://{storage['BACKUP_BUCKET']}/{key}.sha256", str(Path(tmp) / 'sha256'))
        if hashlib.sha256(encrypted.read_bytes()).hexdigest() != (Path(tmp) / 'sha256').read_text().strip():
            raise SystemExit('Checksum mismatch; nothing was restored')
        fd = os.open(identity_file, os.O_WRONLY | os.O_CREAT, 0o600)
        with os.fdopen(fd, 'w') as stream:
            stream.write(identity.strip() + '\n')
        archive = Path(tmp) / 'config.tar.gz'
        subprocess.run(['age', '--decrypt', '--identity', str(identity_file), '--output', str(archive), str(encrypted)], check=True)
        target.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tarfile.open(archive) as tar:
            members = [m for m in tar.getmembers() if m.name.startswith(HOME.name + '/') and '..' not in Path(m.name).parts]
            for member in members:
                member.name = str(Path(*Path(member.name).parts[1:]))
            tar.extractall(target, members=[m for m in members if m.name != '.'], filter='data')
    return {'restored_to': str(target), 'key': key}


def main():
    cli = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    cli.add_argument('--list', action='store_true')
    cli.add_argument('--restore', metavar='KEY')
    cli.add_argument('--to', type=Path, default=HOME, help='Restore destination (must be empty)')
    cli.add_argument('--backup-service', default='health-backup')
    cli.add_argument('--database-service', default='health-db')
    args = cli.parse_args()
    storage = variables(args.backup_service)
    if args.list:
        listing = aws(storage, 'ls', f"s3://{storage['BACKUP_BUCKET']}/{PREFIX}", capture=True).stdout
        print(listing.strip())
    elif args.restore:
        identity = variables(args.database_service)['BACKUP_DECRYPTION_IDENTITY']
        print(json.dumps(restore(storage, identity, args.restore, args.to.expanduser()), indent=2))
    else:
        print(json.dumps(backup(storage), indent=2))


if __name__ == '__main__':
    sys.exit(main())
