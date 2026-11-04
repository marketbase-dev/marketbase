-- MarketBase schema, migration 073 — Voices: whose posts we collect engagers from
--
-- A voice is a LinkedIn company page, showcase page or person whose posts we
-- harvest reactions and comments from. Until now the list lived in tool flags
-- and scratch files, and posts named their author only by URL. A URL is not an
-- identity: vanity URLs get renamed, the same person appears as a vanity URL in
-- one feed and a member URN in another, and a list keyed by URL silently splits
-- one voice into two. So:
--
--   voices        — who is a voice, keyed by entity id (a companies row or a
--                   leads row, never a URL), with the group it belongs to, how
--                   it was chosen, and a soft-delete history.
--   voice_policy  — the selection rules per voice group, versioned, with the
--                   evidence they came from. The same shape as territory_policy:
--                   rules are client facts that live in the instance, never in
--                   a config file.
--   posts         — gains poster_company_id / poster_lead_id, filled at write
--                   time by a trigger from the poster URL, and backfilled here.
--
-- Three voice groups, each read as its own signal:
--   own         — the client's own page, showcase pages and leaders who post
--   competitor  — a competitor's page, or a person there picked by the rules
--   industry    — a page or person outside the client and its competitors whose
--                 posts draw the client's buyers
--
-- Showcase pages are companies rows with is_showcase = true (migration 024).

-- ── voice_policy ───────────────────────────────────────────────────────────
-- One ACTIVE row per group; a change is a new version and the old row is
-- marked superseded, so voices selected under it can still name their rules.
-- A NULL threshold means "this rule does not filter on that"; it is not a
-- default to be filled in.

CREATE TABLE IF NOT EXISTS voice_policy (
    voice_group         text        NOT NULL CHECK (voice_group IN ('own', 'competitor', 'industry')),
    version             text        NOT NULL,
    title_keywords      text[],
    min_rank            integer,
    post_recency_days   integer     CHECK (post_recency_days > 0),
    min_target_reposts  integer     CHECK (min_target_reposts >= 0),
    min_finance_share   numeric     CHECK (min_finance_share BETWEEN 0 AND 1),
    source              text        NOT NULL,
    rationale           text        NOT NULL,
    created_by          text        NOT NULL,
    created_at          timestamptz NOT NULL DEFAULT now(),
    superseded_at       timestamptz,
    PRIMARY KEY (voice_group, version)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_voice_policy_active
    ON voice_policy (voice_group) WHERE superseded_at IS NULL;

COMMENT ON TABLE voice_policy IS
    'Voice selection rules per group, versioned. One active row per group; read by the voice selector, never a file.';
COMMENT ON COLUMN voice_policy.title_keywords IS
    'A person qualifies if the current title contains any of these (case-insensitive). NULL = no title rule.';
COMMENT ON COLUMN voice_policy.min_rank IS
    'Minimum seniority rank from the shared role classifier. NULL = no seniority rule.';
COMMENT ON COLUMN voice_policy.post_recency_days IS
    'The voice must have posted within this many days. NULL = no recency rule.';
COMMENT ON COLUMN voice_policy.min_target_reposts IS
    'Qualifies anyone whose posts at least this many targets reposted. NULL = no repost rule.';
COMMENT ON COLUMN voice_policy.min_finance_share IS
    'Minimum share (0-1) of past engagers who are in the buyer function. NULL = no audience rule.';
COMMENT ON COLUMN voice_policy.source IS
    'The artifact the rule came from, citable: a call, a doc, a person and a date.';

-- ── voices ─────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS voices (
    id              uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id      uuid        REFERENCES companies(id) ON DELETE CASCADE,
    lead_id         uuid        REFERENCES leads(id)     ON DELETE CASCADE,
    voice_group     text        NOT NULL CHECK (voice_group IN ('own', 'competitor', 'industry')),
    selected_by     text        NOT NULL CHECK (selected_by IN ('rule', 'manual', 'client_roster')),
    policy_version  text,
    added_by        text        NOT NULL,
    added_at        timestamptz NOT NULL DEFAULT now(),
    added_reason    text        NOT NULL,
    removed_by      text,
    removed_at      timestamptz,
    removed_reason  text,
    CONSTRAINT voices_one_entity CHECK (num_nonnulls(company_id, lead_id) = 1),
    -- a rule-selected voice names the rule version that selected it
    CONSTRAINT voices_rule_names_version CHECK (selected_by <> 'rule' OR policy_version IS NOT NULL),
    CONSTRAINT voices_policy_fk FOREIGN KEY (voice_group, policy_version)
        REFERENCES voice_policy (voice_group, version),
    -- removal is all three fields or none
    CONSTRAINT voices_removal_complete CHECK (
        num_nonnulls(removed_by, removed_at, removed_reason) IN (0, 3))
);

-- One ACTIVE row per entity. NULLS NOT DISTINCT is load-bearing: every row has
-- one NULL entity column (see 044 for what happens without it).
CREATE UNIQUE INDEX IF NOT EXISTS uq_voices_active ON voices (company_id, lead_id)
    NULLS NOT DISTINCT WHERE removed_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_voices_group ON voices (voice_group) WHERE removed_at IS NULL;

COMMENT ON TABLE voices IS
    'Whose posts we collect engagers from, by entity id. Soft-deleted via removed_*; read v_voices.';
COMMENT ON COLUMN voices.selected_by IS
    'rule (voice_policy picked it; policy_version says which) | manual (a person added it) | client_roster (the client''s own list named it).';

-- ── posts name their author by id ──────────────────────────────────────────

ALTER TABLE posts ADD COLUMN IF NOT EXISTS poster_company_id uuid REFERENCES companies(id) ON DELETE SET NULL;
ALTER TABLE posts ADD COLUMN IF NOT EXISTS poster_lead_id    uuid REFERENCES leads(id)     ON DELETE SET NULL;
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'posts_poster_one_entity') THEN
        ALTER TABLE posts ADD CONSTRAINT posts_poster_one_entity
            CHECK (num_nonnulls(poster_company_id, poster_lead_id) <= 1);
    END IF;
