"""Restore a checked snapshot into an empty configured database and recreate cluster permissions."""
import hashlib
from pathlib import Path
from pg_tools import run_pg_tool

from common import connect, json_out


def restore_snapshot(path, database='health', expected_sha256=None):
    path = Path(path).resolve()
    checksum = Path(str(path) + '.sha256')
    if expected_sha256 is None:
        expected_sha256 = checksum.read_text().split()[0]
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != expected_sha256:
        raise ValueError('Snapshot checksum mismatch; database was not changed.')
    with path.open('rb') as stream:
        if stream.read(5) != b'PGDMP':
            raise ValueError('Expected a PostgreSQL custom-format snapshot.')
    with connect(admin=True, vectors=False, database=database) as conn:
        schemas = conn.execute("""SELECT nspname FROM pg_namespace
            WHERE nspname NOT LIKE 'pg_%'
            AND nspname NOT IN ('public','information_schema')""").fetchall()
        public_objects = conn.execute("""SELECT count(*) AS n FROM pg_class c
            JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public'""").fetchone()['n']
        if schemas or public_objects:
            raise RuntimeError('Snapshot restore requires an empty database. Existing data was not changed.')
    with path.open('rb') as stream:
        run_pg_tool('pg_restore', ['--exit-on-error', '--single-transaction',
                                  '--no-owner', '--no-privileges'], database=database, stdin=stream)
    from repair_restored_age import repair_restored_age
    with connect(admin=True, database=database) as conn:
        repair = repair_restored_age(conn)
    # Database roles and passwords are cluster-local, outside pg_dump. Rebuild
    # scoped permissions using this machine's independently generated secrets.
    from bootstrap import bootstrap
    bootstrap(database=database)
    return {'status': 'restored', 'database': database, 'snapshot': str(path),
            'sha256': actual, 'age_restore_catalog_repair': repair,
            'local_permissions_recreated': True}


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('snapshot', type=Path)
    parser.add_argument('--database', default='health')
    args = parser.parse_args()
    json_out(restore_snapshot(args.snapshot, args.database))
