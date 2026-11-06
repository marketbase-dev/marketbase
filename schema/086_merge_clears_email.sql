-- 086_merge_clears_email.sql
--
-- A merge clears the dup's email, the way it already clears its URL and URN.
--
-- Found 2026-11-06 auditing the writers: merge_lead() (080-084) copied the dup's
-- email onto a keeper that had none but left it on the dup, so a lookup by
-- leads.email could still land on the merged row and be refused (23514). Two
-- writers do exactly that (a weekly CRM roster refresh, the Luma guest ingest).
-- Owner's decision 2026-11-06: clear it at the source.
--
-- merge_lead_move_email(merge):
--   * the dup has no email: nothing;
--   * the keeper has none: the keeper takes it;
--   * the keeper has its own: the dup's address (lowercased) is filed as a
--     non-primary 'email' identity of the keeper through lead_identity_claim(),
--     unless another lead already owns it (then it is left; the finding is not
--     this merge's to make);
--   * then the dup's email is cleared, and lead_merges.cleared keeps it.
-- merge_lead() calls it; it can also be called for a merge made before 086
-- (tools/merge_leads.py --clear-emails).

CREATE OR REPLACE FUNCTION merge_lead_move_email(_merge uuid) RETURNS text AS $$
DECLARE
  v_m      lead_merges;
  v_dup    text;
  v_keep   text;
  v_val    text;
  v_owner  uuid;
  v_out    text;
BEGIN
  SELECT * INTO v_m FROM lead_merges WHERE id = _merge FOR UPDATE;
  IF v_m.id IS NULL THEN
    RAISE EXCEPTION 'merge_lead_move_email: no lead_merges row %', _merge USING ERRCODE = '22023';
  END IF;
  SELECT email INTO v_dup FROM leads WHERE id = v_m.dup_id;
  IF v_dup IS NULL OR btrim(v_dup) = '' THEN
    RETURN 'none';
  END IF;
  SELECT email INTO v_keep FROM leads WHERE id = v_m.keeper_id;
  v_val := lower(btrim(v_dup));

  IF v_keep IS NULL OR btrim(v_keep) = '' THEN
    UPDATE leads SET email = v_dup WHERE id = v_m.keeper_id AND (email IS NULL OR btrim(email) = '');
    v_out := 'filled';
  ELSIF lower(btrim(v_keep)) = v_val THEN
    v_out := 'same';
  ELSE
    SELECT lead_id INTO v_owner FROM lead_identities WHERE kind = 'email' AND value = v_val;
    IF v_owner IS NULL THEN
      PERFORM lead_identity_claim(v_m.keeper_id, 'email', v_val, 'merge_lead', false);
      v_out := 'identity';
    ELSE
      v_out := CASE WHEN v_owner = v_m.keeper_id THEN 'already the keeper''s' ELSE 'owned by another lead' END;
    END IF;
  END IF;

  UPDATE leads SET email = NULL WHERE id = v_m.dup_id;
  UPDATE lead_merges
     SET cleared = cleared || jsonb_build_object('email', v_dup),
         filled  = filled  || CASE WHEN v_out = 'filled' THEN jsonb_build_object('email', v_dup) ELSE '{}'::jsonb END
   WHERE id = v_m.id;
  RETURN v_out;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION merge_lead_move_email(uuid) IS
  'Clear the merged dup''s email: give it to a keeper with none, else file it as the keeper''s '
  'non-primary email identity (unless another lead owns it). lead_merges.cleared keeps it. '
  'Called by merge_lead(); callable for older merges. Returns none | filled | same | identity | '
  'already the keeper''s | owned by another lead. See 086.';

CREATE OR REPLACE FUNCTION merge_lead(_dup uuid, _keeper uuid, _by text) RETURNS jsonb AS $$
DECLARE
  -- Every lead-id column that MOVES. Checked against pg_constraint below.
  c_moves constant text[] := ARRAY[
    'enrichment_calls.lead_id', 'lead_sources.lead_id', 'lead_qualifications.lead_id',
    'campaign_members.lead_id', 'lead_actions.lead_id', 'posts.poster_lead_id',
    'post_engagements.lead_id', 'lead_tags.lead_id', 'lead_signals.lead_id',
    'lead_conversations.lead_id', 'lead_meetings.lead_id', 'signals.subject_lead_id',
    'assertions.subject_lead_id', 'voices.lead_id', 'lead_touches.lead_id',
    'crm_leads.lead_id', 'crm_contacts.lead_id'];
  -- Lead-id columns handled on their own terms, never by the generic move.
  c_special constant text[] := ARRAY[
    'lead_identities.lead_id',                    -- step 3: moved as non-primary
    'lead_identity_conflicts.lead_id',            -- step 5: the pair's finding resolved,
    'lead_identity_conflicts.owner_lead_id',      --   the dup's others handed to the keeper
    'lead_merges.dup_id', 'lead_merges.keeper_id',
    'leads.merged_into'];
  -- leads columns copied onto the keeper where the keeper's is NULL. Every other
  -- column is in c_keep or c_ident. A column in none of them makes the merge refuse.
  c_fill constant text[] := ARRAY[
    'name', 'headline', 'current_title', 'current_company', 'current_company_url', 'city',
    'country', 'bio', 'last_enriched_at'];
  -- The dup's identity columns: cleared on the dup (leads.linkedin_url is UNIQUE,
  -- and an upsert keyed on it must land on the keeper, not the retired row), then
  -- given to the keeper where the keeper's is NULL. The values are never lost:
  -- they are the keeper's lead_identities now, and lead_merges.cleared keeps them.
  c_ident constant text[] := ARRAY['linkedin_url', 'linkedin_urn', 'member_urn', 'public_id'];
  -- email: moved by merge_lead_move_email() after the merge row exists (086)
  c_keep constant text[] := ARRAY['id', 'created_at', 'updated_at', 'merged_into', 'email'];

  v_dup         leads;
  v_keeper      leads;
  v_urn         text;
  v_unlisted    text;
  v_entry       text;
  v_tbl         text;
  v_col         text;
  v_n           bigint;
  v_moved       jsonb := '{}';
  v_stayed      jsonb := '{}';
  v_filled      jsonb := '{}';
  v_cleared     jsonb := '{}';
  v_replaced    jsonb := '{}';
  v_ids         jsonb;
  v_rid         uuid;
  v_merge_id    uuid := gen_random_uuid();
  v_rows_before jsonb;
  v_before      bigint;
  v_after       bigint;
  v_val         jsonb;
  v_retired     int;
  v_email       text;
