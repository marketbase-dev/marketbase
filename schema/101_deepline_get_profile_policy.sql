-- Register the Deepline full-profile tool in enrichment_source_policy.
--
-- `harvestapi_get_profile` is a live LinkedIn profile scrape keyed on the
-- profile URL: headline, certifications, connection count and work history in
-- one call ($0.005, priced in 068). It has been called since 2026-10-29 with
-- no policy row, so its cache never expired on its own; the one caller that
-- bounded it did so with a number hard-coded in the tool.
--
-- 7 days, the default for a new endpoint (decision of 2026-10-28, see 063).
-- The fields read from it (connection count, certifications, current role)
-- change slowly, but they are read to describe a person at a point in time,
-- and nothing argues for a longer window than the default.
--
-- Keyed on the TOOL, not on "deepline", for the reason given in 057: Deepline
-- is an aggregator, and a swap to another tool must arrive as an unregistered
-- endpoint and warn, not inherit this window.
--
-- No `provider_fresh_days`: the payload carries no refresh date to gate on. It
-- is a scrape performed at call time, so the cache window is the only bound.

INSERT INTO enrichment_source_policy (api, endpoint, fresh_days, rationale) VALUES
  ('deepline', 'harvestapi_get_profile', 7,
   'Deepline tool harvestapi_get_profile: a LIVE LinkedIn profile scrape keyed '
   'on profile URL (headline, certifications, connection count, work history), '
   '$0.005 per call. Default window for a new endpoint (2026-10-28). Keyed on '
   'the TOOL rather than on "deepline" so a swap to another tool cannot inherit '
   'this window.')
ON CONFLICT (api, endpoint) DO NOTHING;
