-- 080_lead_merge.sql
--
-- merge_lead(dup, keeper): fold a duplicate lead into its keeper by re-pointing,
-- stamping provenance, and deleting nothing (.scratch/identity-merge/issues/01).
--
-- The shape is the one the comp-intel side built for persons (merge_person),
-- so the two databases behave identically ahead of their unification:
--   * leads.merged_into marks the dup; one lead_merges row per merge.
--   * Every lead-id column is re-pointed in place, with merged_from_<column>
--     beside it holding the dup the row came from. A row that would collide
--     with one the keeper already holds (campaign_members (lead_id,
--     campaign_id), lead_tags (lead_id, tag), ...) STAYS on the dup untouched:
--     the pre-merge fact is true, and it is the provenance.
--   * A trigger on every child table REFUSES (never redirects) a write aimed at
--     a merged lead. A redirect would hide a caller that still believes in a
--     retired row; a refusal makes it fail loudly and get fixed.
--   * lead_identity_conflicts records every "same person twice" finding that
--     lead_identity_claim() makes. The flag:duplicate_lead +
--     expansion:do_not_push tag pair stays as a derived convenience; the row is
--     the finding, and unresolved linkedin_urn rows are the merge queue.
--   * merge_lead() refuses unless the pair already shares a linkedin_urn
--     identity: one owns it and the other holds an open conflict on it. A
--     vanity URL or a name is never enough (an empty or mangled URN groups
--     unrelated people; the claim never files an empty value).
--
-- This supersedes the 2026-10-10 plan (resolve on read, a person_rows view,
-- a denormalised canonical_id): that plan needed a trigger-kept canonical
-- column on every child table anyway. tools/dedupe_leads.py, which moved
-- children and then DELETEd the duplicate, now refuses and points here.
--
-- The column registry inside merge_lead() is checked against pg_constraint on
-- every call: a new FK column to leads that nobody classified makes the merge
-- refuse, instead of silently leaving rows on the dup.
-- scripts/check_merge_lead.py asserts the same in CI.
--
-- Idempotent: IF NOT EXISTS, CREATE OR REPLACE, DROP TRIGGER IF EXISTS,
-- ON CONFLICT DO NOTHING.

-- ── 1. The marks ─────────────────────────────────────────────────────────────

ALTER TABLE leads ADD COLUMN IF NOT EXISTS merged_into uuid REFERENCES leads(id);
CREATE INDEX IF NOT EXISTS idx_leads_merged_into ON leads (merged_into)
  WHERE merged_into IS NOT NULL;
COMMENT ON COLUMN leads.merged_into IS
  'Set by merge_lead(): this row is a duplicate folded into that keeper. Writes '
  'aimed at a merged lead are refused by refuse_inserts_of_merged_lead / '
  'refuse_updates_to_merged_lead. Read the keeper.';

-- Provenance beside every lead-id column: merged_from_<column> = the dup the
-- row was moved from. No FK: it is history, not a reference to follow.
ALTER TABLE enrichment_calls    ADD COLUMN IF NOT EXISTS merged_from_lead_id uuid;
ALTER TABLE lead_sources        ADD COLUMN IF NOT EXISTS merged_from_lead_id uuid;
ALTER TABLE lead_qualifications ADD COLUMN IF NOT EXISTS merged_from_lead_id uuid;
ALTER TABLE campaign_members    ADD COLUMN IF NOT EXISTS merged_from_lead_id uuid;
ALTER TABLE lead_actions        ADD COLUMN IF NOT EXISTS merged_from_lead_id uuid;
ALTER TABLE posts               ADD COLUMN IF NOT EXISTS merged_from_poster_lead_id uuid;
ALTER TABLE post_engagements    ADD COLUMN IF NOT EXISTS merged_from_lead_id uuid;
ALTER TABLE lead_tags           ADD COLUMN IF NOT EXISTS merged_from_lead_id uuid;
ALTER TABLE lead_signals        ADD COLUMN IF NOT EXISTS merged_from_lead_id uuid;
ALTER TABLE lead_conversations  ADD COLUMN IF NOT EXISTS merged_from_lead_id uuid;
ALTER TABLE lead_meetings       ADD COLUMN IF NOT EXISTS merged_from_lead_id uuid;
ALTER TABLE lead_identities     ADD COLUMN IF NOT EXISTS merged_from_lead_id uuid;
ALTER TABLE signals             ADD COLUMN IF NOT EXISTS merged_from_subject_lead_id uuid;
ALTER TABLE assertions          ADD COLUMN IF NOT EXISTS merged_from_subject_lead_id uuid;
ALTER TABLE voices              ADD COLUMN IF NOT EXISTS merged_from_lead_id uuid;
ALTER TABLE lead_touches        ADD COLUMN IF NOT EXISTS merged_from_lead_id uuid;
ALTER TABLE crm_leads           ADD COLUMN IF NOT EXISTS merged_from_lead_id uuid;
ALTER TABLE crm_contacts        ADD COLUMN IF NOT EXISTS merged_from_lead_id uuid;

