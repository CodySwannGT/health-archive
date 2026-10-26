"""Verify database coverage against original archives and exercise read tools."""
import collections
import hashlib
import json
from pathlib import Path

import psycopg
from common import ROOT, DB, connect, current_account, digest, canonical


def verify():
    """Verify the database's own archive and projections. The local checkout is only compared, as drift."""
    global ROOT
    import tempfile
    import ingest
    checkout=ROOT
    with tempfile.TemporaryDirectory(prefix='health-verify-') as tmp:
        ROOT=ingest.ROOT=Path(tmp)
        try:
            return _verify(checkout)
        finally:
            ROOT=ingest.ROOT=checkout


def _verify_semantics(conn,patient):
    """Current records carry registered clinical types and concepts, and current edges follow the relation rules."""
    from semantics import Registry, concept_eligible
    definition=lambda table,key:[r['definition'] for r in conn.execute(f'SELECT definition FROM health.{table} WHERE patient_id=%s ORDER BY {key}',(patient,))]
    types=[dict(r) for r in conn.execute('SELECT clinical_type,label,parent,fhir_resource,concept_family,description FROM health.clinical_types WHERE patient_id=%s',(patient,))]
    registry=Registry(definition('ontology_categories','category'),definition('ontology_relations','relation'),types,[])
    resources={r['resource_id']:r for r in conn.execute('''SELECT resource_id,category,name,clinical_type,concept_id,assertion,
      coalesce(normalized#>>'{source_details,details,symptom_key}',normalized#>>'{source_details,details,exposure_key}') AS key
      FROM health.resources WHERE patient_id=%s AND is_current''',(patient,))}
    untyped=[rid for rid,r in resources.items() if r['clinical_type'] not in registry.types]
    assert not untyped,('records without a registered clinical type',untyped[:5])
    concepts={r['concept_id'] for r in conn.execute('SELECT concept_id FROM health.concepts WHERE patient_id=%s',(patient,))}
    missing=[rid for rid,r in resources.items() if concept_eligible(registry,r['category'],r['name'],r['assertion'],r['key']) and r['concept_id'] not in concepts]
    assert not missing,('concept-eligible records without a concept',missing[:5])
    every={r['resource_id']:r for r in conn.execute('SELECT resource_id,category,clinical_type FROM health.resources WHERE patient_id=%s',(patient,))}
    documents={r['document_id'] for r in conn.execute('SELECT document_id FROM health.documents WHERE patient_id=%s',(patient,))}
    users={r['user_id'] for r in conn.execute('SELECT user_id FROM health.patient_access WHERE patient_id=%s',(patient,))}
    def endpoint(node):
        if node in every:return ('@Resource',every[node]['category'],every[node]['clinical_type'])
        if node==patient:return ('@Patient',None,None)
        if node in users:return ('@User',None,None)
        if node in documents:return ('@Document',None,None)
        for prefix,kind in (('ontology:','@OntologyCategory'),('concept:','@Concept'),('type:','@ClinicalType')):
            if node.startswith(prefix):return (kind,None,None)
        return None
    edges=[(r['from_id'],r['to_id'],r['relation'],r['qualifiers']) for r in conn.execute('SELECT from_id,to_id,relation,qualifiers FROM health.relationships WHERE patient_id=%s AND is_current',(patient,))]
    problems=registry.edge_problems(edges,endpoint)
    assert not problems,problems[:10]
    return {'clinical_types':len(registry.types),'concepts':len(concepts),'current_records_with_concepts':sum(1 for r in resources.values() if r['concept_id']),
            'current_edges_follow_relation_rules':len(edges)}


