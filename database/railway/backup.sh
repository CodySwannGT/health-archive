#!/bin/sh
# One complete logical backup per cron invocation. Only encrypted bytes leave PG.
set -eu
umask 077
: "${BACKUP_RECIPIENT:?age encryption recipient is required}"
: "${BACKUP_BUCKET:?Backup bucket is required}"
: "${BACKUP_ENDPOINT:?S3 endpoint is required}"
: "${PGSSLROOTCERT_PEM:?Pinned database certificate is required}"
export PGSSLMODE=verify-full PGCONNECT_TIMEOUT=30
backup_dir=$(mktemp -d)
trap 'rm -rf "$backup_dir"' EXIT HUP INT TERM
export PGSSLROOTCERT=$backup_dir/ca.crt
printf '%s\n' "$PGSSLROOTCERT_PEM" > "$PGSSLROOTCERT"
backup_key=health/$(date -u +%Y/%m/health_%Y%m%dT%H%M%SZ.dump.age)
pg_dump -Fc --file "$backup_dir/health.dump"
age --recipient "$BACKUP_RECIPIENT" --output "$backup_dir/health.dump.age" "$backup_dir/health.dump"
sha256sum "$backup_dir/health.dump.age" | cut -d ' ' -f 1 > "$backup_dir/sha256"
aws --endpoint-url "$BACKUP_ENDPOINT" s3 cp "$backup_dir/health.dump.age" "s3://$BACKUP_BUCKET/$backup_key" --only-show-errors
aws --endpoint-url "$BACKUP_ENDPOINT" s3 cp "$backup_dir/sha256" "s3://$BACKUP_BUCKET/$backup_key.sha256" --only-show-errors
printf '{"status":"backup_uploaded","key":"%s","format":"PG16 custom dump encrypted with age"}\n' "$backup_key"