-- ── 2. The finding, the ledger, the snapshot ─────────────────────────────────

CREATE TABLE IF NOT EXISTS lead_identity_conflicts (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  lead_id           uuid NOT NULL REFERENCES leads(id),   -- the lead that carries the value
  kind              text NOT NULL REFERENCES lead_identity_kinds(name),
  value             text NOT NULL,
  owner_lead_id     uuid NOT NULL REFERENCES leads(id),   -- the lead lead_identities says owns it
  found_at          timestamptz NOT NULL DEFAULT now(),
  source            text,
  resolved_by_merge uuid,                                 -- lead_merges.id that settled it
  merged_from_lead_id       uuid,                         -- set when a merge handed the finding to its keeper
  merged_from_owner_lead_id uuid,
  CONSTRAINT lead_identity_conflicts_once UNIQUE (lead_id, kind, value),
  CONSTRAINT lead_identity_conflicts_not_self CHECK (lead_id <> owner_lead_id)
);
CREATE INDEX IF NOT EXISTS idx_lead_identity_conflicts_owner ON lead_identity_conflicts (owner_lead_id);
CREATE INDEX IF NOT EXISTS idx_lead_identity_conflicts_open ON lead_identity_conflicts (kind, value)
  WHERE resolved_by_merge IS NULL;
COMMENT ON TABLE lead_identity_conflicts IS
  'One row per "same person twice" finding: lead_id carries an identity value that '
  'lead_identities says owner_lead_id owns. Written by lead_identity_claim() (and '
  'backfilled by 080 from the leads columns). Unresolved linkedin_urn rows are the '
  'merge queue (view lead_merge_queue); merge_lead() resolves the pair''s rows.';