def _verify_connections(conn,patient):
    """Stored associations equal a fresh computation from current records, cite only current records, and pooled
    patterns equal a fresh aggregate of opted-in charts with at least the minimum number of contributors."""
    from associations import MIN_CONTRIBUTORS, find_associations, load_facts, pooled_patterns
    ids=[r['resource_id'] for r in conn.execute('SELECT resource_id FROM health.resources WHERE patient_id=%s AND is_current',(patient,))]
    expected={a['association_id']:digest(a) for a in find_associations(load_facts(conn,patient,ids))}
    stored={r['association_id']:r for r in conn.execute('SELECT association_id,version_sha256,supporting_records,contrary_records FROM health.associations WHERE patient_id=%s AND is_current',(patient,))}
    assert {k:r['version_sha256'] for k,r in stored.items()}==expected,('associations differ from a fresh computation',sorted(set(stored)^set(expected))[:5])
    current=set(ids)
    cited={rid for r in stored.values() for rid in r['supporting_records']+r['contrary_records']}
    assert cited<=current,('associations cite non-current records',sorted(cited-current)[:5])
    versions=conn.execute('SELECT count(*) AS n FROM health.associations a WHERE patient_id=%s AND NOT EXISTS (SELECT 1 FROM health.association_versions v WHERE v.patient_id=a.patient_id AND v.association_id=a.association_id AND v.version_sha256=a.version_sha256)',(patient,)).fetchone()['n']
    assert versions==0,('associations without a preserved version',versions)
    rows=conn.execute('''SELECT a.patient_id,a.method,a.exposure_pool_key,a.exposure_label,a.outcome_pool_key,a.outcome_label,a.direction,a.strength,a.effect,a.exposure_brand_keys
      FROM health.associations a JOIN health.pooling_consent c ON c.patient_id=a.patient_id AND c.opted_in WHERE a.is_current''').fetchall()
    fresh={p['pattern_id']:p['contributors'] for p in pooled_patterns([tuple(r.values()) for r in rows],MIN_CONTRIBUTORS)}
    pooled={r['pattern_id']:r['contributors'] for r in conn.execute('SELECT pattern_id,contributors FROM health.pooled_patterns')}
    assert pooled==fresh,'pooled patterns differ from a fresh aggregate of opted-in charts'
    assert all(n>=MIN_CONTRIBUTORS for n in pooled.values())
    columns={r['column_name'] for r in conn.execute("SELECT column_name FROM information_schema.columns WHERE table_schema='health' AND table_name='pooled_patterns'")}
    assert not columns&{'patient_id','user_id','record_id','resource_id','clinical_date'},columns
    consent=conn.execute('SELECT opted_in FROM health.pooling_consent WHERE patient_id=%s',(patient,)).fetchone()
    return {'current_associations':len(stored),'associations_match_fresh_computation':True,
            'pooled_patterns':len(pooled),'pooling_opted_in':bool(consent and consent['opted_in']),'pool_minimum_contributors':MIN_CONTRIBUTORS}


