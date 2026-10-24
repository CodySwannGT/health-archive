# Railway deployment

Scripts for running the health database on Railway: PostgreSQL 16 with Apache AGE and pgvector, verified TLS, and encrypted daily logical backups to a private bucket.

| File | Purpose |
| --- | --- |
| `../Dockerfile` (`railway` stage), `entrypoint.sh`, `pg_hba.conf` | Database service image: TLS required, plaintext rejected |
| `backup.sh` | Backup worker: a custom-format dump encrypted with an age public recipient, uploaded with its checksum |
| `prepare-context.py` | Builds a code-only upload context for the database or backup service |
| `configure-remote.py` | Recovers connection settings through your authenticated Railway CLI and pins the server certificate |
| `bootstrap-backup-role.py` | Creates the read-only backup role after a restore |
| `verify-backup.py`, `verify-container.py` | Restore a real backup, or test a container candidate, and compare contents |
| `backup-config.py` | Encrypts your personal configuration folder into the backup bucket, and restores it with only `railway login` |

## Your deployment record

Deployment-specific values live outside the code, in `~/.config/health-archive/deployment.json` (override the folder with `HEALTH_HOME`):
- `project_id`, `environment_id`, `database_service_id` and `backup_service_id`;
- `tcp_host` and `tcp_port`;
- `tls_certificate_sha256`;
- `status: "verified"` once you have migrated and restored a backup successfully.

Connection settings and keys are written to `~/.config/health-archive/secrets/` with owner-only permissions. Never commit either file.

## Back up your configuration

Run `python3 database/railway/backup-config.py` after changing anything in `~/.config/health-archive/`. Restore on a new machine into an empty folder:

```sh
railway login && railway link
python3 database/railway/backup-config.py --list
python3 database/railway/backup-config.py --restore config/health-archive-config-TIMESTAMP.tar.gz.age
```

If backup uploads fail with `SignatureDoesNotMatch`, the bucket's credentials have changed. Compare `railway bucket credentials` with the backup service variables and update them.

## Recover access on a new machine

```sh
railway login
python3 database/railway/configure-remote.py --activate
./database/setup-remote.sh
./database/healthdb status
```
