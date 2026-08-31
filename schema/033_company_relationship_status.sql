-- MarketBase schema, migration 033 — Company relationship status
--
-- Adds an optional `status` to company_relationships so we can record whether a
-- flagged company is actually workable. Driven by the competitor-research flow:
-- some competitors are off-limits due to a conflict of interest, others are
-- still being vetted.
--
-- Conventional values (competitor context):
--   'clear'        — no conflict; we can build/monitor a rep list for them.
--   'conflicting'  — conflict of interest; we likely can't pursue them.
--   'checking'     — not yet decided; still vetting the conflict question.
--   NULL           — status not assessed.
--
-- Left as free-ish text with a CHECK so the convention can hold today while
-- staying easy to extend (drop/replace the constraint in a later migration).

ALTER TABLE company_relationships
    ADD COLUMN IF NOT EXISTS status text;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'ck_company_relationship_status'
    ) THEN
        ALTER TABLE company_relationships
            ADD CONSTRAINT ck_company_relationship_status
            CHECK (status IS NULL OR status IN ('clear', 'conflicting', 'checking'));
    END IF;
END$$;

-- Re-expose status on the canonical competitor view. CREATE OR REPLACE can only
-- append columns (not reorder), so `status` goes at the end of the column list,
-- after the showcase columns added by migration 024.
CREATE OR REPLACE VIEW v_competitor_companies AS
SELECT c.id, c.linkedin_slug, c.name, c.linkedin_url, c.website, c.industry,
       c.employee_count, c.employee_range,
       cr.scope, cr.source AS tag_source, cr.notes, cr.recorded_at,
       c.is_showcase, c.parent_linkedin_slug,
       cr.status
FROM companies c
JOIN company_relationships cr ON cr.company_id = c.id
WHERE cr.relationship = 'competitor'
ORDER BY c.name;
