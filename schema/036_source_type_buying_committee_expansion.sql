-- MarketBase schema, migration 036 — register the buying_committee_expansion source type
--
-- Leads discovered by expanding OUT from an already-eligible person to the rest of
-- their organisation's buying committee: once one contact at an org is qualified
-- (their system says "put this person in the SDR queue"), we add every other
-- Director+ person in the finance / accounting function at that same company.
--
-- Distinct from find_senior_execs (leadership at a *target* company, no seed) and
-- from find_competitor_salesperson (reps at a competitor). Here the discovery unit
-- is a SEED PERSON, and the raw_data always records which seed(s) produced the row,
-- so the expansion ratio and per-seed yield stay re-derivable.

INSERT INTO source_types (name, description, purpose, raw_data_shape, examples, created_by) VALUES
  ('buying_committee_expansion',
   'Director+ peer in the same function at the same company as an already-eligible seed lead, discovered via Blitz people search.',
   'Use for people found by marketbase-expand-buying-committee. Rationale: when a competitor is being evaluated at an org, the whole buying committee is involved in that evaluation, not just the one person who tripped the signal. raw_data carries the seed attribution (which seed lead + seed company), the local rank/function verdict that kept the row, and the raw Blitz person record.',
   'seed_linkedin_urls[], seed_company, company_linkedin_url, rank (c_suite|vp|director|head|controller_family), function_match, blitz (raw person record: full_name, headline, experiences[], location{}).',
   'marketbase-expand-buying-committee for Acme (2026-08), seeded from comp-intel webhook logs.',
   'migration-036')
ON CONFLICT (name) DO NOTHING;
