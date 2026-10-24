-- AGE 1.6 is accessed through scoped tools. The reader cannot query raw graph tables.
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
  query := format('MATCH (n:Entity {uid:%s})-[r:RELATED]-(m:Entity) WHERE m.is_current IS NULL OR m.is_current=true OR r.relation="SUPERSEDES_MEASUREMENT" RETURN {id:m.uid,name:m.name,kind:m.kind,is_current:m.is_current,relation:r.relation,evidence_kind:r.evidence_kind} LIMIT %s',to_json(p_root)::text,p_limit);
  delimiter := '$q_' || md5(query) || '$';
  WHILE strpos(query,delimiter)>0 LOOP delimiter := replace(delimiter,'$','') || 'x'; delimiter := '$' || delimiter || '$'; END LOOP;
  RETURN QUERY EXECUTE format('SELECT v::text::jsonb FROM ag_catalog.cypher(''health_graph'',%s%s%s) AS (v ag_catalog.agtype)',delimiter,query,delimiter);
END $$;
CREATE OR REPLACE FUNCTION health.graph_counts(p_patient text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, health, ag_catalog, public AS $$
DECLARE vertices bigint; edges bigint; query text; delimiter text;
BEGIN
  IF NOT health.can_access(p_patient) THEN RAISE EXCEPTION 'Unauthorized graph context'; END IF;
  query := format('MATCH (n:Entity) WHERE n.patient_id=%s RETURN count(n)',to_json(p_patient)::text);
  delimiter := '$q_' || md5(query) || '$';
  EXECUTE format('SELECT v::text::bigint FROM ag_catalog.cypher(''health_graph'',%s%s%s) AS (v ag_catalog.agtype)',delimiter,query,delimiter) INTO vertices;
  query := format('MATCH ()-[r:RELATED]->() WHERE r.patient_id=%s RETURN count(r)',to_json(p_patient)::text);
  delimiter := '$q_' || md5(query) || '$';
  EXECUTE format('SELECT v::text::bigint FROM ag_catalog.cypher(''health_graph'',%s%s%s) AS (v ag_catalog.agtype)',delimiter,query,delimiter) INTO edges;
  RETURN jsonb_build_object('vertices',vertices,'edges',edges);
END $$;
REVOKE ALL ON FUNCTION health.graph_neighbors(text,integer), health.graph_counts(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION health.graph_neighbors(text,integer), health.graph_counts(text) TO health_reader;
