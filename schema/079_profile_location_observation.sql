-- 079: a checked profile with no location is a fact, not a gap.
--
-- Until now a null leads.country meant two different things: "nobody looked
-- yet" and "we fetched the full profile and it carries no location at all".
-- Readers could not tell them apart, so every enrichment run re-fetched the
-- same people and every report filed them under "unknown". This registers
-- the lead-level observation that records the second case. It is written by
-- the enrichment tool that looked, with the profile fetch as its evidence
-- (072 refuses an observation without one), and read by anything that
-- filters or reports on location: an active none_on_profile row means
-- "checked, nothing there" -- no re-fetch, and "No location on profile"
-- instead of "unknown".
--
-- Idempotent: the key is inserted once; a re-run is a no-op.

INSERT INTO tag_definitions (namespace, key, entity_type, layer, kind, description) VALUES
  ('location', 'profile_location', 'lead', 'attribute', 'observation',
   'What the person''s profile says about where they are, as a full profile fetch returned it. '
   'value = none_on_profile when the fetched profile carries no location at all (country, city and '
   'free-text location all empty); notes = which fetches said so and when. Evidence: the profile fetch '
   '(enrichment_calls). Readers treat an active none_on_profile row as "checked, nothing there": no '
   're-fetch, and reports print "No location on profile" rather than "unknown". A later fetch that finds '
   'a location writes leads.country and replaces this observation.')
ON CONFLICT (namespace, key, entity_type) DO NOTHING;
