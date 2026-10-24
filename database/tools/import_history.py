"""Preserve a Git repository's full history in the patient's database archive.

    healthdb import-history --repository NAME --git-dir PATH [--omit-content PREFIX ...]
    healthdb import-history --repository NAME --git-dir PATH --verify

Every commit reachable from any ref, with its message, author, dates and
parents, and every file in its tree are recorded. File bytes are stored once,
by SHA-256, beside the archive's own artifacts. Historical versions of archive
files also join the archive's version history, so `put` refuses them as stale.
Content under an omitted prefix (for example, database dumps that duplicate the
database itself) is recorded with its size and Git blob ID but not stored.
Repeat imports change nothing. --verify reconstructs every commit tree from the
database and checks each stored file against Git's blob ID.
"""
import hashlib
import subprocess

from common import Jsonb, PARSER, connect, current_account, digest
from workspace import in_archive


def _git(git_dir, *args, binary=False):
    result = subprocess.run(['git', '-C', str(git_dir), *args], capture_output=True, check=True)
    return result.stdout if binary else result.stdout.decode()


def _commits(git_dir):
    shas = _git(git_dir, 'rev-list', '--all', '--topo-order', '--reverse').split()
    refs = {}
    for line in _git(git_dir, 'for-each-ref', '--format=%(objectname) %(refname)').splitlines():
        sha, ref = line.split(' ', 1)
        refs.setdefault(sha, []).append(ref)
    for sha in shas:
        fields = _git(git_dir, 'show', '-s', '--format=%H%x00%P%x00%an%x00%aI%x00%cn%x00%cI%x00%B', sha).split('\0')
        yield {'commit_sha': fields[0], 'parent_shas': fields[1].split(), 'author_name': fields[2], 'authored_at': fields[3],
               'committer_name': fields[4], 'committed_at': fields[5], 'message': fields[6].rstrip('\n'), 'refs': sorted(refs.get(sha, []))}


def _tree(git_dir, sha):
    for entry in _git(git_dir, 'ls-tree', '-r', '-l', '-z', sha, binary=True).split(b'\0'):
        if not entry:
            continue
        meta, path = entry.split(b'\t', 1)
        _, kind, blob, size = meta.split()
        if kind == b'blob':
            yield path.decode(), blob.decode(), int(size)


class _Blobs:
    def __init__(self, git_dir):
        self.proc = subprocess.Popen(['git', '-C', str(git_dir), 'cat-file', '--batch'], stdin=subprocess.PIPE, stdout=subprocess.PIPE)

    def read(self, blob):
        self.proc.stdin.write((blob + '\n').encode())
        self.proc.stdin.flush()
        size = int(self.proc.stdout.readline().split()[2])
        data = self.proc.stdout.read(size)
        self.proc.stdout.read(1)
        return data

    def close(self):
        self.proc.stdin.close()
        self.proc.wait()


def git_blob_id(data):
    return hashlib.sha1(b'blob %d\0' % len(data) + data).hexdigest()


