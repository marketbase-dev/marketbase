-- MarketBase schema, migration 030 — CRM deal-state sync (HubSpot → MarketBase)
--
-- Purpose: denormalize "which companies have a live (open or won) deal" into
-- MarketBase so outreach can SKIP people who work at companies we're already in a
-- deal with, and never cold-pitch a company that became a customer (won).
--
-- HubSpot stays the system of record for deal progression. This table holds
-- only enough state to ACT (exclude / pull from outreach). One row per deal,
-- keyed on the HubSpot deal id. An external once-daily job mirrors HubSpot:
--   • per deal:  SELECT gtmdb_sync_deal(...)           (idempotent upsert)
--   • once/run:  SELECT gtmdb_reconcile_deleted_deals(<all live ids>)
--
-- Product rules baked in here:
--   • Open OR won deal  → company is "protected" (all its leads excluded).
--   • Closed-WON        → protected indefinitely (is_won stays true forever).
--   • Closed-LOST       → protection released (is_open=false, is_won=false).
--   • DELETED in HubSpot → is_deleted=true, but the row KEEPS its last-known
--     protection (a deletion may be accidental). Such rows surface in
--     v_deleted_deals_review for a human to confirm before release.
--
-- See: SOW V2 (Acme GTM folder). Companion of 007_company_relationships.

-- ── 1. Table ─────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS company_deals (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    hs_deal_id    text NOT NULL UNIQUE,         -- idempotency key (HubSpot deal object id)
    company_id    uuid REFERENCES companies(id) ON DELETE SET NULL,  -- resolved best-effort
    hs_company_id text,                         -- HubSpot primary-associated company id
    company_name  text,                         -- HubSpot company name (readability)
    match_slug    text,                         -- linkedin slug, lowercased (primary match key)
    match_domain  text,                         -- registrable host, lowercased (fallback key)
    pipeline      text,
    stage_id      text,
    stage_name    text,
    is_open       boolean NOT NULL,
    is_won        boolean NOT NULL DEFAULT false,
    is_deleted    boolean NOT NULL DEFAULT false,
    amount        numeric,
    source        text NOT NULL DEFAULT 'hubspot',
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    synced_at     timestamptz NOT NULL DEFAULT now(),
    deleted_at    timestamptz
);

CREATE INDEX IF NOT EXISTS idx_company_deals_slug    ON company_deals(match_slug);
CREATE INDEX IF NOT EXISTS idx_company_deals_company ON company_deals(company_id);
CREATE INDEX IF NOT EXISTS idx_company_deals_protect ON company_deals(match_slug)
    WHERE is_open OR is_won;

-- ── 2. Write contract A: upsert one deal ─────────────────────────────────────
-- Called once per deal by the external sync job with RAW HubSpot fields. All
-- slug/domain normalization + company resolution happen here, so the job
-- carries no MarketBase business logic.
--
-- COALESCE-preserve on the company columns: if today's company-association
-- fetch failed and the job sends NULLs, we keep the previously-resolved
-- company linkage rather than wiping a company's protection. Deal-derived
-- fields (stage_id, is_open, is_won, amount) always reflect the latest fetch.
CREATE OR REPLACE FUNCTION gtmdb_sync_deal(
    p_hs_deal_id    text,
    p_hs_company_id text,
    p_company_name  text,
    p_li_page       text,
    p_domain        text,
    p_pipeline      text,
    p_stage_id      text,
    p_stage_name    text,
    p_is_open       boolean,
    p_is_won        boolean,
    p_amount        numeric
) RETURNS void LANGUAGE plpgsql AS $$
DECLARE
    v_slug    text := nullif(lower(substring(p_li_page from 'linkedin\.com/company/([^/?#]+)')), '');
    v_domain  text := nullif(lower(regexp_replace(coalesce(p_domain, ''), '^https?://', '')), '');
    v_company uuid;
BEGIN
    -- normalize domain to a bare host: strip leading www., drop any path
    IF v_domain IS NOT NULL THEN
        v_domain := nullif(split_part(regexp_replace(v_domain, '^www\.', ''), '/', 1), '');
    END IF;

    -- resolve company_id best-effort: slug first, then domain host
    IF v_slug IS NOT NULL THEN
        SELECT id INTO v_company FROM companies WHERE lower(linkedin_slug) = v_slug LIMIT 1;
    END IF;
    IF v_company IS NULL AND v_domain IS NOT NULL THEN
        SELECT id INTO v_company FROM companies
        WHERE lower(regexp_replace(coalesce(website, ''), '^https?://(www\.)?', '')) LIKE v_domain || '%'
        LIMIT 1;
    END IF;

    INSERT INTO company_deals (
        hs_deal_id, company_id, hs_company_id, company_name, match_slug, match_domain,
        pipeline, stage_id, stage_name, is_open, is_won, is_deleted, amount, source,
        first_seen_at, synced_at
    ) VALUES (
        p_hs_deal_id, v_company, p_hs_company_id, p_company_name, v_slug, v_domain,
        p_pipeline, p_stage_id, p_stage_name, p_is_open, p_is_won, false, p_amount, 'hubspot',
        now(), now()
    )
    ON CONFLICT (hs_deal_id) DO UPDATE SET
        -- company linkage: preserve prior on null inputs (transient fetch failure safe)
        company_id    = COALESCE(EXCLUDED.company_id,    company_deals.company_id),
        hs_company_id = COALESCE(EXCLUDED.hs_company_id, company_deals.hs_company_id),
        company_name  = COALESCE(EXCLUDED.company_name,  company_deals.company_name),
        match_slug    = COALESCE(EXCLUDED.match_slug,    company_deals.match_slug),
        match_domain  = COALESCE(EXCLUDED.match_domain,  company_deals.match_domain),
        stage_name    = COALESCE(EXCLUDED.stage_name,    company_deals.stage_name),
        -- deal-derived fields reflect HubSpot truth from the (successful) deal fetch
        pipeline      = EXCLUDED.pipeline,
        stage_id      = EXCLUDED.stage_id,
        is_open       = EXCLUDED.is_open,
        is_won        = EXCLUDED.is_won,
        amount        = EXCLUDED.amount,
        -- a deal we just saw in HubSpot is, by definition, not deleted
        is_deleted    = false,
        deleted_at    = NULL,
        synced_at     = now();
