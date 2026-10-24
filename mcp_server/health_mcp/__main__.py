"""Run and administer the health archive MCP server.

    python -m health_mcp serve                      Streamable HTTP with OAuth, for connectors
    python -m health_mcp stdio                      Local stdio as the local profile's account
    python -m health_mcp invite [--days N] [--note TEXT] [--out FILE]
                                                    Create a one-time sign-up invitation
    python -m health_mcp bind-account USERNAME USER_ID < password-hash
                                                    Give an existing user a sign-in account
    python -m health_mcp hash-password < password   Print a scrypt hash for bind-account

Administrative commands use this machine's database administrator profile.
"""
import argparse
import os
from pathlib import Path
import sys
from urllib.parse import urlparse

from health_mcp.app import build


def serve(host, port):
    import uvicorn
    from mcp.server.transport_security import TransportSecuritySettings
    from starlette.responses import HTMLResponse
    from starlette.routing import Route
    from common import connect
    from health_mcp.oauth import HealthOAuthProvider, _page
    public_url = os.environ['HEALTH_MCP_PUBLIC_URL'].rstrip('/')
    if urlparse(public_url).scheme != 'https' and urlparse(public_url).hostname not in ('localhost', '127.0.0.1'):
        raise SystemExit('HEALTH_MCP_PUBLIC_URL must use https outside loopback testing')
    provider = HealthOAuthProvider(lambda: connect(admin=True, vectors=False), public_url)
    provider.migrate()
    server = build(provider, public_url)
    server.custom_route('/oauth/consent', methods=['GET', 'POST'], include_in_schema=False)(provider.consent)
    server.custom_route('/oauth/signup', methods=['GET', 'POST'], include_in_schema=False)(provider.signup)

    async def document_page(request):
        return _page('Health archive document', '<p>Open this document through your connected assistant. The archive is not browsable on the web.</p>')
    server.custom_route('/documents/{document_id}', methods=['GET'], include_in_schema=False)(document_page)

    async def health(request):
        return HTMLResponse('ok')
    server.custom_route('/healthz', methods=['GET'], include_in_schema=False)(health)
    hostname = urlparse(public_url).netloc
    security = TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=[hostname],
                                         allowed_origins=['https://claude.ai', 'https://chatgpt.com', 'https://chat.openai.com', public_url])
    app = server.streamable_http_app(transport_security=security, host=host)
    for index, route in enumerate(app.router.routes):
        if getattr(route, 'path', None) == '/revoke':
            app.router.routes[index] = Route('/revoke', provider.revoke, methods=['POST'])
            break
    else:
        raise RuntimeError('Revocation route not found')
    uvicorn.run(app, host=host, port=port, proxy_headers=True, forwarded_allow_ips='*', log_level='info')


def admin_connection():
    from common import connect
    from health_mcp import accounts
    conn = connect(admin=True, vectors=False)
    accounts.migrate(conn)
    return conn


def main():
    cli = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = cli.add_subparsers(dest='command', required=True)
    s = sub.add_parser('serve')
    s.add_argument('--host', default=os.environ.get('HOST', '0.0.0.0'))
    s.add_argument('--port', type=int, default=int(os.environ.get('PORT', '8080')))
    sub.add_parser('stdio')
    i = sub.add_parser('invite')
    i.add_argument('--days', type=int, default=14)
    i.add_argument('--note')
    i.add_argument('--out', type=Path, help='Write the invitation to this file instead of printing it')
    b = sub.add_parser('bind-account')
    b.add_argument('username')
    b.add_argument('user_id')
    sub.add_parser('hash-password')
    args = cli.parse_args()
    if args.command == 'serve':
        serve(args.host, args.port)
    elif args.command == 'stdio':
        build().run('stdio')
    elif args.command == 'invite':
        from health_mcp import accounts
        with admin_connection() as conn:
            code = accounts.create_invite(conn, args.note, args.days)
        if args.out:
            fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'w') as stream:
                stream.write(code + '\n')
            print(f'Invitation written to {args.out}, valid for {args.days} days')
        else:
            print(code)
    elif args.command == 'bind-account':
        from health_mcp import accounts
        password_hash = sys.stdin.readline().strip()
        if not password_hash.startswith('scrypt$'):
            raise SystemExit('Provide a scrypt hash from hash-password on stdin')
        with admin_connection() as conn:
            accounts.create_account(conn, args.username, password_hash, None, None, user_id=args.user_id)
        print(f'Account {args.username} can now sign in as {args.user_id}')
    else:
        from health_mcp.hashing import hash_passphrase
        password = sys.stdin.readline().rstrip('\n')
        if len(password) < 12:
            raise SystemExit('Use a password of at least 12 characters')
        print(hash_passphrase(password))


if __name__ == '__main__':
    main()