CREATE TABLE IF NOT EXISTS lead_merges (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  dup_id       uuid NOT NULL REFERENCES leads(id),
  keeper_id    uuid NOT NULL REFERENCES leads(id),
  merged_at    timestamptz NOT NULL DEFAULT now(),
  merged_by    text NOT NULL,
  moved        jsonb NOT NULL,     -- {"table.column": rows moved}
  stayed       jsonb NOT NULL,     -- {"table.column": [ids of rows left on the dup]}
  filled       jsonb NOT NULL,     -- {"column": value copied onto a NULL keeper column}
  cleared      jsonb NOT NULL,     -- {"column": the dup's value, cleared so the keeper can hold it}
  snapshot_ref text,               -- set by tools/merge_leads.py: snapshot id in merge_snapshot_rows
  CONSTRAINT lead_merges_one_per_dup UNIQUE (dup_id),
  CONSTRAINT lead_merges_not_self CHECK (dup_id <> keeper_id)
);
COMMENT ON TABLE lead_merges IS
  'One row per merge_lead() call: what moved, what stayed on the dup, which keeper '
  'columns were filled and which dup identity columns were cleared. Written only by '
  'merge_lead().';

CREATE TABLE IF NOT EXISTS merge_snapshot_rows (
  snapshot_id uuid NOT NULL,
  taken_at    timestamptz NOT NULL DEFAULT now(),
  tbl         text NOT NULL,
  row_data    jsonb NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_merge_snapshot_rows_snapshot ON merge_snapshot_rows (snapshot_id);
COMMENT ON TABLE merge_snapshot_rows IS
  'Pre-merge copy of every row a merge touches, written by tools/merge_leads.py in '
  'the same transaction as the merge. Kept in the database, never on disk.';

-- ── 3. The claim records the finding ─────────────────────────────────────────
-- Same function as 071; the only change is the conflict row on the duplicate
-- outcome. The tag pair still lands, so every existing reader keeps working.

CREATE OR REPLACE FUNCTION lead_identity_claim(p_lead uuid, p_kind text, p_value text,
                                               p_source text,
                                               p_primary boolean DEFAULT true,
                                               p_verified_at timestamptz DEFAULT NULL)
RETURNS text AS $$
DECLARE
  v_owner     uuid;
  v_owner_url text;
  v_note      text;
BEGIN
  IF p_value IS NULL OR p_value = '' THEN
    RETURN 'empty';
  END IF;

  SELECT lead_id INTO v_owner
    FROM lead_identities WHERE kind = p_kind AND value = p_value;

  IF v_owner IS NULL THEN
    IF p_primary THEN
      UPDATE lead_identities SET is_primary = false
       WHERE lead_id = p_lead AND kind = p_kind AND is_primary;
    END IF;
    BEGIN
      INSERT INTO lead_identities (lead_id, kind, value, is_primary, verified_at, source)
      VALUES (p_lead, p_kind, p_value, p_primary, p_verified_at, p_source);
      RETURN 'inserted';
    EXCEPTION WHEN unique_violation THEN
      -- a concurrent writer claimed it between the SELECT and the INSERT
      SELECT lead_id INTO v_owner
        FROM lead_identities WHERE kind = p_kind AND value = p_value;
      IF v_owner IS NULL THEN
        v_owner := p_lead;
      END IF;
    END;
  END IF;

  IF v_owner = p_lead THEN
    IF p_primary THEN
      -- it is the value on the row now, so it is the primary
      UPDATE lead_identities SET is_primary = false
       WHERE lead_id = p_lead AND kind = p_kind AND is_primary AND value <> p_value;
      UPDATE lead_identities SET is_primary = true
       WHERE lead_id = p_lead AND kind = p_kind AND value = p_value AND NOT is_primary;
    END IF;
    IF p_verified_at IS NOT NULL THEN
      UPDATE lead_identities SET verified_at = p_verified_at
       WHERE lead_id = p_lead AND kind = p_kind AND value = p_value AND verified_at IS NULL;
    END IF;
    RETURN 'ours';
  END IF;

  -- owned by a different lead: the same person twice. Never reassign; record.
  SELECT linkedin_url INTO v_owner_url FROM leads WHERE id = v_owner;
  v_note := format('same person as lead %s (%s): %s %s is already that lead''s identity; '
                   'kept, never push; recorded by %s',
                   v_owner, coalesce(v_owner_url, '?'), p_kind, p_value, p_source);
  INSERT INTO lead_tags (lead_id, tag, notes, tagged_by) VALUES
    (p_lead, 'flag:duplicate_lead',    v_note, p_source),
    (p_lead, 'expansion:do_not_push',  v_note, p_source)
  ON CONFLICT (lead_id, tag) DO NOTHING;
  INSERT INTO lead_identity_conflicts (lead_id, kind, value, owner_lead_id, source)
  VALUES (p_lead, p_kind, p_value, v_owner, p_source)
  ON CONFLICT (lead_id, kind, value) DO NOTHING;
  RETURN 'duplicate:' || v_owner::text;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION lead_identity_claim(uuid, text, text, text, boolean, timestamptz) IS
  'Record that a lead carries an identity value; the ONE write path into '
  'lead_identities for the trigger and every tool. Owned by nobody: insert '
  '(primary unless p_primary is false). Owned by this lead: promote when '
  'p_primary, fill a NULL verified_at from p_verified_at. Owned by another lead: '
  'leave the owner alone, write a lead_identity_conflicts row, and tag this lead '
  'flag:duplicate_lead + expansion:do_not_push with a note naming the owner. '
  'Returns inserted | ours | duplicate:<owner id> | empty.';

UPDATE tag_definitions
   SET description = 'This row is the same person as another lead: an identity it carries (vanity '
                     'URL, member URN or e-mail) is already that other lead''s. The finding is the '
                     'lead_identity_conflicts row; this tag is its convenience copy. merge_lead() '
                     '(080) folds the pair; the tag stays on the merged row. Written by '
                     'lead_identity_claim(), which every identity writer goes through.'
 WHERE namespace = 'flag' AND key = 'duplicate_lead' AND entity_type = 'lead';

-- ── 4. Backfill the findings already on record ──────────────────────────────
-- Every lead whose own linkedin_url or member_urn is a lead_identities value
-- owned by ANOTHER lead is a "same person twice" the claim already saw (that is
-- how the tag pair got there) but had nowhere to record. Derived from the data,
-- the same on every instance; empty values never match.

INSERT INTO lead_identity_conflicts (lead_id, kind, value, owner_lead_id, source, found_at)
SELECT l.id, v.kind, v.value, i.lead_id, '080_lead_merge backfill', now()
  FROM leads l
 CROSS JOIN LATERAL (VALUES
        ('linkedin_url',        CASE WHEN l.linkedin_url !~* '/in/AC[A-Za-z0-9_-]{15,}' THEN l.linkedin_url END),
        ('linkedin_urn',        CASE WHEN l.member_urn LIKE 'ACoAA%' THEN l.member_urn END),
        ('linkedin_urn_search', CASE WHEN l.member_urn LIKE 'ACwAA%' THEN l.member_urn END)
      ) AS v(kind, value)
  JOIN lead_identities i ON i.kind = v.kind AND i.value = v.value AND i.lead_id <> l.id
 WHERE v.value IS NOT NULL AND v.value <> ''
ON CONFLICT (lead_id, kind, value) DO NOTHING;

-- ── 5. The queue ─────────────────────────────────────────────────────────────

CREATE OR REPLACE VIEW lead_merge_queue AS
SELECT c.id AS conflict_id, c.kind, c.value,
       c.owner_lead_id, o.name AS owner_name, o.linkedin_url AS owner_url, o.created_at AS owner_created_at,
       c.lead_id AS other_lead_id, l.name AS other_name, l.linkedin_url AS other_url, l.created_at AS other_created_at,
       c.found_at, c.source
  FROM lead_identity_conflicts c
  JOIN leads o ON o.id = c.owner_lead_id
  JOIN leads l ON l.id = c.lead_id
 WHERE c.resolved_by_merge IS NULL
   AND c.kind = 'linkedin_urn'
   AND o.merged_into IS NULL AND l.merged_into IS NULL;
COMMENT ON VIEW lead_merge_queue IS
  'Pairs the identity system says are one human (a shared linkedin_urn: one owns it, '
  'the other holds an open conflict on it) and neither is merged yet. The only pairs '
  'merge_lead() accepts.';

-- ── 6. The refusal ───────────────────────────────────────────────────────────
-- TG_ARGV = the lead-id columns of the table it is attached to. INSERTs are
-- checked once per STATEMENT against the inserted rows (a transition table),
-- so bulk loaders pay one join, not one lookup per row. UPDATEs of a lead
-- column are rare and checked per row.

CREATE OR REPLACE FUNCTION refuse_inserts_of_merged_lead() RETURNS trigger AS $$
DECLARE
  v_col  text;
  v_id   uuid;
  v_into uuid;
BEGIN
  FOREACH v_col IN ARRAY TG_ARGV LOOP
    EXECUTE format('SELECT n.%1$I, l.merged_into FROM new_rows n JOIN leads l ON l.id = n.%1$I
                     WHERE l.merged_into IS NOT NULL LIMIT 1', v_col)
      INTO v_id, v_into;
    IF v_id IS NOT NULL THEN
      RAISE EXCEPTION '%.% = % is a merged lead (merged into %); write to the keeper instead',
        TG_TABLE_NAME, v_col, v_id, v_into
        USING ERRCODE = '23514';
    END IF;
  END LOOP;
  RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION refuse_updates_to_merged_lead() RETURNS trigger AS $$
DECLARE
  v_new  jsonb := to_jsonb(NEW);
  v_old  jsonb := to_jsonb(OLD);
  v_col  text;
  v_id   uuid;
  v_into uuid;
BEGIN
  FOREACH v_col IN ARRAY TG_ARGV LOOP
    v_id := (v_new ->> v_col)::uuid;
    IF v_id IS NULL OR v_id IS NOT DISTINCT FROM (v_old ->> v_col)::uuid THEN
      CONTINUE;    -- unset, or this column did not change
    END IF;
    SELECT merged_into INTO v_into FROM leads WHERE id = v_id;
    IF v_into IS NOT NULL THEN
      RAISE EXCEPTION '%.% = % is a merged lead (merged into %); write to the keeper instead',
        TG_TABLE_NAME, v_col, v_id, v_into
        USING ERRCODE = '23514';
    END IF;
  END LOOP;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DO $$
DECLARE
  r      record;
  v_cols text;
  v_args text;
BEGIN
  FOR r IN SELECT * FROM (VALUES
      ('enrichment_calls',        ARRAY['lead_id']),
      ('lead_sources',            ARRAY['lead_id']),
      ('lead_qualifications',     ARRAY['lead_id']),
      ('campaign_members',        ARRAY['lead_id']),
      ('lead_actions',            ARRAY['lead_id']),
      ('posts',                   ARRAY['poster_lead_id']),
      ('post_engagements',        ARRAY['lead_id']),
      ('lead_tags',               ARRAY['lead_id']),
      ('lead_signals',            ARRAY['lead_id']),
      ('lead_conversations',      ARRAY['lead_id']),
      ('lead_meetings',           ARRAY['lead_id']),
      ('lead_identities',         ARRAY['lead_id']),
      ('signals',                 ARRAY['subject_lead_id']),
      ('assertions',              ARRAY['subject_lead_id']),
      ('voices',                  ARRAY['lead_id']),
      ('lead_touches',            ARRAY['lead_id']),
      ('crm_leads',               ARRAY['lead_id']),
      ('crm_contacts',            ARRAY['lead_id']),
      ('lead_identity_conflicts', ARRAY['lead_id', 'owner_lead_id'])
    ) AS t(tbl, cols)
  LOOP
    v_cols := (SELECT string_agg(quote_ident(c), ', ') FROM unnest(r.cols) c);
    v_args := (SELECT string_agg(quote_literal(c), ', ') FROM unnest(r.cols) c);
    EXECUTE format('DROP TRIGGER IF EXISTS trg_refuse_merged_lead_ins ON %I', r.tbl);
    EXECUTE format(
      'CREATE TRIGGER trg_refuse_merged_lead_ins AFTER INSERT ON %I '
      'REFERENCING NEW TABLE AS new_rows FOR EACH STATEMENT '
      'EXECUTE FUNCTION refuse_inserts_of_merged_lead(%s)', r.tbl, v_args);
    EXECUTE format('DROP TRIGGER IF EXISTS trg_refuse_merged_lead_upd ON %I', r.tbl);
    EXECUTE format(
      'CREATE TRIGGER trg_refuse_merged_lead_upd BEFORE UPDATE OF %s ON %I '
      'FOR EACH ROW EXECUTE FUNCTION refuse_updates_to_merged_lead(%s)', v_cols, r.tbl, v_args);
  END LOOP;
END $$;

-- ── 7. The merge ─────────────────────────────────────────────────────────────

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
    'headline', 'current_title', 'current_company', 'current_company_url', 'city',
    'country', 'bio', 'last_enriched_at', 'email'];
  -- The dup's identity columns: cleared on the dup (leads.linkedin_url is UNIQUE,
  -- and an upsert keyed on it must land on the keeper, not the retired row), then
  -- given to the keeper where the keeper's is NULL. The values are never lost:
  -- they are the keeper's lead_identities now, and lead_merges.cleared keeps them.
  c_ident constant text[] := ARRAY['linkedin_url', 'linkedin_urn', 'member_urn', 'public_id'];
  c_keep constant text[] := ARRAY['id', 'name', 'created_at', 'updated_at', 'merged_into'];

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
  v_ids         jsonb;
  v_rid         uuid;
  v_merge_id    uuid := gen_random_uuid();
  v_rows_before jsonb;
  v_before      bigint;
  v_after       bigint;
  v_val         jsonb;
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
  INSERT INTO lead_merges (id, dup_id, keeper_id, merged_by, moved, stayed, filled, cleared)
  VALUES (v_merge_id, _dup, _keeper, _by, v_moved, v_stayed, v_filled, v_cleared);
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

  -- ── 6. nothing deleted: every touched table has exactly its row count ───
  FOR v_tbl IN SELECT jsonb_object_keys(v_rows_before) LOOP
    EXECUTE format('SELECT count(*) FROM %I', v_tbl) INTO v_after;
    v_before := (v_rows_before ->> v_tbl)::bigint;
    IF v_after <> v_before THEN
      RAISE EXCEPTION 'merge_lead: %.count changed % -> %; rolled back', v_tbl, v_before, v_after;
    END IF;
  END LOOP;

  RETURN (SELECT to_jsonb(m) FROM lead_merges m WHERE m.id = v_merge_id);
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION merge_lead(uuid, uuid, text) IS
  'Fold dup into keeper: re-point every lead-id column (colliding rows, and the dup''s '
  'own duplicate tag pair, stay on the dup), move the dup''s identities as non-primary, '
  'clear the dup''s identity columns and fill the keeper''s NULL columns, set '
  'dup.merged_into, resolve the pair''s conflicts, record lead_merges. Deletes nothing. '
  'Refuses without a shared linkedin_urn identity. Mechanism only: who keeps, and '
  'approval, are tools/merge_leads.py''s.';
