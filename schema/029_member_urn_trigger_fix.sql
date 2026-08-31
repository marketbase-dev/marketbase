-- MarketBase schema, migration 029 — fix the public_id guard in leads_set_member_urn
--
-- Migration 028's trigger guarded vanity-slug capture with `left(slug,2) <> 'AC'`,
-- which is case-sensitive — a URN-form slug stored lowercased (acoaa…) slipped
-- through and got written into public_id as if it were a vanity handle. Replace
-- the guard with a case-insensitive, anchored member-URN pattern so only genuine
-- vanity handles land in public_id.

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
    -- Keep genuine vanity handles only; reject URN-form slugs in either case
    -- (ACoAA…, acoaa…, ACwAA…, …).
    IF slug IS NOT NULL AND slug !~* '^AC[A-Za-z0-9_-]{15,}$' THEN
      NEW.public_id := slug;
    END IF;
  END IF;

  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
