-- MarketBase schema, migration 012 — Pending-removal state + sequencer-facing view
--
-- Adds an explicit `pending_removal` status to the campaign_member lifecycle
-- and a view (`v_pending_removals`) that the sequencer reads to learn which
-- leads should be pulled out of an outreach tool (Smartlead, Smartlead, Dripify, …).
--
-- Two sources of "should be removed":
--   (1) Explicit  — `campaign_members.status = 'pending_removal'`. Used for
--       manual marks or removals that don't come from a disqualification
--       (e.g. policy: "lead is at a flagged competitor", staffing change, etc).
--   (2) Computed  — lead is in an active campaign status AND their
--       latest qualification row says `qualified=false`. The view surfaces
--       these without anyone having to flip the status preemptively.
--
-- The sequencer's contract with the MarketBase:
--   • To learn what to remove:  SELECT * FROM v_pending_removals;
--   • After successfully removing in the upstream tool:
--       UPDATE campaign_members
--          SET status='removed_other',
--              last_status_source='sequencer:<sequencer-name>:<reason>'
--        WHERE id = <campaign_member_id>;
--     The trigger trg_cm_status_change appends to status_history automatically.

-- ── 1. Add pending_removal to the enum ───────────────────────────────────────
-- IF NOT EXISTS is supported on PG 12+; wrap in DO/EXCEPTION just in case.
DO $$ BEGIN
    ALTER TYPE campaign_member_status ADD VALUE IF NOT EXISTS 'pending_removal' BEFORE 'removed_other';
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- ── 2. Sequencer-facing view ─────────────────────────────────────────────────
CREATE OR REPLACE VIEW v_pending_removals AS
SELECT
    cm.id              AS campaign_member_id,
    cm.lead_id,
    cm.campaign_id,
    cm.status,
    cm.uploaded_at,
    cm.last_status_at,
    l.linkedin_url,
    l.name             AS lead_name,
    c.name             AS campaign_name,
    q.qualified,
    q.persona,
    q.disqualified_reason,
    q.qualifier_name,
    q.qualifier_version,
    q.qualified_at,
    CASE
        WHEN cm.status::text = 'pending_removal' THEN 'manual'
        ELSE 'auto:disqualified'
    END                AS removal_source
FROM campaign_members cm
JOIN leads l                                  ON l.id = cm.lead_id
JOIN campaigns c                              ON c.id = cm.campaign_id
LEFT JOIN lead_current_qualification q        ON q.lead_id = cm.lead_id
WHERE cm.status::text = 'pending_removal'
   OR (
        cm.status::text IN ('staged',
                            'uploaded',
                            'connection_requested',
                            'connection_accepted',
                            'message_sent')
        AND q.qualified IS FALSE
   );
