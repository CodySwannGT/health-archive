"""Restore a backup into a temporary database and compare real contents and queries."""
from datetime import datetime,timezone
import json
from pathlib import Path
import uuid
from psycopg import sql
from common import connect, DB, current_account, digest


def snapshot(conn):
    tables=['users','patients','patient_access','ingestion_runs','artifacts','artifact_versions','records','record_versions','source_record_aliases',
            'resources','resource_versions','relationships','documents','chunks','embedding_models','import_issues']
    result={}
    for table in tables:
        # JSON text includes all vector components, source payloads, dates and identities.
        rows=conn.execute('SELECT row_to_json(t)::text AS row FROM health.'+table+' t ORDER BY row_to_json(t)::text').fetchall()
        result[table]={'rows':len(rows),'content_sha256':digest([r['row'] for r in rows])}
    result['source_bytes']=conn.execute('SELECT count(*) AS contents,sum(octet_length(bytes)) AS bytes FROM health.artifact_contents').fetchone()
    result['graph']=conn.execute('SELECT health.graph_counts(%s) AS counts',(current_account().patient_id,)).fetchone()['counts']
    entities=conn.execute('SELECT id::text AS id,properties::text AS properties FROM health_graph."Entity" ORDER BY id').fetchall()
    edges=conn.execute('SELECT id::text AS id,start_id::text AS start_id,end_id::text AS end_id,properties::text AS properties FROM health_graph."RELATED" ORDER BY id').fetchall()
    result['graph_entity_contents_sha256']=digest(entities)
    result['graph_edge_contents_sha256']=digest(edges)
    return result


def verify_restore(backup):
    name='health_restore_verify_'+uuid.uuid4().hex[:12]
    created=False
    try:
        with connect(admin=True, vectors=False, database='postgres') as admin:
            admin.autocommit = True
            admin.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
        created=True
        from restore_snapshot import restore_snapshot
        import hashlib
        restore = restore_snapshot(backup, database=name,
                                   expected_sha256=hashlib.sha256(Path(backup).read_bytes()).hexdigest())
        from repair_restored_age import repair_restored_age
        with connect(admin=True,database=name) as restored:
            repair=restore['age_restore_catalog_repair']
            assert repair_restored_age(restored)['repaired_graphs']==0
        with connect(admin=True) as original,connect(admin=True,database=name) as restored:
            expected=snapshot(original);actual=snapshot(restored)
            assert expected==actual,'Restored contents differ'
            hashes=original.execute('SELECT sha256,encode(sha256(bytes),\'hex\') AS actual FROM health.artifact_contents ORDER BY sha256').fetchall()
            restored_hashes=restored.execute('SELECT sha256,encode(sha256(bytes),\'hex\') AS actual FROM health.artifact_contents ORDER BY sha256').fetchall()
            assert hashes==restored_hashes and all(r['sha256']==r['actual'] for r in hashes)
        with connect(database=name) as reader:
            assert reader.execute("SELECT count(*) AS n FROM health.observations WHERE name='pulse'").fetchone()['n']>0
            assert reader.execute('SELECT health.graph_neighbors(%s,1) AS edge',(current_account().patient_id,)).fetchone()
            assert reader.execute('SELECT count(*) AS n FROM health.chunks WHERE embedding IS NOT NULL').fetchone()['n']>0
            nearest=reader.execute('SELECT embedding <=> (SELECT embedding FROM health.chunks WHERE embedding IS NOT NULL LIMIT 1) AS distance FROM health.chunks WHERE embedding IS NOT NULL ORDER BY distance LIMIT 1').fetchone()
            assert abs(nearest['distance'])<0.00001
            assert reader.execute("SELECT count(*) AS n FROM health.chunks WHERE search_vector<>''::tsvector").fetchone()['n']>0
        result={'status':'verified','backup':str(backup),'restored_at':datetime.now(timezone.utc).isoformat(),
                'tables_compared_by_all_row_contents':actual,'source_content_hashes_verified':len(hashes),
                'age_restore_catalog_repair':repair,'catalog_repair_idempotent':True,
                'portable_restore':restore,
                'restored_reader_graph_and_vector_queries_passed':True,'temporary_restore_database_removed':True}
        (DB/'artifacts/restore-verification.json').write_text(json.dumps(result,indent=2,default=str)+'\n')
        return result
    finally:
        if created:
            with connect(admin=True, vectors=False, database='postgres') as admin:
                admin.autocommit = True
                admin.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(name)))


if __name__=='__main__':
    import sys
    from common import json_out
    json_out(verify_restore(sys.argv[1]))
