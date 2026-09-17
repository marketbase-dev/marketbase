-- 041_signals.sql
-- Enrichment history for ANY subject, append-only.
--
-- lead_signals is lead-scoped (lead_id NOT NULL) and version-keyed
-- (UNIQUE (lead_id, enricher_name, enricher_version)), so re-running the same
-- version overwrites and there is nowhere to record an observation about a
-- company or an edge. Both limits go.
--
-- Generalising here rather than adding company_signals is deliberate: a second
-- enrichment table would have been followed by a third. One subject pattern,
-- shared with assertions.
--
-- A signal is an OBSERVATION ("acme.vendorcloud.com returned 200 and redirected to
-- login.microsoftonline.com with tenant GUID X") -- refreshable, idempotent, no
-- judgement. An assertion is a JUDGEMENT ("acme is a Vendor-A customer"). If they
-- shared a table, re-running an enricher would churn decision history.

CREATE TABLE IF NOT EXISTS signals (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),

    subject_lead_id            uuid REFERENCES leads(id)            ON DELETE CASCADE,
    subject_company_id         uuid REFERENCES companies(id)        ON DELETE CASCADE,
    subject_company_edge_id    uuid REFERENCES company_edges(id)    ON DELETE CASCADE,
    subject_post_engagement_id uuid REFERENCES post_engagements(id) ON DELETE CASCADE,
    CONSTRAINT signals_one_subject CHECK (num_nonnulls(
        subject_lead_id, subject_company_id,
        subject_company_edge_id, subject_post_engagement_id) = 1),

    enricher_name    text NOT NULL,
    enricher_version text,
    payload          jsonb NOT NULL,
    enriched_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_signals_lead    ON signals (subject_lead_id, enricher_name, enriched_at DESC);
CREATE INDEX IF NOT EXISTS idx_signals_company ON signals (subject_company_id, enricher_name, enriched_at DESC);
CREATE INDEX IF NOT EXISTS idx_signals_edge    ON signals (subject_company_edge_id, enricher_name, enriched_at DESC);

CREATE OR REPLACE VIEW v_latest_signal AS
    SELECT DISTINCT ON (subject_lead_id, subject_company_id,
                        subject_company_edge_id, subject_post_engagement_id, enricher_name) *
    FROM signals
    ORDER BY subject_lead_id, subject_company_id, subject_company_edge_id,
             subject_post_engagement_id, enricher_name, enriched_at DESC;

COMMENT ON TABLE signals IS
    'Append-only enrichment observations about any subject. Nothing is ever '
    'deleted; read v_latest_signal for current values.';
