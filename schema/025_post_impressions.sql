-- MarketBase schema, migration 025 — Owner-analytics impressions on posts
--
-- The `posts` table carries public engagement counters (likes,
-- comments_count, shares) that ANY scraper can see — Saleleads, LeadMagic,
-- etc. Impressions (reach / view count) are different: LinkedIn only exposes
-- them to the post's OWNER. We can therefore only fill them when we read a
-- post through the owning account's own Unipile-connected session
-- (the legitimate owner-analytics path — NOT third-party scraping).
--
-- Three new columns on `posts`:
--   • impressions       — owner-only reach/view count. NULL when we've never
--                         had owner access to the post (e.g. competitor posts
--                         pulled via Saleleads, which can never carry this).
--   • analytics_source  — where the impressions value came from
--                         (e.g. 'unipile'). Distinguishes owner-analytics
--                         rows from public-scrape rows.
--   • analytics_at      — when we last captured the owner analytics. Impressions
--                         keep climbing for days after publish, so this stamps
--                         the snapshot's age (distinct from last_scraped_at,
--                         which tracks the public-counter scrape).

ALTER TABLE posts ADD COLUMN IF NOT EXISTS impressions      integer;
ALTER TABLE posts ADD COLUMN IF NOT EXISTS analytics_source text;
ALTER TABLE posts ADD COLUMN IF NOT EXISTS analytics_at     timestamptz;

-- Support "best-reach posts" queries.
CREATE INDEX IF NOT EXISTS idx_posts_impressions ON posts(impressions DESC)
    WHERE impressions IS NOT NULL;