def import_history(repository, git_dir, omit_prefixes=()):
    patient = current_account().patient_id
    commits = list(_commits(git_dir))
    stats = {'commits': len(commits), 'new_commits': 0, 'tree_entries': 0, 'new_tree_entries': 0,
             'new_contents': 0, 'new_archive_versions': 0, 'omitted_entries': 0}
    blobs, seen = _Blobs(git_dir), {}
    try:
        with connect(admin=True, vectors=False) as conn:
            conn.execute("SELECT pg_advisory_xact_lock(hashtext('health_archive_schema'))")
            from ingest import migrate
            migrate(conn)
            conn.execute("SELECT pg_advisory_xact_lock(hashtext('health_archive_ingest:'||%s))", (patient,))
            run = conn.execute('INSERT INTO health.ingestion_runs(parser_version,manifest_sha256,operation,patient_id) VALUES (%s,%s,%s,%s) RETURNING run_id',
                               (PARSER, digest([repository, [c['commit_sha'] for c in commits]]), 'import-history', patient)).fetchone()['run_id']
            existing = {r['commit_sha'] for r in conn.execute('SELECT commit_sha FROM health.source_commits WHERE patient_id=%s AND repository=%s', (patient, repository))}
            commit_rows, history_rows, version_rows, contents = [], [], set(), {}
            for c in commits:
                stats['new_commits'] += c['commit_sha'] not in existing
                commit_rows.append((patient, repository, c['commit_sha'], c['parent_shas'], c['author_name'], c['authored_at'], c['committer_name'],
                                    c['committed_at'], c['message'], c['refs'], run))
                for path, blob, size in _tree(git_dir, c['commit_sha']):
                    stats['tree_entries'] += 1
                    omitted = next((p for p in omit_prefixes if path.startswith(p)), None)
                    if omitted:
                        sha, reason = None, f'Content under {omitted} is not duplicated in the database; recorded by size and Git blob ID.'
                        stats['omitted_entries'] += 1
                    else:
                        if blob not in seen:
                            data = blobs.read(blob)
                            seen[blob] = hashlib.sha256(data).hexdigest()
                            contents[seen[blob]] = data
                        sha, reason = seen[blob], None
                    history_rows.append((patient, repository, c['commit_sha'], path, blob, size, sha, reason))
                    if sha and in_archive(path):
                        version_rows.add((path, sha, patient, run))
            known = {r['sha256'] for r in conn.execute('SELECT sha256 FROM health.artifact_contents WHERE sha256=ANY(%s)', (list(contents),))}
            stats['new_contents'] = len(set(contents) - known)
            before = conn.execute('SELECT (SELECT count(*) FROM health.source_history WHERE patient_id=%(p)s AND repository=%(r)s) AS h, (SELECT count(*) FROM health.artifact_versions WHERE patient_id=%(p)s) AS v',
                                  {'p': patient, 'r': repository}).fetchone()
            with conn.cursor() as cur:
                cur.executemany('INSERT INTO health.artifact_contents VALUES (%s,%s) ON CONFLICT DO NOTHING', [(s, d) for s, d in contents.items() if s not in known])
                cur.executemany('INSERT INTO health.source_commits VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (patient_id,repository,commit_sha) DO UPDATE SET refs=EXCLUDED.refs', commit_rows)
                cur.executemany('INSERT INTO health.source_history VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING', history_rows)
                cur.executemany('INSERT INTO health.artifact_versions VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING', sorted(version_rows))
            after = conn.execute('SELECT (SELECT count(*) FROM health.source_history WHERE patient_id=%(p)s AND repository=%(r)s) AS h, (SELECT count(*) FROM health.artifact_versions WHERE patient_id=%(p)s) AS v',
                                 {'p': patient, 'r': repository}).fetchone()
            stats['new_tree_entries'] = after['h'] - before['h']
            stats['new_archive_versions'] = after['v'] - before['v']
            conn.execute('UPDATE health.ingestion_runs SET completed_at=now(),summary=%s WHERE run_id=%s', (Jsonb(stats), run))
            stats['run_id'] = run
    finally:
        blobs.close()
    return stats


def verify_history(repository, git_dir):
    """Every commit tree in Git must be reconstructable from the database."""
    patient = current_account().patient_id
    checked = stored = omitted = 0
    with connect(admin=True, vectors=False) as conn:
        for c in _commits(git_dir):
            rows = {r['path']: r for r in conn.execute('''SELECT h.path,h.git_blob,h.size_bytes,h.sha256,h.content_omitted_reason,c.bytes
              FROM health.source_history h LEFT JOIN health.artifact_contents c USING(sha256)
              WHERE h.patient_id=%s AND h.repository=%s AND h.commit_sha=%s''', (patient, repository, c['commit_sha']))}
            commit = conn.execute('SELECT message FROM health.source_commits WHERE patient_id=%s AND repository=%s AND commit_sha=%s',
                                  (patient, repository, c['commit_sha'])).fetchone()
            assert commit and commit['message'] == c['message'], c['commit_sha']
            tree = list(_tree(git_dir, c['commit_sha']))
            assert {p for p, _, _ in tree} == set(rows), c['commit_sha']
            for path, blob, size in tree:
                row = rows[path]
                assert row['git_blob'] == blob and row['size_bytes'] == size, path
                if row['sha256']:
                    data = bytes(row['bytes'])
                    assert git_blob_id(data) == blob and hashlib.sha256(data).hexdigest() == row['sha256'], path
                    stored += 1
                else:
                    assert row['content_omitted_reason'], path
                    omitted += 1
                checked += 1
    return {'status': 'verified', 'repository': repository, 'tree_entries_checked': checked,
            'stored_entries_byte_identical': stored, 'omitted_entries_recorded': omitted}
