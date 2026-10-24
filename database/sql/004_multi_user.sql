-- Multiple users and patients. Each patient's archive is separate: archive
-- paths, file versions and ontology definitions are keyed by patient.
-- Idempotent. Existing rows already carry their patient.
SET search_path = health, ag_catalog, public;

DO $$ BEGIN
  IF (SELECT array_length(conkey,1) FROM pg_constraint WHERE conname='artifacts_pkey' AND conrelid='health.artifacts'::regclass)=1 THEN
    ALTER TABLE health.records DROP CONSTRAINT IF EXISTS records_source_path_fkey;
    ALTER TABLE health.documents DROP CONSTRAINT IF EXISTS documents_source_path_fkey;
    -- record_versions has no patient column; its record's patient scopes the path.
    ALTER TABLE health.record_versions DROP CONSTRAINT IF EXISTS record_versions_source_path_fkey;
    ALTER TABLE health.artifacts DROP CONSTRAINT artifacts_pkey;
    ALTER TABLE health.artifacts ADD CONSTRAINT artifacts_pkey PRIMARY KEY (patient_id, path);
    ALTER TABLE health.records ADD CONSTRAINT records_source_artifact_fkey
      FOREIGN KEY (patient_id, source_path) REFERENCES health.artifacts(patient_id, path);
    ALTER TABLE health.documents ADD CONSTRAINT documents_source_artifact_fkey
      FOREIGN KEY (patient_id, source_path) REFERENCES health.artifacts(patient_id, path);
  END IF;
  IF (SELECT array_length(conkey,1) FROM pg_constraint WHERE conname='artifact_versions_pkey' AND conrelid='health.artifact_versions'::regclass)=2 THEN
    ALTER TABLE health.artifact_versions DROP CONSTRAINT artifact_versions_pkey;
    ALTER TABLE health.artifact_versions ADD CONSTRAINT artifact_versions_pkey PRIMARY KEY (patient_id, path, sha256);
  END IF;
END $$;

ALTER TABLE health.ingestion_runs ADD COLUMN IF NOT EXISTS patient_id text REFERENCES health.patients;
ALTER TABLE health.patients ADD COLUMN IF NOT EXISTS created_at timestamptz NOT NULL DEFAULT now();

DO $$ DECLARE t text; key text; BEGIN
  FOREACH t IN ARRAY ARRAY['ontology_categories','ontology_relations'] LOOP
    key := CASE t WHEN 'ontology_categories' THEN 'category' ELSE 'relation' END;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='health' AND table_name=t AND column_name='patient_id') THEN
      IF (SELECT count(*) FROM health.patients)>1 AND EXISTS (SELECT 1 FROM pg_class WHERE oid=('health.'||t)::regclass AND reltuples>0) THEN
        RAISE EXCEPTION 'Assign existing ontology rows to their patient before migrating %', t;
      END IF;
      EXECUTE format('ALTER TABLE health.%I ADD COLUMN patient_id text REFERENCES health.patients', t);
      EXECUTE format('UPDATE health.%I SET patient_id=(SELECT patient_id FROM health.patients LIMIT 1)', t);
      EXECUTE format('ALTER TABLE health.%I ALTER COLUMN patient_id SET NOT NULL', t);
      EXECUTE format('ALTER TABLE health.%I DROP CONSTRAINT %I', t, t || '_pkey');
      EXECUTE format('ALTER TABLE health.%I ADD CONSTRAINT %I PRIMARY KEY (patient_id, %I)', t, t || '_pkey', key);
    END IF;
    EXECUTE format('ALTER TABLE health.%I ENABLE ROW LEVEL SECURITY', t);
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname='health' AND tablename=t AND policyname='patient_access') THEN
      EXECUTE format('CREATE POLICY patient_access ON health.%I FOR SELECT USING (health.can_access(patient_id))', t);
    END IF;
  END LOOP;
END $$;

-- Graph neighbors stay within the root's patient partition.
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
  IF p_patient IS NULL OR NOT health.can_access(p_patient) THEN
    RAISE EXCEPTION 'Unknown or unauthorized graph root';
  END IF;
  IF p_limit<1 OR p_limit>1000 THEN RAISE EXCEPTION 'Invalid result limit'; END IF;
  -- An explicit correction must remain navigable to its preserved original.
  query := format('MATCH (n:Entity {uid:%s})-[r:RELATED]-(m:Entity) WHERE n.patient_id=%s AND m.patient_id=%s AND (m.is_current IS NULL OR m.is_current=true OR r.relation="SUPERSEDES_MEASUREMENT") RETURN {id:m.uid,name:m.name,kind:m.kind,is_current:m.is_current,relation:r.relation,evidence_kind:r.evidence_kind} LIMIT %s',
                  to_json(p_root)::text,to_json(p_patient)::text,to_json(p_patient)::text,p_limit);
  delimiter := '$q_' || md5(query) || '$';
  WHILE strpos(query,delimiter)>0 LOOP delimiter := replace(delimiter,'$','') || 'x'; delimiter := '$' || delimiter || '$'; END LOOP;
  RETURN QUERY EXECUTE format('SELECT v::text::jsonb FROM ag_catalog.cypher(''health_graph'',%s%s%s) AS (v ag_catalog.agtype)',delimiter,query,delimiter);
END $$;
REVOKE ALL ON FUNCTION health.graph_neighbors(text,integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION health.graph_neighbors(text,integer) TO health_reader;
