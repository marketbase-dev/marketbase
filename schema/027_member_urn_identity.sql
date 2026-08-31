-- MarketBase schema, migration 027 — Person identity = LinkedIn member URN
--
-- Until now a lead's identity was `linkedin_url` (UNIQUE NOT NULL). That makes
-- the URL *string* the identity, so the same real person ingested under two URL
-- forms — a vanity slug (/in/ryanwinstanley) and a URN slug
-- (/in/ACoAAAyB9jcB…) — becomes TWO lead rows. The DB then believes there are
-- two Ryans, splitting his conversations, tags, qualification, and queue state
-- across both. (~2,300 such splits observed in Acme alone.) It also makes
-- sync_conversations trip uq_lconv_channel_chat: the second "Ryan" tries to
-- staple a chat that already belongs to the first.
--
-- The real identity is the stable LinkedIn member URN (the `AC…` token), which
-- is the SAME regardless of which URL form we stored. `member_urn` captures it.
-- Members span several URN prefixes (ACoAA, ACwAA, ACEAA, …) — we match the
-- whole `AC…` family, not just ACoAA.
--
-- NOTE ON ORDERING: this migration only ADDS the column + backfill + a plain
-- lookup index. It deliberately does NOT add the UNIQUE index, because existing
-- duplicates would make that fail. The partial-unique index
-- (uq_leads_member_urn, WHERE member_urn IS NOT NULL) is created per-client by
-- dedupe_leads.py right after it merges that client's duplicates, and is
-- codified as a follow-up migration once every client is deduped.

ALTER TABLE leads ADD COLUMN IF NOT EXISTS member_urn text;

COMMENT ON COLUMN leads.member_urn IS
  'Stable LinkedIn member URN token (AC…), derived from linkedin_url''s /in/AC… '
  'slug or from linkedin_urn. The canonical person identity; deduped on by '
  'dedupe_leads.py and enforced by the partial-unique index uq_leads_member_urn. '
  'NULL only for rows with no resolvable URN (vanity URL + no backfilled urn).';

-- Backfill: prefer the URL''s URN token, fall back to linkedin_urn (which may be
-- a bare token or a full `urn:li:fsd_profile:AC…` string).
UPDATE leads
   SET member_urn = COALESCE(
         substring(linkedin_url from 'in/(AC[A-Za-z0-9_-]+)'),
         substring(linkedin_urn from '(AC[A-Za-z0-9_-]+)')
       )
 WHERE member_urn IS NULL;

-- Plain (non-unique) lookup index. The UNIQUE variant comes after dedup.
CREATE INDEX IF NOT EXISTS idx_leads_member_urn ON leads(member_urn);
