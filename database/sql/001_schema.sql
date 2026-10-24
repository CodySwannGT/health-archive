CREATE EXTENSION IF NOT EXISTS age;
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
LOAD 'age';
SET search_path = health, ag_catalog, public;
CREATE SCHEMA IF NOT EXISTS health;

CREATE TABLE IF NOT EXISTS health.users (
  user_id text PRIMARY KEY,
  display_name text NOT NULL,
  account_type text NOT NULL
);
CREATE TABLE IF NOT EXISTS health.patients (
  patient_id text PRIMARY KEY,
  display_name text NOT NULL,
  profile jsonb NOT NULL
);
CREATE TABLE IF NOT EXISTS health.patient_access (
  user_id text REFERENCES health.users,
  patient_id text REFERENCES health.patients,
  role text NOT NULL CHECK (role IN ('owner', 'reader')),
  PRIMARY KEY (user_id, patient_id)
);
CREATE TABLE IF NOT EXISTS health.ingestion_runs (
  run_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  started_at timestamptz NOT NULL DEFAULT now(),
  completed_at timestamptz,
  parser_version text NOT NULL,
  manifest_sha256 text NOT NULL,
  summary jsonb
);
CREATE TABLE IF NOT EXISTS health.artifact_contents (
  sha256 text PRIMARY KEY CHECK (length(sha256) = 64),
  bytes bytea NOT NULL
);
CREATE TABLE IF NOT EXISTS health.artifacts (
  path text PRIMARY KEY,
  patient_id text NOT NULL REFERENCES health.patients,
  sha256 text NOT NULL REFERENCES health.artifact_contents,
  size_bytes bigint NOT NULL,
  source text NOT NULL,
  evidence_kind text NOT NULL,
  media_type text NOT NULL,
  last_seen_run bigint REFERENCES health.ingestion_runs
);
CREATE TABLE IF NOT EXISTS health.artifact_versions (
  path text NOT NULL,
  sha256 text NOT NULL REFERENCES health.artifact_contents,
  patient_id text NOT NULL REFERENCES health.patients,
  first_seen_run bigint REFERENCES health.ingestion_runs,
  PRIMARY KEY (path, sha256)
);
CREATE TABLE IF NOT EXISTS health.records (
  record_id text PRIMARY KEY,
  patient_id text NOT NULL REFERENCES health.patients,
  source text NOT NULL,
  source_kind text NOT NULL,
  source_record_id text NOT NULL,
  current_version_sha256 text NOT NULL,
  source_path text NOT NULL REFERENCES health.artifacts,
  first_seen_run bigint REFERENCES health.ingestion_runs,
  last_seen_run bigint REFERENCES health.ingestion_runs,
  UNIQUE (patient_id, source, source_kind, source_record_id)
);
CREATE TABLE IF NOT EXISTS health.record_versions (
  record_id text NOT NULL REFERENCES health.records,
  version_sha256 text NOT NULL,
  payload jsonb NOT NULL,
  source_provenance jsonb NOT NULL,
  source_path text NOT NULL REFERENCES health.artifacts,
  first_seen_run bigint REFERENCES health.ingestion_runs,
  PRIMARY KEY (record_id, version_sha256)
);
CREATE TABLE IF NOT EXISTS health.resources (
  resource_id text PRIMARY KEY,
  record_id text NOT NULL REFERENCES health.records,
  patient_id text NOT NULL REFERENCES health.patients,
  resource_type text NOT NULL,
  category text NOT NULL,
  name text NOT NULL,
  clinical_date date,
  clinical_date_role text NOT NULL,
  recorded_at timestamptz,
  assertion text NOT NULL,
  evidence_kind text NOT NULL,
  status text,
  numeric_value double precision,
  comparator text,
  value_text text,
  unit_source text,
  unit_ucum text,
  reference_text text,
  flag_source text,
  code_system text,
  code text,
  normalized jsonb NOT NULL,
  provenance jsonb NOT NULL,
  projection_sha256 text NOT NULL
);
CREATE TABLE IF NOT EXISTS health.source_record_aliases (
  patient_id text NOT NULL REFERENCES health.patients,
  source text NOT NULL,
  source_kind text NOT NULL,
  stable_key text NOT NULL,
  record_id text NOT NULL REFERENCES health.records,
  PRIMARY KEY (patient_id,source,source_kind,stable_key)
);
ALTER TABLE health.resources ADD COLUMN IF NOT EXISTS is_current boolean NOT NULL DEFAULT true;
CREATE INDEX IF NOT EXISTS resource_patient_date ON health.resources(patient_id, clinical_date);
CREATE INDEX IF NOT EXISTS resource_name_search ON health.resources USING gin(name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS resource_type_source ON health.resources(patient_id, resource_type, category);
CREATE TABLE IF NOT EXISTS health.resource_versions (
  resource_id text NOT NULL REFERENCES health.resources,
  projection_sha256 text NOT NULL,
  normalized jsonb NOT NULL,
  provenance jsonb NOT NULL,
  first_seen_run bigint REFERENCES health.ingestion_runs,
  PRIMARY KEY (resource_id, projection_sha256)
);
CREATE TABLE IF NOT EXISTS health.relationships (
  relationship_id text PRIMARY KEY,
  patient_id text NOT NULL REFERENCES health.patients,
  from_id text NOT NULL,
  to_id text NOT NULL,
  relation text NOT NULL,
  evidence_kind text NOT NULL,
  provenance jsonb NOT NULL
);
CREATE INDEX IF NOT EXISTS relationships_endpoints ON health.relationships(patient_id, from_id, to_id);
CREATE TABLE IF NOT EXISTS health.documents (
  document_id text PRIMARY KEY,
  patient_id text NOT NULL REFERENCES health.patients,
  source text NOT NULL,
  title text NOT NULL,
  source_path text NOT NULL REFERENCES health.artifacts,
  record_id text REFERENCES health.records,
  clinical_date date,
  evidence_kind text NOT NULL,
  version_sha256 text NOT NULL,
  text_content text NOT NULL,
  provenance jsonb NOT NULL,
  is_current boolean NOT NULL DEFAULT true
);
CREATE TABLE IF NOT EXISTS health.chunks (
  chunk_id text PRIMARY KEY,
  document_id text NOT NULL REFERENCES health.documents,
  patient_id text NOT NULL REFERENCES health.patients,
  ordinal integer NOT NULL,
  start_char integer NOT NULL,
  end_char integer NOT NULL,
  text_content text NOT NULL,
  search_vector tsvector GENERATED ALWAYS AS (to_tsvector('english', text_content)) STORED,
  embedding vector(384),
  embedding_model text,
  UNIQUE (document_id, ordinal)
);
CREATE INDEX IF NOT EXISTS chunks_fts ON health.chunks USING gin(search_vector);
CREATE TABLE IF NOT EXISTS health.embedding_models (
  model_id text PRIMARY KEY,
  model_name text NOT NULL,
  dimensions integer NOT NULL,
  runtime text NOT NULL,
  files jsonb NOT NULL,
  config jsonb NOT NULL
);
CREATE TABLE IF NOT EXISTS health.import_issues (
  issue_id text PRIMARY KEY,
  patient_id text NOT NULL REFERENCES health.patients,
  source_path text NOT NULL,
  source_record_id text,
  severity text NOT NULL,
  reason text NOT NULL,
  details jsonb NOT NULL
);
CREATE OR REPLACE VIEW health.current_records WITH (security_invoker = true) AS
SELECT r.*, v.payload, v.source_provenance FROM health.records r
JOIN health.record_versions v ON v.record_id = r.record_id AND v.version_sha256 = r.current_version_sha256;
DROP VIEW IF EXISTS health.observations;
DROP VIEW IF EXISTS health.medications;
CREATE VIEW health.observations WITH (security_invoker = true) AS
SELECT x.*, r.source, r.source_record_id, r.source_path FROM health.resources x
JOIN health.records r USING (record_id) WHERE resource_type = 'Observation' AND x.is_current;
CREATE VIEW health.medications WITH (security_invoker = true) AS
SELECT x.*, r.source, r.source_record_id, r.source_path FROM health.resources x
JOIN health.records r USING (record_id)
WHERE resource_type IN ('MedicationStatement', 'MedicationRequest', 'MedicationAdministration') AND x.is_current;

CREATE OR REPLACE FUNCTION health.can_access(p_patient text) RETURNS boolean
LANGUAGE sql STABLE AS $$
  SELECT EXISTS (SELECT 1 FROM health.patient_access
    WHERE patient_id = p_patient AND user_id = current_setting('health.user_id', true))
$$;
DO $$ DECLARE t text; BEGIN
  FOREACH t IN ARRAY ARRAY['patients','artifacts','artifact_versions','records','resources',
    'relationships','documents','chunks','import_issues','source_record_aliases'] LOOP
    EXECUTE format('ALTER TABLE health.%I ENABLE ROW LEVEL SECURITY', t);
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname='health' AND tablename=t AND policyname='patient_access') THEN
      EXECUTE format('CREATE POLICY patient_access ON health.%I FOR SELECT USING (health.can_access(patient_id))', t);
    END IF;
  END LOOP;
END $$;
ALTER TABLE health.users ENABLE ROW LEVEL SECURITY;
ALTER TABLE health.patient_access ENABLE ROW LEVEL SECURITY;
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname='health' AND tablename='users' AND policyname='self') THEN
    CREATE POLICY self ON health.users FOR SELECT USING (user_id = current_setting('health.user_id', true));
    CREATE POLICY self ON health.patient_access FOR SELECT USING (user_id = current_setting('health.user_id', true));
  END IF;
END $$;
ALTER TABLE health.record_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE health.resource_versions ENABLE ROW LEVEL SECURITY;
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname='health' AND tablename='record_versions' AND policyname='parent_access') THEN
    CREATE POLICY parent_access ON health.record_versions FOR SELECT USING (
      EXISTS (SELECT 1 FROM health.records r WHERE r.record_id = record_versions.record_id));
    CREATE POLICY parent_access ON health.resource_versions FOR SELECT USING (
      EXISTS (SELECT 1 FROM health.resources r WHERE r.resource_id = resource_versions.resource_id));
  END IF;
END $$;
