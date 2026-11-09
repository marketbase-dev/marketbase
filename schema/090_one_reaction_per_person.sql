-- 090_one_reaction_per_person.sql
--
-- One person's reaction to one post is one row.
--
-- A harvest that read a post's reactions once for every feed the post appeared
-- in, inserting without checking, stored one reaction up to 13 times: 32,315
-- extra copies on one instance, all from two days of one run. The writers add
-- only what is missing since, and the copies were removed by a tool (the
-- earliest of each kept). The database now refuses a second copy.
--
-- Rows a lead merge moved onto the keeper carry merged_from_lead_id: they are
-- the merge's record of the other lead's reaction, so they sit outside the
-- rule, and merging two leads who both reacted to a post never fails.
-- Comments are not covered: a person can comment on a post more than once.

CREATE UNIQUE INDEX IF NOT EXISTS uq_post_engagements_reaction
    ON post_engagements (post_id, lead_id)
    WHERE engagement_type = 'reaction' AND merged_from_lead_id IS NULL;