END$$;
CREATE INDEX IF NOT EXISTS idx_posts_poster_company ON posts (poster_company_id) WHERE poster_company_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_posts_poster_lead    ON posts (poster_lead_id)    WHERE poster_lead_id IS NOT NULL;

-- Resolve a poster URL to an entity. Exact matches only, through the indexes:
--   person  — lead_identities (vanity URL, then member URN), then leads.linkedin_url,
--             the row key, because some leads predate their identity rows;
--   page    — companies.linkedin_slug (company, showcase or school page), exact,
--             then case-insensitive.
-- No match returns NULLs. Never guesses, never creates a row.
CREATE OR REPLACE FUNCTION post_poster_entity(url text, OUT company_id uuid, OUT lead_id uuid)
LANGUAGE plpgsql STABLE AS $$
DECLARE
    m    text[];
    slug text;
BEGIN
    m := regexp_match(url, 'linkedin\.com/(in|company|showcase|school)/([^/?#]+)', 'i');
    IF m IS NULL THEN
        RETURN;
    END IF;
    slug := m[2];
    IF lower(m[1]) = 'in' THEN
        SELECT li.lead_id INTO lead_id FROM lead_identities li
         WHERE li.kind = 'linkedin_url' AND li.value = 'https://www.linkedin.com/in/' || slug;
        IF lead_id IS NULL AND slug ~ '^AC[ow]AA' THEN
            SELECT li.lead_id INTO lead_id FROM lead_identities li
             WHERE li.kind IN ('linkedin_urn', 'linkedin_urn_search') AND li.value = slug
             LIMIT 1;
        END IF;
        IF lead_id IS NULL THEN
            SELECT l.id INTO lead_id FROM leads l
             WHERE l.linkedin_url = 'https://www.linkedin.com/in/' || slug;
        END IF;
    ELSE
        SELECT c.id INTO company_id FROM companies c WHERE c.linkedin_slug = slug;
        IF company_id IS NULL THEN
            SELECT c.id INTO company_id FROM companies c
             WHERE lower(c.linkedin_slug) = lower(slug)
             ORDER BY c.created_at LIMIT 1;
        END IF;
    END IF;
END$$;

