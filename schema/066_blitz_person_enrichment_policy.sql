-- 066_blitz_person_enrichment_policy.sql
-- Blitz POST /v2/enrichment/person had no enrichment_source_policy row, so
-- its cached responses never expired. Found 2026-10-29 after a comparison run
-- against the saleleads employment readings on two instances.
--
-- 7 days, the employment window. The payload is a career history, and the
-- question anyone reads it for -- where does this person work -- is the one
-- that decays. The same run measured Blitz about one job behind (6 of 21 known
-- departures caught, 10 false departures), so it is barred from writing an
-- employment verdict regardless; the window only stops a stale copy being
-- replayed as though it were today's.

INSERT INTO enrichment_source_policy (api, endpoint, fresh_days, rationale) VALUES
  ('blitz', '/v2/enrichment/person', 7,
   'Blitz person enrichment: full career history keyed on a LinkedIn URL. An '
   'INDEX, measured ~one job behind (2026-10-29), so never an employment '
   'verdict source; 7 days because what it is read for decays.')
ON CONFLICT (api, endpoint) DO NOTHING;
