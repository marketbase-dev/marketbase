-- 071_lead_identity_claim_tool_writers.sql
--
-- Identity ticket 2: a declined identity write records a finding.
--
-- 070 built lead_identity_claim() for the trigger: owned by nobody -> insert;
-- owned by this lead -> promote; owned by ANOTHER lead -> leave the owner
-- alone and tag this lead flag:duplicate_lead + expansion:do_not_push with a
-- note naming the owner. That tag pair IS the recorded finding (lead, kind,
-- value, owner), so the four tool-side writers should go through the same
-- function instead of their own INSERT ... ON CONFLICT DO NOTHING -- the
-- silence that let 49 duplicate people on one instance go unnoticed.
--
-- Two things the tools need that the trigger did not:
--
--   p_primary      The trigger files the value that is on the lead row now, so
--                  it is the primary and older rows are demoted. A tool that
--                  records an ALIAS (the vanity URL Blitz resolved for a lead
--                  keyed on its URN URL, a Luma guest's e-mail) must not demote
--                  anything: it passes false and the row is inserted
--                  non-primary. Default true keeps the trigger's behaviour.
--   p_verified_at  The trigger saw a row being written, never a profile being
--                  confirmed, so it leaves verified_at NULL. A tool that DID
--                  confirm the identity (Blitz returned the profile, Fresh
--                  resolved the URN) passes when. On a row this lead already
--                  holds, a NULL verified_at is filled in; an earlier
--                  verification is never overwritten.
--
-- The function now RETURNS the outcome as text, so a tool can count and print
-- it: 'inserted', 'ours', 'duplicate:<owner lead id>', or 'empty'. The trigger
-- ignores it (PERFORM).
--
-- The 4-argument signature is dropped, not overloaded: with both present a
-- 4-argument call would be ambiguous. The trigger's PERFORM with four arguments
-- resolves to this function through the defaults. One file, one implicit
-- transaction, so there is no moment without the function.

DROP FUNCTION IF EXISTS lead_identity_claim(uuid, text, text, text);

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
  RETURN 'duplicate:' || v_owner::text;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION lead_identity_claim(uuid, text, text, text, boolean, timestamptz) IS
  'Record that a lead carries an identity value; the ONE write path into '
  'lead_identities for the trigger and every tool. Owned by nobody: insert '
  '(primary unless p_primary is false). Owned by this lead: promote when '
  'p_primary, fill a NULL verified_at from p_verified_at. Owned by another lead: '
  'leave the owner alone and tag this lead flag:duplicate_lead + '
  'expansion:do_not_push with a note naming the owner. Returns inserted | ours | '
  'duplicate:<owner id> | empty.';

UPDATE tag_definitions
   SET description = 'This row is the same person as another lead: an identity it carries (vanity '
                     'URL, member URN or e-mail) is already that other lead''s. Kept, never merged -- '
                     'identity merge is non-destructive and merged_into is not built. The note '
                     'names the owning lead id and URL and the colliding kind and value. Written by '
                     'lead_identity_claim(), which every identity writer goes through: the '
                     'trg_leads_sync_identities trigger and the tools.'
 WHERE namespace = 'flag' AND key = 'duplicate_lead' AND entity_type = 'lead';
