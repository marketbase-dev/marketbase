-- MarketBase schema, migration 007 — Company relationships
--
-- Tags companies with a relationship category (competitor, customer, partner,
-- vendor, ecosystem). Modeled like lead_sources for symmetry: many-to-many,
-- append-only, with provenance + free-text notes.
--
-- Why a separate table rather than a `category` column on companies?
--   • A company can belong to multiple categories (e.g. an analyst firm that's
--     also an ecosystem partner).
--   • We want an audit trail (who/when/why) we don't want to clobber on update.
--   • Lets us add new relationship types without ALTER TYPE / migration pain.
--
-- Companion view (v_leads_at_competitor_company) lets downstream tools —
-- qualify.py, Smartlead exclusion lists, Slack alerts — answer
-- "is this lead currently employed by a flagged competitor?" with one join.

CREATE TABLE IF NOT EXISTS company_relationships (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id      uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,

    -- Conventional values: 'competitor', 'customer', 'partner', 'vendor',
    -- 'ecosystem', 'analyst', 'investor', 'press'. Free text so the convention
    -- can evolve without a migration.
    relationship    text NOT NULL,

    -- For competitors: 'direct' / 'adjacent' / 'aspirational'.
    -- For customers: 'paying' / 'pilot' / 'trial' / 'former'.
    -- For partners: 'strategic' / 'reseller' / 'integration'.
    -- Free text — convention by relationship.
    scope           text,

    -- How we know. Examples: 'manual', 'find-senior-execs', 'research-competitors',
    -- 'crm-import', 'analyst-report'.
    source          text NOT NULL DEFAULT 'manual',

    notes           text,
    recorded_at     timestamptz NOT NULL DEFAULT now(),

    -- One row per (company, relationship) pair. Same company can hold multiple
    -- relationship types but each appears once.
    CONSTRAINT uq_company_relationship UNIQUE (company_id, relationship)
);

CREATE INDEX IF NOT EXISTS idx_company_relationships_company       ON company_relationships(company_id);
CREATE INDEX IF NOT EXISTS idx_company_relationships_relationship  ON company_relationships(relationship);

-- ── View: leads currently employed at a competitor ───────────────────────────
-- Joins leads → companies (on current_company_url) → company_relationships.
-- "Currently employed at a competitor" = lead's current_company_url matches a
-- company that has a 'competitor' relationship row.
--
-- Use this in qualify-style filters and Smartlead exclusion exports:
--   SELECT lead_id FROM v_leads_at_competitor_company;

CREATE OR REPLACE VIEW v_leads_at_competitor_company AS
SELECT
    l.id              AS lead_id,
    l.linkedin_url    AS lead_linkedin_url,
    l.name            AS lead_name,
    l.current_title,
    c.id              AS company_id,
    c.name            AS company_name,
    c.linkedin_slug   AS company_slug,
    cr.scope          AS competitor_scope,
    cr.source         AS tag_source,
    cr.notes          AS competitor_notes
FROM leads l
JOIN companies c
  ON regexp_replace(lower(l.current_company_url), '/+$', '')
   = regexp_replace(lower(c.linkedin_url),       '/+$', '')
JOIN company_relationships cr
  ON cr.company_id = c.id
 AND cr.relationship = 'competitor';

-- ── Helper view: all flagged competitors ─────────────────────────────────────
CREATE OR REPLACE VIEW v_competitor_companies AS
SELECT c.id, c.linkedin_slug, c.name, c.linkedin_url, c.website, c.industry,
       c.employee_count, c.employee_range,
       cr.scope, cr.source AS tag_source, cr.notes, cr.recorded_at
FROM companies c
JOIN company_relationships cr ON cr.company_id = c.id
WHERE cr.relationship = 'competitor'
ORDER BY c.name;
