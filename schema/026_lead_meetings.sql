-- MarketBase schema, migration 026 — Scheduled meetings + reminder tracking
--
-- Until now a booked call only existed as `campaign_members.status =
-- 'meeting_booked'` plus the event on the host's Unipile calendar. Neither is
-- queryable as "a meeting at time T for lead L", so the pending-conversations
-- review had no way to surface meeting reminders.
--
-- `lead_meetings` stores the absolute instant of each scheduled call so the
-- review (review_pending_replies.py) can compute reminder windows (T-4 / T-2 /
-- T-0 days out) and the `reminders_sent` log lets it avoid double-sending the
-- same reminder across runs.
--
-- One row per (lead, scheduled_at): a reschedule is a new row (old one set to
-- status='rescheduled' or 'cancelled'); the latest non-terminal row is "the"
-- upcoming meeting. Times are stored as timestamptz (absolute), with
-- `scheduled_tz` kept only for human-readable display in the prospect's /
-- host's local zone.

CREATE TABLE IF NOT EXISTS lead_meetings (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    lead_id         uuid NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    campaign_id     uuid REFERENCES campaigns(id) ON DELETE SET NULL,
    scheduled_at    timestamptz NOT NULL,            -- absolute instant of the call
    scheduled_tz    text,                            -- IANA tz for display (e.g. 'America/New_York')
    host            text,                            -- who runs it: 'dana' | 'bob' | 'eyar'
    title           text,                            -- e.g. 'Acme <> Baxter - Nick'
    status          text NOT NULL DEFAULT 'scheduled', -- scheduled | held | cancelled | no_show | rescheduled
    reminders_sent  jsonb NOT NULL DEFAULT '[]'::jsonb, -- [{kind:'T-4'|'T-2'|'T-0', at:ts, channel, message_id}, ...]
    source          text,                            -- 'calendar:unipile' | 'manual' | 'calendly'
    calendar_event_id text,                          -- Unipile/Google event id when known (dedupe / RSVP lookup)
    created_at      timestamptz NOT NULL DEFAULT now(),
    created_by      text,
    notes           text
);

CREATE INDEX IF NOT EXISTS idx_lead_meetings_lead     ON lead_meetings (lead_id);
CREATE INDEX IF NOT EXISTS idx_lead_meetings_upcoming ON lead_meetings (status, scheduled_at);

-- Convenience view: the single current upcoming meeting per lead (soonest
-- future call that's still 'scheduled'). Mirrors the lead_current_* pattern.
CREATE OR REPLACE VIEW lead_upcoming_meeting AS
SELECT DISTINCT ON (lead_id)
       id, lead_id, campaign_id, scheduled_at, scheduled_tz, host, title,
       status, reminders_sent, source, calendar_event_id, created_at, notes
FROM lead_meetings
WHERE status = 'scheduled' AND scheduled_at >= now()
ORDER BY lead_id, scheduled_at ASC, id DESC;
