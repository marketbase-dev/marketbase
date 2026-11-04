-- MarketBase schema, migration 077 — Voice policy picks leaders by FUNCTION
--
-- 073 gave voice_policy three thresholds (post recency, reposts by targets,
-- buyer-function share of past engagers). They were never set, and they are
-- withdrawn: none of them says whether a person is worth following. Who is
-- worth following is a leader in a function whose posts reach the client's
-- buyers, and then, among those, someone whose posts actually draw reactions.
--
-- So the rule becomes:
--   keep_functions            the house default, the same on every instance:
--                             executives and the market-facing functions.
--   workspace_keep_functions  what this workspace adds because its buyers sit
--                             there (finance for an FP&A vendor, security and
--                             IT for a security vendor). Kept apart from the
--                             default so each list names its own source.
--   min_post_reactions        the traction cutoff, read from the voice's own
--                             posts. NULL until it is chosen from the data.
--
-- A person's function is a decision by the registered title-review processor,
-- recorded in assertions; this table only says which functions count. A
-- function in neither list is not followed.

-- ── withdraw the three unused thresholds ───────────────────────────────────
DO $$
DECLARE
    set_rows bigint := 0;
BEGIN
    -- Dynamic, so a re-run (columns already gone) does not fail to parse.
    IF EXISTS (SELECT 1 FROM information_schema.columns
                WHERE table_name = 'voice_policy' AND column_name = 'post_recency_days') THEN
        EXECUTE 'SELECT count(*) FROM voice_policy WHERE post_recency_days IS NOT NULL
                    OR min_target_reposts IS NOT NULL OR min_finance_share IS NOT NULL' INTO set_rows;
    END IF;
    IF set_rows > 0 THEN
        RAISE EXCEPTION '077: % voice_policy row(s) set a threshold this migration withdraws; resolve first', set_rows;
    END IF;
END $$;

ALTER TABLE voice_policy
    DROP COLUMN IF EXISTS post_recency_days,
    DROP COLUMN IF EXISTS min_target_reposts,
    DROP COLUMN IF EXISTS min_finance_share;

-- ── the function rule ──────────────────────────────────────────────────────
ALTER TABLE voice_policy
    ADD COLUMN IF NOT EXISTS keep_functions           text[],
    ADD COLUMN IF NOT EXISTS workspace_keep_functions text[],
    ADD COLUMN IF NOT EXISTS min_post_reactions       numeric;

DO $$
DECLARE
    vocab text := $v$ARRAY['executive','sales','marketing','product','partnerships','solutions',
                          'customer_success','strategy','finance','security_it','engineering',
                          'people','legal','operations','procurement','support','assistant',
                          'other']::text[]$v$;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_voice_policy_keep_functions') THEN
        EXECUTE format('ALTER TABLE voice_policy ADD CONSTRAINT ck_voice_policy_keep_functions
                          CHECK (keep_functions <@ %s AND workspace_keep_functions <@ %s)', vocab, vocab);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_voice_policy_min_post_reactions') THEN
        ALTER TABLE voice_policy ADD CONSTRAINT ck_voice_policy_min_post_reactions
            CHECK (min_post_reactions >= 0);
    END IF;
END $$;

COMMENT ON COLUMN voice_policy.keep_functions IS
    'House default: functions whose leaders are followed on every instance. NULL = no function rule.';
COMMENT ON COLUMN voice_policy.workspace_keep_functions IS
    'Functions this workspace adds because its buyers sit there; its own evidence goes in source.';
COMMENT ON COLUMN voice_policy.min_post_reactions IS
    'Median reactions per post a voice must show. NULL = traction not yet chosen; nobody is cut on it.';

-- ── the house default, seeded once per group that is chosen by rule ────────
-- Industry voices are named by the client, not picked by a rule, so they get no row.
INSERT INTO voice_policy (voice_group, version, title_keywords, keep_functions,
                          source, rationale, created_by)
SELECT g, 'house-default@2026-11-04',
       ARRAY['founder','co-founder','ceo','cto','cmo','coo','cfo','cro','cpo','chief','president',
             'vp','vice president','head','director'],
       ARRAY['executive','sales','marketing','product','partnerships','solutions',
             'customer_success','strategy'],
       'Operator decision, 2026-11-04: follow competitor employees who are actually leaders and '
       'likely to get traction; a default plus workspace additions',
       'Search keywords are research_competitor EXEC_KEYWORDS (the existing leader sweep). A leader '
       'is kept when the title-review processor puts them in a listed function.',
       '077_voice_policy_functions'
  FROM unnest(ARRAY['competitor', 'own']) g
 WHERE NOT EXISTS (SELECT 1 FROM voice_policy p WHERE p.voice_group = g AND p.superseded_at IS NULL);
