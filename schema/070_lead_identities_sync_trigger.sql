-- 070_lead_identities_sync_trigger.sql
--
-- Every lead creator feeds lead_identities, with zero per-call-site code.
--
-- The defect: lead_identities is the identity authority (a lead keyed on its
-- URN-form URL carries its vanity URL only there), but sixteen tools INSERT INTO
-- leads and only four of them write an identity row. Roughly 35,000 leads across
-- the instances have no identity row at all, so every lookup that trusts the
-- authority has to fall back to the legacy leads.linkedin_url column. The fix a
-- tool-side helper offers recreates the failure it fixes -- a creator that
-- forgets to call it -- so, as with trg_leads_set_member_urn (028), only the
-- database can make forgetting impossible.
--
-- What the trigger records, after INSERT and after an UPDATE that changes the
-- value:
--   * leads.linkedin_url in vanity form  -> kind linkedin_url
--     (the URN-form URL /in/ACoAA... is the URN itself, not a vanity URL)
--   * leads.member_urn starting ACoAA    -> kind linkedin_urn        (canonical)
--   * leads.member_urn starting ACwAA    -> kind linkedin_urn_search (alias;
--     the search namespace, decided 2026-10-25 -- same member, different token)
-- member_urn is already derived by the BEFORE trigger from 028, so this one
-- reads NEW.member_urn and never re-parses the URL for a token.
--
-- Stamping: source = the trigger name; is_primary = true for the value that is
-- on the lead row now, demoting that lead's other rows of the same kind (the
-- convention rehandle_leads already follows); verified_at stays NULL, because
-- the trigger saw a row being written, not a profile being confirmed. Older
-- values are kept as aliases, so a previous handle still resolves.
--
-- The duplicate case: uq_lead_identities_kind_value is GLOBALLY unique, so a
-- value already owned by a DIFFERENT lead means the new row is the same person
-- twice. The identity is never reassigned -- the authority keeps the owner it
-- had -- and the new row is tagged flag:duplicate_lead + expansion:do_not_push
-- with a note naming the owner, the pattern resolve_urn_identities uses. That
-- is the recorded finding Identity ticket 2 asks for, on this write path.
-- Creators avoid this case in the first place by routing through
-- lib.resolve_canonical_url, which consults the authority before the upsert.
--
-- Idempotent: CREATE OR REPLACE, DROP TRIGGER IF EXISTS, ON CONFLICT DO NOTHING.

-- ── the alias kind ─────────────────────────────────────────────────────────
INSERT INTO lead_identity_kinds (name, description) VALUES
  ('linkedin_urn_search',
   'LinkedIn member URN in the search namespace (ACwAA...). An alias of the same '
   'member; the ACoAA profile-namespace token is the canonical linkedin_urn. '
   'Chars 3-11 of the two tokens match; treat that as a hint, never a key.')
ON CONFLICT (name) DO NOTHING;

-- ── registry for the two tags the duplicate case writes ───────────────────
INSERT INTO tag_definitions (namespace, key, entity_type, layer, description) VALUES
  ('flag', 'duplicate_lead', 'lead', 'state',
   'This row is the same person as another lead: an identity it carries (vanity '
   'URL or member URN) is already that other lead''s. Kept, never merged -- '
   'identity merge is non-destructive and merged_into is not built. The note '
   'names the owning lead id and URL and the colliding kind and value. Written by '
   'trg_leads_sync_identities and resolve_urn_identities.'),
  ('expansion', 'do_not_push', 'lead', 'state',
   'Never hand this lead to a client pipeline (buying-committee push, webhook, '
   'export). Set alongside flag:duplicate_lead so the canonical row is the one '
   'that travels.')
ON CONFLICT (namespace, key, entity_type) DO NOTHING;

-- ── claim one identity for one lead ────────────────────────────────────────
CREATE OR REPLACE FUNCTION lead_identity_claim(p_lead uuid, p_kind text, p_value text,
                                               p_source text) RETURNS void AS $$
