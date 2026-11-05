-- 082_member_urn_unique.sql
--
-- The follow-up 027 promised: one lead per LinkedIn member URN, enforced.
--
-- 027 added leads.member_urn with only a plain lookup index, because existing
-- duplicates would have made a UNIQUE index fail, and left the partial-unique
-- index uq_leads_member_urn to be created per instance by dedupe_leads.py after
-- it had merged that instance's duplicates, "codified as a follow-up migration
-- once every client is deduped". Two instances got it that way; four never did.
--
-- Every instance is deduped now: merge_lead() (080/081) folded the remaining
-- shared-URN pairs, and it clears a merged row's member_urn. dedupe_leads.py,
-- which deleted rows, is retired, so this migration is now the only creator.
--
-- Refuses rather than half-applies: if any member_urn is still held by two
-- leads, it raises and names how many, and the duplicates go through
-- tools/merge_leads.py first. Same definition as the instances that already
-- carry it, so IF NOT EXISTS leaves those untouched.

DO $$
DECLARE
  v_groups bigint;
BEGIN
  SELECT count(*) INTO v_groups FROM (
    SELECT member_urn FROM leads WHERE member_urn IS NOT NULL
     GROUP BY member_urn HAVING count(*) > 1) d;
  IF v_groups > 0 THEN
    RAISE EXCEPTION '082: % member_urn value(s) are held by more than one lead; merge them with tools/merge_leads.py, then re-run',
      v_groups USING ERRCODE = '23505';
  END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS uq_leads_member_urn ON leads (member_urn)
  WHERE member_urn IS NOT NULL;

COMMENT ON INDEX uq_leads_member_urn IS
  'One lead per LinkedIn member URN (027''s follow-up, 082). A second row for the same '
  'member is a merge (merge_lead, 080), never a second lead.';
