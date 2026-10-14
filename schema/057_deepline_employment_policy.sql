-- Register the Deepline employment rung in enrichment_source_policy.
--
-- Deepline is an AGGREGATOR, so the policy key is the TOOL, not the vendor:
-- `edges_linkedin_extract_people_experiences` is a live LinkedIn extract that
-- returns experiences[] (company_name, title, date, job_time_period), which is
-- the same shape as the saleleads authority. Registering it under the vendor
-- name alone would let a future swap to an INDEX-backed tool (peopledatalabs,
-- crustdata) inherit a window that was justified by a live read, which is
-- precisely the confusion that bars Blitz and icypeas from writing verdicts.
-- Keyed per tool, a swap arrives as an unregistered endpoint and warns.
--
-- 7 days, the employment window: it answers "do they still work there", the
-- same question as saleleads user/experience and fresh /enrich-lead, and the
-- tool exists because that fact decays.
--
-- No `provider_fresh_days`: unlike Clay cpj-enrich-person, this payload
-- carries no `last_refresh` to gate on. It is an extract performed at call
-- time, so the cache window IS the only staleness bound -- which is an
-- argument for keeping that window short, not for trusting the read less.

INSERT INTO enrichment_source_policy (api, endpoint, fresh_days, rationale) VALUES
  ('deepline', 'edges_linkedin_extract_people_experiences', 7,
   'Deepline tool edges_linkedin_extract_people_experiences: a LIVE LinkedIn '
   'experience extract keyed on profile URL, returning experiences[] with '
   'company_name/title/date -- the same shape as the saleleads employment '
   'authority, at $0.003/result. Gets the employment window because it answers '
   'the same decaying question. Keyed on the TOOL rather than on "deepline" so '
   'a swap to an index-backed tool cannot inherit a live read''s window.')
ON CONFLICT (api, endpoint) DO NOTHING;
