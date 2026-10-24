"""Recreate the cluster-local read-only backup role after a database disaster restore."""
import json
from pathlib import Path
import subprocess
import sys
from psycopg import sql

DB = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DB / 'tools'))
from common import connect, deployment_record

deployment = deployment_record()
result = subprocess.run(['railway','variable','list','--json','--project',deployment['project_id'],
                         '--environment',deployment['environment_id'],'--service',deployment['backup_service_id']],
                        capture_output=True,text=True)
if result.returncode:
    raise RuntimeError('Unable to retrieve private backup role credentials from Railway.')
variables = json.loads(result.stdout)
if variables.get('PGUSER') != 'health_backup':
    raise RuntimeError('Unexpected backup role identity.')
with connect(admin=True) as conn:
    if not conn.execute("SELECT 1 FROM pg_roles WHERE rolname='health_backup'").fetchone():
        conn.execute(sql.SQL('CREATE ROLE health_backup LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE BYPASSRLS').format(sql.Literal(variables['PGPASSWORD'])))
    conn.execute('GRANT pg_read_all_data TO health_backup')
    conn.execute('ALTER ROLE health_backup SET default_transaction_read_only=on')
    conn.execute("ALTER ROLE health_backup SET statement_timeout='120s'")
print('Read-only backup role configured. Password was not printed.')
