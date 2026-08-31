-- MarketBase schema, migration 035 — Company monitoring intent
--
-- Adds `monitor_desired` to company_relationships — an axis ORTHOGONAL to the
-- market-landscape classification already modelled here:
--   • relationship  (competitor / suspected_competitor / customer / partner …)  — what the company IS to us
--   • scope         (direct / adjacent / aspirational)                          — how close a competitor
--   • status        (clear / conflicting / checking / NULL)                     — conflict-of-interest vetting
--
-- `monitor_desired = TRUE` means we have DELIBERATELY chosen to monitor this
-- company's salespeople for the Buyer Monitor flow (their LinkedIn connections
-- reveal in-market buyers). It is not implied by being a competitor — plenty of
-- flagged competitors are landscape context we never actively monitor. The
-- companion lead-level marking is the `desired_for_monitoring` tag in lead_tags.
--
-- Downstream: marketbase-export-buyer-monitor-csvs can filter the upload to
-- monitor_desired companies + desired_for_monitoring reps instead of every
-- flagged competitor.

ALTER TABLE company_relationships
    ADD COLUMN IF NOT EXISTS monitor_desired    boolean NOT NULL DEFAULT false;
ALTER TABLE company_relationships
    ADD COLUMN IF NOT EXISTS monitor_desired_at timestamptz;

-- Re-expose on the canonical competitor view. CREATE OR REPLACE can only append
-- columns, so the two new fields go at the end, after `status` (migration 033).
CREATE OR REPLACE VIEW v_competitor_companies AS
SELECT c.id, c.linkedin_slug, c.name, c.linkedin_url, c.website, c.industry,
       c.employee_count, c.employee_range,
       cr.scope, cr.source AS tag_source, cr.notes, cr.recorded_at,
       c.is_showcase, c.parent_linkedin_slug,
       cr.status,
       cr.monitor_desired, cr.monitor_desired_at
FROM companies c
JOIN company_relationships cr ON cr.company_id = c.id
WHERE cr.relationship = 'competitor'
ORDER BY c.name;
