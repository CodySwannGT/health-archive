"""End-to-end connector check for an existing account: discovery, registration, PKCE authorization,
sign-in, token exchange, MCP tool calls, refresh rotation and revocation.

    HEALTH_MCP_TEST_USERNAME=name HEALTH_MCP_TEST_PASSWORD_FILE=path python connector_flow.py https://server.example
"""
import json
import os
from pathlib import Path
import sys

from flow import Connector, check, results

USERNAME = os.environ['HEALTH_MCP_TEST_USERNAME']
PASSWORD = Path(os.environ['HEALTH_MCP_TEST_PASSWORD_FILE']).read_text().strip()
c = Connector(sys.argv[1]).discover()
check('unauthenticated request rejected with resource metadata', c.unauthenticated_status == 401)
check('protected resource metadata names the authorization server', c.resource['authorization_servers'] and c.resource['resource'].endswith('/mcp'))
check('authorization server supports PKCE S256 and registration', 'S256' in c.meta['code_challenge_methods_supported'] and c.meta.get('registration_endpoint'))
check('dynamic client registration', c.register('Connector flow test').get('client_id'))
response = c.start_authorization()
check('authorization redirects to sign-in', response.status_code == 302 and '/oauth/consent' in c.consent_url)
page = c.http.get(c.consent_url)
check('sign-in page names the client and redirect host', page.status_code == 200 and 'Connector flow test' in page.text and '127.0.0.1:53682' in page.text)
wrong = c.sign_in(USERNAME, 'not the password')
check('wrong password is refused', wrong.status_code == 200 and 'Incorrect username or password' in wrong.text)
code = c.code_from(c.sign_in(USERNAME, PASSWORD))
check('sign-in returns a code and the original state', code)
check('code exchange with PKCE issues tokens', c.exchange(code).status_code == 200 and c.tokens.get('refresh_token'))
check('authorization code cannot be replayed', c.http.post(c.meta['token_endpoint'], data=c.token_form).status_code == 400)
init, body = c.initialize()
check('MCP initialize with bearer token', init.status_code == 200 and body['result']['serverInfo']['name'] == 'health-archive')
_, listed = c.rpc('tools/list', {})
names = {t['name'] for t in listed['result']['tools']}
check('tools listed', {'search', 'fetch', 'vitals', 'record_narrative', 'capture_conversation', 'ontology'} <= names)
result, status = c.call('status', {})
check('status reports the signed-in patient', not result.get('isError') and status['patient_id'].startswith('pat_'))
result, found = c.call('search', {'query': 'weight'})
check('search returns results', not result.get('isError') and 'results' in found)
if found['results']:
    result, doc = c.call('fetch', {'id': found['results'][0]['id']})
    check('fetch returns document text', not result.get('isError') and doc['text'])
result, onto = c.call('ontology', {'category': 'vital_sign_measurement'})
check('ontology returns a category JSON Schema', not result.get('isError') and onto['json_schema'])
result, _ = c.call('query', {'sql': 'DELETE FROM health.resources'})
check('query tool cannot write', result.get('isError'))
old_access, old_refresh = c.tokens['access_token'], c.tokens['refresh_token']
refreshed = c.http.post(c.meta['token_endpoint'], data={'grant_type': 'refresh_token', 'refresh_token': old_refresh, 'client_id': c.client['client_id']}).json()
check('refresh token issues new tokens', refreshed.get('access_token') and refreshed['refresh_token'] != old_refresh)
check('rotated refresh token cannot be reused', c.http.post(c.meta['token_endpoint'], data={'grant_type': 'refresh_token', 'refresh_token': old_refresh, 'client_id': c.client['client_id']}).status_code == 400)
check('access token from the rotated family is revoked', c.rpc('tools/list', {}, token=old_access)[0].status_code == 401)
revocation = c.http.post(c.meta['revocation_endpoint'], data={'token': refreshed['refresh_token'], 'token_type_hint': 'refresh_token', 'client_id': c.client['client_id']})
check('revocation endpoint accepts the request', revocation.status_code == 200)
check('revocation invalidates the token family', c.rpc('tools/list', {}, token=refreshed['access_token'])[0].status_code == 401)
print(json.dumps({'passed': sum(results.values()), 'total': len(results)}))
