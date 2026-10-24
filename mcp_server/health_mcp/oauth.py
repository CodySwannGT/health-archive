"""OAuth 2.1 authorization server for the multi-user health archive connector.

Implements the MCP authorization flow: protected resource and authorization
server metadata, dynamic client registration, authorization code with PKCE
(S256), refresh token rotation and revocation. State lives in the
`health_oauth` schema, which the read-only database role cannot reach. Codes,
tokens and invitations are stored only as SHA-256 hashes, and passwords as
scrypt hashes. Each token is bound to the user who approved it, and tools act
only on that user's patient chart.

People sign in on the consent page with their username and password. New
people create an account with an invitation from the operator, which keeps
the service closed to strangers' health data unless the operator invites them.
"""
import asyncio
import hmac
import html
import secrets
import time

from mcp.server.auth.provider import (AccessToken, AuthorizationCode, AuthorizationParams,
                                      OAuthAuthorizationServerProvider, RefreshToken, TokenError,
                                      construct_redirect_uri)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse

from health_mcp import accounts
from health_mcp.hashing import DECOY, hash_passphrase, sha, verify_passphrase

SCOPES = ['health.read', 'health.write']
ACCESS_TTL = 3600
REFRESH_TTL = 30 * 86400
CODE_TTL = 300
PENDING_TTL = 900
MAX_FAILURES = 5
GLOBAL_MAX_FAILURES = 50
LOCKOUT_SECONDS = 900
MIN_PASSWORD = 12

SCHEMA = '''
CREATE SCHEMA IF NOT EXISTS health_oauth;
REVOKE ALL ON SCHEMA health_oauth FROM PUBLIC;
CREATE TABLE IF NOT EXISTS health_oauth.clients (
  client_id text PRIMARY KEY, info jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS health_oauth.pending (
  request_id text PRIMARY KEY, client_id text NOT NULL REFERENCES health_oauth.clients,
  params jsonb NOT NULL, csrf_sha256 text NOT NULL, expires_at double precision NOT NULL);
CREATE TABLE IF NOT EXISTS health_oauth.codes (
  code_sha256 text PRIMARY KEY, client_id text NOT NULL REFERENCES health_oauth.clients,
  data jsonb NOT NULL, expires_at double precision NOT NULL);
CREATE TABLE IF NOT EXISTS health_oauth.tokens (
  token_sha256 text PRIMARY KEY, kind text NOT NULL CHECK (kind IN ('access','refresh')),
  client_id text NOT NULL REFERENCES health_oauth.clients, scopes jsonb NOT NULL,
  resource text, expires_at bigint, family text NOT NULL, revoked boolean NOT NULL DEFAULT false,
  created_at timestamptz NOT NULL DEFAULT now());
ALTER TABLE health_oauth.tokens ADD COLUMN IF NOT EXISTS user_id text REFERENCES health.users;
-- Tokens issued before per-user accounts have no user and stop working.
UPDATE health_oauth.tokens SET revoked=true WHERE user_id IS NULL AND NOT revoked;
CREATE TABLE IF NOT EXISTS health_oauth.login_failures (at double precision NOT NULL);
ALTER TABLE health_oauth.login_failures ADD COLUMN IF NOT EXISTS username text;
'''


