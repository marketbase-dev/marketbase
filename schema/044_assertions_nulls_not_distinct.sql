-- 044_assertions_nulls_not_distinct.sql
-- Repair uq_assertions_active on databases that applied 042 before the fix.
--
-- Postgres treats NULLs as DISTINCT in a unique index unless told otherwise.
-- Every assertion has four NULL subject columns, so the original index could
-- never collide: it enforced nothing, and both guarantees the model rests on
-- were silently absent --
--   "one ACTIVE assertion per claim"  (a re-run doubled 363 rows to 726), and
--   "qualified and disqualified must COLLIDE, not coexist".
-- No-op on a database created after 042 was fixed.

DELETE FROM assertions a USING (
  SELECT id, row_number() OVER (
    PARTITION BY subject_lead_id, subject_company_id, subject_company_edge_id,
                 subject_post_engagement_id, subject_conversation_id, namespace, key
    ORDER BY asserted_at, id) AS rn
  FROM assertions WHERE removed_at IS NULL
) d WHERE a.id = d.id AND d.rn > 1;

DROP INDEX IF EXISTS uq_assertions_active;
CREATE UNIQUE INDEX uq_assertions_active ON assertions (
    subject_lead_id, subject_company_id, subject_company_edge_id,
    subject_post_engagement_id, subject_conversation_id, namespace, key
) NULLS NOT DISTINCT WHERE removed_at IS NULL;
