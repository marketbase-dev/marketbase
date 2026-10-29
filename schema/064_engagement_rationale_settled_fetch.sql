-- Correct the rationale 063 stored for the two post-engagement endpoints.
--
-- 063 said the harvester "freezes the cache once the post is 2 days old". The
-- rule settled the same day is different: check the post on the day it goes
-- up and the next day, fetch ONCE more after it turns 2 days old, then never
-- again. A first fetch made after the 2-day mark is already final. The 1-day
-- fresh_days is unchanged; only the explanation was wrong.
--
-- Keyed by the table's primary key (api, endpoint): exactly two rows.

UPDATE enrichment_source_policy
   SET rationale = 'Engagement arrives mostly on the day of posting and the '
                   'next. Re-check daily while young; the harvester '
                   '(engagement_cache_window) fetches once more after the '
                   'post is 2 days old and keeps that page forever. A first '
                   'fetch after the 2-day mark is already final (2026-10-28).',
       updated_at = now()
 WHERE api = 'fresh-linkedin-profile-data'
   AND endpoint IN ('/get-post-comments', '/get-post-reactions');
