"""Exercise correction, repeat import and conflict behavior in a rolled-back transaction."""
import json
from common import connect, DB, current_account, digest
from ingest import Importer


def verify_delta():
    conn=connect(admin=True)
    try:
        baseline=conn.execute('SELECT (SELECT count(*) FROM health.records) AS records,(SELECT count(*) FROM health.record_versions) AS versions,(SELECT count(*) FROM health.resources) AS resources,(SELECT count(*) FROM health.relationships) AS relationships').fetchone()
        conn.execute("SELECT pg_advisory_xact_lock(hashtext('health_archive_ingest'))")
        run=conn.execute('SELECT max(run_id) AS n FROM health.ingestion_runs').fetchone()['n']
        imp=Importer(conn,run)
        native='verification_only_delta'
        path='symptoms/records.jsonl'
        p1={'patient_scope':current_account().patient_scope,'value':'1','fixture':'rolled_back'}
        p2={**p1,'value':'2'}
        rid,v1=imp.record('verification_fixture','delta',native,p1,path)
        imp.resource(rid,'Rolled-back verification fixture',value='1',evidence='verification_fixture')
        rid2,v2=imp.record('verification_fixture','delta',native,p2,path)
        imp.resource(rid2,'Rolled-back verification fixture',value='2',evidence='verification_fixture')
        assert rid==rid2 and v1!=v2
        assert conn.execute('SELECT count(*) AS n FROM health.record_versions WHERE record_id=%s',(rid,)).fetchone()['n']==2
        assert conn.execute('SELECT count(*) AS n FROM health.resource_versions WHERE resource_id=%s',(rid,)).fetchone()['n']==2
        assert conn.execute('SELECT payload FROM health.current_records WHERE record_id=%s',(rid,)).fetchone()['payload']['value']=='2'
        assert conn.execute('SELECT numeric_value FROM health.resources WHERE resource_id=%s',(rid,)).fetchone()['numeric_value']==2
        counts=conn.execute('SELECT (SELECT count(*) FROM health.records) AS records,(SELECT count(*) FROM health.record_versions) AS versions,(SELECT count(*) FROM health.resource_versions) AS projections').fetchone()
        imp.record('verification_fixture','delta',native,p2,path)
        imp.resource(rid,'Rolled-back verification fixture',value='2',evidence='verification_fixture')
        assert counts==conn.execute('SELECT (SELECT count(*) FROM health.records) AS records,(SELECT count(*) FROM health.record_versions) AS versions,(SELECT count(*) FROM health.resource_versions) AS projections').fetchone()
        try:
            imp.record('verification_fixture','delta','wrong_patient',{'patient_scope':'someone_else'},path)
        except ValueError:
            pass
        else:raise AssertionError('Patient conflict accepted')
        first=Importer(conn,run)
        a=first.lab_observation('verification_fixture','fixture_report','page:1:line:10','Fixture analyte','1',path,'2026-01-01',unit='mg/dL')
        shifted=Importer(conn,run)
        b=shifted.lab_observation('verification_fixture','fixture_report','page:2:line:99','Fixture analyte','2',path,'2026-01-01',unit='mg/dL')
        assert a==b,'Reordered result created a duplicate record'
        assert conn.execute('SELECT count(*) AS n FROM health.source_record_aliases WHERE record_id=%s',(a,)).fetchone()['n']==1
        assert conn.execute('SELECT count(*) AS n FROM health.record_versions WHERE record_id=%s',(a,)).fetchone()['n']==2
        assert conn.execute('SELECT numeric_value FROM health.resources WHERE resource_id=%s',(a,)).fetchone()['numeric_value']==2
        with connect() as reader:
            assert reader.execute('SELECT count(*) AS n FROM health.records WHERE source=%s',('verification_fixture',)).fetchone()['n']==0
        conn.rollback()
        after=conn.execute('SELECT (SELECT count(*) FROM health.records) AS records,(SELECT count(*) FROM health.record_versions) AS versions,(SELECT count(*) FROM health.resources) AS resources,(SELECT count(*) FROM health.relationships) AS relationships').fetchone()
        assert baseline==after
        assert conn.execute("SELECT count(*) AS n FROM health.records WHERE source='verification_fixture'").fetchone()['n']==0
        result={'status':'verified','correction_reuses_stable_identity':True,'previous_source_and_projection_versions_retained':True,
                'unchanged_repeat_adds_no_records_or_versions':True,'patient_conflict_rejected':True,
                'result_page_and_line_reordering_reuses_identity':True,
                'uncommitted_fixture_invisible_to_reader':True,'fixture_fully_rolled_back':True}
        (DB/'artifacts/delta-verification.json').write_text(json.dumps(result,indent=2)+'\n')
        return result
    finally:conn.rollback();conn.close()


if __name__=='__main__':
    from common import json_out
    json_out(verify_delta())
