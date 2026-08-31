-- MarketBase schema, migration 001 — Identity & enrichment
-- Per-client Neon DB. Schema is identical across clients.

CREATE EXTENSION IF NOT EXISTS "pgcrypto";  -- gen_random_uuid()

-- ── leads ─────────────────────────────────────────────────────────────────
-- One row per person (canonical LinkedIn URL). Identity-only fields.
-- Every additional fact (sources, qualifications, campaign membership,
-- engagement) lives in its own table and references this row.

CREATE TABLE IF NOT EXISTS leads (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    linkedin_url        text UNIQUE NOT NULL,        -- normalized, lowercase, no trailing slash, no query
    linkedin_urn        text,                         -- /in/ACoAA... or vanity slug
    public_id           text,                         -- vanity slug if available
    name                text,
    headline            text,
    current_title       text,
    current_company     text,
    current_company_url text,                         -- LinkedIn URL of current company
    city                text,
    country             text,
    bio                 text,
    last_enriched_at    timestamptz,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_leads_current_company ON leads(current_company);
CREATE INDEX IF NOT EXISTS idx_leads_country         ON leads(country);
CREATE INDEX IF NOT EXISTS idx_leads_linkedin_urn    ON leads(linkedin_urn);

-- ── companies ─────────────────────────────────────────────────────────────
-- One row per LinkedIn company slug. Multiple leads can share a company.

CREATE TABLE IF NOT EXISTS companies (
    id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    linkedin_slug         text UNIQUE NOT NULL,       -- e.g. "caterpillar-inc"
    linkedin_url          text,
    name                  text,
    website               text,
    industry              text,
    employee_count        integer,
    employee_range        text,                       -- "10001+" / "1001-5000" / etc.
    saleleads_id          text,                       -- numeric id from Saleleads API
    cloud_sec_count       integer,                    -- N matching "cloud security" in /search/people
    cloud_sec_returned    integer,                    -- N people we actually fetched
    cloud_sec_fetched_at  timestamptz,
    size_fetched_at       timestamptz,
    raw_data              jsonb,                      -- full last API response
    created_at            timestamptz NOT NULL DEFAULT now(),
    updated_at            timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_companies_name           ON companies(name);
CREATE INDEX IF NOT EXISTS idx_companies_employee_count ON companies(employee_count);

-- ── enrichment_calls ──────────────────────────────────────────────────────
-- Audit log of every paid API call. Doubles as a cost ledger.

CREATE TABLE IF NOT EXISTS enrichment_calls (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    lead_id       uuid REFERENCES leads(id) ON DELETE SET NULL,
    company_id    uuid REFERENCES companies(id) ON DELETE SET NULL,
    api           text NOT NULL,                      -- 'leadmagic' | 'fresh_linkedin' | 'saleleads' | 'icypeas'
    endpoint      text NOT NULL,                      -- e.g. '/profile-search'
    params        jsonb,
    success       boolean,
    response      jsonb,
    cost          numeric(10,4),                      -- in API credits (1 = base unit per API)
    error_message text,
    fetched_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_enrichment_lead    ON enrichment_calls(lead_id);
CREATE INDEX IF NOT EXISTS idx_enrichment_company ON enrichment_calls(company_id);
CREATE INDEX IF NOT EXISTS idx_enrichment_api     ON enrichment_calls(api, fetched_at);
