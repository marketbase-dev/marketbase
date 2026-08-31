-- Replace the rigid `source_type` enum with a free-text column backed by
-- a `source_types` registry. Same pattern as `processors`: each row in
-- the registry describes WHAT a source_type means, WHEN to use it, and
-- what shape its raw_data takes. Free-text + FK gives validation without
-- needing a schema migration every time a new sourcing method appears.

-- 1) Registry table
CREATE TABLE IF NOT EXISTS source_types (
    name             text PRIMARY KEY,
    description      text NOT NULL,           -- one-line "what it is"
    purpose          text,                    -- longer "when to use this"
    raw_data_shape   text,                    -- what fields to expect in lead_sources.raw_data
    examples         text,                    -- prior or canonical uses
    created_at       timestamptz NOT NULL DEFAULT now(),
    created_by       text,
    deprecated_at    timestamptz,
    superseded_by    text REFERENCES source_types(name) ON UPDATE CASCADE
);

-- 2) Seed the registry with every value the enum currently knows about,
-- with honest descriptions of how each was/is used. Idempotent.
INSERT INTO source_types (name, description, purpose, created_by) VALUES

  ('manual_add',
   'Lead added by a human, no automated source.',
   'Use when a person enters a lead directly (paste from a contact, hand-curated wishlist).  No raw_data structure is expected — raw_data may be empty or contain ad-hoc notes.',
   'migration-020'),

  ('linkedin_post_engagement',
   'Reactor or commenter on a LinkedIn post.',
   'Use when a lead was discovered by walking the engagements of a known post (typically via marketbase-engagers-research or find-post-engagers). raw_data carries the engagement payload returned by the LinkedIn API: post_urn, reaction_type or comment_text, engaged_at, etc.',
   'migration-020'),

  ('linkedin_people_search',
   'CSV/XLSX of leads discovered via a LinkedIn People search (the search was performed externally — in the browser, Apollo, a scraper, etc. — then exported and ingested here).',
   'Use when a lead was discovered by running a LinkedIn People search for some ICP-defining criteria (title, geography, keyword, recently-posted-about-topic-X, etc.) and the resulting list was exported as CSV/XLSX. raw_data carries the entire row as JSONB; column names depend on the search tool but typically include name, headline, current_title, current_company, linkedin_url.',
   'migration-020'),

  ('apollo_export',
   'CSV/XLSX export from Apollo.io.',
   'Use when leads come from an Apollo.io search (apollo-account-list, apollo-people-search, manual download). raw_data carries the Apollo row as JSONB.',
   'migration-020'),

  ('engagers_research',
   'Output of marketbase-engagers-research — engagers of competitor/self senior execs.',
   'Use for leads discovered by the marketbase-engagers-research skill: people who reacted to or commented on posts by a flagged company''s senior execs. raw_data carries the per-post engagement payload + the discovery context (which exec, which post).',
   'migration-020'),

  ('find_senior_execs',
   'Output of find-senior-execs skill — founders/CXOs/VPs/Heads/Directors at a target company.',
   'Use for leads discovered by find-senior-execs against a specific target company. raw_data carries the per-row LinkedIn profile data returned by the Fresh LinkedIn Profile Data API.',
   'migration-020'),

  ('dripify_campaign_export',
   'CSV/XLSX export of Dripify outreach-campaign members.',
   'Use when ingesting the members of a Dripify campaign (people the campaign sent messages to). raw_data carries the Dripify row.',
   'migration-020'),

  ('dripify_reply_export',
   'CSV/XLSX export of Dripify reply senders.',
   'Use when ingesting Dripify replies (people who responded to outreach). raw_data carries the Dripify reply row including message text.',
   'migration-020'),

  ('buyer_monitor_likely_to_connect',
   'Leads from a "buyer monitor" workflow — periodic scans for prospects in the customer''s LinkedIn network who match an ICP and are reachable via warm intro.',
   'Use for leads that surfaced from an automated monitoring pipeline (vs a one-shot search). raw_data carries the monitor snapshot row including the warm-intro path and any scoring signals the monitor computed.',
   'migration-020'),

  ('founder_network_export',
   'Leads exported from a customer''s own LinkedIn network — typically a founder''s or exec''s 1st-degree connections — for outbound or buying-committee analysis.',
   'Use when ingesting a customer-owned network export (CSV or scrape) to seed warm-intro flows. raw_data carries the network-export row; expect at minimum a LinkedIn URL and the network owner''s identifier.',
   'migration-020'),

  ('marketing_consultants_search',
   'DEPRECATED. Superseded by linkedin_people_search. Originally used by Acme-AI''s consultants-CSV ingest before the source_type taxonomy was generalized.',
   'Do NOT use for new ingests — use linkedin_people_search instead. Kept for FK reference of historical rows that already used this value.',
   'migration-020')

