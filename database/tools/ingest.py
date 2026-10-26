"""Atomic, repeatable ingestion of the personal archive with source identity intact."""
import collections
from datetime import datetime
import hashlib
import mimetypes
from pathlib import Path
import re

import json
from bs4 import BeautifulSoup
from psycopg import sql
from common import ROOT, DB, MODEL, PARSER, Jsonb, connect, current_account, digest, canonical, cypher, embedding_model
from measurement_corrections import measurement_supersessions
from semantics import Registry, assign_concepts

NUM = re.compile(r'^\s*(<=|>=|<|>|≤|≥)?\s*(-?\d+(?:\.\d+)?(?:[Ee][+-]?\d+)?)\s*([HL])?\s*$')
UCUM = {'mg/dL':'mg/dL','g/dL':'g/dL','ng/dL':'ng/dL','ng/mL':'ng/mL','pg/mL':'pg/mL',
        'mmol/L':'mmol/L','mIU/L':'m[IU]/L','mIU/mL':'m[IU]/mL','IU/L':'[IU]/L',
        'U/L':'U/L','mcg/dL':'ug/dL','ug/dL':'ug/dL','%':'%','mmHg':'mm[Hg]',
        'beats/min':'/min','degF':'[degF]','mL/min/1.73':'mL/min/{1.73_m2}',
        'mL/min/1.73m2':'mL/min/{1.73_m2}','mL/min/1.73 m2':'mL/min/{1.73_m2}',
        'Thousand/uL':'10*3/uL','Million/uL':'10*6/uL','nmol/L':'nmol/L','lb':'[lb_av]','in':'[in_i]','h':'h'}
SYSTEMS = {'2.16.840.1.113883.6.1':'http://loinc.org',
           '2.16.840.1.113883.6.88':'http://www.nlm.nih.gov/research/umls/rxnorm',
           '2.16.840.1.113883.6.96':'http://snomed.info/sct'}


def load(path):
    return json.loads((ROOT / path).read_text())


def rows(path):
    p = ROOT / path
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()] if p.exists() else []


def date(value):
    if not value:
        return None
    value = str(value)
    for pattern in ('%Y-%m-%d','%m/%d/%Y','%Y%m%d'):
        try:
            return datetime.strptime(value[:10] if pattern != '%Y%m%d' else value[:8], pattern).date().isoformat()
        except ValueError:
            pass
    return None


def source_date(data):
    for timing in data.get('effective_times', []):
        val = timing.get('attributes', {}).get('value')
        if date(val):
            return date(val)
        for bound in timing.get('bounds', []):
            if bound.get('kind') == 'low' and date(bound.get('value')):
                return date(bound['value'])
    return None


def quantity(value):
    m = NUM.fullmatch(str(value or ''))
    if not m:
        return None, None, None
    return float(m[2]), {'≤':'<=','≥':'>='}.get(m[1], m[1] or '='), m[3]


def unit_from_reference(text):
    text = (text or '').replace('Reference Range:', '').strip()
    # Source units only. Do not derive or convert doses or laboratory values.
    for unit in sorted(UCUM, key=len, reverse=True):
        if re.search(r'(?<![A-Za-z])'+re.escape(unit)+r'(?![A-Za-z])', text):
            return unit
    m = re.search(r'(?:\d|NOT ESTABLISHED)\s+([A-Za-z%][A-Za-z0-9/^.*()%-]+)\s*$', text)
    return m[1] if m else None


def validate_identity(payload, account):
    scope = payload.get('patient_scope')
    if scope is not None and scope not in account.patient_scopes:
        raise ValueError('Explicit patient scope does not match the patient profile')
    key = payload.get('patient_key')
    allowed = account.source_patient_keys.get('cleveland_clinic', [])
    if key is not None and key not in allowed:
        raise ValueError('Explicit source patient key does not match local profile')


