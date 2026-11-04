-- 076_crm_mirror.sql
--
-- The client's CRM, mirrored: leads, contacts, campaign members, events and
-- field history, next to 074's opportunities and contact roles.
--
-- Why. Every funnel question ("how do deals from each source close, how fast,
-- how far do they get, what did it take to get a person there") starts BEFORE
-- the deal exists, in the CRM's lead, contact, campaign and activity records.
-- 074 holds the deal and the touches on people we delivered; it holds nothing
-- about the rest of the client's pipeline, so a report could compare our
-- source with the others only at the deal level and never from the top. The
-- owner's direction (2026-11-04): MarketBase syncs the client's CRM so a
-- report can say which deal came from which source and what happened on the
-- way, for every source, and the derivation reads MarketBase only.
--
-- How. One shared sync (tools: sf_sync) fills these tables for every instance
-- from the same code; what differs per client is a `crm-sync` processor row
-- whose `inputs` name the objects, the field lists, the since-date and which
-- CRM field feeds each typed column here. The typed columns are CRM-neutral
-- and the ones every derivation groups or clocks on; `raw` keeps the vendor
-- record whole so a later report re-derives without re-fetching. Tasks are
-- NOT mirrored: a CRM holds millions of them and the per-person outbound
-- first/last datetimes the CRM computes on the contact answer the question
-- a report asks (was this person worked, since when).
--
-- Ids. `id` and the `crm_*_id` columns are the CRM's own ids; `lead_id` is
-- the MarketBase person when the sync could match one (a plain reference,
-- never a cascade: 075). Most mirrored rows match nobody and that is right.
--
-- Additive only. Safe to re-run.

-- crm_opportunities: the columns the funnel groups and clocks on, typed.
ALTER TABLE crm_opportunities ADD COLUMN IF NOT EXISTS source     text;        -- the CRM's own source value for the deal, per the sync config
ALTER TABLE crm_opportunities ADD COLUMN IF NOT EXISTS deal_type  text;        -- new business | upsell | renewal..., as the CRM names it
ALTER TABLE crm_opportunities ADD COLUMN IF NOT EXISTS amount     numeric;     -- the deal amount when the CRM keeps it apart from ACV
ALTER TABLE crm_opportunities ADD COLUMN IF NOT EXISTS created_at timestamptz; -- the instant; created_date stays the day
CREATE INDEX IF NOT EXISTS idx_crm_opps_source ON crm_opportunities (source);

CREATE TABLE IF NOT EXISTS crm_leads (
    id                       text PRIMARY KEY,                 -- the CRM's lead id
    lead_id                  uuid REFERENCES leads(id),        -- the MarketBase person, when matched
    email                    text,
    first_name               text,
    last_name                text,
    company                  text,
    title                    text,
    status                   text,
    lead_source              text,                             -- the CRM's own source value, per the sync config
    source_detail            text,                             -- a second source level when the CRM has one (channel, original source)
    owner_name               text,
    created_at               timestamptz,
    is_converted             boolean,
    converted_at             timestamptz,
    converted_contact_id     text,
    converted_opportunity_id text,
    converted_account_id     text,
    linkedin_url             text,
    source_system            text NOT NULL DEFAULT 'salesforce',
    raw                      jsonb,                            -- the vendor record as fetched
    synced_at                timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_crm_leads_created   ON crm_leads (created_at);
CREATE INDEX IF NOT EXISTS idx_crm_leads_email     ON crm_leads (email);
CREATE INDEX IF NOT EXISTS idx_crm_leads_converted ON crm_leads (converted_contact_id);
CREATE INDEX IF NOT EXISTS idx_crm_leads_lead      ON crm_leads (lead_id);
CREATE INDEX IF NOT EXISTS idx_crm_leads_source    ON crm_leads (lead_source);

CREATE TABLE IF NOT EXISTS crm_contacts (
    id                     text PRIMARY KEY,                   -- the CRM's contact id
    lead_id                uuid REFERENCES leads(id),          -- the MarketBase person, when matched
    account_id             text,
    account_name           text,
    email                  text,
    first_name             text,
    last_name              text,
    title                  text,
    owner_name             text,
    created_at             timestamptz,
    lead_source            text,                               -- the CRM's own source value, per the sync config
    source_detail          text,
    converted_from_lead_id text,                               -- the CRM lead this contact was converted from, when it says
    first_outbound_at      timestamptz,                        -- earliest outbound call or email the CRM records on the person
    form_submitted_at      timestamptz,                        -- earliest inbound form or demo request the CRM records
    linkedin_url           text,
    source_system          text NOT NULL DEFAULT 'salesforce',
    raw                    jsonb,
    synced_at              timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_crm_contacts_account ON crm_contacts (account_id);
CREATE INDEX IF NOT EXISTS idx_crm_contacts_created ON crm_contacts (created_at);
CREATE INDEX IF NOT EXISTS idx_crm_contacts_email   ON crm_contacts (email);
CREATE INDEX IF NOT EXISTS idx_crm_contacts_lead    ON crm_contacts (lead_id);

-- One row per campaign membership; the tips themselves also live in
-- lead_sources (074's sf_campaign_member) because they are a source of a
-- person we hold. Here is every membership, ours included, for the funnel top.
CREATE TABLE IF NOT EXISTS crm_campaign_members (
    id                 text PRIMARY KEY,                       -- the CRM's campaign-member id
    campaign_id        text NOT NULL,
    campaign_name      text,
    campaign_type      text,                                   -- event | webinar | form | outbound..., as the CRM types it
    crm_contact_id     text,
    crm_lead_id        text,
    status             text,
    has_responded      boolean,
    first_responded_at timestamptz,
    created_at         timestamptz,                            -- when the person was added
    source_system      text NOT NULL DEFAULT 'salesforce',
    raw                jsonb,
    synced_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_crm_cm_campaign ON crm_campaign_members (campaign_id);
CREATE INDEX IF NOT EXISTS idx_crm_cm_contact  ON crm_campaign_members (crm_contact_id);
CREATE INDEX IF NOT EXISTS idx_crm_cm_lead     ON crm_campaign_members (crm_lead_id);
CREATE INDEX IF NOT EXISTS idx_crm_cm_created  ON crm_campaign_members (created_at);

-- A CRM calendar event: the meeting record with the scheduling tool's fields
-- when the CRM carries them (which router booked it, whether the prospect
-- booked it themselves, the outcome). lead_meetings keeps the booking VIEW of
-- the meetings on people we delivered; this is every event, for every source.
CREATE TABLE IF NOT EXISTS crm_events (
    id                 text PRIMARY KEY,
    crm_contact_id     text,                                   -- the person, when the event is on a contact
    crm_lead_id        text,                                   -- the person, when the event is on a lead
    related_id         text,                                   -- the deal or account the CRM relates it to
    owner_name         text,
    created_by_name    text,
    subject            text,
    starts_at          timestamptz,
    ends_at            timestamptz,
    created_at         timestamptz,                            -- when it was booked, as far as the CRM knows
    status             text,                                   -- outcome as the CRM records it: held, no show, cancelled...
    meeting_type       text,
    router             text,                                   -- the scheduling route that booked it, when the CRM says
    booked_by_prospect boolean,
    scheduled_by       text,
    source_system      text NOT NULL DEFAULT 'salesforce',
    raw                jsonb,
    synced_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_crm_events_contact ON crm_events (crm_contact_id);
CREATE INDEX IF NOT EXISTS idx_crm_events_lead    ON crm_events (crm_lead_id);
CREATE INDEX IF NOT EXISTS idx_crm_events_starts  ON crm_events (starts_at);
CREATE INDEX IF NOT EXISTS idx_crm_events_created ON crm_events (created_at);

-- Field history: one row per change the CRM tracked. Stage changes give the
-- stage-by-stage clock and the stages a deal ever reached; owner changes the
-- hand-offs. Unbiased, unlike a "history" object that only lists deals still
-- open or recently closed.
CREATE TABLE IF NOT EXISTS crm_field_history (
    id            text PRIMARY KEY,
    object        text NOT NULL,                               -- opportunity | lead | contact | account
    record_id     text NOT NULL,                               -- the CRM id of the changed record
    field         text NOT NULL,
    old_value     text,
    new_value     text,
    changed_at    timestamptz NOT NULL,
    changed_by    text,
    source_system text NOT NULL DEFAULT 'salesforce',
    raw           jsonb,
    synced_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_crm_fh_record ON crm_field_history (record_id, field, changed_at);
CREATE INDEX IF NOT EXISTS idx_crm_fh_field  ON crm_field_history (field, changed_at);
