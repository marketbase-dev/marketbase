-- MarketBase schema, migration 034 — register the find_competitor_salesperson source type
--
-- Leads discovered by the competitor-salesperson monitoring flow (Blitz-first
-- people search across a flagged competitor's LinkedIn company page, filtered to
-- client-facing sales / pre-sales / sales-leadership titles, then qualified by
-- the territory + role KEEP/CUT rules). Distinct from find_senior_execs, which
-- targets leadership; this targets the rep layer whose LinkedIn connections are
-- prospects actively evaluating the market.

INSERT INTO source_types (name, description, purpose, raw_data_shape, examples, created_by) VALUES
  ('find_competitor_salesperson',
   'Sales / pre-sales / sales-leadership person at a flagged competitor, discovered via Blitz (or Apollo) people search.',
   'Use for reps found by marketbase-build-competitor-rep-list against a competitor company. We monitor these because a competitor rep''s LinkedIn connections reveal buyers actively evaluating the market. raw_data carries the flattened Blitz/Apollo person record plus the qualification verdict (tags, territory tier, keep/cut/deprioritize).',
   'flattened person: name, title, linkedin_url, country/city/state, organization_name + linkedin_url; plus tags (from competitor_targeting.tag_person), tier_one (bool), verdict.',
   'marketbase-build-competitor-rep-list for Amplifier Security (2026-07).',
   'migration-034')
ON CONFLICT (name) DO NOTHING;
