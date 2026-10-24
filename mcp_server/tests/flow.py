"""OAuth and MCP client steps shared by the end-to-end connector checks."""
import base64
import hashlib
import json
import re
import secrets
from urllib.parse import parse_qs, urlparse

import httpx

REDIRECT = 'http://127.0.0.1:53682/callback'
results = {}


def check(name, condition, detail=''):
    results[name] = bool(condition)
    print(('PASS ' if condition else 'FAIL ') + name + (f' ({detail})' if detail and not condition else ''))
    if not condition:
        raise SystemExit(1)


class Connector:
    def __init__(self, base):
        self.base = base.rstrip('/')
        self.http = httpx.Client(timeout=300, follow_redirects=False)
        self.ids = iter(range(1, 10**6))

    def discover(self):
        unauth = self.http.post(self.base + '/mcp', json={'jsonrpc': '2.0', 'id': 0, 'method': 'initialize', 'params': {}},
                                headers={'Accept': 'application/json, text/event-stream'})
        header = unauth.headers.get('www-authenticate', '')
        self.unauthenticated_status = unauth.status_code
        self.resource = self.http.get(re.search(r'resource_metadata="([^"]+)"', header)[1]).json()
        issuer = self.resource['authorization_servers'][0].rstrip('/')
        self.issuer = issuer
        self.meta = self.http.get(issuer + '/.well-known/oauth-authorization-server').json()
        return self

    def register(self, name):
        self.client = self.http.post(self.meta['registration_endpoint'], json={
            'client_name': name, 'redirect_uris': [REDIRECT], 'grant_types': ['authorization_code', 'refresh_token'],
            'response_types': ['code'], 'token_endpoint_auth_method': 'none', 'scope': 'health.read health.write'}).json()
        return self.client

    def start_authorization(self):
        self.verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(self.verifier.encode()).digest()).decode().rstrip('=')
        self.state = secrets.token_urlsafe(12)
        response = self.http.get(self.meta['authorization_endpoint'], params={
            'response_type': 'code', 'client_id': self.client['client_id'], 'redirect_uri': REDIRECT, 'code_challenge': challenge,
            'code_challenge_method': 'S256', 'state': self.state, 'scope': 'health.read health.write', 'resource': self.resource['resource']})
        self.consent_url = response.headers.get('location', '')
        self.form = {k: v[0] for k, v in parse_qs(urlparse(self.consent_url).query).items()}
        return response

    def sign_in(self, username, password):
        return self.http.post(self.issuer + '/oauth/consent', data={**self.form, 'username': username, 'password': password, 'decision': 'approve'})

    def sign_up(self, invite, username, password, display_name, timezone='America/Chicago'):
        return self.http.post(self.issuer + '/oauth/signup', data={**self.form, 'invite': invite, 'username': username, 'display_name': display_name,
                                                                   'timezone': timezone, 'password': password, 'confirm': password})

    def code_from(self, response):
        location = response.headers.get('location', '')
        query = parse_qs(urlparse(location).query)
        ok = response.status_code == 302 and location.startswith(REDIRECT) and query.get('state') == [self.state]
        return query['code'][0] if ok else None

    def exchange(self, code, verifier=None):
        self.token_form = {'grant_type': 'authorization_code', 'code': code, 'redirect_uri': REDIRECT, 'client_id': self.client['client_id'],
                           'code_verifier': verifier or self.verifier, 'resource': self.resource['resource']}
        response = self.http.post(self.meta['token_endpoint'], data=self.token_form)
        if response.status_code == 200:
            self.tokens = response.json()
        return response

    def rpc(self, method, params, token=None, notify=False):
        headers = {'Authorization': 'Bearer ' + (token or self.tokens['access_token']), 'Accept': 'application/json, text/event-stream',
                   'Content-Type': 'application/json', 'MCP-Protocol-Version': '2025-11-25'}
        if getattr(self, 'session', None):
            headers['Mcp-Session-Id'] = self.session
        body = {'jsonrpc': '2.0', 'method': method, 'params': params}
        if not notify:
            body['id'] = next(self.ids)
        response = self.http.post(self.base + '/mcp', json=body, headers=headers)
        if notify or response.status_code != 200:
            return response, None
        text = response.text
        if response.headers.get('content-type', '').startswith('text/event-stream'):
            text = [line[5:].strip() for line in text.splitlines() if line.startswith('data:')][-1]
        return response, json.loads(text)

    def initialize(self):
        response, body = self.rpc('initialize', {'protocolVersion': '2025-11-25', 'capabilities': {}, 'clientInfo': {'name': 'connector-flow', 'version': '1'}})
        self.session = response.headers.get('mcp-session-id')
        self.rpc('notifications/initialized', {}, notify=True)
        return response, body

    def call(self, name, arguments):
        _, reply = self.rpc('tools/call', {'name': name, 'arguments': arguments})
        result = reply['result']
        if result.get('isError'):
            return result, None
        return result, result.get('structuredContent') or json.loads(result['content'][0]['text'])

    def connect(self, client_name, username=None, password=None, invite=None, display_name=None):
        """Register, authorize, sign in or sign up, exchange the code and initialize."""
        self.register(client_name)
        self.start_authorization()
        response = self.sign_up(invite, username, password, display_name) if invite else self.sign_in(username, password)
        code = self.code_from(response)
        if not code:
            raise SystemExit(f'authorization failed: {response.status_code} {response.text[:300]}')
        self.exchange(code)
        self.initialize()
        return self
