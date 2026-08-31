-- MarketBase schema, migration 012 — Stronger tiebreaker on lead_current_qualification
--
-- The original view picked the latest qualification per lead by `qualified_at`
-- alone. But `qualified_at = now()` returns the transaction-start time, so
-- multiple qualifications inserted in the same transaction all share a
-- timestamp and the tiebreaker is arbitrary.
--
-- Fix: also break ties by `id DESC`. UUIDs aren't strictly monotonic, but
-- combined with timestamp this is "good enough" for the most-recent-INSERT
-- semantics the view promises. For workflows that need deterministic order,
-- run a different qualifier_name with separate timestamps.

CREATE OR REPLACE VIEW lead_current_qualification AS
SELECT DISTINCT ON (lead_id)
    lead_id, qualifier_name, qualifier_version,
    qualified, persona, reason, disqualified_reason,
    seniority, still_employed, still_employed_reason,
    employee_count, cloud_sec_count, qualified_at
FROM lead_qualifications
ORDER BY lead_id, qualified_at DESC, id DESC;
