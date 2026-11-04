-- 074_bookings_report_storage.sql
--
-- Bookings report: one storage shape for every instance (ticket "Bookings
-- report, one MarketBase storage and skill for every instance", 2026-11-03;
-- the operator's decision of 2026-11-04: the "already in pipeline" rule is a
-- versioned processor per instance, parameters in the table, never in a file).
--
-- Until now the two bookings analyses derived everything from raw cached pages
-- (enrichment_calls) and scratchpad JSON, so the same question got a different
-- shape per client. From here the shared process step reads ONLY modelled
-- tables, which every instance has after this migration:
--
--   crm_opportunities / crm_opportunity_contacts
--       The client's CRM deals and their contact roles, in the shape the
--       existing CRM sync script has written since 2026-10-03. Two instances
--       had them from scripts; the other four did not.
--       Column names are CRM-neutral; `source_system` names the CRM and `raw`
--       keeps the vendor record (Opportunity Source, lead source, campaign
--       fields and whatever else a report needs later).
--   lead_meetings (new columns)
--       A booking as the report sees it: when it was BOOKED (booked_at, the
--       dial or the opp creation), by whom, from which system, which deal it
--       led to. scheduled_at stays the meeting instant. `calendar_event_id` is
--       the external id in that system (dialer call id, CRM opportunity id,
--       scheduling-tool event id), unique per source so a re-run upserts.
--   lead_touches
--       One row per touch on a person: a dial, an email, a LinkedIn step, a
--       logged meeting, a sequence entry. `touched_at` is when it happened,
--       `logged_at` when the source created the record (a CRM meeting created
--       before the tip for a date after it is "booked before the tip").
--   tag_definitions namespace bookings_report
--       The assertion keys the process step writes per meeting (category,
--       in_pipeline_reason, mechanism). The meeting subject on `assertions`
--       (subject_meeting_id) lands in 072 from the parallel thread; until it
--       is present the process step keeps its per-meeting rows in the
--       bookings_meeting_rows report input and writes no assertions.
--
-- Additive only. Safe to re-run.