class Importer:
    def __init__(self, conn, run, preload=False):
        self.conn, self.run = conn, run
        self.account = current_account()
        self.patient = self.account.patient_id
        self.stats = collections.Counter()
        self.current_docs = set()
        self.resource_ids = set()
        self.lookup = {}
        self.observation_occurrences = collections.Counter()
        self.categories = {}
        self.relations = set()
        # Per projected resource: (category, name, code system, code, assertion, structured key), for concepts.
        self.meta = {}
        self.doc_ids = set()
        self.edges = []
        self.clinical_types = {}
        # Read-before-write lookups are cached under the ingestion advisory lock.
        # This permits pipelining writes without changing source/version identity.
        self.known_contents = self.known_documents = self.known_narratives = self.known_aliases = None
        if preload:
            self.known_contents = {r['sha256'] for r in conn.execute('SELECT sha256 FROM health.artifact_contents')}
            self.known_documents = {r['document_id'] for r in conn.execute('SELECT document_id FROM health.documents')}
            self.known_narratives = {(r['record_id'],r['version_sha256']):r['payload'] for r in conn.execute("SELECT v.record_id,v.version_sha256,v.payload FROM health.record_versions v JOIN health.records r USING(record_id) WHERE r.source='patient_symptoms'")}
            self.known_aliases = {(r['source'],r['stable_key']):r['source_record_id'] for r in conn.execute("SELECT a.source,a.stable_key,r.source_record_id FROM health.source_record_aliases a JOIN health.records r USING(record_id) WHERE a.patient_id=%s AND a.source_kind='lab_observation'",(self.patient,))}

    def artifact(self, path):
        content = path.read_bytes()
        sha = hashlib.sha256(content).hexdigest()
        rel = str(path.relative_to(ROOT))
        source = ('quest' if rel.startswith('lab-results/quest/') else 'labcorp' if rel.startswith('lab-results/')
                  else 'cleveland_clinic' if rel.startswith('health-records/cleveland-clinic/')
                  else 'oneblood' if rel.startswith('vitals/') else 'health_conversation' if rel.startswith('conversations/')
                  else 'patient_report' if rel.startswith(('meds/','symptoms/'))
                  else 'ontology' if rel.startswith('ontology/') else 'research')
        kind = {'research':'derived_research','ontology':'ontology_definition'}.get(source,'source_archive')
        if self.known_contents is None or sha not in self.known_contents:
            self.conn.execute('INSERT INTO health.artifact_contents VALUES (%s,%s) ON CONFLICT DO NOTHING', (sha, content))
            if self.known_contents is not None: self.known_contents.add(sha)
        self.conn.execute('INSERT INTO health.artifacts VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (patient_id,path) DO UPDATE SET sha256=EXCLUDED.sha256,size_bytes=EXCLUDED.size_bytes,last_seen_run=EXCLUDED.last_seen_run,retired_run=NULL,retired_reason=NULL',
                          (rel,self.patient,sha,len(content),source,kind,mimetypes.guess_type(rel)[0] or 'application/octet-stream',self.run))
        n = self.conn.execute('INSERT INTO health.artifact_versions VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING', (rel,sha,self.patient,self.run)).rowcount
        self.stats['new_artifact_versions'] += n
        self.stats['artifacts_seen'] += 1
        return sha

    def record(self, source, kind, native, payload, path, provenance=None, current=True, version=None):
        validate_identity(payload, self.account)
        rid = digest([self.patient,source,kind,str(native)])
        # Existing source version identifiers remain authoritative. Exclude volatile capture metadata for new fingerprints.
        stable = {k:v for k,v in payload.items() if k not in ('captured_at','last_checked_at','rendered_markup','provenance')}
        vh = version or payload.get('version_sha256') or payload.get('content_sha256') or payload.get('portal_content_sha256') or digest(stable)
        prov = provenance if provenance is not None else payload.get('provenance', {})
        # Narrative content hashes exclude quote provenance. Preserve an evidence-only
        # correction as a separate version without rewriting source hashes or identities.
        if source == 'patient_symptoms' and payload.get('report_id') and not version:
            if self.known_narratives is None:
                prior = self.conn.execute('SELECT payload FROM health.record_versions WHERE record_id=%s AND version_sha256=%s',(rid, vh)).fetchone()
            else:
                prior = {'payload':self.known_narratives[(rid,vh)]} if (rid,vh) in self.known_narratives else None
            if prior and prior['payload'] != payload:
                vh = digest(['narrative_evidence_version', vh, payload])
            if self.known_narratives is not None:
                self.known_narratives.setdefault((rid,vh),payload)
        n = self.conn.execute('INSERT INTO health.records VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING',
            (rid,self.patient,source,kind,str(native),vh,path,self.run,self.run)).rowcount
        self.stats['new_records'] += n
        n = self.conn.execute('INSERT INTO health.record_versions VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING',
            (rid,vh,Jsonb(payload),Jsonb(prov),path,self.run)).rowcount
        self.stats['new_record_versions'] += n
        if current:
            self.conn.execute('UPDATE health.records SET current_version_sha256=%s,source_path=%s,last_seen_run=%s WHERE record_id=%s', (vh,path,self.run,rid))
        self.lookup[(source,str(native))] = rid
        return rid, vh

    def resource(self, rid, name, rtype='Basic', category='other', clinical=None, date_role='unspecified',
                 recorded=None, assertion='reported', evidence='source_record', status=None,
                 value=None, unit=None, reference=None, flag=None, code_system=None, code=None,
                 details=None, provenance=None, numeric=None, value_range=None, key=None):
        if value_range is not None:
            if (rtype != 'Observation' or value is not None or numeric is not None
                    or not 0 <= value_range['minimum'] <= value_range['maximum'] <= 100
                    or unit != '%'):
                raise ValueError('Invalid percentage range observation')
        number, comparator, parsed_flag = quantity(value)
        if numeric is not None:
            number, comparator = numeric, '='
        flag = flag or parsed_flag
        fhir = {'resourceType':rtype,'id':rid,'subject':{'reference':'Patient/'+digest(self.patient)},
                'meta':{'tag':[{'system':'urn:health:evidence-kind','code':evidence}]}}
        if rtype in ('Observation','Condition','Basic','Procedure','DiagnosticReport'):
            fhir['code']={'text':name}
            if code and code_system:
                fhir['code']['coding']=[{'system':code_system,'code':code}]
        if rtype=='Observation':
            fhir['status']='final' if status in ('final','completed','FINAL') else 'unknown'
            if value_range is not None:
                fhir['valueRange']={
                    bound:{'value':value_range[key],'unit':unit,
                           'system':'http://unitsofmeasure.org','code':UCUM[unit]}
                    for bound,key in (('low','minimum'),('high','maximum'))}
            elif number is not None:
                q={'value':number}
                if comparator != '=': q['comparator']=comparator
                if unit: q['unit']=unit
                if unit in UCUM: q.update(system='http://unitsofmeasure.org',code=UCUM[unit])
                fhir['valueQuantity']=q
            elif value is not None:
                fhir['valueString']=str(value)
            if clinical: fhir['effectiveDateTime']=clinical
            if reference: fhir['referenceRange']=[{'text':reference}]
        if rtype=='MedicationStatement':
            fhir['medicationCodeableConcept']={'text':name}
            fhir['status']=status if status in ('active','completed','entered-in-error','intended','stopped','on-hold','unknown','not-taken') else 'unknown'
            fhir['informationSource']={'reference':'Patient/'+digest(self.patient)} if evidence=='patient_report' else {'display':'source clinical record'}
        if rtype=='MedicationAdministration' and category=='medication_administration':
            fhir['medicationCodeableConcept']={'text':name}
            fhir['status']=status or 'unknown'
            if clinical:fhir['effectiveDateTime']=clinical
            if category=='medication_administration' and details.get('dose'):
                dose=details['dose']
                fhir['dosage']={'dose':{'value':dose['value'],'unit':dose['unit']}}
                if dose['unit'] in UCUM:
                    fhir['dosage']['dose'].update(system='http://unitsofmeasure.org',code=UCUM[dose['unit']])
        normalized={'model':'FHIR R4 aligned internal projection', 'exchange_validation':'not_performed',
                    'clinical':fhir, 'assertion':assertion, 'clinical_date_role':date_role,
                    'source_details':details or {}}
        prov=provenance or {}
        projection=digest([normalized,number,comparator,unit,reference,flag,code_system,code,recorded])
        values=(rid,rid,self.patient,rtype,category,name,clinical,date_role,recorded,assertion,evidence,status,
                number,comparator,str(value) if value is not None else None,unit,UCUM.get(unit),reference,flag,
                code_system,code,Jsonb(normalized),Jsonb(prov),projection)
        fields='resource_id,record_id,patient_id,resource_type,category,name,clinical_date,clinical_date_role,recorded_at,assertion,evidence_kind,status,numeric_value,comparator,value_text,unit_source,unit_ucum,reference_text,flag_source,code_system,code,normalized,provenance,projection_sha256'.split(',')
        updates=sql.SQL(',').join(sql.SQL('{}=EXCLUDED.{}').format(sql.Identifier(f),sql.Identifier(f)) for f in fields[3:])
        self.conn.execute(sql.SQL('INSERT INTO health.resources ({}) VALUES ({}) ON CONFLICT (resource_id) DO UPDATE SET {} WHERE health.resources.projection_sha256<>EXCLUDED.projection_sha256 OR health.resources.provenance<>EXCLUDED.provenance OR health.resources.category<>EXCLUDED.category').format(sql.SQL(',').join(sql.Identifier(f) for f in fields),sql.SQL(',').join(sql.Placeholder() for _ in values),updates),values)
        self.stats['new_resource_versions'] += self.conn.execute('INSERT INTO health.resource_versions VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING', (rid,projection,Jsonb(normalized),Jsonb(prov),self.run)).rowcount
        self.resource_ids.add(rid)
        self.categories[rid]=category
        self.meta[rid]=(category,name,code_system,code,assertion,key)
        # Patient ownership is each row's patient_id; no per-record patient edge.
        self.relationship(rid,'ontology:'+category,'INSTANCE_OF','ontology',{})

    def relationship(self, a, b, relation, evidence, provenance, qualifiers=None):
        self.relations.add(relation)
        self.edges.append((a,b,relation,qualifiers or {}))
        key=digest([self.patient,a,b,relation]+([qualifiers] if qualifiers else []))
        # Edges seen in this run are current; earlier ones stay as history.
        self.conn.execute('''INSERT INTO health.relationships(relationship_id,patient_id,from_id,to_id,relation,evidence_kind,provenance,qualifiers,last_seen_run,is_current)
          VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,true) ON CONFLICT (relationship_id) DO UPDATE SET evidence_kind=EXCLUDED.evidence_kind,provenance=EXCLUDED.provenance,
          qualifiers=EXCLUDED.qualifiers,last_seen_run=EXCLUDED.last_seen_run,is_current=true''',
                          (key,self.patient,a,b,relation,evidence,Jsonb(provenance),Jsonb(qualifiers or {}),self.run))

    def document(self, key, source, title, path, text, evidence='source_record', rid=None, clinical=None, provenance=None, current=True):
        if not text or not text.strip(): return
        version=digest([text,provenance or {}])
        did=digest([self.patient,source,key,version])
        existed=(did in self.known_documents) if self.known_documents is not None else self.conn.execute('SELECT 1 FROM health.documents WHERE document_id=%s',(did,)).fetchone()
        self.conn.execute('INSERT INTO health.documents VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (document_id) DO UPDATE SET is_current=EXCLUDED.is_current',
                          (did,self.patient,source,title,path,rid,clinical,evidence,version,text,Jsonb(provenance or {}),current))
        if current: self.current_docs.add(did)
        self.doc_ids.add(did)
        if not existed:
            if self.known_documents is not None: self.known_documents.add(did)
            for ordinal,start in enumerate(range(0,len(text),1250)):
                end=min(start+1500,len(text))
                self.conn.execute('INSERT INTO health.chunks(chunk_id,document_id,patient_id,ordinal,start_char,end_char,text_content) VALUES (%s,%s,%s,%s,%s,%s,%s)',
                                  (digest([did,ordinal]),did,self.patient,ordinal,start,end,text[start:end]))
                if end == len(text): break
            self.stats['new_documents']+=1
        if rid: self.relationship(rid,did,'DOCUMENTED_BY',evidence,provenance or {})

    def issue(self, path, native, reason, details, severity='info'):
        iid=digest([path,native,reason,details])
        self.conn.execute('INSERT INTO health.import_issues VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING',
                          (iid,self.patient,path,str(native),severity,reason,Jsonb(details)))

    def lab_observation(self, source, report, key, name, value, path, clinical, unit=None, reference=None, flag=None, provenance=None, code=None, details=None):
        payload={'name':name,'value':value,'unit':unit,'reference':reference,'flag':flag,'date':clinical,'source_code':code,'details':details or {}}
        panel=(details or {}).get('panel') or (provenance or {}).get('panel') or ''
        concept=(source,str(report),' '.join(str(panel).casefold().split()),' '.join(str(name).casefold().split()))
        occurrence=self.observation_occurrences[concept]
        self.observation_occurrences[concept]+=1
        stable_key=digest([concept,occurrence])
        if self.known_aliases is None:
            alias=self.conn.execute('SELECT r.source_record_id FROM health.source_record_aliases a JOIN health.records r USING(record_id) WHERE a.patient_id=%s AND a.source=%s AND a.source_kind=%s AND a.stable_key=%s',(self.patient,source,'lab_observation',stable_key)).fetchone()
        else:
            alias={'source_record_id':self.known_aliases[(source,stable_key)]} if (source,stable_key) in self.known_aliases else None
        native=alias['source_record_id'] if alias else str(report)+':'+str(key)
        if self.known_aliases is not None: self.known_aliases.setdefault((source,stable_key),native)
        rid,vh=self.record(source,'lab_observation',native,payload,path,provenance)
        self.conn.execute('INSERT INTO health.source_record_aliases VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING',(self.patient,source,'lab_observation',stable_key,rid))
        self.resource(rid,name or 'Unlabeled report commentary','Observation' if value is not None else 'Basic','laboratory' if value is not None else 'lab_commentary',clinical,'source_service_or_collection_date' if clinical else 'unknown',
                      value=value,unit=unit,reference=reference,flag=flag,code_system='http://loinc.org' if code else None,
                      code=code,details=payload,provenance=provenance,status='unknown')
        parent=self.lookup.get((source,str(report)))
        if parent: self.relationship(rid,parent,'PART_OF_REPORT','source_structure',provenance or {})
        self.stats[source+'_lab_observations']+=1
        return rid


