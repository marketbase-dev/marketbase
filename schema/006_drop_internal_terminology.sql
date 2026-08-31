-- MarketBase schema, migration 006 — remove internal Impact 11 terminology
--
-- The schema should be neutral and shareable across clients. Earlier drafts
-- leaked internal-pipeline terminology that doesn't belong in the generic
-- MarketBase interface. This migration:
--
-- 1. Renames the campaigns.<old> column on senders so it doesn't carry
--    internal terminology. The "sender" concept is generic (any account that
--    sends outreach for a campaign).
-- 2. Drops the source_type enum value that referenced internal-only scanning
--    pipelines. Sources from those pipelines should be reformatted into one
--    of the remaining generic source_type values before ingestion.
--
-- Idempotent: safe to re-apply.

-- 1. Column rename (idempotent — guarded by `information_schema` lookup)
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'campaigns' AND column_name = 'vrep_account'
    ) THEN
        ALTER TABLE campaigns RENAME COLUMN vrep_account TO sender_account;
    END IF;
END $$;

-- 2. Drop the internal-pipeline source_type value if it still exists.
-- Postgres doesn't support DROP VALUE on an enum directly; rebuild the type.
DO $$
DECLARE
    has_internal_value boolean;
BEGIN
    SELECT EXISTS (
        SELECT 1 FROM pg_enum e
        JOIN pg_type t ON t.oid = e.enumtypid
        WHERE t.typname = 'source_type' AND e.enumlabel IN ('vrep_scan', 'sender_scan')
    ) INTO has_internal_value;

    IF has_internal_value THEN
        -- Views referencing source_type must be dropped + recreated around the
        -- column-type swap. v_lead_provenance is the only such view (per 005).
        DROP VIEW IF EXISTS v_lead_provenance;

        ALTER TABLE lead_sources ALTER COLUMN source_type TYPE text;
        DROP TYPE source_type;
        CREATE TYPE source_type AS ENUM (
            'buyer_monitor_likely_to_connect',
            'founder_network_export',
            'dripify_campaign_export',
            'dripify_reply_export',
            'linkedin_post_engagement',
            'manual_add',
            'apollo_export',
            'engagers_research',
            'find_senior_execs'
        );
        -- Map any rows that held dropped values to 'manual_add' (neutral).
        UPDATE lead_sources SET source_type = 'manual_add'
            WHERE source_type IN ('vrep_scan', 'sender_scan');
        ALTER TABLE lead_sources
            ALTER COLUMN source_type TYPE source_type
            USING source_type::source_type;

        -- Recreate v_lead_provenance with the same definition as 005.
        CREATE OR REPLACE VIEW v_lead_provenance AS
        SELECT
            l.id                                AS lead_id,
            l.name,
            l.linkedin_url,
            array_agg(DISTINCT s.source_type)   AS source_types,
            array_agg(DISTINCT s.source_label)  AS source_labels,
            MIN(s.recorded_at)                  AS first_seen_at,
            MAX(s.recorded_at)                  AS last_seen_at,
            COUNT(*)                            AS source_count
        FROM leads l
        LEFT JOIN lead_sources s ON s.lead_id = l.id
        GROUP BY l.id, l.name, l.linkedin_url;
    END IF;
END $$;
