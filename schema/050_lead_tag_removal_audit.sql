-- 050_lead_tag_removal_audit.sql
-- Removing a lead_tags row is a hard DELETE, so until now every sweep that
-- retired a tag destroyed the fact that it had ever been set. "When did this
-- lead stop being qualified, and who decided?" was unanswerable -- the exact
-- loss `assertions` was introduced to prevent, still happening because nothing
-- ever wrote to it: the table was EMPTY in every instance.
--
-- Caught 2026-09-22, when a stale-cutoff sweep on one instance removed
-- expansion:qualified from 2,060 leads and the record simply vanished. 2,292
-- rows had to be reconstructed by hand from surviving sibling tags, and only
-- because those sibling tags happened to exist.
--
-- A TRIGGER rather than a helper every caller must remember to import: the
-- offenders are spread across tiering, the stale and geo sweeps, verdict
-- substitution, the outcome importer's stale-tag clearing, and ad-hoc psql --
-- and the next one has not been written yet. Audit that can be bypassed by
-- forgetting an import is not audit.
--
-- The row is written with removed_at already set, so it never collides with
-- uq_assertions_active (which indexes only rows WHERE removed_at IS NULL).
-- Tag/untag cycles therefore each leave their own record rather than fighting
-- over one.

CREATE OR REPLACE FUNCTION log_lead_tag_removal() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    -- Bookkeeping must never break the operation it is recording. A cascading
    -- delete of the lead itself, for one, leaves nothing to point at.
    BEGIN
        IF EXISTS (SELECT 1 FROM leads WHERE id = OLD.lead_id) THEN
            INSERT INTO assertions (
                subject_lead_id, namespace, key, value, notes,
                asserted_by, asserted_at, removed_by, removed_at)
            VALUES (
                OLD.lead_id, 'lead_tag', OLD.tag, 'true', OLD.notes,
                coalesce(OLD.tagged_by, 'unrecorded'),
                coalesce(OLD.tagged_at, now()),
                -- tools set this per connection; lib.connect() does it from the
                -- script name, so attribution costs the caller nothing
                coalesce(nullif(current_setting('marketbase.actor', true), ''),
                         'unrecorded'),
                now());
        END IF;
    EXCEPTION WHEN OTHERS THEN
        RAISE WARNING 'lead_tag removal audit failed for lead % tag %: %',
                      OLD.lead_id, OLD.tag, SQLERRM;
    END;
    RETURN OLD;
END;
$$;

DROP TRIGGER IF EXISTS trg_lead_tag_removal_audit ON lead_tags;
CREATE TRIGGER trg_lead_tag_removal_audit
    AFTER DELETE ON lead_tags
    FOR EACH ROW EXECUTE FUNCTION log_lead_tag_removal();

COMMENT ON FUNCTION log_lead_tag_removal() IS
    'Writes an append-only assertions row for every lead_tags DELETE, because '
    'the delete itself carries no history. Set marketbase.actor on the '
    'connection to attribute the removal; lib.connect() does this from the '
    'script name.';