class HealthOAuthProvider(OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]):
    def __init__(self, connect, issuer_url):
        self.connect = connect
        self.issuer_url = issuer_url.rstrip('/')

    def migrate(self):
        with self.connect() as conn:
            accounts.migrate(conn)
            conn.execute(SCHEMA)

    async def _db(self, fn):
        def run():
            with self.connect() as conn:
                return fn(conn)
        return await asyncio.to_thread(run)

    async def get_client(self, client_id):
        row = await self._db(lambda c: c.execute('SELECT info FROM health_oauth.clients WHERE client_id=%s', (client_id,)).fetchone())
        return OAuthClientInformationFull.model_validate(row['info']) if row else None

    async def register_client(self, client_info):
        from psycopg.types.json import Jsonb
        info = client_info.model_dump(mode='json')
        await self._db(lambda c: c.execute('INSERT INTO health_oauth.clients(client_id,info) VALUES (%s,%s)', (client_info.client_id, Jsonb(info))))

    async def authorize(self, client, params: AuthorizationParams):
        """Hold the request until a person signs in and approves it."""
        from psycopg.types.json import Jsonb
        request_id, csrf = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
        data = params.model_dump(mode='json')
        await self._db(lambda c: c.execute('INSERT INTO health_oauth.pending VALUES (%s,%s,%s,%s,%s)',
                                           (request_id, client.client_id, Jsonb(data), sha(csrf), time.time() + PENDING_TTL)))
        return f'{self.issuer_url}/oauth/consent?request={request_id}&csrf={csrf}'

    async def load_authorization_code(self, client, authorization_code):
        row = await self._db(lambda c: c.execute('SELECT data,expires_at FROM health_oauth.codes WHERE code_sha256=%s AND client_id=%s',
                                                 (sha(authorization_code), client.client_id)).fetchone())
        if not row or row['expires_at'] < time.time():
            return None
        return AuthorizationCode(code=authorization_code, **row['data'])

    def _issue(self, conn, client_id, scopes, resource, family, user_id):
        from psycopg.types.json import Jsonb
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
        now = int(time.time())
        for token, kind, ttl in ((access, 'access', ACCESS_TTL), (refresh, 'refresh', REFRESH_TTL)):
            conn.execute('INSERT INTO health_oauth.tokens(token_sha256,kind,client_id,scopes,resource,expires_at,family,user_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)',
                         (sha(token), kind, client_id, Jsonb(scopes), resource, now + ttl, family, user_id))
        return OAuthToken(access_token=access, token_type='Bearer', expires_in=ACCESS_TTL, scope=' '.join(scopes), refresh_token=refresh)

    async def exchange_authorization_code(self, client, authorization_code):
        def run(conn):
            # One use only: deleting the code makes a replayed code fail.
            if not conn.execute('DELETE FROM health_oauth.codes WHERE code_sha256=%s RETURNING 1', (sha(authorization_code.code),)).fetchone():
                raise TokenError('invalid_grant', 'authorization code already used')
            return self._issue(conn, client.client_id, authorization_code.scopes, authorization_code.resource,
                               secrets.token_hex(16), authorization_code.subject)
        return await self._db(run)

    def _active(self, conn, token, kind, client_id=None):
        return conn.execute(f'''SELECT t.scopes,t.resource,t.expires_at,t.client_id,t.user_id,t.family FROM health_oauth.tokens t
          JOIN health_oauth.accounts a USING(user_id) WHERE t.token_sha256=%s AND t.kind=%s AND NOT t.revoked AND NOT a.disabled
          {'AND t.client_id=%s' if client_id else ''}''', (sha(token), kind, *([client_id] if client_id else []))).fetchone()

    async def load_refresh_token(self, client, refresh_token):
        row = await self._db(lambda c: self._active(c, refresh_token, 'refresh', client.client_id))
        if not row or row['expires_at'] < time.time():
            return None
        return RefreshToken(token=refresh_token, client_id=client.client_id, scopes=row['scopes'], expires_at=row['expires_at'],
                            resource=row['resource'], subject=row['user_id'])

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        def run(conn):
            row = conn.execute("UPDATE health_oauth.tokens SET revoked=true WHERE token_sha256=%s AND kind='refresh' AND NOT revoked RETURNING family,user_id",
                               (sha(refresh_token.token),)).fetchone()
            if not row:
                raise TokenError('invalid_grant', 'refresh token is no longer valid')
            # Rotation: the old refresh token and its access tokens stop working.
            conn.execute("UPDATE health_oauth.tokens SET revoked=true WHERE family=%s AND kind='access'", (row['family'],))
            return self._issue(conn, client.client_id, scopes or refresh_token.scopes, refresh_token.resource, row['family'], row['user_id'])
        return await self._db(run)

    async def load_access_token(self, token):
        row = await self._db(lambda c: self._active(c, token, 'access'))
        if not row or row['expires_at'] < time.time():
            return None
        return AccessToken(token=token, client_id=row['client_id'], scopes=row['scopes'], expires_at=row['expires_at'],
                           resource=row['resource'], subject=row['user_id'])

    async def revoke_token(self, token):
        await self._db(lambda c: c.execute('UPDATE health_oauth.tokens SET revoked=true WHERE family=(SELECT family FROM health_oauth.tokens WHERE token_sha256=%s)', (sha(token.token),)))

    async def revoke(self, request: Request):
        """RFC 7009 revocation. Public clients identify themselves by client_id alone.

        Replaces the SDK handler, which wrongly requires client_secret from public clients."""
        from starlette.responses import JSONResponse, Response
        form = await request.form()
        client = await self.get_client(form.get('client_id', ''))
        if not client or (client.client_secret and not hmac.compare_digest(client.client_secret, form.get('client_secret') or '')):
            return JSONResponse({'error': 'invalid_client'}, 401)
        token = form.get('token')
        if not token:
            return JSONResponse({'error': 'invalid_request'}, 400)
        found = await self.load_access_token(token) or await self.load_refresh_token(client, token)
        if found and found.client_id == client.client_id:
            await self.revoke_token(found)
        return Response(status_code=200, headers={'Cache-Control': 'no-store'})

    # Sign-in, sign-up and consent -------------------------------------------

    def _pending(self, conn, request_id, csrf):
        row = conn.execute('SELECT p.client_id,p.params,p.csrf_sha256,p.expires_at,c.info FROM health_oauth.pending p JOIN health_oauth.clients c USING(client_id) WHERE request_id=%s',
                           (request_id,)).fetchone()
        if not row or row['expires_at'] < time.time() or not hmac.compare_digest(row['csrf_sha256'], sha(csrf or '')):
            return None
        return row

    def _locked(self, conn, username):
        conn.execute('DELETE FROM health_oauth.login_failures WHERE at<%s', (time.time() - LOCKOUT_SECONDS,))
        row = conn.execute('SELECT count(*) AS total, count(*) FILTER (WHERE username=%s) AS mine FROM health_oauth.login_failures', (username,)).fetchone()
        return row['mine'] >= MAX_FAILURES or row['total'] >= GLOBAL_MAX_FAILURES

    def _grant(self, conn, row, request_id, user_id):
        """Issue a one-time code for the pending request to the signed-in user."""
        from psycopg.types.json import Jsonb
        params = row['params']
        code = secrets.token_urlsafe(32)
        requested = params.get('scopes') or SCOPES
        data = {'scopes': [s for s in requested if s in SCOPES], 'expires_at': time.time() + CODE_TTL, 'client_id': row['client_id'],
                'code_challenge': params['code_challenge'], 'redirect_uri': params['redirect_uri'],
                'redirect_uri_provided_explicitly': params['redirect_uri_provided_explicitly'], 'resource': params.get('resource'), 'subject': user_id}
        conn.execute('INSERT INTO health_oauth.codes VALUES (%s,%s,%s,%s)', (sha(code), row['client_id'], Jsonb(data), data['expires_at']))
        conn.execute('DELETE FROM health_oauth.pending WHERE request_id=%s', (request_id,))
        return construct_redirect_uri(params['redirect_uri'], code=code, state=params.get('state'))

    async def _request(self, request):
        form = request.query_params if request.method == 'GET' else await request.form()
        request_id, csrf = form.get('request', ''), form.get('csrf', '')
        row = await self._db(lambda c: self._pending(c, request_id, csrf))
        return form, request_id, csrf, row

    async def consent(self, request: Request):
        form, request_id, csrf, row = await self._request(request)
        if not row:
            return _page('Authorization expired', '<p>This authorization request is invalid or has expired. Start the connection again from your client.</p>', 400)
        if request.method == 'GET':
            return _consent_page(row, request_id, csrf)
        if form.get('decision') == 'deny':
            await self._db(lambda c: c.execute('DELETE FROM health_oauth.pending WHERE request_id=%s', (request_id,)))
            return RedirectResponse(construct_redirect_uri(row['params']['redirect_uri'], error='access_denied', state=row['params'].get('state')), 302)
        username = (form.get('username') or '').strip().lower()
        def check(conn):
            if self._locked(conn, username):
                return 'locked', None
            account = conn.execute('SELECT user_id,password_hash FROM health_oauth.accounts WHERE username=%s AND NOT disabled', (username,)).fetchone()
            if not verify_passphrase(form.get('password', ''), account['password_hash'] if account else DECOY) or not account:
                conn.execute('INSERT INTO health_oauth.login_failures(at,username) VALUES (%s,%s)', (time.time(), username))
                return 'wrong', None
            return 'ok', self._grant(conn, row, request_id, account['user_id'])
        result, location = await self._db(check)
        if result == 'locked':
            return _page('Temporarily locked', '<p>Too many failed sign-ins. Try again in 15 minutes.</p>', 429)
        if result == 'wrong':
            return _consent_page(row, request_id, csrf, error='Incorrect username or password.', username=username)
        return RedirectResponse(location, 302)

    async def signup(self, request: Request):
        form, request_id, csrf, row = await self._request(request)
        if not row:
            return _page('Authorization expired', '<p>This authorization request is invalid or has expired. Start the connection again from your client.</p>', 400)
        if request.method == 'GET':
            return _signup_page(request_id, csrf)
        username = (form.get('username') or '').strip().lower()
        display = (form.get('display_name') or '').strip()[:100]
        timezone = (form.get('timezone') or 'UTC').strip()
        password = form.get('password', '')
        values = {'username': username, 'display_name': display, 'timezone': timezone}
        try:
            import zoneinfo
            zoneinfo.ZoneInfo(timezone)
        except Exception:
            return _signup_page(request_id, csrf, 'Use a time zone name such as America/New_York.', values)
        if len(password) < MIN_PASSWORD or password != form.get('confirm', ''):
            return _signup_page(request_id, csrf, f'Passwords must match and be at least {MIN_PASSWORD} characters.', values)
        if not display:
            return _signup_page(request_id, csrf, 'Enter your name.', values)
        def create(conn):
            if self._locked(conn, 'signup'):
                return 'locked'
            try:
                return accounts.create_account(conn, username, hash_passphrase(password), display, timezone, invite=(form.get('invite') or '').strip())
            except ValueError as error:
                conn.rollback()
                if 'invitation' in str(error):
                    conn.execute('INSERT INTO health_oauth.login_failures(at,username) VALUES (%s,%s)', (time.time(), 'signup'))
                    conn.commit()
                return str(error)
        created = await self._db(create)
        if created == 'locked':
            return _page('Temporarily locked', '<p>Too many failed sign-ups. Try again in 15 minutes.</p>', 429)
        if isinstance(created, str):
            return _signup_page(request_id, csrf, created, values)
        await asyncio.to_thread(accounts.seed_archive, created)
        location = await self._db(lambda c: self._grant(c, row, request_id, created.user_id))
        return RedirectResponse(location, 302)