def import_labcorp(imp):
    path='lab-results/reports.jsonl'
    for r in rows(path):
        native=r['report_id']
        clinical=date(r.get('service_date') or r.get('collection_date') or r.get('listed_date'))
        rid,vh=imp.record('labcorp','report',native,r,path)
        imp.resource(rid,'Labcorp report '+str(native),'DiagnosticReport','lab_report',clinical,
                     'service_or_collection_date',recorded=r.get('captured_at'),details={'archive_kind':r['archive_kind'],'official_pdf_status':r.get('official_pdf_status')},provenance={'path':path,'source_report_id':native})
        text=r.get('text') or ''
        text+='\n\n'+'\n'.join(n.get('title','')+'\n'+n.get('text','') for n in r.get('notes',[]))
        imp.document(native,'labcorp','Labcorp report '+str(native),path,text,rid=rid,clinical=clinical,provenance={'report_id':native,'archive_path':r.get('archive_path')})
        panel=None
        for idx,s in enumerate(r.get('sections',[])):
            if s['kind']=='panel': panel=s.get('heading'); continue
            if s['kind']!='result':continue
            soup=BeautifulSoup(s.get('markup',''),'html.parser')
            label=soup.select_one('.analyte')
            name=label.get_text(' ',strip=True) if label else (s.get('heading') or 'Report commentary')
            v=soup.select_one('.pointer-content, .result-box')
            value=v.get_text(' ',strip=True) if v else None
            ref=soup.select_one('.reference-range')
            reference=ref.get_text(' ',strip=True) if ref else None
            if not reference:
                for interval in soup.select('.result-interval'):
                    content=interval.get_text(' ',strip=True)
                    if content.startswith('Normal '): reference=content; break
            if not reference:
                for leaf in soup.find_all('div'):
                    if not leaf.find('div'):
                        content=leaf.get_text(' ',strip=True)
                        if content.startswith(('Reference Interval:','Unit:')):
                            reference=content;break
            unit=unit_from_reference(reference)
            flag=None
            for icon in soup.select('i[aria-label]'):
                flag=icon.get('aria-label')
            prov={'path':path,'archive_path':r.get('archive_path'),'section_index':idx,'panel':panel,'raw_text':s.get('text','')}
            imp.lab_observation('labcorp',native,'section:'+str(idx),name,value,path,clinical,unit,reference,flag,prov,details={'panel':panel,'parser':'rendered_markup','unparsed':value is None})
            if value is None and not soup.select_one('.comments-panel'): imp.issue(path,str(native)+':'+str(idx),'Labcorp result without a safely extractable value',{'name':name,'text':s.get('text','')})
    for r in rows('lab-results/imports/observations.jsonl'):
        native=r['report_id']
        imp.lab_observation('labcorp',native,r['observation_id'],r['analyte_name'],r['value']['text'],
                            'lab-results/imports/observations.jsonl',r['collection_date'],r['unit_text'],r['reference_text'],r['flag'],r['provenance'],details=r)


def quest_pdf_rows(pages):
    """Accept only explicit result+reference structures, preserving every other line as document evidence."""
    for page in pages:
        lines=[s.strip() for s in page['text'].splitlines() if s.strip()]
        for i,line in enumerate(lines):
            if line.startswith('Reference Range:') and i>=2:
                value=lines[i-1]
                name=lines[i-2]
                if value in ('H','L') and i>=3:
                    value=lines[i-2]+' '+value;name=lines[i-3]
                numeric=quantity(value)[0]
                textual=value in ('NEGATIVE','NON-REACTIVE','REACTIVE','NOT DETECTED','DETECTED','NORMAL','TRACE','NONE SEEN','POSITIVE')
                if (numeric is None and not textual) or not re.fullmatch(r'[A-Z][A-Z0-9 ,()/%.+_-]{1,100}',name):continue
                ref=line.removeprefix('Reference Range:').strip()
                extra=[]
                for x in lines[i+1:i+5]:
                    if x=='Reference Range:' or x.startswith(('--- PAGE','CLIENT','PAGE ')):break
                    if re.fullmatch(r'[A-Z][A-Z ,()_-]{4,}',x) and x not in ('NOT ESTABLISHED',):break
                    if re.match(r'^(?:[<>]=?\s*|[0-9]|NOT ESTABLISHED)',x) or unit_from_reference('1 '+x):extra.append(x)
                    else:break
                ref=' '.join([ref,*extra]).strip()
                yield {'key':f"page:{page['page_number']}:line:{i}",'name':name,'value':value,'reference':ref,'page_number':page['page_number'],'line_index':i,'raw_text':'\n'.join(lines[max(0,i-2):i+1+len(extra)])}
            elif 'Test Name Results Reference Range Lab' in page['text']:
                m=re.match(r'^([A-Z][A-Z ,()/.-]+?)\s+([<>]=?\s*\d+(?:\.\d+)?|\d+(?:\.\d+)?)(?:\s+([HL]))?\s+((?:[<>].*|\d[\d.]*\s*-\s*\d[\d.]*).*?(?:pg/mL|ng/dL|ng/mL|mg/dL|g/dL|mIU/mL|U/L|%))(?:\s+[A-Z]+)?\s*$',line)
                if m:yield {'key':f"page:{page['page_number']}:line:{i}",'name':m[1],'value':m[2]+(' '+m[3] if m[3] else ''),'reference':m[4],'page_number':page['page_number'],'line_index':i,'raw_text':line}


def import_quest(imp):
    path='lab-results/quest/reports.jsonl'
    for r in rows(path):
        native=r['report_id'];clinical=date(r.get('collection_date') or r.get('listed_service_date_source'))
        rid,vh=imp.record('quest','report',native,r,path)
        imp.resource(rid,'Quest report '+native,'DiagnosticReport','lab_report',clinical,'collection_date' if r.get('collection_date') else 'listed_service_date',recorded=r.get('captured_at'),details={'archive_kind':r['archive_kind'],'specimen_id':r['specimen_id']},provenance={'path':path,'pdf_path':r.get('pdf_path')})
        texts=[('header',r.get('header_text',''))]+[(f"page:{p['page_number']}",p['text']) for p in r.get('pages',[])]
        if r.get('rows'):texts.append(('portal','\n\n'.join(s.get('raw_text','') for s in r['rows'])))
        for key,text in texts:
            imp.document(native+':'+key,'quest','Quest report '+native+' '+key,path,text,rid=rid,clinical=clinical,provenance={'report_id':native,'page':key,'pdf_path':r.get('pdf_path'),'text_path':r.get('pdf_text_path')})
        for idx,s in enumerate(r.get('rows',[])):
            link=s.get('source_loinc_link_id','');m=re.search(r'^loincCode-\d+-(\d+-\d+)-',link)
            imp.lab_observation('quest',native,'portal:'+str(s.get('ordinal',idx)),s['analyte_name'],s['result_text'],path,clinical,
                                unit_from_reference(s.get('reference_text')),s.get('reference_text'),provenance={'path':path,'row_index':idx,'raw_text':s.get('raw_text'),'source_loinc_link_id':link},code=m[1] if m else None,details={'parser':'portal_row'})
        if r['archive_kind']=='downloaded_pdf_text':
            accepted=list(quest_pdf_rows(r['pages']))
            for s in accepted:
                imp.lab_observation('quest',native,s['key'],s['name'],s['value'],path,clinical,unit_from_reference(s['reference']),s['reference'],provenance={'path':path,'pdf_path':r.get('pdf_path'),**s},details={'parser':'explicit_pdf_result_reference'})
            imp.issue(path,native,'PDF-only report: only explicit result/reference structures are normalized; complete pages remain searchable',{'accepted_observations':len(accepted),'pages':len(r['pages']),'pdf_path':r.get('pdf_path')})
    for p in (ROOT/'lab-results/quest/revisions').glob('*.json'):
        r=json.loads(p.read_text())
        if r.get('report_id'):imp.record('quest','report',r['report_id'],r,str(p.relative_to(ROOT)),current=False)


