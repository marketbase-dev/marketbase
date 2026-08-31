-- MarketBase schema, migration 002 — Sources & qualifications

-- ── lead_sources ──────────────────────────────────────────────────────────
-- M2M: a lead can come from many sources. Append-only.
-- Carries the raw row as we received it so we can re-derive things later.

DO $$ BEGIN
    CREATE TYPE source_type AS ENUM (
        'buyer_monitor_likely_to_connect',  -- output of recent-connections monitor
        'founder_network_export',           -- founder LinkedIn network exports
        'dripify_campaign_export',          -- LeadsFromDripify (N).csv
        'dripify_reply_export',             -- *[Replied].csv
        'linkedin_post_engagement',         -- reactor / commenter on a tracked post
        'manual_add',                       -- manually added
        'apollo_export',
        'engagers_research',                -- engagers-research skill output
        'find_senior_execs'                 -- find-senior-execs skill output
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS lead_sources (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    lead_id       uuid NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    source_type   source_type NOT NULL,
    source_label  text NOT NULL,                      -- free text: filename, search query, etc.
    source_date   date,                               -- when the source data was captured
    raw_data      jsonb,                              -- the original row (column→value)
    recorded_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_lead_sources_lead    ON lead_sources(lead_id);
CREATE INDEX IF NOT EXISTS idx_lead_sources_type    ON lead_sources(source_type);
CREATE INDEX IF NOT EXISTS idx_lead_sources_label   ON lead_sources(source_label);

-- ── lead_qualifications ───────────────────────────────────────────────────
-- Append-only history of qualification decisions. Current state =
-- most-recent row per lead (see lead_current_qualification view).

CREATE TABLE IF NOT EXISTS lead_qualifications (
    id                     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    lead_id                uuid NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    qualifier_name         text NOT NULL,             -- e.g. 'qualify-acme-target'
    qualifier_version      text NOT NULL,             -- e.g. '2.0.0' / 'legacy-buyer-monitor'
    qualified              boolean NOT NULL,
    persona                text,                      -- CISO / Cloud Security / VP / Head of Cloud Sec / null
    reason                 text,                      -- qualified | consultant | company_too_small | ...
    disqualified_reason    text,                      -- more specific tag when qualified=false
    seniority              text,                      -- C-Suite / VP / Director / Senior IC / IC / Unknown
    still_employed         boolean,
    still_employed_reason  text,
    employee_count         integer,
    cloud_sec_count        integer,
    full_result            jsonb,                     -- the full Qualification record
    qualified_at           timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_lq_lead             ON lead_qualifications(lead_id, qualified_at DESC);
CREATE INDEX IF NOT EXISTS idx_lq_qualified        ON lead_qualifications(qualified, persona);
CREATE INDEX IF NOT EXISTS idx_lq_version          ON lead_qualifications(qualifier_version);

-- View: most recent qualification per lead
CREATE OR REPLACE VIEW lead_current_qualification AS
SELECT DISTINCT ON (lead_id)
    lead_id, qualifier_name, qualifier_version,
    qualified, persona, reason, disqualified_reason,
    seniority, still_employed, still_employed_reason,
    employee_count, cloud_sec_count, qualified_at
FROM lead_qualifications
ORDER BY lead_id, qualified_at DESC;
