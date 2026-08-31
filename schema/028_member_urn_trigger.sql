-- MarketBase schema, migration 028 — keep member_urn populated + preserve vanity slug
--
-- Migration 027 added leads.member_urn (the stable LinkedIn person identity) and
-- backfilled existing rows. This trigger keeps it correct going forward, for
-- EVERY write path (upload_leads, engagers_research[_own], research_competitor,
-- import_founder_connections, sync, and any future one) with zero per-call-site
-- code — so the partial-unique index uq_leads_member_urn (created by
-- dedupe_leads.py after a client's duplicates are merged) can't be bypassed.
--
-- It also captures the vanity slug into public_id when we don't already have one.
-- That preserves the human-readable handle (/in/ryanwinstanley) as a secondary
-- alias even after a person's canonical row is keyed on the URN-form URL, so a
-- later vanity re-upload can be matched back to the same person
-- (lib.resolve_canonical_url falls back to public_id when no member URN is known).

CREATE OR REPLACE FUNCTION leads_set_member_urn() RETURNS trigger AS $$
DECLARE
  slug text;
BEGIN
  IF NEW.member_urn IS NULL THEN
    NEW.member_urn := COALESCE(
      substring(NEW.linkedin_url from 'in/(AC[A-Za-z0-9_-]+)'),
      substring(NEW.linkedin_urn from '(AC[A-Za-z0-9_-]+)'));
  END IF;

  IF NEW.public_id IS NULL THEN
    slug := substring(NEW.linkedin_url from '/in/([^/?#]+)');
    -- Only keep genuine vanity handles, not URN-form slugs (ACoAA…, ACwAA…).
    IF slug IS NOT NULL AND left(slug, 2) <> 'AC' THEN
      NEW.public_id := slug;
    END IF;
  END IF;

  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_leads_set_member_urn ON leads;
CREATE TRIGGER trg_leads_set_member_urn
  BEFORE INSERT OR UPDATE ON leads
  FOR EACH ROW EXECUTE FUNCTION leads_set_member_urn();
