-- Generic and brand names for medication concepts, and brand pooling keys. Additive and idempotent.
SET search_path = health, ag_catalog, public;

-- A medication concept's generic name, curated or stated by its records, and the brands its records name.
ALTER TABLE health.concepts ADD COLUMN IF NOT EXISTS generic_name text;
ALTER TABLE health.concepts ADD COLUMN IF NOT EXISTS brand_names text[] NOT NULL DEFAULT '{}';
-- An association with a medication pools under its generic and, separately, under each brand the patient used.
ALTER TABLE health.associations ADD COLUMN IF NOT EXISTS exposure_brand_keys text[] NOT NULL DEFAULT '{}';
