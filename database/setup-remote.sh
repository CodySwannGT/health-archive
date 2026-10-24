#!/bin/sh
# Install query/ingestion tools on another machine without creating a local PG copy.
set -eu
db_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$db_dir/.."
health_home=${HEALTH_HOME:-$HOME/.config/health-archive}
if [ ! -f "$health_home/secrets/connection.json" ] && [ ! -f "$db_dir/.secrets/connection.json" ]; then
  echo 'Install the private connection.json and pinned certificate from your secure credential backup first.' >&2
  exit 2
fi
health_python=${HEALTH_PYTHON:-python3.13}
if [ ! -x "$db_dir/.venv/bin/python" ]; then
  "$health_python" -m venv "$db_dir/.venv"
fi
"$db_dir/.venv/bin/pip" install -r "$db_dir/requirements.lock"
"$db_dir/.venv/bin/python" - <<'PY'
import sys
sys.path.insert(0,'database/tools')
from common import connection_settings, connect
if connection_settings().get('sslmode') != 'verify-full':
    raise RuntimeError('setup-remote requires a selected remote profile.')
with connect() as conn:
    assert conn.execute('SELECT ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid()').fetchone()['ssl']
from fastembed import TextEmbedding
model=TextEmbedding('BAAI/bge-small-en-v1.5',cache_dir='database/models',threads=2)
assert len(next(model.query_embed('model installation check')))==384
PY
"$db_dir/healthdb" status