def reviewed_medication_sources():
    """Resolve explicit reviewed narrative supersessions without rewriting exports."""
    from jsonschema import Draft202012Validator, FormatChecker
    sources={name:rows(f'meds/{name}.jsonl') for name in
             ('medications','reported-medications','change-log','reported-change-log','reported-administrations')}
    validator=Draft202012Validator(load('meds/medication.schema.json'),format_checker=FormatChecker())
    for name in ('reported-medications','reported-change-log','reported-administrations'):
        for r in sources[name]:
            validator.validate(r)
            prov=r['provenance'];source=(ROOT/prov['source_path']).read_text()
            if hashlib.sha256(source.encode()).hexdigest()!=prov['source_sha256']:
                raise ValueError('Supplemental medication source hash mismatch')
            for quote in prov['evidence']:
                if source[quote['start_char']:quote['end_char']]!=quote['quote']:
                    raise ValueError('Supplemental medication evidence mismatch')
    base={r['medication_id']:r for r in sources['medications']}
    supplemental={r['medication_id']:r for r in sources['reported-medications']}
    if len(base)!=len(sources['medications']) or len(supplemental)!=len(sources['reported-medications']):
        raise ValueError('Duplicate current medication identity requires review')
    overrides={}
    for native,r in supplemental.items():
        prior=base.get(native);review=r.get('supersedes_regimen')
        if prior:
            if not review or review['source_path']!='meds/medications.jsonl' or review['record_sha256']!=digest(prior):
                raise ValueError('Conflicting current medication sources require explicit source-hash review: '+native)
            if not r['as_of_date'] or r['as_of_date']<(prior.get('as_of_date') or ''):
                raise ValueError('Medication supersession predates its source')
            overrides[native]=r
        elif review:
            raise ValueError('Medication supersession source does not exist: '+native)
    return sources,overrides


def import_medications(imp):
    sources,overrides=reviewed_medication_sources()
    for name in ('medications','reported-medications','change-log','reported-change-log','reported-administrations'):
        path=f'meds/{name}.jsonl'
        for r in sources[name]:
            native=r.get('administration_id') or r.get('event_id') or r['medication_id'];kind=r['record_type']
            is_regimen=kind=='medication_regimen'
            is_administration=kind=='medication_administration'
            if name=='medications' and native in overrides:
                imp.record('patient_medications',kind,native,r,path,current=False)
                continue
            rid,vh=imp.record('patient_medications',kind,native,r,path)
            clinical=r.get('as_of_date') if is_regimen else r['occurrence_date'] if is_administration else r['date']['date']
            label=r.get('name_source') or r.get('subject_source') or kind
            imp.resource(rid,label,'MedicationStatement' if is_regimen else 'MedicationAdministration' if is_administration else 'Basic',kind,clinical,
                         'regimen_as_of_date' if is_regimen else 'reported_administration_date' if is_administration else r['date_role'],recorded=r.get('reported_at'),evidence='patient_report',status=r.get('status'),details=r,provenance=r['provenance'])
            imp.document(native,'patient_medications',label,path,canonical(r),evidence='patient_report',rid=rid,clinical=clinical,provenance=r['provenance'])
    for r in sources['change-log']+sources['reported-change-log']:
        med=imp.lookup.get(('patient_medications',r.get('medication_id')))
        event=imp.lookup[('patient_medications',r['event_id'])]
        if med:imp.relationship(event,med,'ABOUT_MEDICATION','patient_report',r['provenance'],{'aspect':'change'})
    for r in sources['reported-administrations']:
        med=imp.lookup.get(('patient_medications',r['medication_id']))
        administration=imp.lookup[('patient_medications',r['administration_id'])]
        if med:imp.relationship(administration,med,'ABOUT_MEDICATION','patient_report',r['provenance'],{'aspect':'administration'})
    for p in (ROOT/'meds/snapshots').glob('*/*.jsonl'):
        if p.name not in ('medications.jsonl','change-log.jsonl'):continue
        for r in rows(str(p.relative_to(ROOT))):
            imp.record('patient_medications',r['record_type'],r.get('event_id') or r['medication_id'],r,str(p.relative_to(ROOT)),current=False)


def import_symptoms(imp):
    path='symptoms/records.jsonl'
    records=rows(path)
    superseded=measurement_supersessions(records)
    for r in records:
        native=r['record_id'];kind=r['record_type'];d=r['details']
        rid,vh=imp.record('patient_symptoms',kind,native,r,path)
        label=d.get('label') or d.get('medication_name') or d.get('source_text') or r['record_key']
        assertion=d.get('assertion') or ('hypothesis' if kind=='patient_hypothesis' else 'absent' if d.get('status')=='none_reported' else 'reported')
        quotes='\n'.join(x['quote'] for x in r['provenance']['evidence'])
        occurrence=d.get('occurrence',{}).get('date')
        # Sleep is a nightly measurement in hours; a report with only quality has no value.
        measurement = kind in ('anthropometric_measurement','vital_sign_measurement','sleep_observation')
        composition = kind == 'body_composition_measurement'
        imp.resource(rid,label,'Observation' if kind=='symptom_observation' or measurement or composition else 'Basic',kind,clinical=occurrence,recorded=r['reported_at'],
                     date_role='patient_reported_occurrence_date' if occurrence else 'onset_or_occurrence_unspecified',assertion=assertion,evidence='patient_hypothesis' if kind=='patient_hypothesis' else 'patient_report',
                     value=d['value'] if measurement else None if composition else d.get('current_presence') or assertion,
                     numeric=d['value'] if measurement else None,unit=d['unit'] if measurement or composition else None,
                     value_range=d['value_range'] if composition else None,
                     details=r,provenance=r['provenance'],key=d.get('symptom_key') or d.get('exposure_key'))
        if native in superseded:
            imp.resource_ids.discard(rid)
        imp.document(native,'patient_symptoms',label,path,label+'\n'+quotes+'\n'+canonical(d),evidence='patient_hypothesis' if kind=='patient_hypothesis' else 'patient_report',rid=rid,provenance=r['provenance'],current=native not in superseded)
    for r in records:
        d=r['details'];rid=imp.lookup[('patient_symptoms',r['record_id'])]
        if r['record_type']=='patient_demographics':
            imp.relationship(rid,imp.patient,'REPORTS_DEMOGRAPHICS_FOR','patient_report',r['provenance'])
        # Systolic and diastolic values from one reading stay separate observations.
        for paired in d.get('paired_component_record_ids',[]):
            imp.relationship(rid,imp.lookup[('patient_symptoms',paired)],'SAME_READING_COMPONENT','patient_report',{'reading_key':d['reading_key'],'source_record_id':r['record_id']})
        if d.get('supersedes_measurement'):
            review=d['supersedes_measurement']
            tid=imp.lookup[('patient_symptoms',review['record_id'])]
            imp.relationship(rid,tid,'SUPERSEDES_MEASUREMENT','patient_report',{'review':review,'source':r['provenance']})
        targets=d.get('to_record_ids',[])+d.get('related_record_ids',[])
        if d.get('from_record_id'):targets.append(d['from_record_id'])
        for target in targets:
            tid=imp.lookup.get(('patient_symptoms',target))
            if tid:imp.relationship(rid,tid,'REPORTED_ASSOCIATION' if r['record_type']=='reported_association' else 'REFERENCES','patient_report',{'causality':'not_established','source_record_id':r['record_id']})
        mid=imp.lookup.get(('patient_medications',d.get('medication_id')))
        if mid:imp.relationship(rid,mid,'ABOUT_MEDICATION','patient_report',{'source_record_id':r['record_id']},{'aspect':'response'})
        for native in d.get('related_administration_ids',[]):
            administration=imp.lookup.get(('patient_medications',native))
            if administration:imp.relationship(rid,administration,'REPORTED_RESPONSE_AFTER_ADMINISTRATION','patient_report',{'causality':'not_established','source_record_id':r['record_id']})
        # Treatment chronology and patient priorities are reports, not diagnoses
        # or proof that a medication caused an underlying symptom.
        for field,relation in (
            ('pre_treatment_record_ids','REPORTED_PRE_TREATMENT_SYMPTOM'),
            ('persistent_record_ids','REPORTED_PERSISTENT_SYMPTOM'),
            ('treatment_goal_record_ids','REPORTED_TREATMENT_GOAL')):
            for target in d.get(field,[]):
                tid=imp.lookup.get(('patient_symptoms',target))
                if tid:imp.relationship(rid,tid,relation,'patient_report',r['provenance'])
        if r['record_type']=='care_preference':
            for target in d.get('medication_ids',[]):
                tid=imp.lookup.get(('patient_medications',target))
                if tid:imp.relationship(rid,tid,'ABOUT_MEDICATION','patient_report',r['provenance'],{'aspect':'preference'})
        if r['record_type']=='patient_hypothesis':
            for target in d.get('medication_ids',[]):
                tid=imp.lookup.get(('patient_medications',target))
                if tid:imp.relationship(rid,tid,'ABOUT_MEDICATION','patient_hypothesis',{'causality':'not_established','source_record_id':r['record_id']},{'aspect':'hypothesis'})
    for p in (ROOT/'symptoms/narratives').glob('*/revisions/*/records.jsonl'):
        for r in rows(str(p.relative_to(ROOT))):imp.record('patient_symptoms',r['record_type'],r['record_id'],r,str(p.relative_to(ROOT)),current=False)
    # Patient-reported clinical history gathered alongside research. The first file keeps
    # the archive's original stable identity; later files are identified by name.
    for index,path in enumerate(sorted((ROOT/'research').glob('patient-context*.json'))):
        context=str(path.relative_to(ROOT));native='research_clarifications' if index==0 else 'research_clarifications:'+path.stem
        r=load(context);rid,vh=imp.record('patient_symptoms','patient_context',native,r,context)
        imp.resource(rid,'Additional patient-reported clinical history','Basic','patient_context',evidence='patient_report',details=r,provenance={'path':context})
        imp.document(native,'patient_symptoms','Additional patient-reported clinical history',context,canonical(r),evidence='patient_report',rid=rid,provenance={'path':context})


