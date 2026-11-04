-- MarketBase schema, migration 078 — Voice policy: the activity rule as one checked JSON value
--
-- 077 kept the traction cutoff as one column, min_post_reactions. The rule that
-- was chosen from the data needs three numbers plus a list of who is always
-- followed, and its measures will be tuned again. So the activity part of the
-- policy becomes one jsonb value, activity_rule, and a change is a new policy
-- version rather than a new migration.
--
-- A jsonb value accepts any typo, and a misspelled key would just never apply.
-- So the database checks the shape (voice_activity_rule_ok): an object, only
-- the known keys, each threshold a non-negative number, and always_include a
-- list of known words. A new measure is added here first, then in the reader.
--
--   own_posts_90d_min          posts the person wrote in the last 90 days
--   own_posts_365d_min         posts the person wrote in the last 365 days
--   median_own_reactions_min   median reactions on the posts the person wrote
--   always_include             followed whatever the numbers say:
--                                founder       a founder or co-founder by title
--                                company_page  a competitor's company page
--                                own_asset     the client's own page and showcase pages
--
-- "Own" means written by the person: a repost or a colleague's post that shows
-- in their feed is not theirs and does not count.
-- NULL = no activity rule; nobody is cut on activity.

CREATE OR REPLACE FUNCTION voice_activity_rule_ok(r jsonb) RETURNS boolean
LANGUAGE sql IMMUTABLE AS $$
    SELECT r IS NULL OR (
        jsonb_typeof(r) = 'object'
        AND NOT EXISTS (SELECT 1 FROM jsonb_object_keys(r) k
                         WHERE k NOT IN ('own_posts_90d_min', 'own_posts_365d_min',
                                         'median_own_reactions_min', 'always_include'))
        AND NOT EXISTS (SELECT 1 FROM jsonb_each(r) e
                         WHERE e.key <> 'always_include'
                           AND (jsonb_typeof(e.value) <> 'number' OR (e.value #>> '{}')::numeric < 0))
        AND (NOT r ? 'always_include' OR (
                 jsonb_typeof(r->'always_include') = 'array'
                 AND NOT EXISTS (SELECT 1 FROM jsonb_array_elements(r->'always_include') x
                                  WHERE jsonb_typeof(x) <> 'string'
                                     OR x #>> '{}' NOT IN ('founder', 'company_page', 'own_asset'))))
    )
$$;

-- The cutoff column 077 added moves into the rule; refuse to drop a value someone set.
DO $$
DECLARE
    set_rows bigint := 0;
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
                WHERE table_name = 'voice_policy' AND column_name = 'min_post_reactions') THEN
        EXECUTE 'SELECT count(*) FROM voice_policy WHERE min_post_reactions IS NOT NULL' INTO set_rows;
    END IF;
    IF set_rows > 0 THEN
        RAISE EXCEPTION '078: % voice_policy row(s) set min_post_reactions; move it into activity_rule first', set_rows;
    END IF;
END $$;

ALTER TABLE voice_policy DROP COLUMN IF EXISTS min_post_reactions;
ALTER TABLE voice_policy ADD COLUMN IF NOT EXISTS activity_rule jsonb;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_voice_policy_activity_rule') THEN
        ALTER TABLE voice_policy ADD CONSTRAINT ck_voice_policy_activity_rule
            CHECK (voice_activity_rule_ok(activity_rule));
    END IF;
END $$;

COMMENT ON COLUMN voice_policy.activity_rule IS
    'Who among the kept leaders is followed, by their own posting: {own_posts_90d_min, own_posts_365d_min, '
    'median_own_reactions_min, always_include[founder|company_page|own_asset]}. Shape checked by '
    'voice_activity_rule_ok. NULL = no activity rule.';
