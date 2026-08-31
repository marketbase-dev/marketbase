-- MarketBase schema, migration 021 — Add `disqualified` to campaign_member_status
--
-- Closes a gap where leads disqualified at the lead level
-- (lead_qualifications.qualified=false) had no semantically matching terminal
-- status on the campaign_members side. Previously the convention was to use
-- `removed_other` with the reason in `last_status_source`, but that made the
-- DQ case undiscoverable and indistinguishable from other manual removals
-- without string-matching against text.
--
-- Terminal-status contract (see CONVENTIONS.md → "Campaign member terminal
-- statuses"):
--   • disqualified     — removed because the lead's current qualification
--                        flipped to qualified=false. Reason audit trail lives
--                        in lead_current_qualification.reason +
--                        disqualified_reason.
--   • removed_blocked  — the upstream platform (Smartlead / Smartlead / LinkedIn)
--                        rejected the action. Out of our control.
--   • removed_other    — catch-all (manual deletion, dedupe, list cleanup,
--                        user request, etc.).
--
-- Position in enum: inserted AFTER removed_blocked so terminal-removal
-- statuses cluster together.

DO $$ BEGIN
    ALTER TYPE campaign_member_status
        ADD VALUE IF NOT EXISTS 'disqualified' AFTER 'removed_blocked';
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