DECLARE
  v_owner     uuid;
  v_owner_url text;
  v_note      text;
BEGIN
  IF p_value IS NULL OR p_value = '' THEN
    RETURN;
  END IF;

  SELECT lead_id INTO v_owner
    FROM lead_identities WHERE kind = p_kind AND value = p_value;

  IF v_owner IS NULL THEN
    UPDATE lead_identities SET is_primary = false
     WHERE lead_id = p_lead AND kind = p_kind AND is_primary;
    BEGIN
      INSERT INTO lead_identities (lead_id, kind, value, is_primary, source)
      VALUES (p_lead, p_kind, p_value, true, p_source);
      RETURN;
    EXCEPTION WHEN unique_violation THEN
      -- a concurrent writer claimed it between the SELECT and the INSERT
      SELECT lead_id INTO v_owner
        FROM lead_identities WHERE kind = p_kind AND value = p_value;
      IF v_owner IS NULL OR v_owner = p_lead THEN
        RETURN;
      END IF;
    END;
  END IF;

  IF v_owner = p_lead THEN
    -- already ours: it is the value on the row now, so it is the primary
    UPDATE lead_identities SET is_primary = false
     WHERE lead_id = p_lead AND kind = p_kind AND is_primary AND value <> p_value;
    UPDATE lead_identities SET is_primary = true
     WHERE lead_id = p_lead AND kind = p_kind AND value = p_value AND NOT is_primary;
    RETURN;
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
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION lead_identity_claim(uuid, text, text, text) IS
  'Record that a lead carries an identity value. Owned by nobody: insert as '
  'primary. Owned by this lead: make it primary. Owned by another lead: leave '
  'the owner alone and tag this lead flag:duplicate_lead + expansion:do_not_push '
  'with a note naming the owner.';

-- ── the trigger ────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION leads_sync_identities() RETURNS trigger AS $$
DECLARE
  v_url text;
  v_urn text;
BEGIN
  v_url := NEW.linkedin_url;
  v_urn := NEW.member_urn;

  IF TG_OP = 'UPDATE' THEN
    IF OLD.linkedin_url IS NOT DISTINCT FROM NEW.linkedin_url THEN v_url := NULL; END IF;
    IF OLD.member_urn   IS NOT DISTINCT FROM NEW.member_urn   THEN v_urn := NULL; END IF;
  END IF;

  -- A URN-form URL (/in/ACoAA..., any case) is the URN, not a vanity URL.
  IF v_url IS NOT NULL AND v_url ~* '/in/AC[A-Za-z0-9_-]{15,}' THEN
    v_url := NULL;
  END IF;

  IF v_url IS NOT NULL THEN
    PERFORM lead_identity_claim(NEW.id, 'linkedin_url', v_url, 'trg_leads_sync_identities');
  END IF;

  IF v_urn IS NOT NULL THEN
    IF v_urn LIKE 'ACoAA%' THEN
      PERFORM lead_identity_claim(NEW.id, 'linkedin_urn', v_urn, 'trg_leads_sync_identities');
    ELSIF v_urn LIKE 'ACwAA%' THEN
      PERFORM lead_identity_claim(NEW.id, 'linkedin_urn_search', v_urn, 'trg_leads_sync_identities');
    END IF;
    -- Any other prefix is unknown (none exist across 141,541 tokens surveyed
    -- 2026-10-25) and is deliberately not filed under a kind it may not be.
  END IF;

  RETURN NULL;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_leads_sync_identities ON leads;
CREATE TRIGGER trg_leads_sync_identities
  AFTER INSERT OR UPDATE ON leads
  FOR EACH ROW EXECUTE FUNCTION leads_sync_identities();

COMMENT ON TRIGGER trg_leads_sync_identities ON leads IS
  'Every lead write lands its vanity URL and member URN in lead_identities, the '
  'identity authority, so no creator can forget to. Duplicates are tagged, never '
  'merged. See 070.';
