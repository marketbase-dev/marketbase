-- MarketBase schema, migration 022 — auto-sync leads row from lead_qualifications
--
-- Problem: qualify-acme-target writes rich enrichment data to
-- `lead_qualifications.full_result` (JSONB) but never updates the canonical
-- `leads.current_company` / `current_company_url` / `current_title` /
-- `country` / `city` columns. Sequencers (Smartlead) read from leads.*, so they
-- saw missing data even when the qualification carried it.
--
-- Fix: AFTER INSERT trigger on lead_qualifications that pulls the relevant
-- keys out of full_result and fills any NULL/empty leads columns. Uses
-- COALESCE so existing curated data isn't overwritten — the trigger only
-- fills gaps.
--
-- Scope: fires for any classifier write whose full_result carries the
-- standard enrichment keys. In practice today that's qualify-acme-
-- target, but the trigger is generic so a future enricher can also benefit.

CREATE OR REPLACE FUNCTION sync_lead_from_qualification() RETURNS TRIGGER AS $$
BEGIN
    IF NEW.full_result IS NULL THEN
        RETURN NEW;
    END IF;
    UPDATE leads l SET
        current_company = COALESCE(
            NULLIF(l.current_company, ''),
            NULLIF(NEW.full_result->>'current_company', '')
        ),
        current_company_url = COALESCE(
            NULLIF(l.current_company_url, ''),
            NULLIF(NEW.full_result->>'company_linkedin_url', '')
        ),
        current_title = COALESCE(
            NULLIF(l.current_title, ''),
            NULLIF(NEW.full_result->>'job_title', '')
        ),
        country = COALESCE(
            NULLIF(l.country, ''),
            NULLIF(NEW.full_result->>'country', '')
        ),
        city = COALESCE(
            NULLIF(l.city, ''),
            NULLIF(NEW.full_result->>'city', '')
        ),
        bio = COALESCE(
            NULLIF(l.bio, ''),
            NULLIF(NEW.full_result->>'bio', '')
        ),
        updated_at = NOW()
    WHERE l.id = NEW.lead_id
      AND (
          (l.current_company IS NULL OR l.current_company = '')
       OR (l.current_company_url IS NULL OR l.current_company_url = '')
       OR (l.current_title IS NULL OR l.current_title = '')
       OR (l.country IS NULL OR l.country = '')
       OR (l.city IS NULL OR l.city = '')
       OR (l.bio IS NULL OR l.bio = '')
      );
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS sync_lead_from_qualification_trigger ON lead_qualifications;
CREATE TRIGGER sync_lead_from_qualification_trigger
AFTER INSERT ON lead_qualifications
FOR EACH ROW
EXECUTE FUNCTION sync_lead_from_qualification();
