#!/bin/sh
set -eu
umask 077
if [ "${HEALTH_SERVICE_MODE:-database}" = backup ]; then
  exec /usr/local/bin/health-cloud-backup
fi
: "${POSTGRES_PASSWORD:?Database password is required}"
: "${TLS_CERT_PEM:?Server certificate is required}"
: "${TLS_KEY_PEM:?Server private key is required}"
mkdir -p /run/health-tls
printf '%s\n' "$TLS_CERT_PEM" > /run/health-tls/server.crt
printf '%s\n' "$TLS_KEY_PEM" > /run/health-tls/server.key
chown -R postgres:postgres /run/health-tls
unset TLS_KEY_PEM TLS_CERT_PEM
export PGDATA=${PGDATA:-/data/pgdata}
export POSTGRES_INITDB_ARGS='--auth-host=scram-sha-256'
exec /usr/local/bin/docker-entrypoint.sh postgres \
  -c session_preload_libraries=age \
  -c ssl=on -c ssl_cert_file=/run/health-tls/server.crt -c ssl_key_file=/run/health-tls/server.key \
  -c hba_file=/etc/health-pg_hba.conf \
  -c max_connections=30 -c shared_buffers=32MB -c work_mem=4MB
