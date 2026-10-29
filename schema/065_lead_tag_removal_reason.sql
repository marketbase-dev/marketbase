-- 065_lead_tag_removal_reason.sql
-- 050 made every lead_tags DELETE leave an assertions row: the tag, who set
-- it and when, who removed it and when. It did not record WHY. The trigger
-- fills `notes` with the tag's OWN note (the reason it was set), so there was
-- nowhere to say why it came off.
--
-- Caught 2026-10-29. Two left_competitor tags on one instance were wrong:
-- both people are investors in the competitor, not reps who left it. The
-- decision: archive them, with a reason -- provenance for everything. Without
-- a reason column the history would say only that a tool removed them, which
-- reads exactly like the verifier clearing a tag because the person came back.
--
-- Same mechanism as the actor: a per-connection setting the trigger reads, so
-- a caller that forgets it still gets the removal audited, just without a
-- reason. A NEW column rather than appending to `notes`, so the set-time
-- reason and the removal reason stay separable to a reader.

ALTER TABLE assertions ADD COLUMN IF NOT EXISTS removed_reason text;

COMMENT ON COLUMN assertions.removed_reason IS
    'Why the assertion was retired. For lead_tag rows, copied by the 050 '
    'trigger from the marketbase.removal_reason connection setting.';

CREATE OR REPLACE FUNCTION log_lead_tag_removal() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    -- Bookkeeping must never break the operation it is recording. A cascading
    -- delete of the lead itself, for one, leaves nothing to point at.
    BEGIN
        IF EXISTS (SELECT 1 FROM leads WHERE id = OLD.lead_id) THEN
            INSERT INTO assertions (
                subject_lead_id, namespace, key, value, notes,
                asserted_by, asserted_at, removed_by, removed_at,
                removed_reason)
            VALUES (
                OLD.lead_id, 'lead_tag', OLD.tag, 'true', OLD.notes,
                coalesce(OLD.tagged_by, 'unrecorded'),
                coalesce(OLD.tagged_at, now()),
                coalesce(nullif(current_setting('marketbase.actor', true), ''),
                         'unrecorded'),
                now(),
                nullif(current_setting('marketbase.removal_reason', true), ''));
        END IF;
    EXCEPTION WHEN OTHERS THEN
        RAISE WARNING 'lead_tag removal audit failed for lead % tag %: %',
                      OLD.lead_id, OLD.tag, SQLERRM;
    END;
    RETURN OLD;
END;
$$;

COMMENT ON FUNCTION log_lead_tag_removal() IS
    'Writes an append-only assertions row for every lead_tags DELETE, because '
    'the delete itself carries no history. Set marketbase.actor on the '
    'connection to attribute the removal, and marketbase.removal_reason '
    '(SET LOCAL, per transaction) to say why.';
