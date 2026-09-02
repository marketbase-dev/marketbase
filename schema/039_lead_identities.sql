-- MarketBase schema, migration 039 — Multi-channel person identity
--
-- Until now a person's identity was LinkedIn, and only LinkedIn:
-- `leads.linkedin_url` is UNIQUE NOT NULL (001) and `leads.member_urn` is the
-- canonical person key (027). That makes a LinkedIn profile a precondition for
-- existing at all.
--
-- But the conversation layer (023) has always been multi-channel: `channel` is
-- 'linkedin' | 'gmail' | 'whatsapp' and `channel_identifier` is documented as
-- "LinkedIn URN | gmail addr | phone". The result is a contradiction —
-- MarketBase can hold a WhatsApp or email conversation with a person it cannot
-- represent. Anyone arriving by phone or email (community group, event badge
-- scan, webinar registration, inbound form, newsletter) is unstorable, and
-- `channel_identifier` is a bare string with no path back to a lead.
--
-- This migration makes identity a first-class, multi-channel concept:
--
--   1. `lead_identities` — one row per (kind, value) a person is known by.
--      A lead may hold several: a phone AND a LinkedIn URN AND an email.
--   2. `leads.linkedin_url` becomes NULLABLE. It stays as a denormalized
--      convenience column so the ~47 existing readers keep working unchanged;
--      it is simply no longer a precondition for existence.
--   3. Backfill: every existing lead's member_urn / linkedin_url / email
--      becomes a `lead_identities` row, so the graph is complete from day one.
--
-- WHAT THIS ENABLES — progressive identity resolution. A person can enter as a
-- phone number, then later hand over a LinkedIn URL, and that URL attaches to
-- the SAME lead_id rather than creating a second row. Their conversation
-- history, tags, and qualifications survive the upgrade intact.
--
-- IT ALSO GIVES DEDUPE A PRINCIPLE. Two leads sharing any identity are the same
-- person; merging is "repoint lead_id". That is a general answer to the class
-- of split-identity bug 027 was written to patch for LinkedIn specifically.
--
-- NOTE ON UNIQUENESS: Postgres permits multiple NULLs under a UNIQUE
-- constraint, so relaxing NOT NULL on linkedin_url needs no index surgery —
-- many URL-less leads coexist, and the existing constraint still blocks two
-- leads sharing one URL.
--
-- NOTE ON THE URN TRIGGER: leads_set_member_urn (028/029) is already NULL-safe.
-- `substring(NULL from ...)` yields NULL and the public_id capture is guarded by
-- `IF slug IS NOT NULL`, so a lead with no linkedin_url passes through cleanly,
-- leaving member_urn and public_id NULL. No trigger changes are required.

-- ── 1. Relax LinkedIn-mandatory identity ──────────────────────────────────

ALTER TABLE leads ALTER COLUMN linkedin_url DROP NOT NULL;

COMMENT ON COLUMN leads.linkedin_url IS
  'Normalized LinkedIn profile URL when known. NULLABLE since migration 039 — '
  'a person may be known only by phone or email. Retained as a denormalized '
  'convenience column; lead_identities is the authoritative identity store.';

-- ── 2. The identity graph ─────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS lead_identities (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    lead_id     uuid NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    kind        text NOT NULL,        -- see the CHECK below
    value       text NOT NULL,        -- normalized per kind
    is_primary  boolean NOT NULL DEFAULT false,
    verified_at timestamptz,          -- when the person themselves confirmed it
    source      text,                 -- how we learned it, free text
    created_at  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_lead_identities_kind_value UNIQUE (kind, value),
    CONSTRAINT ck_lead_identities_kind CHECK (kind IN (
        'linkedin_urn',   -- stable AC… member URN. Preferred LinkedIn key.
        'linkedin_url',   -- normalized profile URL, when no URN is resolvable
        'email',          -- lowercased
        'phone',          -- E.164, leading '+', digits only
        'whatsapp',       -- E.164 phone that is known to be on WhatsApp
        'x_handle',       -- lowercased, no leading '@'
        'github',         -- lowercased login
        'domain'          -- personal site, normalized host
    ))
);

CREATE INDEX IF NOT EXISTS idx_lead_identities_lead  ON lead_identities(lead_id);
CREATE INDEX IF NOT EXISTS idx_lead_identities_value ON lead_identities(kind, value);

COMMENT ON TABLE lead_identities IS
  'Identity graph: every handle a person is known by, one row per (kind, value). '
  'A lead may hold many. Two leads sharing any identity are the same person — '
  'this is the basis for dedupe and for progressive identity resolution, where '
  'someone enters by phone and later resolves to a LinkedIn profile.';

COMMENT ON COLUMN lead_identities.value IS
  'Normalized per kind: phone/whatsapp = E.164 with leading +; email = '
  'lowercased; linkedin_urn = the bare AC… token; x_handle/github = lowercased '
  'with no leading @. Normalize BEFORE insert — the UNIQUE constraint is the '
  'dedupe mechanism and it compares raw strings.';

COMMENT ON COLUMN lead_identities.verified_at IS
  'Set when the person themselves confirmed this handle (replied from it, or '
  'handed it over in-thread). NULL for identities we inferred or scraped.';

-- ── 3. Backfill the graph from existing identity columns ──────────────────

INSERT INTO lead_identities (lead_id, kind, value, is_primary, source)
SELECT id, 'linkedin_urn', member_urn, true, 'backfill:039'
  FROM leads
 WHERE member_urn IS NOT NULL
ON CONFLICT (kind, value) DO NOTHING;

-- Only for leads with no resolvable URN, so the URN stays the preferred key.
INSERT INTO lead_identities (lead_id, kind, value, is_primary, source)
SELECT id, 'linkedin_url', linkedin_url, true, 'backfill:039'
  FROM leads
 WHERE linkedin_url IS NOT NULL AND member_urn IS NULL
ON CONFLICT (kind, value) DO NOTHING;

INSERT INTO lead_identities (lead_id, kind, value, is_primary, source)
SELECT id, 'email', lower(email), false, 'backfill:039'
  FROM leads
 WHERE email IS NOT NULL AND email <> ''
ON CONFLICT (kind, value) DO NOTHING;

-- ── 4. Resolution view ────────────────────────────────────────────────────
-- Resolve any handle to a lead without knowing which column it came from.
-- Chiefly for lead_conversations.channel_identifier, which until now was a
-- bare string with no join path back to a person.

CREATE OR REPLACE VIEW v_lead_identity AS
SELECT li.kind,
       li.value,
       li.is_primary,
       li.verified_at,
       l.id   AS lead_id,
       l.name,
       l.linkedin_url,
       l.current_company,
       l.current_title
  FROM lead_identities li
  JOIN leads l ON l.id = li.lead_id;

COMMENT ON VIEW v_lead_identity IS
  'Flat handle -> person lookup across every identity kind. Use to resolve a '
  'lead_conversations.channel_identifier (phone, email, LinkedIn URN) to the '
  'lead it belongs to.';
