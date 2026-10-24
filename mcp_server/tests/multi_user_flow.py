"""Multi-user isolation check against a test server: invitation sign-up, a new
patient archive per account, and no cross-account reads or writes.

    HEALTH_MCP_TEST_INVITES=file-with-two-invites python multi_user_flow.py http://127.0.0.1:8765
Use only against an isolated test database: it creates two accounts.
"""
import json
import os
from pathlib import Path
import secrets
import sys

from flow import Connector, check, results

base = sys.argv[1]
invites = Path(os.environ['HEALTH_MCP_TEST_INVITES']).read_text().split()
suffix = secrets.token_hex(3)
people = [('alice_' + suffix, 'Alice Example', 71), ('bob_' + suffix, 'Bob Example', 88)]
password = 'correct horse ' + secrets.token_hex(6)

probe = Connector(base).discover()
probe.register('Invitation probe')
probe.start_authorization()
bad = probe.sign_up('inv-not-a-real-invitation', 'mallory_' + suffix, password, 'Mallory')
check('sign-up without a valid invitation is refused', bad.status_code == 200 and 'invitation' in bad.text.lower())

sessions = {}
for (username, name, pulse), invite in zip(people, invites):
    c = Connector(base).discover()
    c.connect(f'{name} client', username, password, invite=invite, display_name=name)
    result, status = c.call('status', {})
    check(f'{username}: sign-up creates a separate patient chart', not result.get('isError') and status['patient_scope'] == username)
    result, onto = c.call('ontology', {})
    check(f'{username}: new archive is seeded with the ontology template', not result.get('isError') and onto['count'] >= 40)
    source = f'my pulse was {pulse} this morning'
    extraction = {'reported_at': '2026-10-24T13:00:00.000Z', 'reported_local_date': '2026-10-24', 'records': [{
        'record_key': 'pulse', 'record_type': 'vital_sign_measurement', 'clinical_diagnosis': None, 'terminology_code': None,
        'source_context': 'Multi-user isolation test fixture.',
        'details': {'metric': 'pulse', 'label': 'Pulse', 'value': pulse, 'unit': 'beats/min', 'source_text': source,
                    'value_basis': 'explicit_patient_value', 'unit_basis': 'conventional_unit_for_metric', 'reading_key': 'test',
                    'paired_component_record_ids': [], 'measurement_time_source': 'this morning', 'measurement_conditions': None,
                    'occurrence': {'date': '2026-10-24', 'precision': 'day', 'basis': 'relative_to_report_date', 'source_text': 'this morning', 'anchor_date': '2026-10-24'},
                    'notes': [], 'unknowns': []},
        'evidence_quotes': [source]}]}
    result, recorded = c.call('record_narrative', {'source_name': 'pulse-test', 'source_text': source, 'extraction': extraction})
    check(f'{username}: records a fact in their own archive', not result.get('isError') and recorded['applied']['new_records'] == 1, str(result)[:300])
    result, vitals = c.call('vitals', {})
    values = [r['numeric_value'] for r in vitals['results']]
    check(f'{username}: reads back only their own value', values == [pulse], values)
    sessions[username] = (c, status, vitals['results'][0]['record_id'])

(alice, alice_status, alice_record), (bob, bob_status, bob_record) = sessions.values()
check('patients are distinct', alice_status['patient_id'] != bob_status['patient_id'])
result, leaked = bob.call('get_record', {'record_id': alice_record})
check("bob cannot read alice's record by ID", result.get('isError') and leaked is None, str(result)[:200])
result, _ = bob.call('graph', {'record_id': alice_record})
check("bob cannot traverse alice's graph", result.get('isError'))
result, rows = bob.call('query', {'sql': "SELECT count(*) AS n FROM health.resources WHERE patient_id='" + alice_status['patient_id'] + "'"})
check('bob sees no rows of alice through SQL', not result.get('isError') and rows['rows'][0]['n'] == 0)
result, rows = bob.call('query', {'sql': 'SELECT count(DISTINCT patient_id) AS n FROM health.resources'})
check('bob sees exactly one patient through SQL', rows['rows'][0]['n'] == 1)
result, found = bob.call('search_archive', {'question': 'pulse 71 this morning'})
check("bob's search never returns alice's passages", all('71' not in m['excerpt'] for m in found['matches']))
result, scoped = bob.call('record_narrative', {'source_name': 'scope-test', 'source_text': 'my pulse was 88 this morning',
                                              'extraction': {'patient_scope': people[0][0], 'reported_at': '2026-10-24T13:00:00.000Z', 'reported_local_date': '2026-10-24', 'records': []}})
check("bob cannot write with alice's patient scope", result.get('isError'))
result, vitals = alice.call('vitals', {})
check("alice's archive is unchanged by bob", [r['numeric_value'] for r in vitals['results']] == [people[0][2]])
print(json.dumps({'passed': sum(results.values()), 'total': len(results)}))
