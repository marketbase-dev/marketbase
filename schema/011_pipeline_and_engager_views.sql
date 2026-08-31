-- MarketBase schema, migration 011 — Pipeline state + thought-leader engager views
--
-- Two read-only views that downstream skills and reports rely on:
--
--   v_lead_pipeline_state            one row per lead, rolling up the
--                                    provenance / qualifications / tags /
--                                    campaign memberships so you can see the
--                                    full picture in a single SELECT.
--
--   v_thought_leader_engager_report  for every lead tagged 'thought-leader',
--                                    tallies engagers grouped by the latest
--                                    qualification persona. Used by the
--                                    Acme-AI engager-report workflow but
--                                    generic enough for any client that uses
--                                    the same tag + persona convention.

-- ── v_lead_pipeline_state ──────────────────────────────────────────────────

CREATE OR REPLACE VIEW v_lead_pipeline_state AS
SELECT
    l.id                                                                AS lead_id,
    l.name,
    l.linkedin_url,
    l.current_title,
    l.current_company,
    l.country,
    -- Provenance
    (SELECT array_agg(DISTINCT s.source_type ORDER BY s.source_type)
       FROM lead_sources s WHERE s.lead_id = l.id)                      AS source_types,
    (SELECT array_agg(DISTINCT s.source_label ORDER BY s.source_label)
       FROM lead_sources s WHERE s.lead_id = l.id)                      AS source_labels,
    (SELECT MIN(s.recorded_at) FROM lead_sources s WHERE s.lead_id = l.id) AS first_seen_at,
    -- Tags
    (SELECT array_agg(t.tag ORDER BY t.tag)
       FROM lead_tags t WHERE t.lead_id = l.id)                         AS tags,
    -- Latest qualification (per existing lead_current_qualification view)
    q.qualifier_name,
    q.qualifier_version,
    q.qualified,
    q.persona,
    q.qualified_at,
    -- Active campaign memberships (any campaign type)
    (SELECT array_agg(c.name ORDER BY c.name)
       FROM campaign_members cm
       JOIN campaigns c ON c.id = cm.campaign_id
      WHERE cm.lead_id = l.id
        AND cm.status NOT IN ('completed', 'removed_blocked', 'removed_other'))
                                                                        AS active_campaigns
FROM leads l
LEFT JOIN lead_current_qualification q ON q.lead_id = l.id;

-- ── v_thought_leader_engager_report ────────────────────────────────────────
-- For each lead tagged 'thought-leader', count the unique engagers grouped
-- by the engager's latest qualification persona.
--
-- An "engager" is any lead with a post_engagement row on a post posted by
-- the thought leader. We join through posts.poster_linkedin_url to handle
-- both cached posts and ones ingested via engagers-research.
--
-- The persona column is whatever the latest qualification produced — for
-- Acme-AI this will be the 4-tier engagement_type values; for other clients
-- it'll be their own persona convention.

CREATE OR REPLACE VIEW v_thought_leader_engager_report AS
SELECT
    tl.id                                            AS thought_leader_lead_id,
    tl.name                                          AS thought_leader_name,
    tl.linkedin_url                                  AS thought_leader_linkedin_url,
    COALESCE(eq.persona, '(unqualified)')            AS engager_persona,
    COUNT(DISTINCT eng.id)                           AS engager_count,
    COUNT(DISTINCT p.id)                             AS posts_engaged
FROM leads tl
JOIN lead_tags tlt ON tlt.lead_id = tl.id AND tlt.tag = 'thought-leader'
JOIN posts p ON lower(p.poster_linkedin_url) = lower(tl.linkedin_url)
JOIN post_engagements pe ON pe.post_id = p.id
JOIN leads eng ON eng.id = pe.lead_id AND eng.id <> tl.id  -- exclude self-engagements
LEFT JOIN lead_current_qualification eq ON eq.lead_id = eng.id
GROUP BY tl.id, tl.name, tl.linkedin_url, eq.persona;

COMMENT ON VIEW v_thought_leader_engager_report IS
'One row per (thought leader × engager persona). Counts unique engagers and the number of posts each leader had engaged with by people in that persona.';
