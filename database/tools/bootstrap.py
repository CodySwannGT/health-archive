from psycopg import sql
from common import connect, connection_settings, DB, Jsonb, local_profile


def bootstrap(database='health'):
    profile = local_profile()
    with connect(admin=True, vectors=False, database=database) as conn:
        conn.execute((DB / 'sql/001_schema.sql').read_text(), prepare=False)
        for row in profile['users']:
            conn.execute('INSERT INTO health.users VALUES (%s,%s,%s) ON CONFLICT (user_id) DO UPDATE SET display_name=EXCLUDED.display_name',
                         (row['user_id'], row['display_name'], row['type']))
        for row in profile['patients']:
            conn.execute('INSERT INTO health.patients VALUES (%s,%s,%s) ON CONFLICT (patient_id) DO UPDATE SET profile=EXCLUDED.profile',
                         (row['patient_id'], row['display_name'], Jsonb(row)))
        for row in profile['access']:
            conn.execute('INSERT INTO health.patient_access VALUES (%s,%s,%s) ON CONFLICT (user_id,patient_id) DO UPDATE SET role=EXCLUDED.role',
                         (row['user_id'], row['patient_id'], row['role']))
        if not conn.execute("SELECT 1 FROM pg_roles WHERE rolname='health_reader'").fetchone():
            password = connection_settings(admin=False, database=database)['password']
            conn.execute(sql.SQL('CREATE ROLE health_reader LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS').format(sql.Literal(password)))
        conn.execute('GRANT USAGE ON SCHEMA health, ag_catalog TO health_reader')
        conn.execute('GRANT SELECT ON health.users, health.patients, health.patient_access, health.artifacts, health.artifact_versions, health.records, health.record_versions, health.resources, health.resource_versions, health.relationships, health.documents, health.chunks, health.embedding_models, health.import_issues, health.current_records, health.observations, health.medications TO health_reader')
        conn.execute('GRANT SELECT ON health.source_record_aliases TO health_reader')
        conn.execute('ALTER ROLE health_reader SET default_transaction_read_only=on')
        conn.execute('ALTER ROLE health_reader SET statement_timeout=\'30s\'')
        conn.execute('REVOKE CREATE ON SCHEMA public FROM PUBLIC')
        conn.execute('REVOKE ALL ON health.artifact_contents, health.ingestion_runs FROM health_reader')
        conn.execute((DB / 'sql/002_graph_tools.sql').read_text(), prepare=False)
        for name in ('003_system_of_record.sql', '004_multi_user.sql', '005_source_history.sql'):
            conn.execute((DB / 'sql' / name).read_text(), prepare=False)
        if conn.execute("SELECT 1 FROM pg_namespace WHERE nspname='health_graph'").fetchone():
            conn.execute('REVOKE ALL ON ALL TABLES IN SCHEMA health_graph FROM health_reader')
            conn.execute('REVOKE ALL ON SCHEMA health_graph FROM health_reader')
    return {'status': 'initialized'}


if __name__ == '__main__':
    from common import json_out
    json_out(bootstrap())
