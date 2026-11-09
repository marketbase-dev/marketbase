-- 089_post_rules_on_every_row.sql
--
-- 088 enforced the post key on new rows only; old rows were repaired by a tool
-- since (on one instance: 363 second copies removed, 1,142 rows re-keyed, 1,550
-- rows given back to the author they belong to, 880 repost rows added; no
-- engagement moved or lost). Every row now passes, so the rule is checked on
-- every row and can never be broken again.
--
-- ── 1. The post key, on every row ─────────────────────────────────────────────
-- A CHECK, validated: post_urn is the post's own id and its number is the one in
-- the post's own link (post_key_ok, 088). It replaces 088's trigger.
--
-- ── 2. Index the two foreign keys into post_engagements ──────────────────────
-- signals.subject_post_engagement_id and assertions.subject_post_engagement_id
-- reference post_engagements with no index of their own, so removing one
-- engagement row scans both tables (~90k rows each on one instance). Removing
-- 32,315 duplicate reaction copies (a harvest stored one reaction up to 13
-- times) ran for minutes and was cancelled. Partial indexes: nearly every row
-- is NULL there. 090 adds one-reaction-per-person once the copies are gone.
--
-- ── 3. A plain repost as an engagement ───────────────────────────────────────
-- When a vendor says someone reposted a post but withholds the repost's own id,
-- the repost cannot be a posts row (no key). It is recorded as that person's
-- 'repost' engagement on the original (post_identity.record_repost).
--
-- ── 4. people_sweep ──────────────────────────────────────────────────────────
-- A source type for people found by a company-wide people search at a company
-- with any relationship other than competitor.

ALTER TABLE posts DROP CONSTRAINT IF EXISTS posts_key_matches_link;
ALTER TABLE posts ADD CONSTRAINT posts_key_matches_link CHECK (post_key_ok(post_urn, post_url));
DROP TRIGGER IF EXISTS trg_post_key ON posts;
DROP FUNCTION IF EXISTS refuse_bad_post_key();

CREATE INDEX IF NOT EXISTS idx_signals_subject_post_engagement
    ON signals (subject_post_engagement_id) WHERE subject_post_engagement_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_assertions_subject_post_engagement
    ON assertions (subject_post_engagement_id) WHERE subject_post_engagement_id IS NOT NULL;

ALTER TYPE engagement_type ADD VALUE IF NOT EXISTS 'repost';

INSERT INTO source_types (name, description, purpose, raw_data_shape, examples, created_by)
VALUES (
    'people_sweep',
    'A person found by a company-wide people search, at a company whose relationship is not competitor.',
    'Use for people a company-wide search finds at a non-competitor company; competitor finds keep '
    'their own source type. Label "<Company> (<relationship>)".',
    'The flattened person record (job title, title_source, _source) and the search parts that found them.',
    NULL,
    'migration-089'
)
ON CONFLICT (name) DO NOTHING;
