#!/bin/sh
set -eu
export HEALTH_DB_TARGET=local
db_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$db_dir/.."
snapshot_path=
if [ "$#" -gt 0 ]; then
  if [ "$#" -ne 2 ] || [ "$1" != '--snapshot' ]; then
    echo 'Usage: database/setup.sh [--snapshot PATH_TO_DUMP]' >&2
    exit 2
  fi
  snapshot_path=$2
  if [ ! -f "$snapshot_path" ]; then
    echo 'Snapshot file does not exist.' >&2
    exit 2
  fi
fi
health_python=${HEALTH_PYTHON:-python3.13}
if [ ! -x "$db_dir/.venv/bin/python" ]; then
  "$health_python" -m venv "$db_dir/.venv"
fi
"$db_dir/.venv/bin/pip" install -r "$db_dir/requirements.lock"
mkdir -p "$db_dir/.secrets"
chmod 700 "$db_dir/.secrets"
"$db_dir/.venv/bin/python" - <<'PY'
import os,secrets
from pathlib import Path
for name in ('admin-password','reader-password'):
    p=Path('database/.secrets')/name
    if not p.exists():
        fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'w') as f:f.write(secrets.token_urlsafe(36)+'\n')
PY
docker compose -f "$db_dir/compose.yaml" up -d --build --wait --wait-timeout 120
"$db_dir/.venv/bin/python" - <<'PY'
from fastembed import TextEmbedding
m=TextEmbedding('BAAI/bge-small-en-v1.5',cache_dir='database/models',threads=2)
assert len(next(m.query_embed('model installation check')))==384
PY
if [ -n "$snapshot_path" ]; then
  "$db_dir/.venv/bin/python" "$db_dir/tools/restore_snapshot.py" "$snapshot_path"
else
  "$db_dir/healthdb" bootstrap
  "$db_dir/healthdb" ingest
fi
"$db_dir/healthdb" verify
