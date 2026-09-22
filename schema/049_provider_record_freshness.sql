-- 049_provider_record_freshness.sql
-- A SECOND freshness axis: how old the PROVIDER'S OWN RECORD may be.
--
-- `fresh_days` (schema 047) bounds how long WE may reuse a cached response —
-- the age of our call. It says nothing about how old the answer inside that
-- response was when the provider handed it over.
--
-- Those are different clocks, and conflating them overstates what we know.
-- Observed 2026-09-22: a Clay call made at 14:55 that day returned a profile
-- whose own `last_refresh` was 2026-09-14. A perfectly fresh call, carrying an
-- eight-day-old reading. Under `fresh_days` alone that verdict looked current.
-- Real staleness is our window PLUS the provider's lag, and only the first half
-- was bounded.
--
-- `provider_fresh_days` bounds the second half, where the payload exposes a
-- record timestamp at all. NULL means either the source does not report one, or
-- we have decided not to constrain it.
--
-- We cannot make a provider refresh sooner, so the only honest options are to
-- bound it or to ignore it. This bounds it.

ALTER TABLE enrichment_source_policy
    ADD COLUMN IF NOT EXISTS provider_fresh_days integer;

ALTER TABLE enrichment_source_policy
    DROP CONSTRAINT IF EXISTS enrichment_source_policy_provider_fresh_positive;

ALTER TABLE enrichment_source_policy
    ADD CONSTRAINT enrichment_source_policy_provider_fresh_positive
        CHECK (provider_fresh_days IS NULL OR provider_fresh_days > 0);

COMMENT ON COLUMN enrichment_source_policy.provider_fresh_days IS
    'How old the PROVIDER''S OWN record may be, measured from the timestamp in '
    'their payload (e.g. clay last_refresh) — NOT from when we called. NULL = '
    'the source reports no such timestamp, or we do not constrain it. See '
    'fresh_days for the age of OUR cached call; the two are different clocks.';

-- Clay exposes `last_refresh` on the person payload. 14 days: twice the 7-day
-- window on our own call, because we control when we ask and cannot control
-- when they refresh. A reading older than this is reported with BOTH dates and
-- decided by a human rather than written as a verdict.
UPDATE enrichment_source_policy
   SET provider_fresh_days = 14,
       updated_at = now()
 WHERE api = 'clay' AND endpoint = 'cpj-enrich-person';
