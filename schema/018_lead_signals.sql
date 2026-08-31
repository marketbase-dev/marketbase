-- Generic per-lead enrichment / signal storage. Mirrors lead_qualifications
-- in shape but stores FACTS/SIGNALS (e.g. employment_type, posts_3mo) rather
-- than DECISIONS (e.g. qualified=true, persona=...). Each client's enricher
-- processors write here with their own payload schema; classifiers read via
-- `from-signal:<enricher_name>:<key>` in their fields_consulted block.

CREATE TABLE IF NOT EXISTS lead_signals (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    lead_id           uuid NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    enricher_name     text NOT NULL,
    enricher_version  text NOT NULL,
    payload           jsonb NOT NULL,
    enriched_at       timestamptz NOT NULL DEFAULT now(),
    enriched_by       text,
    UNIQUE (lead_id, enricher_name, enricher_version)
);

CREATE INDEX IF NOT EXISTS idx_lead_signals_lead_id     ON lead_signals (lead_id);
CREATE INDEX IF NOT EXISTS idx_lead_signals_enricher    ON lead_signals (enricher_name, enriched_at DESC);

-- Convenience view: latest version per (lead, enricher).
-- Mirrors the lead_current_qualification pattern. Use this for "what's
-- the current enrichment for X?" reads; query lead_signals directly when
-- you need historical versions.
CREATE OR REPLACE VIEW lead_current_signals AS
SELECT DISTINCT ON (lead_id, enricher_name)
       id, lead_id, enricher_name, enricher_version, payload, enriched_at, enriched_by
FROM lead_signals
ORDER BY lead_id, enricher_name, enriched_at DESC, id DESC;
