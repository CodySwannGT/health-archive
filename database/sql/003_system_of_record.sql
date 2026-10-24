-- The database is the system of record. Ontology definitions are queryable
-- data, and archive files leave the current archive only by explicit retirement.
SET search_path = health, ag_catalog, public;

ALTER TABLE health.artifacts ADD COLUMN IF NOT EXISTS retired_run bigint REFERENCES health.ingestion_runs;
ALTER TABLE health.artifacts ADD COLUMN IF NOT EXISTS retired_reason text;
ALTER TABLE health.ingestion_runs ADD COLUMN IF NOT EXISTS operation text;

CREATE TABLE IF NOT EXISTS health.ontology_categories (
  category text PRIMARY KEY,
  domain text NOT NULL,
  description text NOT NULL,
  schema_path text,
  json_schema jsonb,
  definition_sha256 text NOT NULL,
  updated_run bigint REFERENCES health.ingestion_runs
);
CREATE TABLE IF NOT EXISTS health.ontology_relations (
  relation text PRIMARY KEY,
  semantics text NOT NULL,
  description text NOT NULL,
  establishes_causality boolean NOT NULL,
  definition_sha256 text NOT NULL,
  updated_run bigint REFERENCES health.ingestion_runs
);

GRANT SELECT ON health.ontology_categories, health.ontology_relations TO health_reader;
