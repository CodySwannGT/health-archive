"""Multi-user isolation check against a test server: open sign-up, a new
patient archive per account, and no cross-account reads or writes.

    python multi_user_flow.py http://127.0.0.1:8765
Use only against an isolated test database: it creates two accounts.
"""
import json
import secrets
import sys

from flow import Connector, check, results

base = sys.argv[1]
suffix = secrets.token_hex(3)
people = [('alice_' + suffix, 'Alice Example', 71), ('bob_' + suffix, 'Bob Example', 88)]
password = 'correct horse ' + secrets.token_hex(6)

probe = Connector(base).discover()
probe.register('Sign-up probe')
probe.start_authorization()
page = probe.http.get(probe.consent_url)
check('sign-in page offers sign-up without an invitation', 'Create an account</a>' in page.text and 'invitation' not in page.text.lower())
weak = probe.sign_up('mallory_' + suffix, 'short', 'Mallory')
check('sign-up with a short password is refused', weak.status_code == 200 and 'at least' in weak.text)

sessions = {}
for username, name, pulse in people:
    c = Connector(base).discover()
    c.connect(f'{name} client', username, password, sign_up=True, display_name=name)
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
# Deleting an account removes it and its archive, and leaves everyone else's alone.
page = alice.http.get(base + '/account/delete')
check('delete page asks for the password and a typed confirmation', page.status_code == 200 and 'delete my archive' in page.text)
wrong = alice.http.post(base + '/account/delete', data={'username': people[0][0], 'password': password, 'confirm': 'yes'})
check('delete without the confirmation phrase is refused', wrong.status_code == 200 and 'to confirm' in wrong.text)
done = alice.http.post(base + '/account/delete', data={'username': people[0][0], 'password': password, 'confirm': 'delete my archive'})
check('delete with password and confirmation succeeds', done.status_code == 200 and 'permanently deleted' in done.text)
check("alice's tokens stop working", alice.rpc('tools/list', {})[0].status_code == 401)
again = Connector(base).discover(); again.register('After deletion'); again.start_authorization()
check('alice cannot sign in again', 'Incorrect username or password' in again.sign_in(people[0][0], password).text)
result, vitals = bob.call('vitals', {})
check("bob's archive is intact after alice is deleted", [r['numeric_value'] for r in vitals['results']] == [people[1][2]])
for path in ('/privacy', '/terms'):
    page = alice.http.get(base + path)
    check(f'{path} is served', page.status_code == 200 and 'medical advice' in page.text)
print('ALICE_PATIENT', alice_status['patient_id'])
# Three sign-ups per address per hour: alice and bob used two.
extra = Connector(base).discover(); extra.register('Third account'); extra.start_authorization()
check('a third sign-up from one address is allowed', extra.sign_up('carol_' + suffix, password, 'Carol').status_code == 302)
extra = Connector(base).discover(); extra.register('Fourth account'); extra.start_authorization()
check('a fourth sign-up from one address within the hour is refused', extra.sign_up('dave_' + suffix, password, 'Dave').status_code == 429)
print(json.dumps({'passed': sum(results.values()), 'total': len(results)}))
