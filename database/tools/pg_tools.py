"""Matching PG16 clients for both local and verified-TLS remote operations."""
import os
from pathlib import Path
import subprocess
import tempfile

from common import connection_settings

PG16_IMAGE = 'postgres:16-bookworm@sha256:efedf3595f1d6f415c08568ba171029bf54052e754cc9f030e3f2412b21f3d67'


def run_pg_tool(tool, arguments, *, database='health', stdin=None, stdout=None):
    settings = connection_settings(admin=True, database=database)
    if settings.get('sslmode') != 'verify-full':
        return subprocess.run(['docker', 'exec', '-i', 'codex-health-db', tool,
                               '-U', settings['user'], '-d', database, *arguments],
                              stdin=stdin, stdout=stdout, check=True)
    # A private environment file avoids passwords in process arguments and logs.
    with tempfile.TemporaryDirectory(prefix='health-pg-client-') as folder:
        env_file = Path(folder) / 'pg.env'
        values = {'PGHOST': settings['host'], 'PGPORT': str(settings['port']),
                  'PGDATABASE': database, 'PGUSER': settings['user'],
                  'PGPASSWORD': settings['password'], 'PGSSLMODE': 'verify-full',
                  'PGSSLROOTCERT': '/run/health-ca.crt', 'PGCONNECT_TIMEOUT': '15'}
        if settings.get('hostaddr'):
            values['PGHOSTADDR'] = settings['hostaddr']
        if any('\n' in value or '\r' in value for value in values.values()):
            raise ValueError('Invalid newline in database connection settings.')
        env_file.write_text(''.join(f'{key}={value}\n' for key, value in values.items()))
        os.chmod(env_file, 0o600)
        return subprocess.run(['docker', 'run', '--rm', '-i', '--env-file', str(env_file),
                               '--mount', f"type=bind,src={settings['sslrootcert']},dst=/run/health-ca.crt,readonly",
                               PG16_IMAGE, tool, '-d', database, *arguments], stdin=stdin, stdout=stdout, check=True)
