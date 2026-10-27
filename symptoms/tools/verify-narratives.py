import json,hashlib,subprocess,sys
from pathlib import Path
from jsonschema import Draft202012Validator,FormatChecker
code=Path(__file__).resolve().parents[2]
# Optional argument: an archive root such as a database-materialized workspace.
# Optional arguments: an archive root such as a database-materialized workspace, then the
# source and extraction to replay for the byte-identical repeat-import check.
root=Path(sys.argv[1]).resolve() if len(sys.argv)>1 else code;s=root/'symptoms'
def latest_input():
 # The most recent archived source with its extraction, for the repeat-import check.
 for source in sorted((s/'inputs').glob('*.txt'),reverse=True):
  extraction=source.with_name(source.stem+'.extraction.json')
  if extraction.exists():return source,extraction
 raise SystemExit('No archived narrative input to replay')
repeat=(Path(sys.argv[2]).resolve(),Path(sys.argv[3]).resolve()) if len(sys.argv)>3 else latest_input()
sys.path.insert(0,str(code/'database/tools'))
from measurement_corrections import measurement_supersessions
rows=[json.loads(t) for t in (s/'records.jsonl').read_text().splitlines()]
measurement_supersessions(rows)
validator=Draft202012Validator(json.loads((s/'symptom.schema.json').read_text()),format_checker=FormatChecker())
ids={r['record_id'] for r in rows};assert len(ids)==len(rows)
meds={json.loads(t)['medication_id'] for name in ['medications','reported-medications'] if (root/f'meds/{name}.jsonl').exists() for t in (root/f'meds/{name}.jsonl').read_text().splitlines()}
spans=refs=0
administration_path=root/'meds/reported-administrations.jsonl'
administrations={json.loads(t)['administration_id'] for t in administration_path.read_text().splitlines()} if administration_path.exists() else set()
for r in rows:
 validator.validate(r)
 p=r['provenance'];source=(s/p['source_path']).read_text()
 assert hashlib.sha256(source.encode()).hexdigest()==p['source_sha256']
 for e in p['evidence']:
  assert source[e['start_char']:e['end_char']]==e['quote'];spans+=1
 d=r['details']
 if r['record_type']=='body_composition_measurement':assert d['value_range']['minimum']<=d['value_range']['maximum']
 for field in ['related_record_ids','to_record_ids','paired_component_record_ids','pre_treatment_record_ids','persistent_record_ids','treatment_goal_record_ids']:
  for v in d.get(field,[]):assert v in ids,(r['record_id'],field,v);refs+=1
 if d.get('from_record_id'):assert d['from_record_id'] in ids;refs+=1
 for v in d.get('medication_ids',[]):assert v in meds
 if d.get('medication_id'):assert d['medication_id'] in meds
 for v in d.get('related_administration_ids',[]):assert v in administrations
exports=['records.jsonl','observations.jsonl','patterns.jsonl','exposures.jsonl','associations.jsonl','hypotheses.jsonl','medication-responses.jsonl','care-preferences.jsonl','exercise-routines.jsonl','anthropometrics.jsonl','body-composition.jsonl','demographics.jsonl','measurement-contexts.jsonl','vital-signs.jsonl','sleep.jsonl','check-in-tracking.jsonl','current-state.json','inventory.json']
exports=[p for p in exports if (s/p).exists()]
hashes={p:hashlib.sha256((s/p).read_bytes()).hexdigest() for p in exports}
result=json.loads(subprocess.check_output([sys.executable,str(code/'symptoms/tools/ingest-narrative.py'),str(repeat[0]),str(repeat[1]),'--root',str(s)]))
assert result['new_records']==result['changed_records']==0
assert hashes=={p:hashlib.sha256((s/p).read_bytes()).hexdigest() for p in exports}
prior=(s/'verification.json').read_bytes() if (s/'verification.json').exists() else None
prior_path=s/'verification-history'/('before-narrative-verification-'+hashlib.sha256(prior).hexdigest()[:12]+'.json') if prior else None
if prior_path:
 prior_path.parent.mkdir(exist_ok=True)
 if not prior_path.exists():prior_path.write_bytes(prior)
report={'status':'passed','schema_validated_records':len(rows),'unique_record_ids':len(ids),'source_narratives':len({r['report_id'] for r in rows}),
 'counts':{kind:sum(r['record_type']==kind for r in rows) for kind in sorted({r['record_type'] for r in rows})},
 'quoted_evidence_spans_verified':spans,'cross_record_references_verified':refs,'medication_links_verified':True,
 'all_source_hashes_verified':True,'no_clinical_diagnoses_assigned':all(r['clinical_diagnosis'] is None for r in rows),
 'repeat_import_byte_identical':True,'repeat_source':str(repeat[0].relative_to(s)),'repeat_result':result,
 'prior_verification_path':str(prior_path.relative_to(s)) if prior_path else None,'export_hashes':hashes}
(s/'verification.json').write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps({k:v for k,v in report.items() if k not in ('export_hashes','repeat_result')},indent=2))
