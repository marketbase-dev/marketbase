-- 042_assertions.sql
-- One table for "someone decided something about an entity", replacing four
-- overlapping mechanisms (lead_tags, lead_qualifications, company_relationships,
-- and the certainty columns on company_vendor_customers).
--
-- Three layers over one table:
--   attribute  role:*   seniority:*   geo:*        -- context-free facts
--   audience   aud:<audience> = qualified|disqualified
--   state      state:*                              -- lifecycle
--   relationship  about a company_edges row
--
-- Subjects are typed nullable FKs, which keep referential integrity that a
-- polymorphic subject_id uuid would forfeit. A new subject type costs a COLUMN,
-- not a table.

CREATE TABLE IF NOT EXISTS assertions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),

    subject_lead_id            uuid REFERENCES leads(id)              ON DELETE CASCADE,
    subject_company_id         uuid REFERENCES companies(id)          ON DELETE CASCADE,
    subject_company_edge_id    uuid REFERENCES company_edges(id)      ON DELETE CASCADE,
    subject_post_engagement_id uuid REFERENCES post_engagements(id)   ON DELETE CASCADE,
    subject_conversation_id    uuid REFERENCES lead_conversations(id) ON DELETE CASCADE,
    CONSTRAINT assertions_one_subject CHECK (num_nonnulls(
        subject_lead_id, subject_company_id, subject_company_edge_id,
        subject_post_engagement_id, subject_conversation_id) = 1),

    namespace  text NOT NULL,   -- role | seniority | geo | aud | state | relationship
    key        text NOT NULL,   -- partnerships_channel | comp_intel_target | customer
    value      text,            -- qualified | disqualified | paying | direct | ...

    -- NULL means "clear enough to act on" -- the default, never backfilled.
    -- Non-null is deliberately a tiny vocabulary.
    -- Confidence exists because the unique index below makes qualified and
    -- disqualified COLLIDE by design, so "sources disagree" has nowhere else to
    -- live but a field on the surviving row.
    confidence text CHECK (confidence IN ('suspected', 'conflicting')),

    notes              text,                              -- the reason, per association
    processor_id       uuid REFERENCES processors(id),    -- which rule VERSION decided
    evidence_signal_id uuid REFERENCES signals(id),       -- the observation behind it

    asserted_by text NOT NULL,
    asserted_at timestamptz NOT NULL DEFAULT now(),
    removed_by  text,
    removed_at  timestamptz
);

-- One ACTIVE assertion per claim. `value` is deliberately NOT in the key:
-- qualified and disqualified for one audience must collide, not coexist.
--
-- NULLS NOT DISTINCT IS LOAD-BEARING, NOT A DETAIL. Postgres treats NULLs as
-- distinct in a unique index by default, and every row here has FOUR NULL
-- subject columns -- so without it the index never collides and enforces
-- nothing at all. Caught 2026-09-11 when a re-run silently doubled 363
-- assertions to 726. Requires PG15+.
CREATE UNIQUE INDEX IF NOT EXISTS uq_assertions_active ON assertions (
    subject_lead_id, subject_company_id, subject_company_edge_id,
    subject_post_engagement_id, subject_conversation_id, namespace, key
) NULLS NOT DISTINCT WHERE removed_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_assertions_ns_key ON assertions (namespace, key) WHERE removed_at IS NULL;

-- THE DOCUMENTED INTERFACE. Every query today assumes presence = active; the
-- moment removed_at exists, direct table reads are silently wrong. Read this.
CREATE OR REPLACE VIEW active_assertions AS
    SELECT * FROM assertions WHERE removed_at IS NULL;

COMMENT ON TABLE assertions IS
    'Every judgement about any entity. Soft-deleted via removed_at -- read '
    'active_assertions, not this table, unless you specifically want history.';
