-- 088_post_keys_and_reposts.sql
--
-- A post is one row, keyed on its own id; a repost points at what it reposts;
-- post listings are fetched once.
--
-- ── 1. The key ───────────────────────────────────────────────────────────────
-- LinkedIn gives every post two numbers: the activity id (the feed event, the
-- number in the post's own link, feed/update/urn:li:activity:<n>) and the
-- content id (urn:li:ugcPost:<n> or urn:li:share:<n>, the text and media
-- behind it). They are minted minutes to hours apart (scheduled posts), so
-- neither can be computed from the other. Vendors hand back either one, and for
-- a repost often the ORIGINAL's id, in fields whose names do not say which.
--
-- posts.post_urn was always written with the activity label, whatever number
-- the writer had. On one instance 2,839 rows carry a number that is not the one
-- in their own link: content ids under the activity label, reposts stored under
-- the original's id, and twins of one post stored under each of its two ids.
-- The UNIQUE index on post_urn could not see any of it: the strings differed.
--
-- The rule, post_key_ok(post_urn, post_url):
--   * post_urn is urn:li:activity:<n>, or urn:li:ugcPost:<n> / urn:li:share:<n>
--     for an original known only by its content id, labelled for what it is;
--   * the number in post_urn is the number in the post's own link.
-- This migration enforces it on every row INSERTED and every key change, by
-- trigger. Rows written before it are repaired by a tool, then 089 replaces
-- the trigger with a validated CHECK on every row. A NOT VALID check here would
-- refuse a likes refresh on an unrepaired row; the trigger refuses only keys.
--
-- ── 2. Reposts ───────────────────────────────────────────────────────────────
-- A repost is its own row (its own activity id, the reposter as poster, the
-- reposter's added text if any) pointing at the original's row. A repost with
-- added text and a plain repost differ only in whether post_text is empty.
--
-- ── 3. Post listings never expire ─────────────────────────────────────────────
-- A post listing is fetched for a study window once. Re-listing it on a timer
-- spends credits to learn nothing the window needs. Owner's decision
-- 2026-11-08: the four listing endpoints never expire. A deliberate re-pull
-- uses the cache's fetched-after override.

CREATE OR REPLACE FUNCTION post_key_ok(_urn text, _url text) RETURNS boolean
LANGUAGE sql IMMUTABLE AS $$
  SELECT _urn ~ '^urn:li:(activity|ugcPost|share):[0-9]{15,}$'
     AND (_url IS NULL OR position(substring(_urn from '[0-9]+$') in _url) > 0)
$$;

COMMENT ON FUNCTION post_key_ok(text, text) IS
  'A post''s key is urn:li:activity:<n> (or ugcPost/share for an original known only by '
  'its content id), and <n> is the number in the post''s own link. See 088.';

CREATE OR REPLACE FUNCTION refuse_bad_post_key() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF NOT post_key_ok(NEW.post_urn, NEW.post_url) THEN
    RAISE EXCEPTION 'post key % does not match its own link %', NEW.post_urn, NEW.post_url
      USING ERRCODE = '23514',
            HINT = 'Key a post on its own activity id: the number in its feed/update link. See 088.';
  END IF;
  RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS trg_post_key ON posts;
CREATE TRIGGER trg_post_key BEFORE INSERT OR UPDATE OF post_urn, post_url ON posts
  FOR EACH ROW EXECUTE FUNCTION refuse_bad_post_key();

ALTER TABLE posts ADD COLUMN IF NOT EXISTS reposted_post_id uuid
  CONSTRAINT posts_reposted_post_id_fkey REFERENCES posts(id);
ALTER TABLE posts DROP CONSTRAINT IF EXISTS posts_repost_not_self;
ALTER TABLE posts ADD CONSTRAINT posts_repost_not_self CHECK (reposted_post_id IS DISTINCT FROM id);
CREATE INDEX IF NOT EXISTS idx_posts_reposted ON posts (reposted_post_id) WHERE reposted_post_id IS NOT NULL;

COMMENT ON COLUMN posts.reposted_post_id IS
  'Set on a repost: the row of the post it reposts. The repost row is keyed on its own '
  'activity id; post_text holds the reposter''s added text, empty for a plain repost.';
COMMENT ON COLUMN posts.share_urn IS
  'The post''s content id (urn:li:ugcPost:<n> / urn:li:share:<n>) when a vendor gives it. '
  'Never a key: a repost can sit beside its original''s content id.';

INSERT INTO enrichment_source_policy (api, endpoint, fresh_days, rationale) VALUES
  ('saleleads', '/api/v1/user/posts', NULL,
   'A person''s post listing, fetched once per study window. Never expires: re-listing '
   'on a timer spends credits for nothing the window needs (2026-11-08).'),
  ('saleleads', '/api/v1/company/posts', NULL,
   'A company page''s post listing; never expires, as /api/v1/user/posts (2026-11-08).'),
  ('fresh-linkedin-profile-data', '/get-profile-posts', NULL,
   'A person''s post listing; never expires, as saleleads /api/v1/user/posts (2026-11-08).'),
  ('fresh-linkedin-profile-data', '/get-company-posts', NULL,
   'A company page''s post listing; never expires, as saleleads /api/v1/user/posts (2026-11-08).')
ON CONFLICT (api, endpoint) DO UPDATE
  SET fresh_days = EXCLUDED.fresh_days, rationale = EXCLUDED.rationale, updated_at = now();