def import_vitals(imp):
    path='vitals/oneblood/observations.jsonl'
    for r in rows(path):
        rid,vh=imp.record('oneblood','vital',r['observation_id'],r,path)
        imp.resource(rid,r['metric'].replace('_',' '),'Observation','blood_donor_screening',r['measurement_date'],'measurement_date',
                     status=r['status'],value=r['value'],numeric=r['value'],unit=r['unit'],details=r,provenance=r['provenance'])
        imp.document(r['observation_id'],'oneblood',r['metric'].replace('_',' '),path,f"{r['measurement_date']}: {r['name_source']}: {r['value']} {r['unit']} ({r['status']})",rid=rid,clinical=r['measurement_date'],provenance=r['provenance'])


def import_clinic(imp):
    path='health-records/cleveland-clinic/records.jsonl'
    mapping={'lab_results':'Observation','vitals':'Observation','vital_panels':'Observation',
             'result_reports':'DiagnosticReport','medications':'MedicationStatement','prescriptions':'MedicationRequest',
             'administered_medications':'MedicationAdministration','problems':'Condition','visit_diagnoses':'Condition',
             'clinical_notes':'DocumentReference','encounters':'Encounter','procedures':'Procedure','immunizations':'Immunization',
             'allergies':'AllergyIntolerance','care_plans':'CarePlan','care_teams':'CareTeam','goals':'Goal'}
    for r in rows(path):
        native=r['record_id'];d=r['data'];cat=r['category'];rtype=mapping.get(cat,'Basic')
        rid,vh=imp.record('cleveland_clinic',cat,native,r,path)
        # Lab result timestamps often differ from specimen dates. Keep both in source details.
        panel=d.get('panel',{});clinical=source_date(panel) if cat=='lab_results' else None
        role='panel_collection_date' if clinical else 'source_effective_date'
        clinical=clinical or source_date(d)
        name=d.get('name') or (d.get('code') or {}).get('original_text') or cat
        c=(d.get('assertion') or {}).get('value_code') or d.get('code') or (d.get('medication') or {}).get('code') or {}
        code=c.get('code');system=SYSTEMS.get(c.get('codeSystem'), 'urn:oid:'+c['codeSystem'] if c.get('codeSystem') else None)
        values=d.get('values',[]);numeric=None;value=None;unit=None
        if values:
            attrs=values[0].get('attributes',{});numeric=values[0].get('number');unit=attrs.get('unit')
            value=attrs.get('value') or values[0].get('text') or (values[0].get('code') or {}).get('displayName')
        reference='\n'.join(n.get('text','') for ref in d.get('reference_ranges',[]) for child in ref.get('children',[]) for n in child.get('children',[]) if n.get('text')) or None
        negative=d.get('attributes',{}).get('negationInd')=='true' or (d.get('assertion') or {}).get('attributes',{}).get('negationInd')=='true'
        absence_statement=name.lower().startswith('no known ')
        if absence_statement:rtype='Basic'
        flag=' '.join(x.get('code','') for x in d.get('interpretations',[])) or None
        imp.resource(rid,name,rtype,cat,clinical,role,assertion='source_absence_statement' if absence_statement else 'source_negated' if negative else 'source_record',
                     status=(d.get('status') or {}).get('code'),value=value,numeric=numeric,unit=unit,reference=reference,
                     flag=flag,code_system=system,code=code,details=d,provenance=r['provenance'])
        imp.document(native,'cleveland_clinic',name,path,name+'\n'+d.get('text',''),rid=rid,clinical=clinical,provenance={'source_record_id':native,'xml_provenance':r['provenance']})
        version_path=r.get('versions_path')
        if version_path:
            for version in load('health-records/cleveland-clinic/'+version_path).get('versions',[]):
                imp.record('cleveland_clinic',cat,native,version,'health-records/cleveland-clinic/'+version_path,current=False)
                if version['version_sha256']!=r['version_sha256']:
                    vd=version['data']
                    imp.document(native+':history:'+version['version_sha256'],'cleveland_clinic',name+' (historical source version)',
                                 'health-records/cleveland-clinic/'+version_path,name+'\n'+vd.get('text',''),rid=rid,clinical=source_date(vd),
                                 provenance={'version_sha256':version['version_sha256'],'xml_provenance':version.get('provenance',[])},current=False)
    for r in rows('health-records/cleveland-clinic/documents.jsonl'):
        imp.record('cleveland_clinic','document_header',r['document_id'],r,'health-records/cleveland-clinic/documents.jsonl')
    for r in rows('health-records/cleveland-clinic/narrative-tables.jsonl'):
        native=r.get('table_id') or digest(r)
        imp.record('cleveland_clinic','narrative_table',native,r,'health-records/cleveland-clinic/narrative-tables.jsonl')
    for p in sorted((ROOT/'health-records/cleveland-clinic/text').glob('*.txt')):
        # Full 133-page PDF text preserves content outside structured C-CDA entries.
        rel=str(p.relative_to(ROOT));text=p.read_text();clinical=None
        m=re.search(r'(\d{4}-\d{2}-\d{2})',p.stem)
        if m:clinical=m[1]
        imp.document(rel,'cleveland_clinic',p.stem.replace('_',' '),rel,text,evidence='source_document_text',clinical=clinical,provenance={'path':rel,'extraction':'preserved PDF text or source-linked clinical excerpt'})
    cross_path='health-records/cleveland-clinic/cross-source-reconciliation.json'
    cross=load(cross_path) if (ROOT/cross_path).exists() else {}
    for candidate in cross.get('candidates',[]):
        a=imp.lookup.get(('cleveland_clinic',candidate['cleveland_clinic_record_id']))
        for match in candidate['candidate_reports']:
            b=imp.lookup.get((match['source'],match['report_id']))
            if a and b:imp.relationship(a,b,'POSSIBLE_SAME_SOURCE_EVENT','unreviewed_identity_candidate',candidate)


def import_conversations(imp):
    sessions={}
    for r in rows('conversations/sessions.jsonl'):
        rid,_=imp.record('health_conversation','conversation_session',r['session_id'],r,'conversations/sessions.jsonl')
        imp.resource(rid,'Health conversation '+r['session_id'],'Basic','conversation_session',
                     recorded=r.get('source_created_at'),date_role='conversation_source_time',
                     assertion='audit_context',evidence='conversation_context',details=r)
        sessions[r['session_id']]=rid
    messages=rows('conversations/messages.jsonl')
    prior={}
    user_messages=[]
    for r in messages:
        prov=r['provenance']
        for field,hash_field in (('source_event_path','source_event_sha256'),('text_path','text_sha256')):
            if hashlib.sha256((ROOT/prov[field]).read_bytes()).hexdigest()!=prov[hash_field]:
                raise ValueError('Conversation source hash mismatch: '+r['message_id'])
        if (ROOT/prov['text_path']).read_text()!=r['message_text']:
            raise ValueError('Conversation text differs from preserved source')
        rid,_=imp.record('health_conversation','conversation_message',r['message_id'],r,'conversations/messages.jsonl')
        label=r['role']+' message '+r['source_timestamp']
        evidence='conversation_user' if r['role']=='user' else 'conversation_assistant'
        imp.resource(rid,label,'DocumentReference','conversation_message',recorded=r['source_timestamp'],
                     date_role='conversation_source_time',assertion='not_extracted',evidence=evidence,details=r,provenance=prov)
        imp.document(r['message_id'],'health_conversation',label,prov['text_path'],r['message_text'],
                     evidence=evidence,rid=rid,provenance=prov)
        imp.relationship(rid,sessions[r['session_id']],'PART_OF_CONVERSATION','conversation_structure',prov)
        if r['session_id'] in prior:
            imp.relationship(prior[r['session_id']],rid,'NEXT_CONVERSATION_MESSAGE','conversation_structure',{})
        prior[r['session_id']]=rid
        if r['role']=='user':user_messages.append((rid,r))
    # Source linkage only: an exact user quote is provenance, never a diagnosis
    # or a reconciliation of independent clinical events.
    for path,source,key in (('symptoms/records.jsonl','patient_symptoms','record_id'),
                            ('meds/reported-medications.jsonl','patient_medications','medication_id'),
                            ('meds/reported-change-log.jsonl','patient_medications','event_id')):
        for record in rows(path):
            rid=imp.lookup.get((source,record[key]))
            for evidence in record.get('provenance',{}).get('evidence',[]):
                quote=evidence['quote']
                if len(quote)<8:continue
                for mid,message in user_messages:
                    offset=message['message_text'].find(quote)
                    if offset>=0:
                        imp.relationship(rid,mid,'EXTRACTED_FROM_CONVERSATION_MESSAGE','verbatim_source_link',
                                         {'quote':quote,'start_char':offset,'end_char':offset+len(quote),
                                          'source_record_id':record[key],'conversation_message_id':message['message_id']})


