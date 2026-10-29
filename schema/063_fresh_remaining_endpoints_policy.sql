-- Register the remaining Fresh endpoints.
--
-- 062 registered ('fresh-linkedin-profile-data', '/enrich-lead') and left the
-- other four endpoints under that api name unregistered, so their caches never
-- expired. Live at 2026-10-28: /search-employees 400 rows on one instance;
-- /get-post-comments 832, /get-post-reactions 682 and /search-posts 55 on
-- another.
--
-- Decision (2026-10-28): the default freshness for a NEW endpoint is 7 days.
-- Register at 7 unless there is a stated reason for another number, and put
-- that reason in `rationale`. NULL (immutable) stays a deliberate claim, never
-- the fallback.
--
-- Post engagement has a second rule this table cannot express: check a post on
-- the day it is posted and the next day, then never again. The 1 day below
-- covers the first part. The harvesters (recover_reactions_freshdata,
-- recover_comments_freshdata) apply the second part. They read the post's age
-- from its activity id and pass max_age_days=None once it is 2 days old, so
-- the last cached page becomes final.

INSERT INTO enrichment_source_policy (api, endpoint, fresh_days, rationale) VALUES
  ('fresh-linkedin-profile-data', '/search-employees', 30,
   'A company''s employee list changes slowly; a month is fresh enough to '
   'size or seed from (2026-10-28).'),
  ('fresh-linkedin-profile-data', '/search-posts', 7,
   'Default for a new endpoint (2026-10-28). New posts keep matching a '
   'keyword search.'),
  ('fresh-linkedin-profile-data', '/get-post-comments', 1,
   'Comments arrive mostly on the day of posting and the next. Re-check daily '
   'while young; the harvester freezes the cache once the post is 2 days old '
   '(2026-10-28).'),
  ('fresh-linkedin-profile-data', '/get-post-reactions', 1,
   'Reactions arrive mostly on the day of posting and the next. Re-check daily '
   'while young; the harvester freezes the cache once the post is 2 days old '
   '(2026-10-28).')
ON CONFLICT (api, endpoint) DO NOTHING;
