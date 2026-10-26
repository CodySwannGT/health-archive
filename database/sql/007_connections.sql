-- Derived associations, pooling consent and pooled patterns. Additive and idempotent.
SET search_path = health, ag_catalog, public;

-- Associations the connections engine computes from one patient's records at
-- each projection. Computed, never source facts: evidence kind derived_association.
CREATE TABLE IF NOT EXISTS health.associations (
  patient_id text NOT NULL REFERENCES health.patients,
  association_id text NOT NULL,
  method text NOT NULL,
  exposure_concept text NOT NULL,
  exposure_label text NOT NULL,
  -- A concept identity shared across patients, or NULL when the concept is meaningful only in this archive.
  exposure_pool_key text,
  outcome_concept text NOT NULL,
  outcome_label text NOT NULL,
  outcome_pool_key text,
  strength text NOT NULL CHECK (strength IN ('insufficient','weak','moderate','strong')),
  direction text NOT NULL,
  effect jsonb NOT NULL,
  statement text NOT NULL,
  supporting_records text[] NOT NULL,
  contrary_records text[] NOT NULL,
  competing jsonb NOT NULL,
  limitations jsonb NOT NULL,
  evidence_kind text NOT NULL DEFAULT 'derived_association' CHECK (evidence_kind = 'derived_association'),
  version_sha256 text NOT NULL,
  first_seen_run bigint REFERENCES health.ingestion_runs,
  last_seen_run bigint REFERENCES health.ingestion_runs,
  is_current boolean NOT NULL DEFAULT true,
  PRIMARY KEY (patient_id, association_id)
);
-- Every version of every association, so a changed conclusion stays auditable.
CREATE TABLE IF NOT EXISTS health.association_versions (
  patient_id text NOT NULL REFERENCES health.patients,
  association_id text NOT NULL,
  version_sha256 text NOT NULL,
  payload jsonb NOT NULL,
  first_seen_run bigint REFERENCES health.ingestion_runs,
  PRIMARY KEY (patient_id, association_id, version_sha256)
);
CREATE INDEX IF NOT EXISTS associations_current ON health.associations(patient_id, strength) WHERE is_current;

-- Opt-in to contribute this chart's associations, never its records, to pooled patterns.
CREATE TABLE IF NOT EXISTS health.pooling_consent (
  patient_id text PRIMARY KEY REFERENCES health.patients,
  user_id text NOT NULL REFERENCES health.users,
  opted_in boolean NOT NULL,
  consent_version text NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS health.pooling_consent_events (
  event_id bigserial PRIMARY KEY,
  patient_id text NOT NULL REFERENCES health.patients,
  user_id text NOT NULL REFERENCES health.users,
  opted_in boolean NOT NULL,
  consent_version text NOT NULL,
  recorded_at timestamptz NOT NULL DEFAULT now()
);

-- Patterns shared by at least five opted-in patients. No patient, record, date
-- or free-text column exists, and the floor is enforced here, not only in code.
CREATE TABLE IF NOT EXISTS health.pooled_patterns (
  pattern_id text PRIMARY KEY,
  method text NOT NULL,
  exposure_key text NOT NULL,
  exposure_label text NOT NULL,
  outcome_key text NOT NULL,
  outcome_label text NOT NULL,
  direction text NOT NULL,
  contributors integer NOT NULL CHECK (contributors >= 5),
  share_moderate_or_strong numeric NOT NULL,
  median_effect jsonb,
  computed_at timestamptz NOT NULL DEFAULT now()
);

DO $$ DECLARE t text; BEGIN
  FOREACH t IN ARRAY ARRAY['associations','association_versions','pooling_consent','pooling_consent_events'] LOOP
    EXECUTE format('ALTER TABLE health.%I ENABLE ROW LEVEL SECURITY', t);
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname='health' AND tablename=t AND policyname='patient_access') THEN
      EXECUTE format('CREATE POLICY patient_access ON health.%I FOR SELECT USING (health.can_access(patient_id))', t);
    END IF;
  END LOOP;
END $$;
ALTER TABLE health.pooled_patterns ENABLE ROW LEVEL SECURITY;
-- Any registered account may read pooled patterns; they hold aggregates only.
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname='health' AND tablename='pooled_patterns' AND policyname='registered_accounts') THEN
    CREATE POLICY registered_accounts ON health.pooled_patterns FOR SELECT
      USING (EXISTS (SELECT 1 FROM health.patient_access WHERE user_id = current_setting('health.user_id', true)));
  END IF;
END $$;
GRANT SELECT ON health.associations, health.association_versions, health.pooling_consent,
  health.pooling_consent_events, health.pooled_patterns TO health_reader;
