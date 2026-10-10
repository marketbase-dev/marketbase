-- 055_blitz_search_people_policy.sql
-- Freshness for the two Blitz endpoints that became REAL caches on 2026-10-10
-- and therefore need a window for the first time.
--
-- Until then they had a parallel FILE cache under a per-client `.blitz_cache/`
-- directory, read BEFORE the database. Two consequences, both found 2026-10-09:
-- the database path had never written a single row in any client instance
-- (`/v2/search/people` = 0 rows across all six), and 528 of those JSON pages
-- sat tracked in git. The file cache is gone — the database is now the only
-- store for anything fetched (CLAUDE.md, "Caching fetched data") — so these
-- endpoints resolve their own policy, and with no row here they resolve to
-- FOREVER, which is the silent infinity 047 exists to abolish.
--
-- 30 days for the people search, matching `icypeas /api/find-people`: both are
-- company ROSTER reads answering "who works in sales at this competitor". A
-- roster decays through joiners and leavers, but no verdict rests on it
-- directly — departures are decided by the employment authority (`saleleads
-- user/experience`, 7 days) and a stale roster costs a joiner hint rather than
-- a wrong answer. A 7-day window here would re-fetch thousands of pages for a
-- signal nothing concludes from.
--
-- Blitz is flat-rate on the shared house seat (every cached row records
-- cost=0), so expiry costs wall-clock rather than credits. That is why the
-- window can be this tight without a spend conversation.

INSERT INTO enrichment_source_policy (api, endpoint, fresh_days, rationale)
VALUES (
    'blitz', '/v2/search/people', 30,
    'Company sales ROSTER for competitor-rep building. Decays via joiners and '
    'leavers, but no verdict rests on it: departures come from saleleads '
    'user/experience (7d), and a stale roster costs a hint, not a conclusion. '
    'Matches icypeas /api/find-people for the same reason. Flat-rate source, '
    'so expiry costs wall-clock rather than credits.')
ON CONFLICT (api, endpoint) DO UPDATE
   SET fresh_days = EXCLUDED.fresh_days,
       rationale  = EXCLUDED.rationale,
       updated_at = now();

-- The domain -> company-LinkedIn lookup shared the same file cache and is now
-- database-only too. A company's LinkedIn page effectively does not move, so
-- this is the cheapest thing in the system to cache and the most wasteful to
-- re-fetch — but 365 days rather than NULL, because "effectively" is not
-- "never": a rebrand or an acquisition does move a page.
INSERT INTO enrichment_source_policy (api, endpoint, fresh_days, rationale)
VALUES (
    'blitz', '/v2/enrichment/domain-to-linkedin', 365,
    'domain -> company LinkedIn URL. Near-immutable, but a rebrand or '
    'acquisition can move a page, so a long window rather than NULL.')
ON CONFLICT (api, endpoint) DO UPDATE
   SET fresh_days = EXCLUDED.fresh_days,
       rationale  = EXCLUDED.rationale,
       updated_at = now();
