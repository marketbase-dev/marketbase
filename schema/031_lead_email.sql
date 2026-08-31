-- MarketBase schema, migration 031 — prospect email on leads
--
-- Until now the schema stored no email for the *prospect*. The only email
-- columns were our side (campaigns.sender_email, outbound_operators.email).
-- When a prospect self-books (Calendly) or hands over an address in-thread,
-- that email lived only on the calendar event — invisible to MarketBase and to
-- downstream consumers (e.g. the external booked-meeting → HubSpot deal
-- process, meeting reminders, deliverability follow-ups).
--
-- This adds an optional email field on the lead. NULL until we learn it.
-- No format constraint (LinkedIn URN identity remains the canonical key —
-- email is supplementary contact info, not identity).

ALTER TABLE leads ADD COLUMN IF NOT EXISTS email text;

COMMENT ON COLUMN leads.email IS
  'Prospect work/contact email when known (from a Calendly self-booking, an '
  'in-thread hand-over, or enrichment). Supplementary contact info, NOT '
  'identity — member_urn remains the canonical person key. NULL until learned.';