def import_research(imp):
    for p in sorted((ROOT/'research').glob('*.md')):
        rel=str(p.relative_to(ROOT))
        imp.document(rel,'research',p.stem,rel,p.read_text(),evidence='derived_research',provenance={'path':rel,'warning':'Agent research is an interpretation, not a clinical source record'})


def import_ontology(imp):
    """Load the ontology definitions the archive enforces into queryable tables."""
    categories=load('ontology/categories.json')['categories']
    relations=load('ontology/relations.json')['relations']
    clinical_types=load('ontology/clinical-types.json')['clinical_types']
    concepts=load('ontology/concepts.json')['concepts']
    imp.registry=Registry(categories,relations,clinical_types,concepts)
    schemas={}
    for c in categories:
        if c['schema_path'] and c['schema_path'] not in schemas:schemas[c['schema_path']]=load(c['schema_path'])
    def branch(c):
        # A category's own JSON Schema branch, or the whole schema for single-type files.
        schema=schemas.get(c['schema_path'])
        if not schema:return None
        for b in schema.get('oneOf',[]):
            if b.get('properties',{}).get('record_type',{}).get('const')==c['category']:return {**b,'$defs':schema.get('$defs',{})}
        return schema
    conn,run,patient=imp.conn,imp.run,imp.patient
    for c in categories:
        definition={**c,'json_schema':branch(c)}
        conn.execute('''INSERT INTO health.ontology_categories(category,domain,description,schema_path,json_schema,definition_sha256,updated_run,patient_id,subject_area,clinical_type,definition) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (patient_id,category) DO UPDATE SET
          domain=EXCLUDED.domain,description=EXCLUDED.description,schema_path=EXCLUDED.schema_path,json_schema=EXCLUDED.json_schema,subject_area=EXCLUDED.subject_area,
          clinical_type=EXCLUDED.clinical_type,definition=EXCLUDED.definition,definition_sha256=EXCLUDED.definition_sha256,updated_run=EXCLUDED.updated_run WHERE health.ontology_categories.definition_sha256<>EXCLUDED.definition_sha256''',
          (c['category'],c['domain'],c['description'],c['schema_path'],Jsonb(definition['json_schema']) if definition['json_schema'] else None,digest(definition),run,patient,c['subject_area'],c['clinical_type'],Jsonb(c)))
    for r in relations:
        conn.execute('''INSERT INTO health.ontology_relations(relation,semantics,description,establishes_causality,definition_sha256,updated_run,patient_id,status,definition) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (patient_id,relation) DO UPDATE SET
          semantics=EXCLUDED.semantics,description=EXCLUDED.description,establishes_causality=EXCLUDED.establishes_causality,status=EXCLUDED.status,definition=EXCLUDED.definition,
          definition_sha256=EXCLUDED.definition_sha256,updated_run=EXCLUDED.updated_run WHERE health.ontology_relations.definition_sha256<>EXCLUDED.definition_sha256''',
          (r['relation'],r['semantics'],r['description'],r['establishes_causality'],digest(r),run,patient,r.get('status','active'),Jsonb(r)))
    for t in clinical_types:
        conn.execute('''INSERT INTO health.clinical_types(patient_id,clinical_type,label,parent,fhir_resource,concept_family,description,definition_sha256,updated_run) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
          ON CONFLICT (patient_id,clinical_type) DO UPDATE SET label=EXCLUDED.label,parent=EXCLUDED.parent,fhir_resource=EXCLUDED.fhir_resource,concept_family=EXCLUDED.concept_family,
          description=EXCLUDED.description,definition_sha256=EXCLUDED.definition_sha256,updated_run=EXCLUDED.updated_run WHERE health.clinical_types.definition_sha256<>EXCLUDED.definition_sha256''',
          (patient,t['clinical_type'],t['label'],t['parent'],t['fhir_resource'],t['concept_family'],t['description'],digest(t),run))
    # These tables mirror the current registry files, whose every version the archive keeps.
    for table,key,names in (('ontology_categories','category',[c['category'] for c in categories]),('ontology_relations','relation',[r['relation'] for r in relations]),
                            ('clinical_types','clinical_type',[t['clinical_type'] for t in clinical_types])):
        conn.execute(sql.SQL('DELETE FROM health.{} WHERE patient_id=%s AND {}<>ALL(%s)').format(sql.Identifier(table),sql.Identifier(key)),(patient,names))
    imp.ontology={'categories':set(imp.registry.categories),'relations':set(imp.registry.relations),'definitions':categories}


def project_semantics(imp):
    """Join each projected record to its concept and clinical type, and link the type hierarchy."""
    registry,conn,patient=imp.registry,imp.conn,imp.patient
    unknown=sorted(set(c for c,*_ in imp.meta.values())-set(registry.categories))
    if unknown:raise ValueError(f'Unregistered ontology entries: categories={unknown}. Register them in ontology/ first.')
    concepts,assigned=assign_concepts(registry,imp.meta)
    for t in registry.types.values():
        if t['parent']:imp.relationship('type:'+t['clinical_type'],'type:'+t['parent'],'SUBTYPE_OF','ontology',{})
    for c in registry.categories.values():
        imp.relationship('ontology:'+c['category'],'type:'+c['clinical_type'],'DEFAULT_CLINICAL_TYPE','ontology',{})
    members=collections.Counter(cid for cid,_,_ in assigned.values() if cid)
    with conn.cursor() as cur:
        cur.executemany('''INSERT INTO health.concepts(patient_id,concept_id,label,family,clinical_type,basis,codings,note,member_count,updated_run,generic_name,brand_names) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
          ON CONFLICT (patient_id,concept_id) DO UPDATE SET label=EXCLUDED.label,family=EXCLUDED.family,clinical_type=EXCLUDED.clinical_type,basis=EXCLUDED.basis,
          codings=EXCLUDED.codings,note=EXCLUDED.note,member_count=EXCLUDED.member_count,updated_run=EXCLUDED.updated_run,generic_name=EXCLUDED.generic_name,brand_names=EXCLUDED.brand_names''',
          [(patient,c['concept_id'],c['label'],c['family'],c['clinical_type'],c['basis'],Jsonb(c['codings']),c['note'],members[c['concept_id']],imp.run,c['generic'],c['brands']) for c in concepts.values()])
        cur.executemany('''UPDATE health.resources SET clinical_type=%s,concept_id=%s,concept_basis=%s
          WHERE resource_id=%s AND (clinical_type,concept_id,concept_basis) IS DISTINCT FROM (%s,%s,%s)''',
          [(t,cid,basis,rid,t,cid,basis) for rid,(cid,basis,t) in assigned.items()])
    conn.execute('DELETE FROM health.concepts WHERE patient_id=%s AND concept_id<>ALL(%s)',(patient,list(concepts)))
    for c in concepts.values():
        imp.relationship(c['concept_id'],'type:'+c['clinical_type'],'CONCEPT_CLINICAL_TYPE','ontology',{})
    for rid,(cid,basis,t) in assigned.items():
        imp.clinical_types[rid]=t
        if cid:imp.relationship(rid,cid,'REPRESENTS_CONCEPT',basis,{})
    imp.relationship(imp.account.user_id,patient,'OWNS_PATIENT_PROFILE','ownership',{})
    imp.concepts=concepts
    imp.stats['concepts']=len(concepts)
    imp.stats['records_with_concepts']=sum(1 for cid,_,_ in assigned.values() if cid)