def _verify(checkout):
    from workspace import current_artifacts, local_drift, materialize, tree
    PATIENT=current_account().patient_id
    checks={'patient_id':PATIENT}
    with connect(admin=True) as conn:
        # Compare actual stored bytes with the hashes that identify every archived file.
        artifacts=current_artifacts(conn)
        materialize(conn,ROOT,artifacts)
        assert tree(ROOT)==artifacts,'Materialized archive differs from the database'
        for r in conn.execute('SELECT a.path,a.sha256,encode(sha256(c.bytes),\'hex\') AS actual FROM health.artifacts a JOIN health.artifact_contents c USING(sha256) WHERE a.patient_id=%s AND a.retired_run IS NULL',(PATIENT,)):
            assert r['actual']==r['sha256'],r['path']
        checks['source_files_verified_byte_for_byte']=len(artifacts)
        drift=local_drift(artifacts,checkout)
        checks['local_checkout_drift']={k:{'count':len(v),'paths':v[:20]} for k,v in drift.items()}
        categories={r['category'] for r in conn.execute('SELECT category FROM health.ontology_categories WHERE patient_id=%s',(PATIENT,))}
        relations={r['relation'] for r in conn.execute('SELECT relation FROM health.ontology_relations WHERE patient_id=%s',(PATIENT,))}
        unregistered=[r['category'] for r in conn.execute('SELECT DISTINCT category FROM health.resources WHERE is_current AND patient_id=%s',(PATIENT,)) if r['category'] not in categories]
        assert not unregistered,unregistered
        unregistered=[r['relation'] for r in conn.execute('SELECT DISTINCT relation FROM health.relationships WHERE patient_id=%s',(PATIENT,)) if r['relation'] not in relations]
        assert not unregistered,unregistered
        semantics=_verify_semantics(conn,PATIENT)
        missing=conn.execute("""SELECT count(*) AS n FROM health.resources x WHERE x.is_current AND x.patient_id=%s AND NOT EXISTS
          (SELECT 1 FROM health.relationships r WHERE r.patient_id=x.patient_id AND r.from_id=x.resource_id AND r.relation='INSTANCE_OF' AND r.to_id='ontology:'||x.category)""",(PATIENT,)).fetchone()['n']
        assert missing==0,missing
        checks['ontology']={'categories':len(categories),'relations':len(relations),'current_resources_typed':True,**semantics}
        checks['connections']=_verify_connections(conn,PATIENT)
        current_payloads={r['record_id']:r['payload'] for r in conn.execute('SELECT record_id,payload FROM health.current_records WHERE patient_id=%s',(PATIENT,))}
        version_payloads={(r['record_id'],r['version_sha256']):r['payload'] for r in conn.execute('SELECT v.record_id,v.version_sha256,v.payload FROM health.record_versions v JOIN health.records r USING(record_id) WHERE r.patient_id=%s',(PATIENT,))}
        observations={}
        for r in conn.execute("SELECT source,source_record_id,value_text,numeric_value FROM health.observations WHERE source IN ('labcorp','quest') AND patient_id=%s",(PATIENT,)):
            observations.setdefault((r['source'],r['source_record_id']),r)
        current_documents={(r['title'],r['text_content']) for r in conn.execute('SELECT title,text_content FROM health.documents WHERE is_current AND patient_id=%s',(PATIENT,))}
        from ingest import reviewed_medication_sources
        _,medication_overrides=reviewed_medication_sources()
        # Every authoritative current source row must be present, with its complete payload.
        datasets=[('lab-results/reports.jsonl','labcorp','report','report_id'),
                  ('lab-results/quest/reports.jsonl','quest','report','report_id'),
                  ('meds/medications.jsonl','patient_medications','medication_regimen','medication_id'),
                  ('meds/reported-medications.jsonl','patient_medications','medication_regimen','medication_id'),
                  ('meds/change-log.jsonl','patient_medications','medication_change','event_id'),
                  ('meds/reported-change-log.jsonl','patient_medications','medication_change','event_id'),
                  ('meds/reported-administrations.jsonl','patient_medications','medication_administration','administration_id'),
                  ('symptoms/records.jsonl','patient_symptoms',None,'record_id'),
                  ('conversations/messages.jsonl','health_conversation','conversation_message','message_id'),
                  ('conversations/sessions.jsonl','health_conversation','conversation_session','session_id'),
                  ('vitals/oneblood/observations.jsonl','oneblood','vital','observation_id'),
                  ('health-records/cleveland-clinic/records.jsonl','cleveland_clinic',None,'record_id'),
                  ('health-records/cleveland-clinic/documents.jsonl','cleveland_clinic','document_header','document_id'),
                  ('health-records/cleveland-clinic/narrative-tables.jsonl','cleveland_clinic','narrative_table','table_id')]
        coverage={}
        for path,source,kind,id_field in datasets:
            rows=[json.loads(x) for x in (ROOT/path).read_text().splitlines() if x.strip()] if (ROOT/path).exists() else []
            for row in rows:
                row_kind=kind or row.get('record_type') or row['category']
                native=row.get(id_field) or digest(row)
                rid=digest([PATIENT,source,row_kind,str(native)])
                if path=='meds/medications.jsonl' and native in medication_overrides:
                    actual=version_payloads.get((rid,row['content_sha256']))
                else:
                    actual=current_payloads.get(rid)
                assert actual is not None and actual==row,(path,native)
            coverage[path]=len(rows)
        checks['complete_source_payloads']=coverage
        checks['reviewed_medication_supersessions']=len(medication_overrides)
        conversation_path=ROOT/'conversations/messages.jsonl'
        if conversation_path.exists():
            from jsonschema import Draft202012Validator, FormatChecker
            validator=Draft202012Validator(json.loads((ROOT/'conversations/message.schema.json').read_text()),format_checker=FormatChecker())
            messages=[json.loads(line) for line in conversation_path.read_text().splitlines()]
            for message in messages:
                validator.validate(message)
                prov=message['provenance']
                assert (ROOT/prov['text_path']).read_text()==message['message_text']
                assert hashlib.sha256((ROOT/prov['source_event_path']).read_bytes()).hexdigest()==prov['source_event_sha256']
                assert hashlib.sha256((ROOT/prov['text_path']).read_bytes()).hexdigest()==prov['text_sha256']
                assert message['clinical_assertion']=='not_extracted'
            assert len({m['message_id'] for m in messages})==len(messages)
            checks['conversation_messages_verified']=len(messages)
            # Connector messages stored as source wait for the next write to become archive messages.
            from capture_conversation import CONNECTOR_SESSIONS
            pending=[p.stem for p in sorted((ROOT/CONNECTOR_SESSIONS).glob('*.jsonl'))
                     if not (ROOT/'conversations/sessions'/p.stem/'checkpoint.json').exists()
                     or json.loads((ROOT/'conversations/sessions'/p.stem/'checkpoint.json').read_text())['source_complete_bytes']!=p.stat().st_size]
            checks['connector_sessions_awaiting_capture']=pending
            checks['assistant_messages_remain_nonclinical_audit_context']=True
        clinic_versions=0
        for p in (ROOT/'health-records/cleveland-clinic/versions').glob('*.json'):
            for r in json.loads(p.read_text())['versions']:
                rid=digest([PATIENT,'cleveland_clinic',r['category'],r['record_id']])
                assert (rid,r['version_sha256']) in version_payloads
                clinic_versions+=1
        checks['cleveland_clinic_historical_versions_verified']=clinic_versions
        expected_cards=sum(sum(s['kind']=='result' for s in json.loads(p.read_text())['sections']) for p in (ROOT/'lab-results/portal-reports').glob('*.json'))
        count=conn.execute("SELECT count(*) AS n FROM health.records WHERE patient_id=%s AND source='labcorp' AND source_kind='lab_observation' AND source_record_id LIKE '%%:section:%%'",(PATIENT,)).fetchone()['n']
        assert count==expected_cards
        checks['labcorp_rendered_result_cards_preserved']=count
        local_path=ROOT/'lab-results/imports/observations.jsonl'
        local_obs=[json.loads(x) for x in local_path.read_text().splitlines() if x] if local_path.exists() else []
        for row in local_obs:
            found=observations.get(('labcorp',row['report_id']+':'+row['observation_id']))
            assert found and found['value_text']==row['value']['text']
        checks['local_pdf_observations_verified']=len(local_obs)
        quest_path=ROOT/'lab-results/quest/reports.jsonl'
        quest=[json.loads(x) for x in quest_path.read_text().splitlines() if x] if quest_path.exists() else []
        pages=0
        for report in quest:
            for page in report.get('pages',[]):
                title='Quest report '+report['report_id']+' page:'+str(page['page_number'])
                assert (title,page['text']) in current_documents
                pages+=1
            for i,row in enumerate(report.get('rows',[])):
                native=report['report_id']+':portal:'+str(row.get('ordinal',i))
                found=observations.get(('quest',native))
                assert found and found['value_text']==row['result_text']
        checks['quest_pdf_pages_verified_complete']=pages
        chunks=conn.execute('''SELECT count(*) AS chunks,count(embedding) AS vectors,
          count(*) FILTER (WHERE c.text_content=substring(d.text_content FROM c.start_char+1 FOR c.end_char-c.start_char)) AS exact_excerpts
          FROM health.chunks c JOIN health.documents d USING(document_id) WHERE d.patient_id=%s''',(PATIENT,)).fetchone()
        assert chunks['chunks']==chunks['vectors']==chunks['exact_excerpts']
        checks['embedded_and_source_exact_passages']=chunks['chunks']
        source_ids=conn.execute('SELECT count(*) AS n FROM health.records WHERE patient_id=%s',(PATIENT,)).fetchone()['n']
        resource_versions=conn.execute('SELECT count(*) AS n FROM health.resource_versions v JOIN health.resources x USING(resource_id) WHERE x.patient_id=%s',(PATIENT,)).fetchone()['n']
        checks['database_records']=source_ids
        checks['resource_versions']=resource_versions
        assert conn.execute('SELECT count(*) AS n FROM health.records r LEFT JOIN health.record_versions v ON r.record_id=v.record_id AND r.current_version_sha256=v.version_sha256 WHERE v.record_id IS NULL').fetchone()['n']==0
        checks['all_current_version_references_valid']=True
        aliases=conn.execute('SELECT count(*) AS n FROM health.source_record_aliases WHERE patient_id=%s',(PATIENT,)).fetchone()['n']
        expected_aliases=conn.execute("SELECT count(*) AS n FROM health.records WHERE patient_id=%s AND source_kind='lab_observation'",(PATIENT,)).fetchone()['n']
        assert aliases==expected_aliases
        checks['stable_parsed_result_aliases']=aliases
        graph=conn.execute('SELECT health.graph_counts(%s) AS counts',(PATIENT,)).fetchone()['counts']
        expected_nodes=conn.execute('''SELECT (SELECT count(*) FROM health.resources WHERE patient_id=%(p)s)+(SELECT count(*) FROM health.documents WHERE patient_id=%(p)s)
          +(SELECT count(*) FROM health.ontology_categories WHERE patient_id=%(p)s)+(SELECT count(*) FROM health.clinical_types WHERE patient_id=%(p)s)
          +(SELECT count(*) FROM health.concepts WHERE patient_id=%(p)s)+2 AS n''',{'p':PATIENT}).fetchone()['n']
        assert graph['vertices']==expected_nodes,(graph,expected_nodes)
        expected_edges=conn.execute('SELECT count(*) AS n FROM health.relationships WHERE patient_id=%(p)s AND is_current',{'p':PATIENT}).fetchone()['n']
        assert graph['edges']==expected_edges,(graph,expected_edges)
        checks['graph']=graph
        checks['unmerged_identity_candidates']=conn.execute("SELECT count(*) AS n FROM health.relationships WHERE patient_id=%s AND relation='POSSIBLE_SAME_SOURCE_EVENT'",(PATIENT,)).fetchone()['n']
        last=conn.execute('SELECT run_id,summary FROM health.ingestion_runs WHERE completed_at IS NOT NULL AND patient_id=%s ORDER BY run_id DESC LIMIT 1',(PATIENT,)).fetchone()
        checks['last_ingestion']=last
        checks['patient_resources']=conn.execute('SELECT count(*) AS n FROM health.resources WHERE patient_id=%s',(PATIENT,)).fetchone()['n']
    from cli import search, graph as graph_tool
    with connect() as reader:
        assert reader.execute('SELECT count(*) AS n FROM health.resources WHERE patient_id=%s',(PATIENT,)).fetchone()['n']==checks['patient_resources']
        # Account isolation: the reader sees only patients this user may access.
        foreign=reader.execute('SELECT count(*) AS n FROM health.resources x WHERE NOT EXISTS (SELECT 1 FROM health.patient_access a WHERE a.patient_id=x.patient_id AND a.user_id=current_setting(\'health.user_id\'))').fetchone()['n']
        assert foreign==0,foreign
        checks['reader_sees_only_accessible_patients']=True
        if (ROOT/'vitals/oneblood/observations.jsonl').exists():
            pulse=reader.execute("SELECT count(*) AS n FROM health.observations WHERE source='oneblood' AND name='pulse' AND patient_id=%s",(PATIENT,)).fetchone()['n']
            checks['pulse_observations_accessible']=pulse
            assert pulse>0
        if checks['patient_resources']:
            assert graph_tool(reader,PATIENT,5)['relationships']
        reader.rollback()
        try:
            reader.execute('DELETE FROM health.resources')
        except (psycopg.errors.ReadOnlySqlTransaction,psycopg.errors.InsufficientPrivilege):
            checks['reader_writes_denied']=True
            reader.rollback()
        else:raise AssertionError('Reader could write')
        reader.execute("SELECT set_config('health.user_id','unregistered_user',false)")
        for table in ('users','patients','patient_access','artifacts','artifact_versions','records','record_versions','resources','resource_versions','relationships','documents','chunks','import_issues','source_record_aliases','ontology_categories','ontology_relations'):
            assert reader.execute('SELECT count(*) AS n FROM health.'+table).fetchone()['n']==0,table
        checks['unregistered_account_reads_denied']=True
        reader.commit()
        try:
            reader.execute('SELECT health.graph_neighbors(%s,5)',(PATIENT,)).fetchall()
        except psycopg.Error:
            checks['unregistered_graph_access_denied']=True;reader.rollback()
        else:raise AssertionError('Unauthorized graph access')
    with connect() as reader:
        try:
            reader.execute('SELECT count(*) FROM health_graph."Entity"')
        except psycopg.errors.InsufficientPrivilege:
            checks['raw_graph_access_denied']=True;reader.rollback()
        else:raise AssertionError('Raw graph reader access')
        # Archive-specific retrieval expectations live in the patient's own archive, not in code.
        questions_path=ROOT/'checks/retrieval-questions.json'
        retrieval=[]
        for item in (json.loads(questions_path.read_text())['questions'] if questions_path.exists() else []):
            results=search(reader,item['question'],limit=10)
            assert any(any(e.lower() in m['excerpt'].lower() for e in item['expect_any']) for m in results['matches']),item['question']
            assert not any(m['evidence_kind']=='derived_research' for m in results['matches'])
            retrieval.append({'question_id':item['id'],'top_documents':[m['document_id'] for m in results['matches'][:3]]})
        checks['archive_retrieval_checks']=retrieval
    output={'status':'verified','checks':checks}
    (DB/'artifacts').mkdir(exist_ok=True)
    (DB/'artifacts/verification.json').write_text(json.dumps(output,indent=2,default=str)+'\n')
    return output


if __name__=='__main__':
    from common import json_out
    json_out(verify())
