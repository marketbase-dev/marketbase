-- Register the Luma event-guest pull in enrichment_source_policy.
--
-- tools/ingest_luma_guests.py pages a Luma event's guest list through the
-- admin API and caches every page in enrichment_calls. Until this row exists
-- the cache never expires (api_cache warns on every run), so a "sync" that
-- forgets --no-cache replays the first ingest's statuses FOREVER and reports
-- "no changes" — the exact quiet failure 047 exists to prevent, on a feed
-- whose entire point is that statuses move (pending_approval -> approved /
-- declined as organizers process the queue, checked_in_at on event day).
--
-- 1 day, the floor the CHECK allows: guest status is live operational state,
-- not an enrichment fact with a natural shelf life. A deliberate sync should
-- still pass use_cache=False (explicit argument > this table) to see
-- minutes-old changes; this row bounds the damage when that is forgotten —
-- the stale window shrinks from forever to yesterday. The call is free (our
-- own event admin session), so there is no credit argument for a longer
-- window.

INSERT INTO enrichment_source_policy (api, endpoint, fresh_days, rationale) VALUES
  ('luma', '/event/admin/get-guests', 1,
   'Luma event guest list (admin API, free, paged): approval_status and '
   'checked_in_at are live operational state that moves as organizers process '
   'the queue. 1 day is the minimum the schema allows and only bounds the '
   'forgotten-flag case; a deliberate status sync passes use_cache=False and '
   'hits the API every run. Pages are cached for replay/derivation, not to '
   'avoid refetching.')
ON CONFLICT (api, endpoint) DO NOTHING;
