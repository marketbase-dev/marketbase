-- MarketBase schema, migration 004 — Engager research (Northwind-style pipeline)
--
-- Models the chain: search → posts → engagement → lead

-- ── searches ──────────────────────────────────────────────────────────────
-- Each LinkedIn keyword search we ran (find-linkedin-posts-by-keyword skill).

CREATE TABLE IF NOT EXISTS searches (
    id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    skill                text NOT NULL,                -- 'find-linkedin-posts-by-keyword' / 'engagers-research'
    query                text NOT NULL,
    params               jsonb,                        -- {max_pages, filters, ...}
    ran_at               timestamptz NOT NULL DEFAULT now(),
    total_posts_returned integer,
    cost                 numeric(10,4),
    raw_result_path      text                          -- path to the raw output xlsx, optional
);

CREATE INDEX IF NOT EXISTS idx_searches_query ON searches(query);
CREATE INDEX IF NOT EXISTS idx_searches_ran   ON searches(ran_at);

-- ── posts ─────────────────────────────────────────────────────────────────
-- One row per unique post URN, regardless of how many searches returned it.

CREATE TABLE IF NOT EXISTS posts (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    post_urn            text UNIQUE NOT NULL,           -- numeric urn:li:activity:<id>
    post_url            text,
    poster_name         text,
    poster_linkedin_url text,
    posted_at           timestamptz,
    post_text           text,
    likes               integer,
    comments_count      integer,
    shares              integer,
    post_type           text,                           -- 'ugc' / 'group_post' / etc.
    share_urn           text,                           -- if a reshare/repost, the original urn
    raw_data            jsonb,
    last_scraped_at     timestamptz,
    is_on_topic         boolean,                        -- from keyword-verification step
    off_topic_reason    text                            -- e.g. 'no_mythos_keyword', 'group_post'
);

CREATE INDEX IF NOT EXISTS idx_posts_poster      ON posts(poster_linkedin_url);
CREATE INDEX IF NOT EXISTS idx_posts_likes       ON posts(likes DESC);
CREATE INDEX IF NOT EXISTS idx_posts_on_topic    ON posts(is_on_topic);

-- ── search_posts ──────────────────────────────────────────────────────────
-- Which searches returned which posts (M2M).

CREATE TABLE IF NOT EXISTS search_posts (
    search_id          uuid NOT NULL REFERENCES searches(id) ON DELETE CASCADE,
    post_id            uuid NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    likes_at_scrape    integer,
    position_in_search integer,
    PRIMARY KEY (search_id, post_id)
);

-- ── post_engagements ──────────────────────────────────────────────────────
-- Each reactor + commenter on a post. Every reaction is its own row (no
-- dedup) so we keep full audit trail. The engager joins to leads via lead_id.

DO $$ BEGIN
    CREATE TYPE engagement_type AS ENUM ('reaction', 'comment');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS post_engagements (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    post_id             uuid NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    lead_id             uuid NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    engagement_type     engagement_type NOT NULL,
    reaction_type       text,                           -- LIKE / PRAISE / INTEREST / EMPATHY / etc.
    comment_text        text,
    created_at_linkedin timestamptz,                    -- when the engagement happened on LI
    scraped_at          timestamptz NOT NULL DEFAULT now(),
    raw_data            jsonb
);

CREATE INDEX IF NOT EXISTS idx_pe_post  ON post_engagements(post_id);
CREATE INDEX IF NOT EXISTS idx_pe_lead  ON post_engagements(lead_id);
CREATE INDEX IF NOT EXISTS idx_pe_type  ON post_engagements(engagement_type);