ON CONFLICT (name) DO NOTHING;

-- 2b) Mark marketing_consultants_search as deprecated + superseded
UPDATE source_types
SET deprecated_at = now(),
    superseded_by = 'linkedin_people_search'
WHERE name = 'marketing_consultants_search'
  AND deprecated_at IS NULL;

-- 3) Convert lead_sources.source_type from enum → text. Postgres won't
-- alter a column referenced by views, so drop the dependent views first;
-- recreated below with identical SQL (the array_agg() result type
-- changes from source_type[] → text[], which is what we want).
DROP VIEW IF EXISTS v_lead_pipeline_state;
DROP VIEW IF EXISTS v_lead_provenance;

DO $$
BEGIN
  IF (SELECT data_type FROM information_schema.columns
      WHERE table_name='lead_sources' AND column_name='source_type') = 'USER-DEFINED' THEN
    ALTER TABLE lead_sources
      ALTER COLUMN source_type TYPE text USING source_type::text;
  END IF;
END $$;

CREATE VIEW v_lead_provenance AS
 SELECT l.id AS lead_id,
        l.name,
        l.linkedin_url,
        array_agg(DISTINCT s.source_type)  AS source_types,
        array_agg(DISTINCT s.source_label) AS source_labels,
        min(s.recorded_at) AS first_seen_at,
        max(s.recorded_at) AS last_seen_at,
        count(*) AS source_count
   FROM leads l
   LEFT JOIN lead_sources s ON s.lead_id = l.id
  GROUP BY l.id, l.name, l.linkedin_url;

CREATE VIEW v_lead_pipeline_state AS
 SELECT l.id AS lead_id,
        l.name,
        l.linkedin_url,
        l.current_title,
        l.current_company,
        l.country,
        (SELECT array_agg(DISTINCT s.source_type ORDER BY s.source_type)
           FROM lead_sources s WHERE s.lead_id = l.id) AS source_types,
        (SELECT array_agg(DISTINCT s.source_label ORDER BY s.source_label)
           FROM lead_sources s WHERE s.lead_id = l.id) AS source_labels,
        (SELECT min(s.recorded_at)
           FROM lead_sources s WHERE s.lead_id = l.id) AS first_seen_at,
        (SELECT array_agg(t.tag ORDER BY t.tag)
           FROM lead_tags t WHERE t.lead_id = l.id) AS tags,
        q.qualifier_name,
        q.qualifier_version,
        q.qualified,
        q.persona,
        q.qualified_at,
        (SELECT array_agg(c.name ORDER BY c.name)
           FROM campaign_members cm
           JOIN campaigns c ON c.id = cm.campaign_id
          WHERE cm.lead_id = l.id
            AND (cm.status <> ALL (ARRAY['completed'::campaign_member_status,
                                         'removed_blocked'::campaign_member_status,
                                         'removed_other'::campaign_member_status]))
        ) AS active_campaigns
   FROM leads l
   LEFT JOIN lead_current_qualification q ON q.lead_id = l.id;

-- 4) Move existing marketing_consultants_search rows onto the new name.
-- (Acme-AI is the only client with this value; safe no-op for others.)
UPDATE lead_sources
SET source_type = 'linkedin_people_search'
WHERE source_type = 'marketing_consultants_search';

-- 5) Drop the (now-unused) enum type
DROP TYPE IF EXISTS source_type;

-- 6) Add FK constraint enforcing source_type values exist in the registry.
-- ON UPDATE CASCADE so future renames of a source_type propagate.
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.table_constraints
    WHERE table_name='lead_sources' AND constraint_name='lead_sources_source_type_fkey'
  ) THEN
    ALTER TABLE lead_sources
      ADD CONSTRAINT lead_sources_source_type_fkey
      FOREIGN KEY (source_type) REFERENCES source_types(name)
      ON UPDATE CASCADE;
  END IF;
END $$;