CREATE TABLE IF NOT EXISTS crm_opportunities (
    id                 text PRIMARY KEY,                    -- the CRM's opportunity or deal id
    name               text,
    account_id         text,
    account_name       text,
    stage              text,
    is_closed          boolean,
    is_won             boolean,
    acv                numeric,
    probability        numeric,
    created_date       date,
    close_date         date,
    owner_name         text,
    sourced_by_id      text,
    sourced_by_name    text,
    marketing_campaign text,                                -- the campaign id the CRM credits the deal to
    source_system      text NOT NULL DEFAULT 'salesforce',
    raw                jsonb,                               -- the vendor record as fetched
    synced_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_crm_opps_campaign   ON crm_opportunities (marketing_campaign);
CREATE INDEX IF NOT EXISTS idx_crm_opps_sourced_by ON crm_opportunities (sourced_by_id);
CREATE INDEX IF NOT EXISTS idx_crm_opps_account    ON crm_opportunities (account_id);
CREATE INDEX IF NOT EXISTS idx_crm_opps_created    ON crm_opportunities (created_date);

CREATE TABLE IF NOT EXISTS crm_opportunity_contacts (
    opportunity_id text NOT NULL REFERENCES crm_opportunities(id) ON DELETE CASCADE,
    contact_id     text NOT NULL,                           -- the CRM's contact id
    role           text,
    is_primary     boolean,
    PRIMARY KEY (opportunity_id, contact_id)
);
CREATE INDEX IF NOT EXISTS idx_crm_opp_contacts_contact ON crm_opportunity_contacts (contact_id);

-- lead_meetings: the booking view of a meeting.
ALTER TABLE lead_meetings ADD COLUMN IF NOT EXISTS booked_at      timestamptz;   -- the dial that set it, or the opp's created date
ALTER TABLE lead_meetings ADD COLUMN IF NOT EXISTS booked_by      text;          -- rep name as the source records it
ALTER TABLE lead_meetings ADD COLUMN IF NOT EXISTS booked_by_role text;          -- the rep's role in the CRM at sync time
ALTER TABLE lead_meetings ADD COLUMN IF NOT EXISTS kind           text;          -- 'booking' (a dial or form set it) | 'opportunity' (only the deal exists)
ALTER TABLE lead_meetings ADD COLUMN IF NOT EXISTS opportunity_id text;          -- crm_opportunities.id when linked (no FK: the deal may not be synced yet)
ALTER TABLE lead_meetings ADD COLUMN IF NOT EXISTS crm_contact_id text;          -- the CRM contact the meeting is on
ALTER TABLE lead_meetings ADD COLUMN IF NOT EXISTS raw            jsonb;         -- the source record
CREATE UNIQUE INDEX IF NOT EXISTS uq_lead_meetings_source_event
    ON lead_meetings (source, calendar_event_id) WHERE calendar_event_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_lead_meetings_booked ON lead_meetings (lead_id, booked_at);

-- lead_touches: every touch on a person we can see, one row each.
CREATE TABLE IF NOT EXISTS lead_touches (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    lead_id        uuid NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    touched_at     timestamptz NOT NULL,                    -- when it happened
    logged_at      timestamptz,                             -- when the source created the record (NULL = same as touched_at)
    rep            text NOT NULL,                           -- who touched, as the source names them
    rep_role       text,
    kind           text NOT NULL CHECK (kind IN ('call', 'email', 'linkedin', 'sms', 'chat', 'meeting', 'sequence_enter', 'task')),
    direction      text CHECK (direction IS NULL OR direction IN ('outbound', 'inbound')),
    sequence_name  text,                                    -- the cadence it belonged to, when the source names it
    disposition    text,                                    -- call outcome as the source records it
    subject        text,
    source         text NOT NULL,                           -- the system that recorded it: the dialer, a CRM task, a CRM event, CRM field history, an outreach tool
    external_id    text,                                    -- the source's record id; (source, external_id) is unique
    crm_contact_id text,
    raw            jsonb,
    recorded_at    timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_lead_touches_source_external
    ON lead_touches (source, external_id) WHERE external_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_lead_touches_lead_time ON lead_touches (lead_id, touched_at);
CREATE INDEX IF NOT EXISTS idx_lead_touches_rep       ON lead_touches (rep, touched_at);

-- The tip source every instance's collector writes: a CRM campaign membership.
-- 040 registered it where a CRM sync existed; the shared process step reads tips
-- from lead_sources rows of this type on every instance, so it exists everywhere.
INSERT INTO source_types (name, description, purpose, raw_data_shape, examples, created_by) VALUES
  ('sf_campaign_member',
   'A member of a client CRM campaign that holds delivered prospects (the tips), or of any other campaign the person was later added to.',
   'Use for people found as CampaignMembers of a client Salesforce campaign. raw_data carries the CampaignMember record: campaign id+name, contact id, name, title, email, company, phone/mobile, member status, CreatedDate (the date delivered = the tip date) and CreatedBy. The bookings report reads the tip from raw_data.added (fallback: source_date).',
   'member record: member_id or campaign_member_id, campaign_id, campaign, contact_id, name, title, email, company, phone, mobile, phone_digits, mobile_digits, member_status, added, added_by.',
   'The delivery campaigns of a client (2026); event and ad campaigns the same people were added to later.',
   'migration-074')
ON CONFLICT (name) DO NOTHING;

-- The assertion keys the process step writes per meeting. `layer` is limited to
-- attribute|audience|state|relationship by 012's CHECK; a derived classification
-- is an attribute of the meeting. entity_type 'lead_meeting' is new here.
INSERT INTO tag_definitions (namespace, key, entity_type, layer, description) VALUES
  ('bookings_report', 'category', 'lead_meeting', 'attribute',
   'Where the meeting sits against the tip, per the bookings-report-rules processor that produced it: booked before the tip | account or person already in pipeline at the tip | booked by the Tipline rep, tagged | booked by the Tipline rep, untagged | another route after a Tipline touch | another route, no Tipline touch found. One assertion per meeting (subject_meeting_id, 072); processor_id names the rule version. An operator''s override is a further assertion on the same key, asserted_by the operator.'),
  ('bookings_report', 'in_pipeline_reason', 'lead_meeting', 'attribute',
   'For category "already in pipeline": the first reason that applied, read as of the tip date: open_deal | customer | person_worked | account_worked | older_booking. Windows come from the processor inputs.'),
  ('bookings_report', 'mechanism', 'lead_meeting', 'attribute',
   'How the meeting was booked when the source says: phone | email | self_booked_form | agency_page | ae_invite | not_logged. Nullable: an instance without a self-booking tool has nothing to say here.')
ON CONFLICT (namespace, key, entity_type) DO NOTHING;
