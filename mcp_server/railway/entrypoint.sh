#!/bin/sh
# Write the database connection profile from service variables, then serve.
set -eu
umask 077
secrets=/app/database/.secrets
mkdir -p "$secrets/railway"
printf '%s\n' "$HEALTH_DB_TLS_CERT_PEM" > "$secrets/railway/server.crt"
python - <<'PY'
import json, os
profile = {role: dict(host=os.environ['HEALTH_DB_HOST'], port=int(os.environ['HEALTH_DB_PORT']), user='health_' + role,
                      password=os.environ[key], sslmode='verify-full', sslrootcert='railway/server.crt')
           for role, key in (('reader', 'HEALTH_READER_PASSWORD'), ('admin', 'HEALTH_ADMIN_PASSWORD'))}
with open('/app/database/.secrets/connection.json', 'w') as f:
    json.dump({'profiles': {'railway': profile}, 'default': 'railway'}, f)
PY
exec python -m health_mcp serve
