-- MarketBase schema, migration 014 — Expose full_result on lead_current_qualification
--
-- The view's signature was identity-only — callers couldn't drill into the
-- decision payload without joining back to lead_qualifications. Many reports
-- want fields out of full_result (e.g. engagement_type, employment_type),
-- so expose it here.
--
-- View definition is otherwise identical to migration 013.

-- CREATE OR REPLACE VIEW only allows ADDING columns at the END (cannot
-- reorder existing columns), so full_result goes at the end here.
CREATE OR REPLACE VIEW lead_current_qualification AS
SELECT DISTINCT ON (lead_id)
    lead_id, qualifier_name, qualifier_version,
    qualified, persona, reason, disqualified_reason,
    seniority, still_employed, still_employed_reason,
    employee_count, cloud_sec_count, qualified_at,
    full_result
FROM lead_qualifications
ORDER BY lead_id, qualified_at DESC, id DESC;