END $$;

-- ── 3. Write contract B: reconcile deletions ─────────────────────────────────
-- Called ONCE at the end of a run with the full array of HubSpot deal ids seen.
-- Marks any not-already-deleted hubspot row whose id is absent as deleted.
-- The caller MUST only call this after a complete, error-free pull of all
-- deals; a partial list would wrongly mark live deals as deleted. As a
-- backstop this no-ops on a NULL/empty array. Returns the count newly deleted.
CREATE OR REPLACE FUNCTION gtmdb_reconcile_deleted_deals(p_live_deal_ids text[])
RETURNS integer LANGUAGE plpgsql AS $$
DECLARE
    v_count integer;
BEGIN
    IF p_live_deal_ids IS NULL OR array_length(p_live_deal_ids, 1) IS NULL THEN
        RETURN 0;
    END IF;

    UPDATE company_deals
       SET is_deleted = true,
           deleted_at = now(),
           synced_at  = now()
     WHERE NOT is_deleted
       AND source = 'hubspot'
       AND hs_deal_id <> ALL (p_live_deal_ids);
    GET DIAGNOSTICS v_count = ROW_COUNT;
    RETURN v_count;
END $$;

-- ── 4. Exclusion view: leads at a company with a live/won deal ───────────────
-- Mirrors v_leads_at_competitor_company (007). Two match paths, UNION-ed:
--   (a) via a resolved company_id → companies.linkedin_url == lead url, and
--   (b) via the deal's match_slug == slug parsed from the lead's company url.
-- Path (b) protects even a company that has no `companies` row yet, and works
-- for leads ingested AFTER the deal was recorded. Deleted-but-still-protecting
-- rows are INCLUDED here on purpose (deletion does not auto-release).
CREATE OR REPLACE VIEW v_leads_at_deal_company AS
WITH protecting AS (
    SELECT * FROM company_deals WHERE is_open OR is_won
),
matched AS (
    SELECT l.id AS lead_id, l.linkedin_url, l.name, l.current_title,
           d.hs_deal_id, d.company_name, d.stage_name, d.is_open, d.is_won, d.is_deleted
    FROM protecting d
    JOIN companies c ON c.id = d.company_id
    JOIN leads l
      ON regexp_replace(lower(l.current_company_url), '/+$', '')
       = regexp_replace(lower(c.linkedin_url),        '/+$', '')
    UNION
    SELECT l.id, l.linkedin_url, l.name, l.current_title,
           d.hs_deal_id, d.company_name, d.stage_name, d.is_open, d.is_won, d.is_deleted
    FROM protecting d
    JOIN leads l
      ON d.match_slug IS NOT NULL
     AND lower(substring(l.current_company_url from 'linkedin\.com/company/([^/?#]+)')) = d.match_slug
)
SELECT
    lead_id, linkedin_url, name, current_title,
    bool_or(is_won)     AS is_customer,
    bool_or(is_open)    AS has_open_deal,
    bool_or(is_deleted) AS has_deleted_deal,
    max(stage_name) FILTER (WHERE is_open)  AS open_stage,
    array_agg(DISTINCT hs_deal_id)          AS deal_ids,
    array_agg(DISTINCT company_name)        AS deal_companies
FROM matched
GROUP BY lead_id, linkedin_url, name, current_title;

-- ── 5. Companies-level roll-up (informational) ───────────────────────────────
CREATE OR REPLACE VIEW v_companies_with_active_deal AS
SELECT
    d.company_id,
    d.match_slug,
    max(d.company_name)                     AS company_name,
    bool_or(d.is_won)                       AS is_customer,
    bool_or(d.is_open)                      AS has_open_deal,
    bool_or(d.is_deleted)                   AS has_deleted_deal,
    max(d.stage_name) FILTER (WHERE d.is_open) AS open_stage,
    array_agg(DISTINCT d.hs_deal_id)        AS deal_ids,
    max(d.synced_at)                        AS last_synced_at
FROM company_deals d
WHERE d.is_open OR d.is_won
GROUP BY d.company_id, d.match_slug;

-- ── 6. Human review surface: deals deleted while still protecting ────────────
-- Per the product rule, deletion keeps protection until a human confirms.
-- Release = manually UPDATE the row (e.g. set is_open=false, is_won=false).
CREATE OR REPLACE VIEW v_deleted_deals_review AS
SELECT d.hs_deal_id, d.company_id, d.company_name, d.match_slug, d.match_domain,
       d.stage_name, d.is_open, d.is_won, d.amount, d.deleted_at, d.synced_at
FROM company_deals d
WHERE d.is_deleted AND (d.is_open OR d.is_won)
ORDER BY d.deleted_at DESC NULLS LAST;
