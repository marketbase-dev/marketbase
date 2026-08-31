-- MarketBase schema, migration 005 — Convenience views
--
-- Views that downstream agents and humans will query the most.

-- Who's currently in any active campaign?
CREATE OR REPLACE VIEW v_in_campaign AS
SELECT
    l.id          AS lead_id,
    l.name,
    l.current_title,
    l.current_company,
    l.country,
    c.name        AS campaign,
    c.channel,
    cm.status     AS campaign_status,
    cm.last_status_at,
    cm.uploaded_at,
    cm.staged_at
FROM campaign_members cm
JOIN leads l ON l.id = cm.lead_id
JOIN campaigns c ON c.id = cm.campaign_id
WHERE cm.status NOT IN ('completed', 'removed_blocked', 'removed_other');

-- Who is staged + ready to upload to a campaign tool?
CREATE OR REPLACE VIEW v_staged_for_upload AS
SELECT
    l.id          AS lead_id,
    l.name,
    l.current_title,
    l.current_company,
    l.linkedin_url,
    l.country,
    c.name        AS campaign,
    c.channel,
    cm.staged_at
FROM campaign_members cm
JOIN leads l ON l.id = cm.lead_id
JOIN campaigns c ON c.id = cm.campaign_id
WHERE cm.status = 'staged';

-- Who is qualified by the latest run and not yet assigned to any campaign?
CREATE OR REPLACE VIEW v_qualified_no_campaign AS
SELECT
    l.id          AS lead_id,
    l.name,
    l.current_title,
    l.current_company,
    l.country,
    q.persona,
    q.seniority,
    q.qualifier_version,
    q.qualified_at
FROM lead_current_qualification q
JOIN leads l ON l.id = q.lead_id
WHERE q.qualified = true
  AND NOT EXISTS (
      SELECT 1 FROM campaign_members cm
      WHERE cm.lead_id = l.id
        AND cm.status NOT IN ('removed_blocked', 'removed_other')
  );

-- Who replied across all campaigns? (warmest set)
CREATE OR REPLACE VIEW v_replied AS
SELECT
    l.id          AS lead_id,
    l.name,
    l.current_title,
    l.current_company,
    l.linkedin_url,
    c.name        AS campaign,
    cm.last_status_at AS replied_at
FROM campaign_members cm
JOIN leads l ON l.id = cm.lead_id
JOIN campaigns c ON c.id = cm.campaign_id
WHERE cm.status IN ('replied', 'meeting_booked');

-- Lead-level provenance summary: which sources have we seen for this lead?
CREATE OR REPLACE VIEW v_lead_provenance AS
SELECT
    l.id                                AS lead_id,
    l.name,
    l.linkedin_url,
    array_agg(DISTINCT s.source_type)   AS source_types,
    array_agg(DISTINCT s.source_label)  AS source_labels,
    MIN(s.recorded_at)                  AS first_seen_at,
    MAX(s.recorded_at)                  AS last_seen_at,
    COUNT(*)                            AS source_count
FROM leads l
LEFT JOIN lead_sources s ON s.lead_id = l.id
GROUP BY l.id, l.name, l.linkedin_url;

-- Per-campaign roll-up: how many at each status?
CREATE OR REPLACE VIEW v_campaign_funnel AS
SELECT
    c.name      AS campaign,
    c.channel,
    cm.status,
    COUNT(*)    AS n
FROM campaigns c
LEFT JOIN campaign_members cm ON cm.campaign_id = c.id
GROUP BY c.name, c.channel, cm.status
ORDER BY c.name, cm.status;