def project_associations(imp):
    """Compute this patient's associations, keep every version, and link associated concepts in the graph."""
    from associations import brand_keys, find_associations, load_facts, pool_key
    conn,patient=imp.conn,imp.patient
    facts=load_facts(conn,patient,imp.resource_ids)
    found=find_associations(facts)
    concepts=facts['concepts']
    medication_concepts={m['concept'] for m in facts['medications']}
    def pooled(cid):
        basis,family,label,generic,_=concepts.get(cid,(None,None,None,None,()))
        return pool_key(cid,basis,'medication' if cid in medication_concepts else family,label,generic)
    with conn.cursor() as cur:
        for a in found:
            version=digest(a)
            cur.execute('''INSERT INTO health.association_versions VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING''',(patient,a['association_id'],version,Jsonb(a),imp.run))
            cur.execute('''INSERT INTO health.associations(patient_id,association_id,method,exposure_concept,exposure_label,exposure_pool_key,outcome_concept,outcome_label,outcome_pool_key,
              strength,direction,effect,statement,supporting_records,contrary_records,competing,limitations,version_sha256,first_seen_run,last_seen_run,is_current,exposure_brand_keys)
              VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,true,%s) ON CONFLICT (patient_id,association_id) DO UPDATE SET exposure_brand_keys=EXCLUDED.exposure_brand_keys,
              method=EXCLUDED.method,exposure_concept=EXCLUDED.exposure_concept,exposure_label=EXCLUDED.exposure_label,exposure_pool_key=EXCLUDED.exposure_pool_key,
              outcome_concept=EXCLUDED.outcome_concept,outcome_label=EXCLUDED.outcome_label,outcome_pool_key=EXCLUDED.outcome_pool_key,strength=EXCLUDED.strength,
              direction=EXCLUDED.direction,effect=EXCLUDED.effect,statement=EXCLUDED.statement,supporting_records=EXCLUDED.supporting_records,
              contrary_records=EXCLUDED.contrary_records,competing=EXCLUDED.competing,limitations=EXCLUDED.limitations,version_sha256=EXCLUDED.version_sha256,
              last_seen_run=EXCLUDED.last_seen_run,is_current=true''',
              (patient,a['association_id'],a['method'],a['exposure_concept'],a['exposure_label'],pooled(a['exposure_concept']),a['outcome_concept'],a['outcome_label'],
               pooled(a['outcome_concept']),a['strength'],a['direction'],Jsonb(a['effect']),a['statement'],a['supporting_records'],a['contrary_records'],
               Jsonb(a['competing']),Jsonb(a['limitations']),version,imp.run,imp.run,brand_keys(concepts[a['exposure_concept']][4] if a['exposure_concept'] in medication_concepts else ())))
    conn.execute('UPDATE health.associations SET is_current=(last_seen_run IS NOT DISTINCT FROM %s) WHERE patient_id=%s',(imp.run,patient))
    for a in found:
        if a['strength']!='insufficient' and a['exposure_concept'] in imp.concepts and a['outcome_concept'] in imp.concepts:
            imp.relationship(a['exposure_concept'],a['outcome_concept'],'DERIVED_ASSOCIATION','derived_association',{'association_id':a['association_id']},
                             {'method':a['method'],'strength':a['strength']})
    imp.stats['associations']=len(found)
    imp.stats['associations_moderate_or_strong']=sum(a['strength'] in ('moderate','strong') for a in found)


