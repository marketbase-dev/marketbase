-- MarketBase schema, migration 024 — Showcase pages as first-class companies
--
-- LinkedIn distinguishes between /company/<slug> (a real company page with
-- employees) and /showcase/<slug> (a product-line page owned by a parent
-- company, with no employees of its own). For competitor research,
-- showcase pages matter because they carry product-specific posts and
-- followers — but their "senior execs" live at the parent company, not at
-- the showcase URN.
--
-- Two new columns on `companies`:
--   • is_showcase           — TRUE iff this row represents a LinkedIn showcase
--                             page (URL pattern /showcase/<slug>). Default FALSE.
--   • parent_linkedin_slug  — Required when is_showcase=TRUE. Slug of the
--                             parent /company/ page. Tells downstream tools
--                             "to find people who work on this product, look
--                             at the parent's employees and filter by product
--                             keyword".
--
-- Conservative migration: nullable column + boolean default, no data
-- backfill needed. Existing rows stay non-showcase.

ALTER TABLE companies
    ADD COLUMN IF NOT EXISTS is_showcase           boolean NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS parent_linkedin_slug  text;

-- A showcase row MUST have a parent slug. Non-showcase rows MUST NOT.
ALTER TABLE companies
    DROP CONSTRAINT IF EXISTS chk_companies_showcase_parent;
ALTER TABLE companies
    ADD CONSTRAINT chk_companies_showcase_parent CHECK (
        (is_showcase = FALSE AND parent_linkedin_slug IS NULL)
     OR (is_showcase = TRUE  AND parent_linkedin_slug IS NOT NULL)
    );

-- Index to support "all showcases under company X" queries.
CREATE INDEX IF NOT EXISTS idx_companies_parent_slug
    ON companies(parent_linkedin_slug)
    WHERE parent_linkedin_slug IS NOT NULL;

-- Refresh v_competitor_companies to surface the showcase flag + parent.
-- (Append new columns at the end to keep CREATE OR REPLACE VIEW happy —
-- Postgres won't let us reorder existing view columns.)
CREATE OR REPLACE VIEW v_competitor_companies AS
SELECT c.id, c.linkedin_slug, c.name, c.linkedin_url, c.website, c.industry,
       c.employee_count, c.employee_range,
       cr.scope, cr.source AS tag_source, cr.notes, cr.recorded_at,
       c.is_showcase, c.parent_linkedin_slug
FROM companies c
JOIN company_relationships cr ON cr.company_id = c.id
WHERE cr.relationship = 'competitor'
ORDER BY c.name;
