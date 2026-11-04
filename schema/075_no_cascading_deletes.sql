-- MarketBase schema, migration 075 — Nothing deletes, foreign keys included
--
-- Every foreign key that CASCADEs or SETs NULL on delete becomes NO ACTION:
-- deleting a row that others point at is REFUSED, never silently propagated.
--
-- Why. A cascade turns one delete into an unbounded, unrecorded one. Deleting a
-- lead took its identities, sources, tags, assertions, meetings and
-- conversations with it, and SET NULL quietly erased the link from the rows it
-- left behind. Nothing records that it happened, and none of it can be read back.
-- The owner's rule (2026-11-04, set first for the identity tables of the
-- sibling app, then for every table here): nothing deletes anything. Duplicates
-- are merged by re-pointing their rows, retired things are soft-deleted with a
-- reason, and a delete that would orphan history should fail loudly so a person
-- sees it.
--
-- How. Generic over the catalog rather than a list, so it also converts keys
-- created by migrations this file does not name. Each key is dropped and re-added
-- under its own name with the same columns, target and ON UPDATE behaviour, in
-- one ALTER TABLE, so no moment exists without it. Existing rows already satisfy
-- every key, so re-adding cannot fail on data.
--
-- From here on a new foreign key must be declared without ON DELETE CASCADE /
-- SET NULL / SET DEFAULT; scripts/check_no_cascading_deletes.py fails CI if one
-- appears.

DO $$
DECLARE
    r      record;
    newdef text;
    n      int := 0;
BEGIN
    FOR r IN
        SELECT c.conname, c.conrelid::regclass AS tbl, pg_get_constraintdef(c.oid) AS def
          FROM pg_constraint c
          JOIN pg_namespace ns ON ns.oid = c.connamespace
         WHERE c.contype = 'f'
           AND ns.nspname = 'public'
           AND c.confdeltype IN ('c', 'n', 'd')
         ORDER BY c.conrelid::regclass::text, c.conname
    LOOP
        newdef := regexp_replace(r.def,
                    '\s+ON DELETE (CASCADE|SET NULL|SET DEFAULT)(\s*\([^)]*\))?', '', 'i');
        IF newdef = r.def THEN
            RAISE EXCEPTION '075: could not strip the delete action from % on %: %',
                            r.conname, r.tbl, r.def;
        END IF;
        EXECUTE format('ALTER TABLE %s DROP CONSTRAINT %I, ADD CONSTRAINT %I %s',
                       r.tbl, r.conname, r.conname, newdef);
        n := n + 1;
    END LOOP;

    IF EXISTS (SELECT 1 FROM pg_constraint c JOIN pg_namespace ns ON ns.oid = c.connamespace
                WHERE c.contype = 'f' AND ns.nspname = 'public'
                  AND c.confdeltype IN ('c', 'n', 'd')) THEN
        RAISE EXCEPTION '075: a cascading foreign key survived the conversion';
    END IF;
    RAISE NOTICE '075: % foreign keys now refuse the delete', n;
END$$;
