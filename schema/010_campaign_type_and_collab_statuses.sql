-- MarketBase schema, migration 010 — Campaign type column + collaboration statuses
--
-- Separates two orthogonal things on `campaigns`:
--   - channel       = the outreach medium (linkedin_dm, email, smartlead, …)
--   - campaign_type = the intent / purpose (outreach, collaboration, …)
--
-- A "collaboration" campaign tracks inviacmens to a thought leader to
-- co-create something (a carousel, a panel, a podcast). The same shape as
-- an outreach campaign, just with different statuses on the member rows.
--
-- Conventional campaign_type values:
--   'outreach'                — cold outreach / sequenced messaging
--   'collaboration'           — asking someone to co-create with us
--   'carousel_participation'  — invite to be a quoted voice in a carousel
--   'event_invite'            — invite to an event / panel / webinar
--   'feedback_panel'          — invite to give feedback on a product / idea
--
-- Free text so the convention can evolve without ALTER TYPE pain.

ALTER TABLE campaigns
    ADD COLUMN IF NOT EXISTS campaign_type text NOT NULL DEFAULT 'outreach';

CREATE INDEX IF NOT EXISTS idx_campaigns_campaign_type ON campaigns(campaign_type);

-- Extend the campaign_member_status enum with collaboration-flavored values.
-- Wrapped in DO blocks because ADD VALUE IF NOT EXISTS only exists in PG 12+
-- AND because ALTER TYPE cannot run inside a transaction block on some
-- Postgres versions — we tolerate the duplicate_object error if the value
-- already exists.

DO $$ BEGIN ALTER TYPE campaign_member_status ADD VALUE IF NOT EXISTS 'invited';            EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN ALTER TYPE campaign_member_status ADD VALUE IF NOT EXISTS 'agreed';             EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN ALTER TYPE campaign_member_status ADD VALUE IF NOT EXISTS 'declined';           EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN ALTER TYPE campaign_member_status ADD VALUE IF NOT EXISTS 'no_response';        EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN ALTER TYPE campaign_member_status ADD VALUE IF NOT EXISTS 'in_progress';        EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN ALTER TYPE campaign_member_status ADD VALUE IF NOT EXISTS 'follow_up_offered';  EXCEPTION WHEN duplicate_object THEN NULL; END $$;
