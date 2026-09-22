-- 048_manual_check_policy.sql
-- Register the 'manual' source: a person's own reading of a profile.
--
-- A human opening the page and looking is the most reliable source available,
-- and it previously had nowhere to live. The verdict stayed in a chat message,
-- so the next run re-fetched that person, spent a throttled call, and sometimes
-- came back LESS certain than the human already was.
--
-- Recording it as a cache row rather than a tag is the whole point. A tag would
-- be permanent; a manual check is not. The person was right WHEN THEY LOOKED,
-- so it carries a fetched_at and ages out on the same window as every provider.
-- Nobody has to remember to revoke it, and no reader needs a special case.
--
-- Same 7 days as the live-employment sources it stands in for: a check is
-- evidence with a date, not an exemption.

INSERT INTO enrichment_source_policy (api, endpoint, fresh_days, rationale) VALUES
  ('manual', 'user/experience', 7,
   'A person''s own reading of the profile, via record_manual_check.py. The '
   'strongest source there is — no provider can look at a page and judge it — '
   'and consulted before anything is fetched. Expires on the same window as '
   'the live-employment providers because it is evidence with a date: they '
   'were right when they looked, not permanently.')
ON CONFLICT (api, endpoint) DO NOTHING;
