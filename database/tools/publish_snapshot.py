"""Publish a verified, portable database snapshot for the private Git archive."""
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil

from backup import backup
from common import DB, ROOT
from verify_restore import verify_restore


def publish_snapshot():
    saved = backup()
    verified = verify_restore(saved['path'])
    folder = DB / 'snapshots'
    folder.mkdir(exist_ok=True)
    target = folder / 'health.dump'
    temporary = folder / 'health.dump.tmp'
    shutil.copyfile(saved['path'], temporary)
    temporary.replace(target)
    (folder / 'health.dump.sha256').write_text(saved['sha256'] + '  health.dump\n')
    checkpoints = {}
    for path in sorted((ROOT / 'conversations/sessions').glob('*/checkpoint.json')):
        row = json.loads(path.read_text())
        checkpoints[row['session_id']] = {
            key: row.get(key) for key in ('captured_through_timestamp', 'capture_scope', 'excluded_ranges')
        }
    manifest = {'schema_version': '1.0', 'exported_at': datetime.now(timezone.utc).isoformat(),
                'snapshot': 'health.dump', 'bytes': saved['bytes'], 'sha256': saved['sha256'],
                'includes': ['source bytes and historical versions', 'clinical records and versions',
                             'ingestion history', 'conversation archive through stated checkpoints',
                             'graph entities and relationships', 'search text and vector embeddings'],
                'excludes': ['cluster roles and passwords', 'Python environment', 'embedding model cache'],
                'conversation_checkpoints': checkpoints,
                'restore_verification': {key: verified[key] for key in (
                    'status', 'tables_compared_by_all_row_contents', 'source_content_hashes_verified',
                    'catalog_repair_idempotent', 'restored_reader_graph_and_vector_queries_passed',
                    'temporary_restore_database_removed')}}
    (folder / 'manifest.json').write_text(json.dumps(manifest, indent=2, default=str) + '\n')
    return {'status': 'published', 'path': str(target), 'bytes': saved['bytes'],
            'sha256': saved['sha256'], 'isolated_restore_verified': True}


if __name__ == '__main__':
    from common import json_out
    json_out(publish_snapshot())
