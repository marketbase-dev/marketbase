-- MarketBase schema, migration 003 — Campaigns & state

-- ── campaigns ─────────────────────────────────────────────────────────────
-- Convention: name follows <owner>_<persona>_<channel>_<yyyy_mm>
--   e.g. 'dana_ciso_dripify_2026_05', 'engaged_with_northwind_cloud_security_2026_05'

DO $$ BEGIN
    CREATE TYPE campaign_channel AS ENUM (
        'linkedin_dm', 'linkedin_invite', 'email', 'dripify', 'smartlead',
        'manual', 'other'
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE campaign_status AS ENUM (
        'draft', 'staged', 'active', 'paused', 'closed'
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS campaigns (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name          text UNIQUE NOT NULL,                -- e.g. 'dana_ciso_dripify_2026_05'
    description   text,
    channel       campaign_channel NOT NULL,
    status        campaign_status NOT NULL DEFAULT 'draft',
    owner         text,                                -- e.g. 'dana', 'bob', 'alice' 
    persona_target text,                               -- e.g. 'CISO', 'Cloud Security'
    period        text,                                -- e.g. '2026-05'
    sender_account text,                               -- handle of the LinkedIn / email account that sends
    sender_email  text,
    notes         text,
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_campaigns_status   ON campaigns(status);
CREATE INDEX IF NOT EXISTS idx_campaigns_owner    ON campaigns(owner);

-- ── campaign_members ──────────────────────────────────────────────────────
-- One row per (lead, campaign) pair. Status tracks the journey.

DO $$ BEGIN
    CREATE TYPE campaign_member_status AS ENUM (
        'staged',                  -- qualified + assigned to campaign, not yet pushed to Dripify/Smartlead
        'uploaded',                -- pushed to the outreach tool
        'connection_requested',    -- LinkedIn invite sent
        'connection_accepted',     -- they accepted the LinkedIn request
        'message_sent',            -- a DM/email was sent
        'replied',                 -- they replied
        'meeting_booked',          -- a call was scheduled
        'completed',               -- happy ending (closed-won / nurture / etc.)
        'removed_blocked',         -- the platform blocked the message or they blocked us
        'removed_other'            -- pulled from campaign manually
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS campaign_members (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    lead_id             uuid NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    campaign_id         uuid NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    status              campaign_member_status NOT NULL DEFAULT 'staged',
    staged_at           timestamptz NOT NULL DEFAULT now(),
    uploaded_at         timestamptz,
    last_status_at      timestamptz NOT NULL DEFAULT now(),
    last_status_source  text,                           -- 'marketbase-stage', 'dripify-reply-import', etc.
    status_history      jsonb DEFAULT '[]'::jsonb,      -- [{status, at, source, notes}, ...]
    notes               text,
    UNIQUE (lead_id, campaign_id)
);

CREATE INDEX IF NOT EXISTS idx_cm_lead     ON campaign_members(lead_id);
CREATE INDEX IF NOT EXISTS idx_cm_campaign ON campaign_members(campaign_id, status);
CREATE INDEX IF NOT EXISTS idx_cm_status   ON campaign_members(status);

-- Trigger: every time status changes, append to history + bump last_status_at
CREATE OR REPLACE FUNCTION cm_status_change() RETURNS trigger AS $$
BEGIN
    IF NEW.status IS DISTINCT FROM OLD.status THEN
        NEW.last_status_at := now();
        NEW.status_history := COALESCE(OLD.status_history, '[]'::jsonb) ||
            jsonb_build_array(jsonb_build_object(
                'status', NEW.status::text,
                'at', NEW.last_status_at,
                'source', NEW.last_status_source,
                'notes', NEW.notes
            ));
    END IF;
    RETURN NEW;
END $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_cm_status_change ON campaign_members;
CREATE TRIGGER trg_cm_status_change
    BEFORE UPDATE ON campaign_members
    FOR EACH ROW EXECUTE FUNCTION cm_status_change();

-- ── lead_actions ──────────────────────────────────────────────────────────
-- An intent queue. Other agents read this to know "what should I do next?"

DO $$ BEGIN
    CREATE TYPE lead_action AS ENUM (
        'add_to_campaign',
        'remove_from_campaign',
        'requalify',
        'mark_unqualified_manual',
        'mark_qualified_manual',
        'archive'
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS lead_actions (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    lead_id       uuid NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    action        lead_action NOT NULL,
    campaign_id   uuid REFERENCES campaigns(id) ON DELETE SET NULL,
    requested_by  text NOT NULL,                       -- 'marketbase-stage-to-campaign' / 'alice' / etc.
    requested_at  timestamptz NOT NULL DEFAULT now(),
    applied_at    timestamptz,
    notes         text
);

CREATE INDEX IF NOT EXISTS idx_la_lead         ON lead_actions(lead_id);
CREATE INDEX IF NOT EXISTS idx_la_pending      ON lead_actions(applied_at) WHERE applied_at IS NULL;
