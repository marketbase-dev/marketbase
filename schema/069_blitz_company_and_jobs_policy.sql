-- 069_blitz_company_and_jobs_policy.sql
-- Two Blitz endpoints were being cached with no enrichment_source_policy row,
-- so their responses never expired. Found 2026-10-31 after
-- blitz_enrich_people.py enriched ~11k companies in Impact 11.
--
-- /v2/enrichment/company: 90 days. Industry, headcount, size band and HQ move
-- far more slowly than a person's job; same window as saleleads'
-- /api/v1/company/profile.
--
-- /v2/jobs/company: 7 days, the house default for a new endpoint. Open roles
-- turn over weekly, and nothing gives a reason to hold them longer.

INSERT INTO enrichment_source_policy (api, endpoint, fresh_days, rationale) VALUES
  ('blitz', '/v2/enrichment/company', 90,
   'Blitz company enrichment: industry, employees on LinkedIn, size band, HQ. '
   'Moves far more slowly than a person''s job; same window as saleleads '
   '/api/v1/company/profile (2026-10-31).'),
  ('blitz', '/v2/jobs/company', 7,
   'Blitz open roles for a company. Default for a new endpoint (2026-10-31): '
   'postings turn over weekly.')
ON CONFLICT (api, endpoint) DO NOTHING;
