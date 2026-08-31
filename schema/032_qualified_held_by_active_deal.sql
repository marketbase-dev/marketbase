-- 032: surface leads that WOULD be qualified but are currently held out only
-- because their employer has an active/won deal (the marketbase-policy@active_deal_dq
-- override won the lead_current_qualification tiebreaker). Use this to retrieve
-- them once a deal closes-lost: re-run the client's real qualifier on these leads.
--
-- Generic across clients: "real qualifier" = the latest NON-policy qualification.

CREATE OR REPLACE VIEW v_qualified_held_by_active_deal AS
WITH real_q AS (
    SELECT DISTINCT ON (lead_id)
           lead_id, qualified, persona, qualifier_name, qualifier_version, qualified_at
    FROM lead_qualifications
    WHERE qualifier_name <> 'marketbase-policy'          -- the real classifier, not a policy override
    ORDER BY lead_id, qualified_at DESC, id DESC
)
SELECT lcq.lead_id,
       l.linkedin_url,
       l.name,
       l.current_title,
       l.current_company,
       real_q.persona                        AS would_be_persona,
       real_q.qualifier_name                 AS real_qualifier,
       real_q.qualifier_version              AS real_qualifier_version,
       lcq.reason                            AS deal_reason,        -- company_in_open_deal | company_is_customer
       lcq.full_result -> 'deal_companies'   AS deal_companies,
       lcq.full_result -> 'open_stage'       AS open_stage,
       lcq.qualified_at                      AS held_since
FROM lead_current_qualification lcq
JOIN real_q          ON real_q.lead_id = lcq.lead_id
JOIN leads l         ON l.id = lcq.lead_id
WHERE lcq.qualifier_name = 'marketbase-policy'
  AND lcq.qualifier_version = 'active_deal_dq_v1.0'
  AND lcq.qualified = false
  AND real_q.qualified = true;

COMMENT ON VIEW v_qualified_held_by_active_deal IS
  'Leads whose real qualifier said qualified=true but are currently DQ''d solely by the active-deal policy. Retrieve (re-qualify) when the deal closes-lost.';
