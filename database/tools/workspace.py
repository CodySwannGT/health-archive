"""The database is the system of record for the archive's files.

Every write materializes the database's current archive files into a private
workspace, applies one change there, and stores the resulting new file versions
before reprojecting. Local checkout files are read only by an explicit `put`,
which refuses stale versions and record-dropping rewrites.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil

from common import ARCHIVE_PREFIXES, DB, ROOT, current_account

CACHE = DB / '.cache' / 'artifacts'
EXCLUDED_PARTS = {'tools', '__pycache__'}
# Agent-maintained record collections: a new version must keep every record the
# database already holds. Removal happens only through explicit supersession.
RECORD_COLLECTIONS = {
    'symptoms/records.jsonl': 'record_id',
    'conversations/messages.jsonl': 'message_id',
    'conversations/sessions.jsonl': 'session_id',
    'meds/reported-medications.jsonl': 'medication_id',
    'meds/reported-change-log.jsonl': 'event_id',
    'meds/reported-administrations.jsonl': 'administration_id',
}


class StaleSourceError(ValueError):
    """A local file would replace newer database content."""


def archive_prefixes():
    return list(ARCHIVE_PREFIXES)


def in_archive(rel):
    parts = Path(rel).parts
    return (bool(parts) and parts[0] in archive_prefixes() and not EXCLUDED_PARTS.intersection(parts)
            and Path(rel).name != '.DS_Store')


def tree(root):
    """Archive files under a root, keyed by relative path, with SHA-256."""
    result = {}
    for prefix in archive_prefixes():
        base = Path(root) / prefix
        if not base.exists():
            continue
        for p in base.rglob('*'):
            rel = str(p.relative_to(root))
            if p.is_file() and in_archive(rel):
                result[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
    return result


def current_artifacts(conn):
    """The current archive files of the account's patient."""
    return {r['path']: r['sha256'] for r in conn.execute(
        'SELECT path,sha256 FROM health.artifacts WHERE patient_id=%s AND retired_run IS NULL ORDER BY path', (current_account().patient_id,))}


def _cached(conn, shas):
    """Fetch content-addressed bytes once. Cached files are verified by hash."""
    CACHE.mkdir(parents=True, exist_ok=True)
    os.chmod(CACHE.parent, 0o700)
    missing = [s for s in set(shas) if not (CACHE / s).exists()]
    for start in range(0, len(missing), 200):
        batch = missing[start:start + 200]
        for row in conn.execute('SELECT sha256,bytes FROM health.artifact_contents WHERE sha256=ANY(%s)', (batch,)):
            content = bytes(row['bytes'])
            if hashlib.sha256(content).hexdigest() != row['sha256']:
                raise ValueError('Stored artifact bytes do not match their hash: ' + row['sha256'])
            temp = CACHE / (row['sha256'] + '.tmp')
            temp.write_bytes(content)
            os.replace(temp, CACHE / row['sha256'])
    for sha in shas:
        path = CACHE / sha
        if hashlib.sha256(path.read_bytes()).hexdigest() != sha:
            path.unlink()
            raise ValueError('Cached artifact was corrupted; retry to refetch ' + sha)


def materialize(conn, dest, artifacts=None):
    """Write the database's current archive files into dest."""
    artifacts = artifacts if artifacts is not None else current_artifacts(conn)
    _cached(conn, artifacts.values())
    for rel, sha in artifacts.items():
        target = Path(dest) / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(CACHE / sha, target)
    return artifacts


def known_versions(conn, rel):
    return {r['sha256'] for r in conn.execute('SELECT sha256 FROM health.artifact_versions WHERE patient_id=%s AND path=%s', (current_account().patient_id, rel))}


def _record_ids(content, key):
    return {json.loads(line)[key] for line in content.decode().splitlines() if line.strip()}


def is_retired(conn, rel):
    return conn.execute('SELECT 1 FROM health.artifacts WHERE patient_id=%s AND path=%s AND retired_run IS NOT NULL', (current_account().patient_id, rel)).fetchone() is not None


def check_put(conn, rel, content, current_sha, restore=False):
    """Refuse a local file that is stale or drops records the database holds.

    Restoring a retired file requires an explicit put (`restore=True`).
    """
    sha = hashlib.sha256(content).hexdigest()
    if sha == current_sha:
        return 'unchanged'
    if current_sha is None and is_retired(conn, rel):
        if not restore:
            raise StaleSourceError(f'{rel} was retired from the archive. Put it explicitly to restore it.')
        return 'restored'
    if sha in known_versions(conn, rel):
        raise StaleSourceError(f'{rel} is an older version already in the database. Run `healthdb export` to refresh local files.')
    if rel in RECORD_COLLECTIONS and current_sha:
        current = (CACHE / current_sha).read_bytes() if (CACHE / current_sha).exists() else bytes(conn.execute(
            'SELECT bytes FROM health.artifact_contents WHERE sha256=%s', (current_sha,)).fetchone()['bytes'])
        dropped = _record_ids(current, RECORD_COLLECTIONS[rel]) - _record_ids(content, RECORD_COLLECTIONS[rel])
        if dropped:
            raise StaleSourceError(f'{rel} would drop {len(dropped)} records held by the database. Run `healthdb export`, then reapply the change.')
    return 'new' if current_sha is None else 'changed'


def local_drift(artifacts, root=ROOT):
    """Compare a checkout with the database. Informational: the database is authoritative."""
    local = tree(root)
    return {'missing_locally': sorted(set(artifacts) - set(local)),
            'differs_from_database': sorted(p for p in set(artifacts) & set(local) if artifacts[p] != local[p]),
            'local_only': sorted(set(local) - set(artifacts))}


def export(conn, dest=ROOT):
    """Refresh local files from the database. Local-only files are reported, never deleted."""
    if (Path(dest) / '.public-repository').exists():
        raise ValueError('Refusing to export patient archive files into the public code repository')
    artifacts = current_artifacts(conn)
    before = local_drift(artifacts, dest)
    changed = [p for p in artifacts if p in before['missing_locally'] or p in before['differs_from_database']]
    materialize(conn, dest, {p: artifacts[p] for p in changed})
    return {'written': len(changed), 'paths': changed, 'local_only_not_in_database': before['local_only']}
