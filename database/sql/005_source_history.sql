-- Version-control history of a patient's former archive repository, preserved
-- in the database so no file version or commit is lost when the repository is
-- retired. Every commit's full file tree is kept, so any past version of any
-- file can be reconstructed. Idempotent.
SET search_path = health, ag_catalog, public;

CREATE TABLE IF NOT EXISTS health.source_commits (
  patient_id text NOT NULL REFERENCES health.patients,
  repository text NOT NULL,
  commit_sha text NOT NULL,
  parent_shas text[] NOT NULL,
  author_name text NOT NULL,
  authored_at timestamptz NOT NULL,
  committer_name text NOT NULL,
  committed_at timestamptz NOT NULL,
  message text NOT NULL,
  refs text[] NOT NULL DEFAULT '{}',
  imported_run bigint REFERENCES health.ingestion_runs,
  PRIMARY KEY (patient_id, repository, commit_sha)
);
CREATE TABLE IF NOT EXISTS health.source_history (
  patient_id text NOT NULL,
  repository text NOT NULL,
  commit_sha text NOT NULL,
  path text NOT NULL,
  git_blob text NOT NULL,
  size_bytes bigint NOT NULL,
  -- NULL only when the content is deliberately not duplicated, with the reason recorded.
  sha256 text REFERENCES health.artifact_contents,
  content_omitted_reason text,
  PRIMARY KEY (patient_id, repository, commit_sha, path),
  FOREIGN KEY (patient_id, repository, commit_sha) REFERENCES health.source_commits,
  CHECK ((sha256 IS NULL) = (content_omitted_reason IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS source_history_path ON health.source_history(patient_id, path);

DO $$ DECLARE t text; BEGIN
  FOREACH t IN ARRAY ARRAY['source_commits','source_history'] LOOP
    EXECUTE format('ALTER TABLE health.%I ENABLE ROW LEVEL SECURITY', t);
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname='health' AND tablename=t AND policyname='patient_access') THEN
      EXECUTE format('CREATE POLICY patient_access ON health.%I FOR SELECT USING (health.can_access(patient_id))', t);
    END IF;
  END LOOP;
END $$;
GRANT SELECT ON health.source_commits, health.source_history TO health_reader;
