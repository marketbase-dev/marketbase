-- MarketBase schema, migration 008 — Lead tags (mutable categorization)
--
-- Three-way separation of how we describe a lead:
--
--   lead_sources         → provenance (immutable history of how we found them)
--   lead_qualifications  → algorithmic classification results (append-only)
--   lead_tags            → mutable categorization applied by humans or scripts
--
-- Tags answer questions like:
--   - Is this person a thought leader we're researching?
--   - Have we marked them as a qualified creator?
--   - Are they flagged do-not-pursue?
--
-- Multiple tags per lead. Each (lead, tag) pair appears once.

CREATE TABLE IF NOT EXISTS lead_tags (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    lead_id     uuid NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    tag         text NOT NULL,                        -- e.g. 'thought-leader', 'qualified-creator', 'do-not-pursue'
    notes       text,                                  -- why this tag was applied
    tagged_by   text,                                  -- 'alice' / 'claude' / 'auto-classifier' / script name
    tagged_at   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_lead_tags_lead_tag UNIQUE (lead_id, tag)
);

CREATE INDEX IF NOT EXISTS idx_lead_tags_lead ON lead_tags(lead_id);
CREATE INDEX IF NOT EXISTS idx_lead_tags_tag  ON lead_tags(tag);
