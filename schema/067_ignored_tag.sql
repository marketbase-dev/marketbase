-- 067_ignored_tag.sql
-- One word for "we stopped monitoring this person": IGNORED, scoped to what
-- they are ignored from. Three tags said it before, in three vocabularies, and
-- the same decision read as "ignored" in Comp Intel and "removed" here, so the
-- two could not be told apart (decided 2026-10-30; GLOSSARY.md).
--
--   state:removed_from_monitoring   -> ignored:comp_intel_target     -- glossary-ok
--   state:ignored_in_workspace      -> ignored:comp_intel_target     -- glossary-ok
--   state:suppressed_in_workspace   -> ignored:comp_intel_target     -- glossary-ok
--
-- Every existing row is ARCHIVED (deleted with marketbase.removal_reason set,
-- so the 050/065 trigger keeps the tag, its note, who set it and when, and why
-- it went) and re-added under the new tag with its note, author and date
-- intact. A lead that carried two of the old tags keeps one new row; the other
-- old row survives in `assertions`. The notes themselves are NOT rewritten
-- here: that is a reviewed mapping (ticket 4), not a rename.
--
-- After the rename, a BEFORE trigger makes the old names fail loudly, no grace
-- period, and holds every `ignored:*` note to one shape:
--
--   <category>:<detail>; via <manual|reviewed_batch|automatic>[ (<tool>)]; <scope> <YYYY-MM-DD>
--
-- built by tools/marketbase/ignored_note.py. The shape check runs only on
-- INSERT and on a change of tag/notes, so the renamed rows (old notes) stand
-- until ticket 4 rewrites them, and nothing else about them is frozen.

-- ── registry ──────────────────────────────────────────────────────────────
INSERT INTO tag_definitions (namespace, key, entity_type, layer, description, deprecated_by) VALUES
  ('ignored', 'comp_intel_target', 'lead', 'state',
   'A decision: this lead is no longer monitored as a competitor target in Comp Intel. '
   'Scoped by the key (what they are ignored from). The note is one fixed shape — '
   '<category>:<detail>; via <manual|reviewed_batch|automatic>[ (<tool>)]; <scope> <date> — '
   'enforced by trigger; build it with marketbase/ignored_note.py. Reversible by Unignore '
   'in Comp Intel; the row is never deleted.', NULL),
  ('state', 'removed_from_monitoring', 'lead', 'state',                                         -- glossary-ok
   'RETIRED 2026-10-30 (067): the same decision as ignored:comp_intel_target, in older words.',
   'ignored:comp_intel_target'),
  ('state', 'ignored_in_workspace', 'lead', 'state',                                            -- glossary-ok
   'RETIRED 2026-10-30 (067): the same decision as ignored:comp_intel_target, in older words.',
   'ignored:comp_intel_target'),
  ('state', 'suppressed_in_workspace', 'lead', 'state',                                         -- glossary-ok
   'RETIRED 2026-10-30 (067): the same decision as ignored:comp_intel_target, in older words.',
   'ignored:comp_intel_target')
ON CONFLICT (namespace, key, entity_type) DO UPDATE
   SET description = EXCLUDED.description, deprecated_by = EXCLUDED.deprecated_by;

-- ── rename: archive + re-add, one transaction, counts asserted ────────────
DO $$
DECLARE
    old_tags  text[] := ARRAY['state:removed_from_monitoring',                                  -- glossary-ok
                              'state:ignored_in_workspace',                                     -- glossary-ok
                              'state:suppressed_in_workspace'];                                 -- glossary-ok
    n_old     int;
    n_leads   int;
    n_new     int;
    n_deleted int;