def _page(title, body, status=200):
    content = f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title><style>
:root{{color-scheme:light dark;--bg:#f7f7f5;--fg:#1c1c1a;--muted:#5f5f5a;--card:#fff;--line:#d9d9d4;--accent:#2f5d50}}
@media (prefers-color-scheme:dark){{:root{{--bg:#151513;--fg:#ecece8;--muted:#a3a39c;--card:#1f1f1c;--line:#3a3a35;--accent:#7fb8a6}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,sans-serif;display:grid;place-items:center;min-height:100vh;padding:16px}}
main{{background:var(--card);border:1px solid var(--line);border-radius:10px;max-width:440px;width:100%;padding:24px}}
h1{{font-size:1.25rem;margin:0 0 12px}} p,li{{color:var(--muted)}} code{{word-break:break-all}} a{{color:var(--accent)}}
label{{display:block;margin:14px 0 6px;font-weight:600}} input{{width:100%;box-sizing:border-box;padding:10px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg)}}
.row{{display:flex;gap:8px;margin-top:16px}} button{{flex:1;padding:10px;border-radius:6px;border:1px solid var(--line);background:var(--bg);color:var(--fg);font-weight:600}}
button.primary{{background:var(--accent);border-color:var(--accent);color:var(--card)}} .error{{color:#b3261e}}
</style></head><body><main><h1>{html.escape(title)}</h1>{body}</main></body></html>'''
    return HTMLResponse(content, status, headers={'Cache-Control': 'no-store', 'X-Frame-Options': 'DENY', 'Referrer-Policy': 'no-referrer',
                                                   'Content-Security-Policy': "default-src 'none'; style-src 'unsafe-inline'; form-action 'self' https: http://127.0.0.1:* http://localhost:*"})


def _hidden(request_id, csrf):
    return f'<input type="hidden" name="request" value="{html.escape(request_id)}"><input type="hidden" name="csrf" value="{html.escape(csrf)}">'


def _consent_page(row, request_id, csrf, error=None, username=''):
    from urllib.parse import quote, urlparse
    params, client = row['params'], row['info']
    name = html.escape(client.get('client_name') or 'An unnamed client')
    redirect = urlparse(params['redirect_uri'])
    scopes = ''.join(f'<li><code>{html.escape(s)}</code>: {"read your health records" if s == "health.read" else "record new health facts"}</li>'
                     for s in (params.get('scopes') or SCOPES) if s in SCOPES)
    message = f'<p class="error">{html.escape(error)}</p>' if error else ''
    signup = f'/oauth/signup?request={quote(request_id)}&csrf={quote(csrf)}'
    body = f'''<p><strong>{name}</strong> is requesting access to your health archive.</p>
<p>After approval you return to <code>{html.escape(redirect.scheme)}://{html.escape(redirect.netloc)}</code>.</p><ul>{scopes}</ul>{message}
<form method="post" action="/oauth/consent">{_hidden(request_id, csrf)}
<label for="username">Username</label><input id="username" name="username" autocomplete="username" value="{html.escape(username)}" autofocus>
<label for="password">Password</label><input id="password" name="password" type="password" autocomplete="current-password">
<div class="row"><button name="decision" value="deny" type="submit">Deny</button><button class="primary" name="decision" value="approve" type="submit">Sign in and approve</button></div></form>
<p>New here? <a href="{html.escape(signup)}">Create an account with an invitation</a>.</p>'''
    return _page('Connect health archive', body)


def _signup_page(request_id, csrf, error=None, values=None):
    values = values or {}
    field = lambda key, label, kind='text', auto='off': (f'<label for="{key}">{label}</label><input id="{key}" name="{key}" type="{kind}" autocomplete="{auto}" '
                                                        f'value="{html.escape(values.get(key, "") if kind != "password" else "")}">')
    message = f'<p class="error">{html.escape(error)}</p>' if error else ''
    body = f'''<p>Your account gets its own private health archive. Only you can read it.</p>{message}
<form method="post" action="/oauth/signup">{_hidden(request_id, csrf)}
{field('invite', 'Invitation code')}{field('display_name', 'Your name', auto='name')}{field('username', 'Username', auto='username')}
{field('timezone', 'Time zone, such as America/New_York')}{field('password', f'Password, at least {MIN_PASSWORD} characters', 'password', 'new-password')}
{field('confirm', 'Confirm password', 'password', 'new-password')}
<div class="row"><button class="primary" type="submit">Create account and approve</button></div></form>'''
    return _page('Create your health archive', body)
