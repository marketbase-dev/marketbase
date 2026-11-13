-- 091_blitz_company_search_policy.sql
-- Blitz POST /v2/search/companies had no enrichment_source_policy row, so its
-- cached pages never expired. A metro-wide company pull (state + size band
-- filters) is cached page by page, keyed on the query and cursor.
--
-- 7 days, the default for a new endpoint. The pages hold company profiles whose
-- headcount, HQ and size band drift; a week-old page is fine to replay within a
-- run and its retries, but not as next month's answer.

INSERT INTO enrichment_source_policy (api, endpoint, fresh_days, rationale) VALUES
  ('blitz', '/v2/search/companies', 7,
   'Blitz company search (hq / size-band filters), cached per page. Headcount, '
   'HQ and size band drift, so 7 days: the default for a new endpoint.')
ON CONFLICT (api, endpoint) DO NOTHING;
