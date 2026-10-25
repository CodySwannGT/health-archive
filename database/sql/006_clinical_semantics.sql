-- Clinical types, concepts, relation rules and current-edge tracking. Additive and idempotent.
SET search_path = health, ag_catalog, public;

ALTER TABLE health.relationships ADD COLUMN IF NOT EXISTS qualifiers jsonb NOT NULL DEFAULT '{}';
ALTER TABLE health.relationships ADD COLUMN IF NOT EXISTS last_seen_run bigint;
-- Edges the latest projection produced. Edges it no longer produces stay as history.
ALTER TABLE health.relationships ADD COLUMN IF NOT EXISTS is_current boolean NOT NULL DEFAULT true;
CREATE INDEX IF NOT EXISTS relationships_current ON health.relationships(patient_id, relation) WHERE is_current;

ALTER TABLE health.resources ADD COLUMN IF NOT EXISTS clinical_type text;
ALTER TABLE health.resources ADD COLUMN IF NOT EXISTS concept_id text;
ALTER TABLE health.resources ADD COLUMN IF NOT EXISTS concept_basis text;
CREATE INDEX IF NOT EXISTS resources_concept ON health.resources(patient_id, concept_id) WHERE is_current;
CREATE INDEX IF NOT EXISTS resources_clinical_type ON health.resources(patient_id, clinical_type) WHERE is_current;

ALTER TABLE health.ontology_categories ADD COLUMN IF NOT EXISTS subject_area text;
ALTER TABLE health.ontology_categories ADD COLUMN IF NOT EXISTS clinical_type text;
ALTER TABLE health.ontology_categories ADD COLUMN IF NOT EXISTS definition jsonb;
ALTER TABLE health.ontology_relations ADD COLUMN IF NOT EXISTS status text NOT NULL DEFAULT 'active';
ALTER TABLE health.ontology_relations ADD COLUMN IF NOT EXISTS definition jsonb;

CREATE TABLE IF NOT EXISTS health.clinical_types (
  patient_id text NOT NULL REFERENCES health.patients,
  clinical_type text NOT NULL,
  label text NOT NULL,
  parent text,
  fhir_resource text,
  concept_family text,
  description text NOT NULL,
  definition_sha256 text NOT NULL,
  updated_run bigint REFERENCES health.ingestion_runs,
  PRIMARY KEY (patient_id, clinical_type)
);
CREATE TABLE IF NOT EXISTS health.concepts (
  patient_id text NOT NULL REFERENCES health.patients,
  concept_id text NOT NULL,
  label text NOT NULL,
  family text,
  clinical_type text NOT NULL,
  -- curated_registry, source_code or source_name: how the concept's identity was established.
  basis text NOT NULL,
  codings jsonb NOT NULL DEFAULT '[]',
  note text,
  member_count integer NOT NULL DEFAULT 0,
  updated_run bigint REFERENCES health.ingestion_runs,
  PRIMARY KEY (patient_id, concept_id)
);

DO $$ DECLARE t text; BEGIN
  FOREACH t IN ARRAY ARRAY['clinical_types','concepts'] LOOP
    EXECUTE format('ALTER TABLE health.%I ENABLE ROW LEVEL SECURITY', t);
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname='health' AND tablename=t AND policyname='patient_access') THEN
      EXECUTE format('CREATE POLICY patient_access ON health.%I FOR SELECT USING (health.can_access(patient_id))', t);
    END IF;
  END LOOP;
END $$;
GRANT SELECT ON health.clinical_types, health.concepts TO health_reader;

-- The views expand x.*, so they are rebuilt to include the new resource columns.
DROP VIEW IF EXISTS health.observations;
DROP VIEW IF EXISTS health.medications;
CREATE VIEW health.observations WITH (security_invoker = true) AS
SELECT x.*, r.source, r.source_record_id, r.source_path FROM health.resources x
JOIN health.records r USING (record_id) WHERE resource_type = 'Observation' AND x.is_current;
CREATE VIEW health.medications WITH (security_invoker = true) AS
SELECT x.*, r.source, r.source_record_id, r.source_path FROM health.resources x
JOIN health.records r USING (record_id)
WHERE resource_type IN ('MedicationStatement', 'MedicationRequest', 'MedicationAdministration') AND x.is_current;
GRANT SELECT ON health.observations, health.medications TO health_reader;

-- Graph neighbors stay within the root's patient partition. Concepts, clinical
-- types and ontology categories can be roots, and edges carry their qualifiers.
CREATE OR REPLACE FUNCTION health.graph_neighbors(p_root text, p_limit integer DEFAULT 40)
RETURNS SETOF jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, health, ag_catalog, public AS $$
DECLARE p_patient text; query text; delimiter text;
BEGIN
  SELECT patient_id INTO p_patient FROM health.resources WHERE resource_id=p_root;
  IF p_patient IS NULL THEN
    SELECT patient_id INTO p_patient FROM health.documents WHERE document_id=p_root;
  END IF;
  IF p_patient IS NULL THEN
    SELECT patient_id INTO p_patient FROM health.patients WHERE patient_id=p_root;
  END IF;
  IF p_patient IS NULL AND (p_root LIKE 'concept:%' OR p_root LIKE 'type:%' OR p_root LIKE 'ontology:%') THEN
    p_patient := current_setting('health.patient_id', true);
  END IF;
  IF p_patient IS NULL OR p_patient = '' OR NOT health.can_access(p_patient) THEN
    RAISE EXCEPTION 'Unknown or unauthorized graph root';
  END IF;
  IF p_limit<1 OR p_limit>1000 THEN RAISE EXCEPTION 'Invalid result limit'; END IF;
  -- An explicit correction must remain navigable to its preserved original.
  query := format('MATCH (n:Entity {uid:%s})-[r:RELATED]-(m:Entity) WHERE n.patient_id=%s AND m.patient_id=%s AND (m.is_current IS NULL OR m.is_current=true OR r.relation="SUPERSEDES_MEASUREMENT") RETURN {id:m.uid,name:m.name,kind:m.kind,is_current:m.is_current,relation:r.relation,evidence_kind:r.evidence_kind,qualifiers:r.qualifiers,direction:CASE WHEN startNode(r)=n THEN "out" ELSE "in" END} LIMIT %s',
                  to_json(p_root)::text,to_json(p_patient)::text,to_json(p_patient)::text,p_limit);
  delimiter := '$q_' || md5(query) || '$';
  WHILE strpos(query,delimiter)>0 LOOP delimiter := replace(delimiter,'$','') || 'x'; delimiter := '$' || delimiter || '$'; END LOOP;
  RETURN QUERY EXECUTE format('SELECT v::text::jsonb FROM ag_catalog.cypher(''health_graph'',%s%s%s) AS (v ag_catalog.agtype)',delimiter,query,delimiter);
END $$;
REVOKE ALL ON FUNCTION health.graph_neighbors(text,integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION health.graph_neighbors(text,integer) TO health_reader;