-- Set at write time: a writer that names the poster only by URL still gets the
-- ids. A writer that sets an id itself is trusted; a changed URL re-resolves.
CREATE OR REPLACE FUNCTION posts_set_poster_entity() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    r record;
BEGIN
    IF NEW.poster_linkedin_url IS NULL THEN
        RETURN NEW;
    END IF;
    IF TG_OP = 'INSERT' AND num_nonnulls(NEW.poster_company_id, NEW.poster_lead_id) > 0 THEN
        RETURN NEW;
    END IF;
    IF TG_OP = 'UPDATE' AND NEW.poster_linkedin_url IS NOT DISTINCT FROM OLD.poster_linkedin_url THEN
        RETURN NEW;
    END IF;
    SELECT * INTO r FROM post_poster_entity(NEW.poster_linkedin_url);
    NEW.poster_company_id := r.company_id;
    NEW.poster_lead_id    := r.lead_id;
    RETURN NEW;
END$$;

DROP TRIGGER IF EXISTS trg_posts_poster_entity ON posts;
CREATE TRIGGER trg_posts_poster_entity
    BEFORE INSERT OR UPDATE OF poster_linkedin_url ON posts
    FOR EACH ROW EXECUTE FUNCTION posts_set_poster_entity();

-- Re-resolve every post whose poster is still unknown: the poster's lead or
-- company row may have been created after the post. Safe to run any time;
-- returns how many posts it resolved.
CREATE OR REPLACE FUNCTION resolve_post_posters() RETURNS integer
LANGUAGE plpgsql AS $$
DECLARE
    n integer;
BEGIN
    UPDATE posts p
       SET poster_company_id = r.company_id,
           poster_lead_id    = r.lead_id
      FROM (SELECT p2.id, e.company_id, e.lead_id
              FROM posts p2, LATERAL post_poster_entity(p2.poster_linkedin_url) e
             WHERE p2.poster_linkedin_url IS NOT NULL
               AND p2.poster_company_id IS NULL AND p2.poster_lead_id IS NULL
               AND num_nonnulls(e.company_id, e.lead_id) = 1) r
     WHERE p.id = r.id;
    GET DIAGNOSTICS n = ROW_COUNT;
    RETURN n;
END$$;

SELECT resolve_post_posters();

-- The posts whose author is still unknown, one row per poster URL.
CREATE OR REPLACE VIEW v_posts_unresolved_poster AS
SELECT poster_linkedin_url, min(poster_name) AS poster_name, count(*) AS posts,
       min(posted_at) AS first_posted_at, max(posted_at) AS last_posted_at
FROM posts
WHERE poster_company_id IS NULL AND poster_lead_id IS NULL
GROUP BY poster_linkedin_url
ORDER BY count(*) DESC;

-- ── v_voices ───────────────────────────────────────────────────────────────

CREATE OR REPLACE VIEW v_voices AS
SELECT v.id,
       v.voice_group,
       CASE WHEN v.lead_id IS NOT NULL THEN 'person'
            WHEN c.is_showcase          THEN 'showcase_page'
            ELSE 'company_page' END AS kind,
       v.company_id, v.lead_id,
       coalesce(c.name, l.name) AS name,
       CASE WHEN v.lead_id IS NOT NULL THEN l.linkedin_url
            WHEN c.is_showcase THEN 'https://www.linkedin.com/showcase/' || c.linkedin_slug
            ELSE 'https://www.linkedin.com/company/' || c.linkedin_slug END AS canonical_url,
       v.selected_by, v.policy_version, v.added_by, v.added_at, v.added_reason,
       (SELECT count(*) FROM posts p
         WHERE p.poster_company_id = v.company_id OR p.poster_lead_id = v.lead_id) AS posts_held,
       (SELECT max(p.posted_at) FROM posts p
         WHERE p.poster_company_id = v.company_id OR p.poster_lead_id = v.lead_id) AS last_post_at
FROM voices v
LEFT JOIN companies c ON c.id = v.company_id
LEFT JOIN leads     l ON l.id = v.lead_id
WHERE v.removed_at IS NULL;

COMMENT ON VIEW v_voices IS
    'Active voices with kind (company_page | showcase_page | person), name, canonical URL and the posts we hold for each.';
