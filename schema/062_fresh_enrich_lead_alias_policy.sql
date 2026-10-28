-- Register Fresh `/enrich-lead` under its SECOND api name.
--
-- 053 registered ('fresh', '/enrich-lead') at 7 days. The same endpoint is also
-- cached under api = 'fresh-linkedin-profile-data' -- the name eight marketbase
-- tools pass to api_cache (backfill_urn_from_enrich, fill_follower_count_fresh,
-- fill_short_committees, enrich_rereview_evidence, resolve_urn_identities, ...).
-- That pair had no policy row, so its cache NEVER EXPIRED: one instance held
-- ~1,000 such responses at 2026-10-28, and two of those callers feed
-- employment decisions -- the stale-job-title replay 053 was written to stop.
--
-- Register the alias rather than rename the api: changing the name changes
-- every cache KEY, orphaning the paid rows and re-billing them (the same
-- reasoning as api_cache's slash-normalised lookup -- normalise the lookup,
-- never the key).
--
-- Same 7 days and same reasoning as 053: the payload carries identity AND
-- employment, and the shorter clock has to win.
--
-- Deliberately NOT registered here: the other 'fresh-linkedin-profile-data'
-- endpoints (/search-employees, /search-posts, /get-post-comments,
-- /get-post-reactions). Each needs its own freshness decision; they still warn.

INSERT INTO enrichment_source_policy (api, endpoint, fresh_days, rationale) VALUES
  ('fresh-linkedin-profile-data', '/enrich-lead', 7,
   'Alias of (fresh, /enrich-lead) -- same Fresh LinkedIn by-url lookup, cached '
   'under the RapidAPI host name by most marketbase tools. Identity AND '
   'employment in one payload, so it takes the employment window, as 053 does.')
ON CONFLICT (api, endpoint) DO NOTHING;
