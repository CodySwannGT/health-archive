"""Agent tools over the configured health database, the archive's system of record.

Queries are bounded and read-only. Write commands change the database-owned
archive atomically and never read the local checkout except for an explicit put.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

from common import ROOT, DB, MODEL, connect, connection_settings, current_account, embedding_model, cypher, json_out, digest


def status(conn):
    account=current_account()
    return {
      'database':'health', 'user_id':account.user_id, 'patient_id':account.patient_id, 'patient_scope':account.patient_scope,
      'timezone':account.timezone,
      'connection':{'host':conn.info.host,'port':conn.info.port,'role':conn.info.user,
                    'tls':conn.execute('SELECT ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid()').fetchone()['ssl']},
      'postgres':conn.execute('SELECT version() AS version').fetchone()['version'],
      'extensions':conn.execute("SELECT extname,extversion FROM pg_extension WHERE extname IN ('age','vector') ORDER BY extname").fetchall(),
      'records_by_source':conn.execute('SELECT source,source_kind,count(*) AS records FROM health.records GROUP BY source,source_kind ORDER BY source,source_kind').fetchall(),
      'resources_by_type':conn.execute('SELECT resource_type,count(*) AS resources FROM health.resources GROUP BY resource_type ORDER BY resource_type').fetchall(),
      'artifacts':conn.execute('SELECT count(*) AS paths,sum(size_bytes) AS bytes FROM health.artifacts').fetchone(),
      'versions':conn.execute('SELECT (SELECT count(*) FROM health.record_versions) AS source_versions,(SELECT count(*) FROM health.resource_versions) AS resource_versions').fetchone(),
      'documents':conn.execute('SELECT evidence_kind,is_current,count(*) AS documents FROM health.documents GROUP BY evidence_kind,is_current ORDER BY evidence_kind,is_current').fetchall(),
      'embeddings':conn.execute('SELECT count(*) AS chunks,count(embedding) AS embedded_chunks,count(DISTINCT embedding_model) AS model_versions FROM health.chunks').fetchone(),
      'graph':conn.execute('SELECT health.graph_counts(%s) AS counts',(account.patient_id,)).fetchone()['counts'],
      'issues':conn.execute('SELECT severity,reason,count(*) AS occurrences FROM health.import_issues GROUP BY severity,reason ORDER BY occurrences DESC').fetchall(),
      'cross_provider_duplicates':'Separate source identities are retained. POSSIBLE_SAME_SOURCE_EVENT edges are review candidates, not merged facts.'
    }


def search(conn, question, limit=10, research=False, history=False, conversation=False):
    model_ids=conn.execute('SELECT DISTINCT c.embedding_model,m.model_name,m.files FROM health.chunks c JOIN health.embedding_models m ON m.model_id=c.embedding_model').fetchall()
    if not model_ids:
        # A new archive has no indexed passages yet.
        return {'question':question,'matches':[],'scope':{'patient_id':current_account().patient_id,'include_research':research,'include_historical_documents':history,'include_conversation':conversation}}
    if len(model_ids)!=1 or model_ids[0]['model_name']!=MODEL:
        raise RuntimeError('Embedding model mismatch: re-run ingest before searching')
    for item in model_ids[0]['files']:
        if '/snapshots/' not in item['path']:continue
        path=DB/'models'/item['path']
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=item['sha256']:
            raise RuntimeError('Local model file differs from indexed embeddings: re-run setup and ingest before searching')
    model=embedding_model()
    vector=next(model.query_embed(question))
    found=conn.execute('''
      WITH eligible AS (
        SELECT c.*,d.source,d.title,d.source_path,d.record_id,d.clinical_date,d.evidence_kind,d.provenance,d.is_current
        FROM health.chunks c JOIN health.documents d USING(document_id)
        WHERE c.patient_id=%s AND (%s OR d.is_current)
        AND (%s OR d.evidence_kind NOT IN ('derived_research','patient_hypothesis'))
        AND (%s OR d.source<>'health_conversation')
      ), semantic AS (
        SELECT chunk_id, row_number() OVER (ORDER BY embedding <=> %s) AS rank,
               1-(embedding <=> %s) AS similarity FROM eligible ORDER BY embedding <=> %s LIMIT 80
      ), lexical AS (
        SELECT chunk_id,row_number() OVER (ORDER BY ts_rank_cd(search_vector,websearch_to_tsquery('english',%s)) DESC) AS rank
        FROM eligible WHERE search_vector @@ websearch_to_tsquery('english',%s)
        ORDER BY ts_rank_cd(search_vector,websearch_to_tsquery('english',%s)) DESC LIMIT 80
      ), scored AS (
        SELECT e.*,s.similarity,COALESCE(1.0/(60+s.rank),0)+COALESCE(1.0/(60+l.rank),0) AS score
        FROM eligible e LEFT JOIN semantic s USING(chunk_id) LEFT JOIN lexical l USING(chunk_id)
        WHERE s.rank IS NOT NULL OR l.rank IS NOT NULL
      ), ranked AS (
        SELECT *,row_number() OVER (PARTITION BY document_id ORDER BY score DESC) AS per_document FROM scored
      ) SELECT chunk_id,document_id,record_id,title,source,source_path,clinical_date,evidence_kind,is_current,
          ordinal,start_char,end_char,text_content AS excerpt,provenance,similarity,score
        FROM ranked WHERE per_document<=2 ORDER BY score DESC LIMIT %s
    ''',(current_account().patient_id,history,research,conversation,vector,vector,vector,question,question,question,limit)).fetchall()
    return {'question':question,'retrieval':'pgvector semantic + PostgreSQL full-text reciprocal rank fusion with local embeddings',
            'scope':{'patient_id':current_account().patient_id,'include_research':research,'include_historical_documents':history,'include_conversation':conversation},
            'instruction':'These are retrieved evidence passages, not a diagnosis. Read exact sources and use clinical dates and assertion states before drawing conclusions.',
            'matches':found}


def record(conn, rid):
    row=conn.execute('SELECT * FROM health.current_records WHERE record_id=%s',(rid,)).fetchone()
    if not row:
        matches=conn.execute('SELECT record_id,source,source_kind,source_record_id FROM health.records WHERE source_record_id=%s',(rid,)).fetchall()
        return {'matches':matches,'hint':'Use the database record_id to read one record.'}
    return {'record':row,'resources':conn.execute('SELECT * FROM health.resources WHERE record_id=%s',(rid,)).fetchall(),
            'versions':conn.execute('SELECT version_sha256,source_path,payload,source_provenance FROM health.record_versions WHERE record_id=%s ORDER BY first_seen_run,version_sha256',(rid,)).fetchall()}


def graph(conn, rid, limit):
    return {'root':rid,'relationships':[r['value'] for r in conn.execute('SELECT health.graph_neighbors(%s,%s) AS value',(rid,limit))]}


def execute(conn,args):
    """Run one read-only query command. Shared by the CLI and the MCP server."""
    result=None
    if args.command=='status':result=status(conn)
    elif args.command=='search':result=search(conn,args.question,args.limit,args.include_research,args.history,args.include_conversation)
    elif args.command=='record':result=record(conn,args.record_id)
    elif args.command=='graph':result=graph(conn,args.record_id or current_account().patient_id,args.limit)
    elif args.command=='document':
        result=conn.execute('SELECT * FROM health.documents WHERE document_id=%s',(args.document_id,)).fetchone()
    elif args.command=='query':
        query=args.file.read_text() if args.file else args.sql
        if not query:cli.error('Provide SQL or --file')
        with conn.cursor() as cur:
            cur.execute(query,prepare=False)
            fetched=cur.fetchmany(args.limit+1)
            result={'rows':fetched[:args.limit],'truncated':len(fetched)>args.limit,'limit':args.limit}
    elif args.command=='labs':
        result=conn.execute('''SELECT resource_id,source,source_record_id,name,clinical_date,clinical_date_role,numeric_value,comparator,value_text,unit_source,reference_text,flag_source,code_system,code,assertion,provenance
          FROM health.observations WHERE category IN ('laboratory','lab_results') AND name ILIKE %s ORDER BY clinical_date DESC NULLS LAST,name LIMIT %s''',('%'+args.name+'%',args.limit)).fetchall()
    elif args.command=='timeline':
        result=conn.execute('''SELECT x.resource_id,r.source,x.resource_type,x.category,x.name,x.clinical_date,x.clinical_date_role,x.recorded_at,x.assertion,x.evidence_kind,x.value_text,x.unit_source,x.provenance
          FROM health.resources x JOIN health.records r USING(record_id)
          WHERE x.is_current AND x.name ILIKE %s AND (%s::date IS NULL OR x.clinical_date>=%s::date) AND (%s::date IS NULL OR x.clinical_date<=%s::date)
          ORDER BY x.clinical_date DESC NULLS LAST,x.recorded_at DESC NULLS LAST LIMIT %s''',('%'+args.name+'%',args.from_date,args.from_date,args.to_date,args.to_date,args.limit)).fetchall()
    elif args.command=='medications':
        result=conn.execute('''SELECT x.resource_id,r.source,x.name,x.status,x.clinical_date,x.clinical_date_role,x.evidence_kind,x.normalized,x.provenance
          FROM health.resources x JOIN health.records r USING(record_id)
          WHERE x.is_current AND (x.resource_type IN ('MedicationStatement','MedicationRequest','MedicationAdministration') OR (%s AND x.category='medication_change'))
          AND (%s OR r.source='patient_medications') ORDER BY x.clinical_date DESC NULLS LAST,x.name''',(args.changes,args.all_sources)).fetchall()
    elif args.command=='anthropometrics':
        result=conn.execute("SELECT resource_id,record_id,is_current,name,clinical_date,clinical_date_role,recorded_at,numeric_value,unit_source,unit_ucum,assertion,evidence_kind,normalized,provenance FROM health.resources WHERE (is_current OR %s) AND category IN ('anthropometric_measurement','body_composition_measurement','measurement_context') ORDER BY clinical_date DESC NULLS LAST,recorded_at DESC NULLS LAST,name",(args.history,)).fetchall()
    elif args.command=='vitals':
        result=conn.execute("SELECT x.resource_id,x.record_id,r.source,x.name,x.clinical_date,x.clinical_date_role,x.recorded_at,x.numeric_value,x.unit_source,x.unit_ucum,x.evidence_kind,x.normalized->'source_details'->'details'->>'reading_key' AS reading_key,x.provenance FROM health.resources x JOIN health.records r USING(record_id) WHERE x.is_current AND x.category IN ('vital_sign_measurement','vital-signs') ORDER BY x.clinical_date DESC NULLS LAST,x.recorded_at DESC NULLS LAST,x.name").fetchall()
    elif args.command=='history':
        result=conn.execute('''SELECT c.repository,c.commit_sha,c.committed_at,c.author_name,split_part(c.message,E'\\n',1) AS subject,h.sha256,h.size_bytes,h.content_omitted_reason
          FROM health.source_history h JOIN health.source_commits c USING(patient_id,repository,commit_sha)
          WHERE h.patient_id=current_setting('health.patient_id') AND h.path=%s AND (%s::text IS NULL OR c.repository=%s)
          ORDER BY c.committed_at,c.commit_sha''',(args.path,args.repository,args.repository)).fetchall()
        changes,last=[],None
        for row in result:
            # Report only commits where the file's content changed.
            if row['sha256']!=last or row['content_omitted_reason']:changes.append(row)
            last=row['sha256']
        result={'path':args.path,'versions':changes,'commits_containing_path':len(result)}
    elif args.command=='ontology':
        if args.category:
            result=conn.execute('SELECT * FROM health.ontology_categories WHERE category=%s',(args.category,)).fetchone()
            if result:result['current_resources']=conn.execute('SELECT count(*) AS n FROM health.resources WHERE is_current AND category=%s',(args.category,)).fetchone()['n']
        elif args.relations:
            result=conn.execute('SELECT o.relation,o.semantics,o.description,o.establishes_causality,(SELECT count(*) FROM health.relationships r WHERE r.relation=o.relation) AS edges FROM health.ontology_relations o ORDER BY relation').fetchall()
        else:
            result=conn.execute('SELECT o.category,o.domain,o.description,o.schema_path,(SELECT count(*) FROM health.resources x WHERE x.is_current AND x.category=o.category) AS current_resources FROM health.ontology_categories o ORDER BY domain,category').fetchall()
    elif args.command=='profile':
        result=conn.execute("SELECT resource_id,name,recorded_at,assertion,evidence_kind,normalized,provenance FROM health.resources WHERE is_current AND category='patient_demographics' ORDER BY recorded_at DESC NULLS LAST,name").fetchall()
    elif args.command=='activity':
        result=conn.execute("SELECT resource_id,name,category,clinical_date,clinical_date_role,recorded_at,assertion,evidence_kind,normalized,provenance FROM health.resources WHERE is_current AND category IN ('exercise_routine','exercise_session') ORDER BY recorded_at DESC NULLS LAST,name").fetchall()
    elif args.command=='symptoms':
        result=conn.execute("SELECT resource_id,name,category,clinical_date,clinical_date_role,recorded_at,assertion,value_text,normalized,provenance FROM health.resources WHERE is_current AND category IN ('symptom_observation','recurring_pattern','exposure_report','reported_exposure','reported_association','patient_hypothesis','medication_response','patient_context','care_preference') ORDER BY recorded_at DESC NULLS LAST,name").fetchall()
    return result


def main():
    cli=argparse.ArgumentParser(description=__doc__)
    sub=cli.add_subparsers(dest='command',required=True)
    sub.add_parser('status')
    sub.add_parser('bootstrap')
    i=sub.add_parser('ingest',help='Reproject the database-owned archive. Reads no local files unless --from-disk')
    i.add_argument('--from-disk',action='store_true',help='Also put local archive files that are new or changed; stale or record-dropping files are refused and reported')
    p=sub.add_parser('put',help='Store local archive files as new database versions, refusing stale or record-dropping versions');p.add_argument('paths',nargs='+')
    rt=sub.add_parser('retire',help='Remove archive files from the current archive, keeping their history');rt.add_argument('paths',nargs='+');rt.add_argument('--reason',required=True)
    rn=sub.add_parser('record-narrative',help='Record a reviewed symptom/lifestyle extraction against its exact source text')
    rn.add_argument('source',type=Path);rn.add_argument('extraction',type=Path);rn.add_argument('--revise',action='store_true');rn.add_argument('--dry-run',action='store_true')
    rm=sub.add_parser('record-medication',help='Record a reviewed supplemental medication regimen, change or administration row')
    rm.add_argument('source',type=Path);rm.add_argument('row',type=Path);rm.add_argument('--report',required=True,help='patient-reports directory name, such as 2026-10-24-dose-change');rm.add_argument('--dry-run',action='store_true')
    ih=sub.add_parser('import-history',help='Preserve a Git repository\'s full history in this patient\'s database archive')
    ih.add_argument('--repository',required=True);ih.add_argument('--git-dir',type=Path,required=True)
    ih.add_argument('--omit-content',action='append',default=[],help='Path prefix whose content is recorded by size and blob ID only')
    ih.add_argument('--verify',action='store_true',help='Reconstruct every commit tree from the database and compare with Git')
    hs=sub.add_parser('history',help='Versions of an archive or repository file across preserved history');hs.add_argument('path');hs.add_argument('--repository')
    sub.add_parser('seed',help='Store the template archive (ontology and schemas) as the first files of a new patient archive')
    sub.add_parser('export',help='Write the database archive files into this checkout for Git backup. Local-only files are reported, never deleted')
    o=sub.add_parser('ontology',help='Show registered categories and relations, with JSON Schema for one category');o.add_argument('--category');o.add_argument('--relations',action='store_true')
    sub.add_parser('verify')
    sub.add_parser('start')
    sub.add_parser('backup')
    c=sub.add_parser('capture-conversation');c.add_argument('--session',type=Path,required=True)
    c.add_argument('--exclude-range',nargs=2,action='append',default=[],metavar=('START','END_EXCLUSIVE'),help='Persist a user-requested no-record interval using timezone-aware timestamps')
    s=sub.add_parser('search');s.add_argument('question');s.add_argument('--limit',type=int,default=10);s.add_argument('--include-research',action='store_true');s.add_argument('--history',action='store_true');s.add_argument('--include-conversation',action='store_true')
    q=sub.add_parser('query');q.add_argument('sql',nargs='?');q.add_argument('--file',type=Path);q.add_argument('--limit',type=int,default=200)
    r=sub.add_parser('record');r.add_argument('record_id')
    d=sub.add_parser('document');d.add_argument('document_id')
    g=sub.add_parser('graph');g.add_argument('record_id',nargs='?');g.add_argument('--limit',type=int,default=40)
    l=sub.add_parser('labs');l.add_argument('--name',default='');l.add_argument('--limit',type=int,default=100)
    t=sub.add_parser('timeline');t.add_argument('--name',default='');t.add_argument('--from',dest='from_date');t.add_argument('--to',dest='to_date');t.add_argument('--limit',type=int,default=100)
    m=sub.add_parser('medications');m.add_argument('--all-sources',action='store_true');m.add_argument('--changes',action='store_true')
    sub.add_parser('symptoms')
    sub.add_parser('activity')
    sub.add_parser('profile', help='Retrieve source-backed patient-reported demographics and their report dates')
    sub.add_parser('snapshot', help='Write a complete, restore-verified snapshot for the private Git archive')
    sub.add_parser('vitals', help='Retrieve patient-reported blood pressure and pulse plus OneBlood donor vital signs')
    a=sub.add_parser('anthropometrics');a.add_argument('--history',action='store_true',help='Include original measurements superseded by explicit patient corrections')
    args=cli.parse_args()
    if getattr(args,'limit',100)<1 or getattr(args,'limit',100)>1000:cli.error('limit must be between 1 and 1000')
    if args.command=='start':
        if connection_settings().get('sslmode') == 'verify-full':
            with connect() as conn:
                conn.execute('SELECT 1')
            json_out({'status':'connected','target':'configured remote database','local_container_started':False})
            return
        subprocess.run(['docker','compose','-f',str(DB/'compose.yaml'),'up','-d'],check=True);return
    if args.command=='bootstrap':
        from bootstrap import bootstrap
        json_out(bootstrap());return
    if args.command=='ingest':
        from ingest import ingest
        json_out(ingest(from_disk=args.from_disk,operation='ingest-from-disk' if args.from_disk else 'reproject'));return
    if args.command in ('put','retire','record-narrative','record-medication'):
        import writes
        if args.command=='put':json_out(writes.put(args.paths))
        elif args.command=='retire':json_out(writes.retire(args.paths,args.reason))
        elif args.command=='record-narrative':json_out(writes.record_narrative(args.source.resolve(),args.extraction.resolve(),args.revise,args.dry_run))
        else:json_out(writes.record_medication(args.source.resolve(),args.row.resolve(),args.report,args.dry_run))
        return
    if args.command=='import-history':
        from import_history import import_history, verify_history
        json_out(verify_history(args.repository,args.git_dir) if args.verify else import_history(args.repository,args.git_dir,args.omit_content));return
    if args.command=='seed':
        from ingest import ingest
        template=ROOT/'template'
        json_out(ingest(put_paths=sorted(str(p.relative_to(template)) for p in template.rglob('*') if p.is_file() and p.name!='.DS_Store'),put_root=template,operation='seed-archive'));return
    if args.command=='export':
        from workspace import export
        with connect(admin=True) as conn:json_out(export(conn))
        return
    if args.command=='verify':
        from verify import verify
        json_out(verify());return
    if args.command=='backup':
        from backup import backup
        json_out(backup());return
    if args.command=='snapshot':
        from publish_snapshot import publish_snapshot
        json_out(publish_snapshot());return
    if args.command=='capture-conversation':
        import writes
        json_out(writes.capture_conversation(args.session.resolve(),args.exclude_range));return
    with connect() as conn:
        json_out(execute(conn,args))


if __name__=='__main__':main()