def refresh_pooled_patterns(conn):
    """Recompute pooled patterns from every opted-in patient's current associations.

    Revoked or never-given consent removes a patient's contribution at the next refresh.
    Only aggregates of at least MIN_CONTRIBUTORS distinct patients are stored."""
    from associations import pooled_patterns, MIN_CONTRIBUTORS
    conn.execute("SELECT pg_advisory_xact_lock(hashtext('health_pooled_patterns'))")
    rows=conn.execute('''SELECT a.patient_id,a.method,a.exposure_pool_key,a.exposure_label,a.outcome_pool_key,a.outcome_label,a.direction,a.strength,a.effect,a.exposure_brand_keys
      FROM health.associations a JOIN health.pooling_consent c ON c.patient_id=a.patient_id AND c.opted_in WHERE a.is_current''').fetchall()
    patterns=pooled_patterns([tuple(r.values()) for r in rows],MIN_CONTRIBUTORS)
    conn.execute('DELETE FROM health.pooled_patterns')
    with conn.cursor() as cur:
        cur.executemany('''INSERT INTO health.pooled_patterns(pattern_id,method,exposure_key,exposure_label,outcome_key,outcome_label,direction,contributors,share_moderate_or_strong,median_effect)
          VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',[(p['pattern_id'],p['method'],p['exposure_key'],p['exposure_label'],p['outcome_key'],p['outcome_label'],p['direction'],
          p['contributors'],p['share_moderate_or_strong'],Jsonb(p['median_effect']) if p['median_effect'] else None) for p in patterns])
    return len(patterns)


def edge_endpoint(imp):
    def endpoint(node):
        if node in imp.categories:return ('@Resource',imp.categories[node],imp.clinical_types[node])
        if node==imp.patient:return ('@Patient',None,None)
        if node==imp.account.user_id:return ('@User',None,None)
        if node in imp.doc_ids:return ('@Document',None,None)
        for prefix,kind in (('ontology:','@OntologyCategory'),('concept:','@Concept'),('type:','@ClinicalType')):
            if node.startswith(prefix):return (kind,None,None)
        return None
    return endpoint


def enforce_ontology(imp):
    unknown_categories=sorted(set(imp.categories.values())-imp.ontology['categories'])
    unknown_relations=sorted(imp.relations-imp.ontology['relations'])
    if unknown_categories or unknown_relations:
        raise ValueError(f'Unregistered ontology entries: categories={unknown_categories} relations={unknown_relations}. Register them in ontology/ first.')
    problems=imp.registry.edge_problems(imp.edges,edge_endpoint(imp))
    if problems:
        raise ValueError('Edges violate the registered relation rules: '+'; '.join(problems[:20]))


def sync_embeddings(imp):
    model=embedding_model()
    files=[]
    for p in sorted((DB/'models').rglob('*')):
        if p.is_file() and p.name not in ('.lock',):
            files.append({'path':str(p.relative_to(DB/'models')),'sha256':hashlib.sha256(p.read_bytes()).hexdigest()})
    model_id=digest([MODEL,files])
    imp.conn.execute('INSERT INTO health.embedding_models VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING',
                     (model_id,MODEL,384,'fastembed ONNX CPU',Jsonb(files),Jsonb({'query':'query_embed','passage':'passage_embed','local_only':True})))
    pending=imp.conn.execute('SELECT chunk_id,text_content FROM health.chunks WHERE embedding IS NULL OR embedding_model IS DISTINCT FROM %s ORDER BY chunk_id',(model_id,)).fetchall()
    for start in range(0,len(pending),128):
        batch=pending[start:start+128]
        vectors=list(model.passage_embed([r['text_content'] for r in batch],batch_size=32))
        for r,vector in zip(batch,vectors):
            imp.conn.execute('UPDATE health.chunks SET embedding=%s,embedding_model=%s WHERE chunk_id=%s',(vector,model_id,r['chunk_id']))
        if start%512==0:print(f'Embedded {min(start+len(batch),len(pending))}/{len(pending)} passages',flush=True)
    imp.stats['new_embeddings']=len(pending)


def sync_graph(imp):
    """Rebuild one patient's partition of the graph. Other patients' vertices are untouched."""
    conn,patient,account=imp.conn,imp.patient,imp.account
    if not conn.execute("SELECT 1 FROM ag_catalog.ag_graph WHERE name='health_graph'").fetchone():
        conn.execute("SELECT ag_catalog.create_graph('health_graph')")
    else:
        cypher(conn,'MATCH (n:Entity) WHERE n.patient_id='+canonical(patient)+' DETACH DELETE n RETURN count(*)')
    entities=[{'uid':patient,'name':account.patient_name,'kind':'Patient'}, {'uid':account.user_id,'name':account.user_name,'kind':'User'}]
    for c in imp.ontology['definitions']:
        entities.append({'uid':'ontology:'+c['category'],'name':c['category'],'kind':'OntologyCategory','domain':c['domain'],'subject_area':c['subject_area'],'description':c['description']})
    for t in imp.registry.types.values():
        entities.append({'uid':'type:'+t['clinical_type'],'name':t['label'],'kind':'ClinicalType','fhir_resource':t['fhir_resource'] or '','description':t['description']})
    for c in imp.concepts.values():
        entities.append({'uid':c['concept_id'],'name':c['label'],'kind':'Concept','family':c['family'] or '','basis':c['basis'],
                         'codes':[x['system']+'|'+x['code'] for x in c['codings']]})
    for r in conn.execute('SELECT resource_id,name,resource_type,category,clinical_type,evidence_kind,clinical_date,is_current FROM health.resources WHERE patient_id=%s ORDER BY resource_id',(patient,)):
        entities.append({'uid':r['resource_id'],'name':r['name'],'kind':r['resource_type'],'category':r['category'],'clinical_type':r['clinical_type'] or '','evidence_kind':r['evidence_kind'],'clinical_date':str(r['clinical_date']) if r['clinical_date'] else '', 'is_current':r['is_current']})
    for d in conn.execute('SELECT document_id,title,evidence_kind,is_current FROM health.documents WHERE patient_id=%s',(patient,)):
        entities.append({'uid':d['document_id'],'name':d['title'],'kind':'Document','evidence_kind':d['evidence_kind'],'is_current':d['is_current']})
    def literal(obj):
        if isinstance(obj,dict):return '{'+','.join(k+':'+literal(v) for k,v in obj.items())+'}'
        return canonical(obj)
    for entity in entities:entity['patient_id']=patient
    for start in range(0,len(entities),100):
        batch=entities[start:start+100]
        query='CREATE '+','.join(f'(n{i}:Entity '+literal(r)+')' for i,r in enumerate(batch))+' RETURN count(*)'
        cypher(conn,query)
    entity_ids={r['uid'] for r in entities}
    edges=[{'a':r['from_id'],'b':r['to_id'],'relation':r['relation'],'evidence_kind':r['evidence_kind'],'qualifiers':r['qualifiers']}
           for r in conn.execute('SELECT * FROM health.relationships WHERE patient_id=%s AND is_current',(patient,)) if r['from_id'] in entity_ids and r['to_id'] in entity_ids]
    for start in range(0,len(edges),100):
        query=('UNWIND '+literal_list(edges[start:start+100],literal)+' AS link MATCH (a:Entity), (b:Entity) WHERE a.uid=link.a AND b.uid=link.b'
               ' AND a.patient_id='+literal(patient)+' AND b.patient_id='+literal(patient)+
               ' CREATE (a)-[:RELATED {relation:link.relation,evidence_kind:link.evidence_kind,qualifiers:link.qualifiers,patient_id:'+literal(patient)+'}]->(b) RETURN count(*)')
        cypher(conn,query)
    conn.execute('REVOKE ALL ON ALL TABLES IN SCHEMA health_graph FROM health_reader')
    conn.execute('REVOKE ALL ON SCHEMA health_graph FROM health_reader')
    imp.stats['graph_vertices']=len(entities)
    imp.stats['graph_edges']=len(edges)


def literal_list(items, literal):
    return '['+','.join(literal(item) for item in items)+']'


def migrate(conn):
    for name in ('003_system_of_record.sql','004_multi_user.sql','005_source_history.sql','006_clinical_semantics.sql','007_connections.sql','008_medication_generics.sql'):
        conn.execute((DB/'sql'/name).read_text(),prepare=False)


def ingest(put_paths=(), apply=None, from_disk=False, operation='reproject', retire_paths=(), retire_reason=None, put_root=None):
    """Apply one change to the database-owned archive and reproject it atomically.

    The pipeline reads a workspace materialized from the database under the
    ingestion lock, never the local checkout. Local files enter only through
    `put_paths` or `from_disk`, which refuse stale or record-dropping versions.
    `apply(workspace)` lets a write tool change archive files in that workspace.
    `retire_paths` explicitly removes files from the current archive; their
    bytes, versions and records remain in the database history.
    """
    global ROOT
    import tempfile
    from workspace import StaleSourceError, check_put, current_artifacts, in_archive, materialize, tree
    checkout=ROOT
    source_root=Path(put_root) if put_root else checkout
    with connect(admin=True) as conn, tempfile.TemporaryDirectory(prefix='health-workspace-') as tmp:
        # Archive-wide schema changes serialize globally; writes serialize per patient.
        conn.execute("SELECT pg_advisory_xact_lock(hashtext('health_archive_schema'))")
        migrate(conn)
        conn.execute("SELECT pg_advisory_xact_lock(hashtext('health_archive_ingest:'||%s))",(current_account().patient_id,))
        workspace=Path(tmp)
        artifacts=materialize(conn,workspace,current_artifacts(conn))
        if from_disk:
            local=tree(checkout)
            put_paths=[rel for rel,sha in sorted(local.items()) if artifacts.get(rel)!=sha]
        refused=[]
        put_paths=[str(Path(rel).resolve().relative_to(source_root.resolve())) if Path(rel).is_absolute() else str(Path(rel)) for rel in put_paths]
        for rel in put_paths:
            if not in_archive(rel):raise ValueError('Not an archive file: '+rel)
            content=(source_root/rel).read_bytes()
            try:check_put(conn,rel,content,artifacts.get(rel),restore=not from_disk)
            except StaleSourceError as error:
                if not from_disk:raise
                refused.append({'path':rel,'reason':str(error)});continue
            (workspace/rel).parent.mkdir(parents=True,exist_ok=True)
            (workspace/rel).write_bytes(content)
        applied=apply(workspace) if apply else None
        retired=[]
        for rel in retire_paths:
            if rel not in artifacts:raise ValueError('Not a current database archive file: '+rel)
            if not retire_reason:raise ValueError('Retiring an archive file requires a reason')
            (workspace/rel).unlink();retired.append(rel)
        after=tree(workspace)
        removed=sorted(set(artifacts)-set(after)-set(retired))
        if removed:raise ValueError('Archive files cannot be deleted by a write; retire them explicitly: '+', '.join(removed[:5]))
        changed=sorted(rel for rel,sha in after.items() if artifacts.get(rel)!=sha)
        for rel in changed:check_put(conn,rel,(workspace/rel).read_bytes(),artifacts.get(rel),restore=rel in put_paths and not from_disk)
        ROOT=workspace
        try:
            summary=_project(conn,workspace,operation,changed)
            if retired:
                conn.execute('UPDATE health.artifacts SET retired_run=%s,retired_reason=%s WHERE patient_id=%s AND path=ANY(%s)',(summary['run_id'],retire_reason,current_account().patient_id,retired))
        finally:
            ROOT=checkout
    summary.update(changed_paths=changed,retired_paths=retired,refused_local_files=refused)
    if applied is not None:summary['applied']=applied
    (DB/'artifacts').mkdir(exist_ok=True)
    (DB/'artifacts/last-ingestion.json').write_text(json.dumps(summary,indent=2,default=str)+'\n')
    return summary


def _project(conn,workspace,operation,changed):
    from workspace import tree
    files=[workspace/rel for rel in sorted(tree(workspace))]
    manifest=[{'path':str(p.relative_to(workspace)),'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for p in files]
    run=conn.execute('INSERT INTO health.ingestion_runs(parser_version,manifest_sha256,operation,patient_id) VALUES (%s,%s,%s,%s) RETURNING run_id',(PARSER,digest(manifest),operation,current_account().patient_id)).fetchone()['run_id']
    def version_counts():
        return conn.execute('SELECT (SELECT count(*) FROM health.artifact_versions) AS new_artifact_versions,(SELECT count(*) FROM health.records) AS new_records,(SELECT count(*) FROM health.record_versions) AS new_record_versions,(SELECT count(*) FROM health.resource_versions) AS new_resource_versions').fetchone()
    before=version_counts()
    imp=Importer(conn,run,preload=True)
    with conn.pipeline():
        for p in files:imp.artifact(p)
        import_ontology(imp)
        for importer in (import_labcorp,import_quest,import_medications,import_symptoms,import_vitals,import_clinic,import_conversations,import_research):
            importer(imp); print(importer.__name__+' queued',flush=True)
        project_semantics(imp)
        project_associations(imp)
        enforce_ontology(imp)
        # The workspace is the database's complete current archive, so a
        # projection absent from it was removed by an explicit database change.
        conn.execute('UPDATE health.documents SET is_current=false WHERE patient_id=%s AND document_id<>ALL(%s)',(imp.patient,list(imp.current_docs)))
        conn.execute('UPDATE health.resources SET is_current=(resource_id=ANY(%s)) WHERE patient_id=%s',(list(imp.resource_ids),imp.patient))
        conn.execute('UPDATE health.relationships SET is_current=(last_seen_run IS NOT DISTINCT FROM %s) WHERE patient_id=%s',(run,imp.patient))
    # Pending cursors do not expose rowcounts until synchronization. Measure
    # actual committed-in-transaction insert deltas instead of pending -1s.
    after=version_counts()
    for key in before: imp.stats[key]=after[key]-before[key]
    imp.stats['pooled_patterns']=refresh_pooled_patterns(conn)
    sync_graph(imp)
    sync_embeddings(imp)
    conn.execute('UPDATE health.ingestion_runs SET completed_at=now(),summary=%s WHERE run_id=%s',(Jsonb(dict(imp.stats)),run))
    return {'run_id':run,'operation':operation,'manifest_sha256':digest(manifest),**dict(imp.stats)}


if __name__=='__main__':
    from common import json_out
    json_out(ingest())