BEGIN
    PERFORM set_config('marketbase.actor', 'schema 067', true);
    PERFORM set_config('marketbase.removal_reason',
                       'renamed to ignored:comp_intel_target (067)', true);

    SELECT count(*), count(DISTINCT lead_id) INTO n_old, n_leads
      FROM lead_tags WHERE tag = ANY(old_tags);
    IF n_old = 0 THEN
        RAISE NOTICE '067: no retired ignore tags on this instance';
        RETURN;
    END IF;

    -- The most specific old tag wins the single new row: removed_from_monitoring
    -- carried a reason, the other two usually carried a sync note.
    INSERT INTO lead_tags (lead_id, tag, notes, tagged_by, tagged_at)
    SELECT DISTINCT ON (lead_id) lead_id, 'ignored:comp_intel_target', notes, tagged_by, tagged_at
      FROM lead_tags
     WHERE tag = ANY(old_tags)
     ORDER BY lead_id, array_position(old_tags, tag), tagged_at
    ON CONFLICT (lead_id, tag) DO NOTHING;
    GET DIAGNOSTICS n_new = ROW_COUNT;

    SELECT count(*) INTO n_new FROM lead_tags t
     WHERE t.tag = 'ignored:comp_intel_target'
       AND EXISTS (SELECT 1 FROM lead_tags o WHERE o.lead_id = t.lead_id AND o.tag = ANY(old_tags));
    IF n_new <> n_leads THEN
        RAISE EXCEPTION '067: % leads carried a retired ignore tag but % carry the new one; rolled back',
                        n_leads, n_new;
    END IF;

    DELETE FROM lead_tags WHERE tag = ANY(old_tags);
    GET DIAGNOSTICS n_deleted = ROW_COUNT;
    IF n_deleted <> n_old THEN
        RAISE EXCEPTION '067: archived % of % retired rows; rolled back', n_deleted, n_old;
    END IF;
    RAISE NOTICE '067: % retired ignore tags on % leads archived; % ignored:comp_intel_target rows carry them',
                 n_old, n_leads, n_new;
END $$;

-- ── the old names fail loudly; ignored:* notes hold one shape ─────────────
CREATE OR REPLACE FUNCTION lead_tags_ignored_vocabulary() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    category text;
    detail   text;
    via      text;
    tool     text;
BEGIN
    IF NEW.tag IN ('state:removed_from_monitoring', 'state:ignored_in_workspace',              -- glossary-ok
                   'state:suppressed_in_workspace', 'excluded_target')                        -- glossary-ok
       OR NEW.tag LIKE 'excluded:%' THEN
        RAISE EXCEPTION 'tag % is retired (GLOSSARY.md, 2026-10-30): write ignored:comp_intel_target with a note built by marketbase/ignored_note.py',
                        NEW.tag USING ERRCODE = 'check_violation';
    END IF;

    IF NEW.tag LIKE 'ignored:%'
       AND (TG_OP = 'INSERT' OR NEW.tag IS DISTINCT FROM OLD.tag OR NEW.notes IS DISTINCT FROM OLD.notes) THEN
        IF NEW.notes !~ '^(departed_competitor|excluded_role|profile_unavailable|no_current_position_on_profile|duplicate_profile|client_request|other)(:[^;]*)?; via (manual|reviewed_batch|automatic)( \([^)]+\))?; (ignored in Comp Intel|MarketBase only, never a Comp Intel target) \d{4}-\d{2}-\d{2}$' THEN
            RAISE EXCEPTION 'ignored:* note % does not have the shape <category>:<detail>; via <manual|reviewed_batch|automatic>[ (<tool>)]; <ignored in Comp Intel | MarketBase only, never a Comp Intel target> <YYYY-MM-DD> (marketbase/ignored_note.py)',
                            coalesce(NEW.notes, 'NULL') USING ERRCODE = 'check_violation';
        END IF;
        category := split_part(split_part(NEW.notes, ';', 1), ':', 1);
        detail   := btrim(substr(split_part(NEW.notes, ';', 1), length(category) + 2));
        IF category IN ('departed_competitor', 'duplicate_profile', 'other') AND detail = '' THEN
            RAISE EXCEPTION 'ignored:* note: category % needs a detail', category
                  USING ERRCODE = 'check_violation';
        END IF;
        via  := (regexp_match(NEW.notes, '; via (manual|reviewed_batch|automatic)'))[1];
        tool := (regexp_match(NEW.notes, '; via [a-z_]+ \(([^)]+)\)'))[1];
        IF (via = 'manual') = (tool IS NOT NULL) THEN
            RAISE EXCEPTION 'ignored:* note: via manual has no tool, via % names one', via
                  USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS trg_lead_tags_ignored_vocabulary ON lead_tags;
CREATE TRIGGER trg_lead_tags_ignored_vocabulary
    BEFORE INSERT OR UPDATE ON lead_tags
    FOR EACH ROW EXECUTE FUNCTION lead_tags_ignored_vocabulary();

COMMENT ON FUNCTION lead_tags_ignored_vocabulary() IS
    'Refuses the retired ignore tags (GLOSSARY.md) and holds every ignored:* note to '
    'the shape built by marketbase/ignored_note.py. Shape-checked on insert and on a '
    'change of tag or notes only, so rows renamed by 067 keep their original notes '
    'until the reviewed rewrite.';