BEGIN
  -- ── 1. refusals ──────────────────────────────────────────────────────────
  -- First, before anything about the pair: the registry must cover every FK to
  -- leads, and c_fill/c_ident/c_keep every leads column.
  SELECT string_agg(x, ', ') INTO v_unlisted FROM (
    SELECT c.conrelid::regclass::text || '.' || a.attname AS x
      FROM pg_constraint c
      JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY(c.conkey)
     WHERE c.contype = 'f' AND c.confrelid = 'leads'::regclass
    EXCEPT SELECT unnest(c_moves || c_special)) u;
  IF v_unlisted IS NOT NULL THEN
    RAISE EXCEPTION 'merge_lead: unclassified FK column(s) to leads: %. Add each to c_moves or c_special (and a merged_from_ column, and the refusal trigger) first.',
      v_unlisted USING ERRCODE = '0A000';
  END IF;
  SELECT string_agg(column_name, ', ') INTO v_unlisted FROM (
    SELECT column_name::text FROM information_schema.columns
     WHERE table_schema = current_schema() AND table_name = 'leads'
    EXCEPT SELECT unnest(c_fill || c_ident || c_keep)) u;
  IF v_unlisted IS NOT NULL THEN
    RAISE EXCEPTION 'merge_lead: unclassified leads column(s): %. Add each to c_fill, c_ident or c_keep first.',
      v_unlisted USING ERRCODE = '0A000';
  END IF;

  IF _dup IS NULL OR _keeper IS NULL OR _by IS NULL OR btrim(_by) = '' THEN
    RAISE EXCEPTION 'merge_lead: dup, keeper and by are all required' USING ERRCODE = '22004';
  END IF;
  IF _dup = _keeper THEN
    RAISE EXCEPTION 'merge_lead: dup and keeper are the same row %', _dup USING ERRCODE = '22023';
  END IF;
  SELECT * INTO v_dup FROM leads WHERE id = _dup FOR UPDATE;
  SELECT * INTO v_keeper FROM leads WHERE id = _keeper FOR UPDATE;
  IF v_dup.id IS NULL OR v_keeper.id IS NULL THEN
    RAISE EXCEPTION 'merge_lead: no such lead (dup %, keeper %)', _dup, _keeper USING ERRCODE = '22023';
  END IF;
  IF v_dup.merged_into IS NOT NULL OR v_keeper.merged_into IS NOT NULL THEN
    RAISE EXCEPTION 'merge_lead: already merged (dup % into %, keeper % into %)',
      _dup, v_dup.merged_into, _keeper, v_keeper.merged_into USING ERRCODE = '22023';
  END IF;
  -- The identity system must already say they are one human: one of them owns a
  -- linkedin_urn identity and the other holds an open conflict on that value.
  SELECT i.value INTO v_urn
    FROM lead_identities i
    JOIN lead_identity_conflicts c
      ON c.kind = i.kind AND c.value = i.value AND c.owner_lead_id = i.lead_id
     AND c.resolved_by_merge IS NULL
   WHERE i.kind = 'linkedin_urn'
     AND ((i.lead_id = _keeper AND c.lead_id = _dup) OR (i.lead_id = _dup AND c.lead_id = _keeper))
   LIMIT 1;
  IF v_urn IS NULL THEN
    RAISE EXCEPTION 'merge_lead: % and % share no linkedin_urn identity; never merge without a verified URN',
      _dup, _keeper USING ERRCODE = '22023';
  END IF;

  -- Row counts of every table touched, to prove nothing is lost.
  SELECT jsonb_object_agg(t, 0) INTO v_rows_before
    FROM (SELECT DISTINCT split_part(e, '.', 1) AS t
            FROM unnest(c_moves || ARRAY['lead_identities.lead_id', 'leads.id']) e) s;
  FOR v_tbl IN SELECT jsonb_object_keys(v_rows_before) LOOP
    EXECUTE format('SELECT count(*) FROM %I', v_tbl) INTO v_n;
    v_rows_before := jsonb_set(v_rows_before, ARRAY[v_tbl], to_jsonb(v_n));
  END LOOP;

  -- ── 2. re-point, colliding rows stay ─────────────────────────────────────
  FOREACH v_entry IN ARRAY c_moves LOOP
    v_tbl := split_part(v_entry, '.', 1);
    v_col := split_part(v_entry, '.', 2);
    v_n := 0;
    IF v_tbl = 'lead_tags' THEN
      -- The dup's own "I am a duplicate" pair describes the dup, not the human:
      -- it stays, or the keeper would come out flagged and never pushed. A
      -- do_not_push is part of that pair only when the dup also carries the flag.
      EXECUTE $q$
        WITH m AS (UPDATE lead_tags t SET lead_id = $2, merged_from_lead_id = $1
                    WHERE t.lead_id = $1
                      AND t.tag <> 'flag:duplicate_lead'
                      AND NOT (t.tag = 'expansion:do_not_push'
                               AND EXISTS (SELECT 1 FROM lead_tags f
                                            WHERE f.lead_id = $1 AND f.tag = 'flag:duplicate_lead'))
                      AND NOT EXISTS (SELECT 1 FROM lead_tags k WHERE k.lead_id = $2 AND k.tag = t.tag)
                    RETURNING 1) SELECT count(*) FROM m $q$
        USING _dup, _keeper INTO v_n;
    ELSE
      BEGIN
        -- Fast path: everything moves in one statement.
        EXECUTE format('WITH m AS (UPDATE %I SET %I = $2, %I = $1 WHERE %I = $1 RETURNING 1)
                        SELECT count(*) FROM m', v_tbl, v_col, 'merged_from_' || v_col, v_col)
          USING _dup, _keeper INTO v_n;
      EXCEPTION WHEN unique_violation THEN
        -- Something collides: row by row, so each colliding row stays where it
        -- is. The real unique indexes decide, partial ones included.
        v_n := 0;
        FOR v_rid IN EXECUTE format('SELECT id FROM %I WHERE %I = $1', v_tbl, v_col) USING _dup LOOP
          BEGIN
            EXECUTE format('UPDATE %I SET %I = $2, %I = $1 WHERE id = $3',
                           v_tbl, v_col, 'merged_from_' || v_col)
              USING _dup, _keeper, v_rid;
            v_n := v_n + 1;
          EXCEPTION WHEN unique_violation THEN
            NULL;   -- stays on the dup
          END;
        END LOOP;
      END;
    END IF;
    IF v_n > 0 THEN
      v_moved := v_moved || jsonb_build_object(v_entry, v_n);
    END IF;

    -- What stayed, by id (every child table's primary key is id).
    EXECUTE format('SELECT jsonb_agg(id ORDER BY id) FROM %I WHERE %I = $1', v_tbl, v_col)
      USING _dup INTO v_ids;
    IF v_ids IS NOT NULL THEN
      v_stayed := v_stayed || jsonb_build_object(v_entry, v_ids);
    END IF;
  END LOOP;

  -- ── 3. identities: the dup's become the keeper's, never primary ──────────
  UPDATE lead_identities
     SET lead_id = _keeper, is_primary = false, merged_from_lead_id = _dup
   WHERE lead_id = _dup;
  GET DIAGNOSTICS v_n = ROW_COUNT;
  IF v_n > 0 THEN
    v_moved := v_moved || jsonb_build_object('lead_identities.lead_id', v_n);
  END IF;

  -- ── 4. the dup's identity columns cleared; the keeper's NULLs filled ─────
  FOREACH v_col IN ARRAY c_ident LOOP
    v_val := to_jsonb(v_dup) -> v_col;
    IF v_val IS NOT NULL AND v_val <> 'null'::jsonb THEN
      v_cleared := v_cleared || jsonb_build_object(v_col, v_val);
    END IF;
  END LOOP;
  IF v_cleared <> '{}'::jsonb THEN
    -- one statement, so the BEFORE trigger cannot re-derive member_urn/public_id
    -- from a column cleared in the same breath
    UPDATE leads SET linkedin_url = NULL, linkedin_urn = NULL, member_urn = NULL, public_id = NULL
     WHERE id = _dup;
  END IF;
  -- A keeper keyed on its URN-form URL (/in/ACoAA...) takes the dup's vanity URL:
  -- a URN-form URL is not a vanity identity, and left in place an upsert keyed on
  -- the vanity URL would create the twin again. The replaced value is kept in
  -- lead_merges.replaced; the keeper's member_urn is untouched.
  IF v_keeper.linkedin_url ~* '/in/AC[A-Za-z0-9_-]{15,}'
     AND (v_cleared ->> 'linkedin_url') IS NOT NULL
     AND (v_cleared ->> 'linkedin_url') !~* '/in/AC[A-Za-z0-9_-]{15,}' THEN
    UPDATE leads SET linkedin_url = v_cleared ->> 'linkedin_url' WHERE id = _keeper;
    v_replaced := jsonb_build_object('linkedin_url', v_keeper.linkedin_url);
    v_filled := v_filled || jsonb_build_object('linkedin_url', v_cleared -> 'linkedin_url');
  END IF;
  FOREACH v_col IN ARRAY c_fill || c_ident LOOP
    v_val := CASE WHEN v_col = ANY(c_ident) THEN v_cleared -> v_col ELSE to_jsonb(v_dup) -> v_col END;
    IF (to_jsonb(v_keeper) -> v_col) IS NULL OR (to_jsonb(v_keeper) -> v_col) = 'null'::jsonb THEN
      IF v_val IS NOT NULL AND v_val <> 'null'::jsonb THEN
        -- "AND IS NULL" again: filling linkedin_url lets the BEFORE trigger derive
        -- member_urn/public_id, so a later column may already be set.
        EXECUTE format('UPDATE leads SET %1$I = ($2::jsonb ->> 0)::%2$s WHERE id = $1 AND %1$I IS NULL',
                       v_col,
                       (SELECT format_type(atttypid, atttypmod) FROM pg_attribute
                         WHERE attrelid = 'leads'::regclass AND attname = v_col))
          USING _keeper, jsonb_build_array(v_val);
        GET DIAGNOSTICS v_n = ROW_COUNT;
        IF v_n > 0 THEN
          v_filled := v_filled || jsonb_build_object(v_col, v_val);
        END IF;
      END IF;
    END IF;
  END LOOP;

  -- ── 5. mark, record, resolve ─────────────────────────────────────────────
  UPDATE leads SET merged_into = _keeper WHERE id = _dup;
  INSERT INTO lead_merges (id, dup_id, keeper_id, merged_by, moved, stayed, filled, cleared, replaced)
  VALUES (v_merge_id, _dup, _keeper, _by, v_moved, v_stayed, v_filled, v_cleared, v_replaced);
  UPDATE lead_identity_conflicts
     SET resolved_by_merge = v_merge_id
   WHERE resolved_by_merge IS NULL
     AND ((lead_id = _dup AND owner_lead_id = _keeper) OR (lead_id = _keeper AND owner_lead_id = _dup));
  -- The dup's other open findings now describe the keeper: the identities a third
  -- lead collided with moved to the keeper (step 3), and what the dup carried is
  -- the keeper's now. Re-pointed with provenance, so a group of three or more
  -- merges one pair at a time. One the keeper already holds stays where it is.
  UPDATE lead_identity_conflicts c
     SET owner_lead_id = _keeper, merged_from_owner_lead_id = _dup
   WHERE c.resolved_by_merge IS NULL AND c.owner_lead_id = _dup AND c.lead_id <> _keeper;
  UPDATE lead_identity_conflicts c
     SET lead_id = _keeper, merged_from_lead_id = _dup
   WHERE c.resolved_by_merge IS NULL AND c.lead_id = _dup AND c.owner_lead_id <> _keeper
     AND NOT EXISTS (SELECT 1 FROM lead_identity_conflicts k
                      WHERE k.lead_id = _keeper AND k.kind = c.kind AND k.value = c.value);
  -- The keeper's own "same person as <the dup>" flags are resolved by this merge.
  v_retired := retire_merge_flags(v_merge_id);
  -- The dup's email: cleared, given to the keeper or filed as its identity (086).
  v_email := merge_lead_move_email(v_merge_id);

  -- ── 6. nothing lost: every touched table has exactly its row count, except
  -- the retired flags, each of which left exactly one assertions row (083) ──
  FOR v_tbl IN SELECT jsonb_object_keys(v_rows_before) LOOP
    EXECUTE format('SELECT count(*) FROM %I', v_tbl) INTO v_after;
    v_before := (v_rows_before ->> v_tbl)::bigint
                + CASE v_tbl WHEN 'lead_tags' THEN -v_retired WHEN 'assertions' THEN v_retired
                              WHEN 'lead_identities' THEN CASE WHEN v_email = 'identity' THEN 1 ELSE 0 END
                              ELSE 0 END;
    IF v_after <> v_before THEN
      RAISE EXCEPTION 'merge_lead: %.count changed % -> %; rolled back', v_tbl, v_before, v_after;
    END IF;
  END LOOP;

  RETURN (SELECT to_jsonb(m) FROM lead_merges m WHERE m.id = v_merge_id);
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION merge_lead(uuid, uuid, text) IS
  'Fold dup into keeper: re-point every lead-id column (colliding rows, and the dup''s '
  'own duplicate tag pair, stay on the dup; the keeper''s flags naming the dup are retired, '
  'audited, by retire_merge_flags), move the dup''s identities as non-primary, '
  'clear the dup''s identity columns and fill the keeper''s NULL columns (a URN-form keeper URL '
  'takes the dup''s vanity URL), set '
  'dup.merged_into, resolve the pair''s conflicts, record lead_merges. Deletes no lead and '
  'no history: the only rows removed are the resolved flags, each audited. '
  'Refuses without a shared linkedin_urn identity. Mechanism only: who keeps, and '
  'approval, are tools/merge_leads.py''s.';
