-- Register Fresh LinkedIn `/enrich-lead` in enrichment_source_policy.
--
-- It was never registered, so `api_cache` resolved its freshness to None and
-- its cached responses NEVER EXPIRED. That is the exact failure 047's own
-- header describes: an unregistered source gets an implicit answer of FOREVER.
-- Four callers share this endpoint and one of them decides employment, so a
-- permanently cached hit could let a stale job title be replayed as current.
--
-- 7 days, matching the employment authority (`saleleads user/experience`),
-- because the payload carries BOTH kinds of fact and the shorter clock has to
-- win:
--
--   identity     urn / public_id / canonical vanity url   effectively stable
--   employment   job_title / company / location           decays
--
-- Caching to the identity clock would be cheaper and wrong: the same row would
-- answer "where do they work now?" from a months-old read. The reverse costs
-- almost nothing, because the URN backfill only ever queries leads that have
-- NO urn yet -- once one is captured the lead leaves that working set, so a
-- short window never makes it re-pay.
--
-- Related trap fixed the same day: a caller passing '/user/experience' did not
-- match the row registered as 'user/experience', so the authority's own 7-day
-- window silently did not apply either. api_cache now matches both forms when
-- it LOOKS UP a policy, without changing any cache key.

INSERT INTO enrichment_source_policy (api, endpoint, fresh_days, rationale) VALUES
  ('fresh', '/enrich-lead', 7,
   'Fresh LinkedIn Profile Data by-url lookup. Returns identity (ACoAA urn, '
   'public_id, canonical vanity url) AND employment (job_title, company, '
   'location) in one payload, and is the only rung that reliably returns the '
   'person''s own country. Gets the employment window rather than the identity '
   'one because the shorter clock must win when a payload carries both.')
ON CONFLICT (api, endpoint) DO NOTHING;
